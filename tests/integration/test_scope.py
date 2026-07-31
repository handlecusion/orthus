"""P2.1 acceptance: tenant scope (company | personal) + retrieval isolation.

docs/architecture-v2.md §2. The same corpus->wiki pipeline serves a shared company
layer (visible to everyone) and per-user personal layers (visible only to the owner).

The `_isolate_wiki_store` fixture is session-scoped, so the markdown store is shared
across tests (only Postgres is wiped per-test by `clean`). Each test therefore uses
UNIQUE topics/users and asserts on its own content, never on global counts.

Scope filtering lives on `embeddings.scope` (+ `embeddings.user_id` for personal
ownership); retrieval returns company OR own-personal, never another user's personal.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.tables import corpus_chunks, documents, embeddings, users, wiki_pages
from orthus.wiki import ask
from orthus.wiki.retrieve import retrieve


def _distill_json(summary: str, claim: str, *, page_slug: str, page_title: str) -> str:
    return json.dumps(
        {
            "summary": summary,
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": claim,
                    "evidence": claim,
                    "confidence": "high",
                    "page": {"slug": page_slug, "title": page_title},
                    "conflicting": [],
                }
            ],
        }
    )


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def test_company_content_is_shared_across_users(user_id):
    """Company content authored by user A is retrievable by user B."""
    user_b = _make_user()
    topic = "스코프-회사-급여규정"
    save_editor_document(
        user_id,
        "급여 규정",
        [{}],
        f"{topic}: 급여는 매월 25일에 지급된다.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic} 요약",
                f"{topic}: 급여는 매월 25일 지급.",
                page_slug=topic,
                page_title="급여",
            )
        ),
        scope="company",
    )

    # user B (who authored nothing) still sees the shared company page.
    hits_b = retrieve(user_b, f"{topic} 급여 지급일", k=5)
    assert any(topic in h.page_slug for h in hits_b), "company content must be shared"


def test_personal_content_is_isolated_to_owner(user_id):
    """Personal content (scope='personal', owner=A) is retrievable by A, not B."""
    user_b = _make_user()
    topic = "스코프-개인-내일정"
    save_editor_document(
        user_id,
        "내 일정",
        [{}],
        f"{topic}: 나는 금요일에 치과 예약이 있다.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic} 요약",
                f"{topic}: 금요일 치과 예약.",
                page_slug=topic,
                page_title="내 일정",
            )
        ),
        scope="personal",
    )

    # owner A sees their own personal page.
    hits_a = retrieve(user_id, f"{topic} 치과 예약", k=5)
    assert any(topic in h.page_slug for h in hits_a), "owner must see own personal content"

    # user B must NOT see A's personal page.
    hits_b = retrieve(user_b, f"{topic} 치과 예약", k=5)
    assert not any(topic in h.page_slug for h in hits_b), (
        "another user must never see A's personal content"
    )


def test_ask_for_other_user_excludes_personal_claims(user_id):
    """`ask` for user B excludes A's personal claims."""
    user_b = _make_user()
    topic = "스코프-개인-비밀프로젝트"
    save_editor_document(
        user_id,
        "비밀 프로젝트",
        [{}],
        f"{topic}: 코드네임은 오로라이다.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic} 요약", f"{topic}: 코드네임 오로라.", page_slug=topic, page_title="비밀"
            )
        ),
        scope="personal",
    )

    # A can ground an answer on their own personal claim.
    out_a = ask(
        user_id,
        f"{topic} 코드네임?",
        k=5,
        chat_model=MockChat(default="오로라입니다."),
        learn=False,
    )
    assert out_a.sources, "owner can ground on own personal content"
    assert any(topic in s.page_slug for s in out_a.sources)

    # B gets no grounding from A's personal content.
    out_b = ask(user_b, f"{topic} 코드네임?", k=5, chat_model=MockChat(default="모름"), learn=False)
    assert not any(topic in s.page_slug for s in out_b.sources), (
        "B must not ground on A's personal claim"
    )


def test_personal_scope_filter_is_explicit(user_id):
    """scope='company' hides personal; scope='personal' hides company."""
    topic_c = "스코프-필터-회사규정"
    topic_p = "스코프-필터-개인메모"
    save_editor_document(
        user_id,
        "회사 규정",
        [{}],
        f"{topic_c}: 점심시간은 12시부터 1시.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic_c}", f"{topic_c}: 점심 12-1시.", page_slug=topic_c, page_title="규정"
            )
        ),
        scope="company",
    )
    save_editor_document(
        user_id,
        "개인 메모",
        [{}],
        f"{topic_p}: 내 책상은 창가 자리.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic_p}", f"{topic_p}: 창가 자리.", page_slug=topic_p, page_title="메모"
            )
        ),
        scope="personal",
    )

    company_only = retrieve(user_id, f"{topic_c} {topic_p}", k=10, scope="company")
    assert any(topic_c in h.page_slug for h in company_only)
    assert not any(topic_p in h.page_slug for h in company_only)

    personal_only = retrieve(user_id, f"{topic_c} {topic_p}", k=10, scope="personal")
    assert any(topic_p in h.page_slug for h in personal_only)
    assert not any(topic_c in h.page_slug for h in personal_only)


def test_migration_0004_columns_default_company(user_id):
    """Migration 0004 applied: scope columns exist and existing-style rows
    (inserted without an explicit scope) default to 'company'."""
    # Direct insert WITHOUT scope mimics a pre-0004 seeded row; the DB DEFAULT fills it.
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="seeded-style",
                block_json=[{}],
                markdown="본문",
                source="editor",
                schema_version=1,
            )
        )
        s.commit()
        scope = s.execute(select(documents.c.scope).where(documents.c.doc_id == doc_id)).scalar()
    assert scope == "company", "rows inserted without scope must default to company"

    # The scope column exists on all four scoped tables.
    with session() as s:
        cols = {
            r[0]
            for r in s.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE column_name = 'scope' "
                    "AND table_name IN "
                    "('documents','corpus_chunks','embeddings','wiki_pages')"
                )
            ).all()
        }
        has_owner = s.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='wiki_pages' AND column_name='owner_id'"
            )
        ).scalar()
    assert cols == {"documents", "corpus_chunks", "embeddings", "wiki_pages"}
    assert has_owner == 1, "wiki_pages.owner_id must exist"


def test_wiki_pages_slug_scope_owner_unique_semantics(user_id):
    """The DB unique index identity is (slug, scope, owner_id), with company
    owner_id NULL treated as a real dedupe value."""

    def _insert_wiki_page(slug: str, scope: str, owner_id: uuid.UUID | None) -> None:
        with session() as s:
            s.execute(
                insert(wiki_pages).values(
                    page_id=uuid.uuid4(),
                    slug=slug,
                    kind="page",
                    path=f"{scope}/{owner_id or 'company'}/{slug}.md",
                    title=slug,
                    content_hash="hash",
                    schema_version=1,
                    scope=scope,
                    owner_id=owner_id,
                )
            )
            s.commit()

    shared_slug = "스코프-유니크-공유슬러그"
    _insert_wiki_page(shared_slug, "company", None)
    with pytest.raises(IntegrityError):
        _insert_wiki_page(shared_slug, "company", None)

    # Different scope: a personal page may reuse a company slug.
    _insert_wiki_page(shared_slug, "personal", user_id)

    personal_slug = "스코프-유니크-개인슬러그"
    _insert_wiki_page(personal_slug, "personal", user_id)
    with pytest.raises(IntegrityError):
        _insert_wiki_page(personal_slug, "personal", user_id)

    # Different owner: personal users may independently use the same slug.
    user_b = _make_user()
    _insert_wiki_page(personal_slug, "personal", user_b)


def test_corpus_and_embeddings_carry_scope(user_id):
    """save_editor_document(scope='personal') stamps scope on corpus_chunks +
    embeddings so the isolation filter has something to match on."""
    topic = "스코프-스탬프-개인문서"
    save_editor_document(
        user_id,
        "개인 문서",
        [{}],
        f"{topic}: 임의의 본문 내용 한 줄.",
        chat_model=MockChat(
            default=_distill_json(f"{topic}", f"{topic}: 본문.", page_slug=topic, page_title="문서")
        ),
        scope="personal",
    )
    with session() as s:
        chunk_scopes = {
            r[0]
            for r in s.execute(
                select(corpus_chunks.c.scope)
                .join(documents, corpus_chunks.c.doc_id == documents.c.doc_id)
                .where(documents.c.title == "개인 문서")
            ).all()
        }
        emb_scopes = {
            r[0]
            for r in s.execute(
                select(embeddings.c.scope).where(
                    embeddings.c.user_id == user_id, embeddings.c.kind == "corpus_chunk"
                )
            ).all()
        }
    assert chunk_scopes == {"personal"}
    assert emb_scopes == {"personal"}

    # the authored wiki page is owned by the user under personal scope.
    with session() as s:
        owner = s.execute(
            select(wiki_pages.c.owner_id).where(
                wiki_pages.c.slug == topic, wiki_pages.c.scope == "personal"
            )
        ).scalar()
    assert owner == user_id
