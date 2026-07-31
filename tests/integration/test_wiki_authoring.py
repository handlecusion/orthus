"""W2 acceptance: self-authoring pipeline (corpus doc -> claims -> pages).

Covers docs/llm-wiki.md §12: E2E author, conflict -> task (no silent overwrite),
redaction of prose + audit output, idempotency. Uses tmp_path as the wiki-store
root so the repo `wiki-store/` is never polluted.

The editor save itself triggers authoring (T1, docs/llm-wiki.md §7), so every
`save_editor_document` here passes the same scripted chat it later authors with —
keeping the save-time and explicit authoring runs slug-identical (idempotent)."""

from __future__ import annotations

import json

from sqlalchemy import select, text

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.tables import audit_log, wiki_chunks, wiki_pages
from orthus.wiki import author_from_document, store
from orthus.wiki.slugs import source_slug_from_title


def _distill_json(summary: str, claim: str, *, page_slug: str, page_title: str, conflicting=None):
    return json.dumps(
        {
            "summary": summary,
            "key_concepts": ["연차", "휴가"],
            "terminology": ["연차: annual leave"],
            "open_questions": ["반차 정책은?"],
            "claims": [
                {
                    "claim": claim,
                    "evidence": f"문서 근거: {claim}",
                    "confidence": "high",
                    "page": {"slug": page_slug, "title": page_title},
                    "conflicting": conflicting or [],
                }
            ],
        }
    )


def test_author_from_document_e2e(user_id, tmp_path):
    chat = MockChat(
        default=_distill_json(
            "휴가 정책 요약.",
            "연차는 입사 1년차부터 15일 부여된다.",
            page_slug="vacation-policy",
            page_title="휴가 정책",
        )
    )
    doc_id = save_editor_document(
        user_id, "휴가 정책", [{}], "연차 휴가는 입사 1년차부터 15일이 부여됩니다.", chat_model=chat
    )

    counts = author_from_document(doc_id, user_id, chat_model=chat, root=tmp_path)
    assert counts["sources"] == 1
    assert counts["claims"] == 1
    assert counts["pages"] == 1
    assert counts["tasks"] == 1

    # markdown files exist under tmp root
    src_slug = source_slug_from_title("휴가 정책", doc_id)
    claim_slugs = store.list_slugs("claim", root=tmp_path)
    assert store.exists("source", src_slug, root=tmp_path)
    assert len(claim_slugs) == 1
    assert store.exists("page", "vacation-policy", root=tmp_path)
    task_slugs = store.list_slugs("task", root=tmp_path)
    assert len(task_slugs) == 1
    task = store.load_task(task_slugs[0], root=tmp_path)
    assert task is not None
    assert task.kind == "open_question"
    assert task.related[0] == src_slug
    assert "vacation-policy" in task.related
    assert claim_slugs[0] in task.related

    # wiki_pages rows mirrored
    with session() as s:
        kinds = {r.kind for r in s.execute(select(wiki_pages.c.kind)).all()}
        n_wiki_chunks = s.execute(select(wiki_chunks.c.chunk_id)).all()
    assert {"source", "claim", "page", "task"} <= kinds
    assert len(n_wiki_chunks) > 0  # page + claim bodies embedded

    # load_page round-trips
    page = store.load_page("vacation-policy", root=tmp_path)
    assert page is not None
    assert page.slug == "vacation-policy"
    assert claim_slugs[0] in page.evidence
    assert src_slug in page.sources


def test_conflict_creates_task_without_overwrite(user_id, tmp_path):
    # First document establishes the claim.
    page_slug = "vacation-policy"
    original_claim = "연차는 입사 1년차부터 15일 부여된다."
    chat1 = MockChat(
        default=_distill_json("원본.", original_claim, page_slug=page_slug, page_title="휴가 정책")
    )
    doc1 = save_editor_document(user_id, "휴가 정책", [{}], "연차는 15일.", chat_model=chat1)
    author_from_document(doc1, user_id, chat_model=chat1, root=tmp_path)

    claim_slugs = store.list_slugs("claim", root=tmp_path)
    assert len(claim_slugs) == 1
    claim_slug = claim_slugs[0]
    before = store.load_claim(claim_slug, root=tmp_path)

    # Second document: SAME claim text would dedupe; to force a same-slug-different-text
    # conflict we reference the existing claim via `conflicting`.
    chat2 = MockChat(
        default=_distill_json(
            "상충 버전.",
            "연차는 입사 1년차부터 20일 부여된다.",  # different claim text, same page
            page_slug=page_slug,
            page_title="휴가 정책",
            conflicting=[claim_slug],
        )
    )
    doc2 = save_editor_document(
        user_id, "휴가 정책", [{}], "연차는 20일이라는 주장.", chat_model=chat2
    )
    counts = author_from_document(doc2, user_id, chat_model=chat2, root=tmp_path)

    # a conflict task exists
    assert counts["tasks"] >= 1
    task_slugs = store.list_slugs("task", root=tmp_path)
    assert task_slugs, "expected a conflict task file"
    conflict_tasks = [store.load_task(slug, root=tmp_path) for slug in task_slugs]
    assert any(t.kind == "conflict" for t in conflict_tasks)
    assert any(claim_slug in t.related for t in conflict_tasks)

    # original claim unchanged (no silent overwrite)
    after = store.load_claim(claim_slug, root=tmp_path)
    assert after == before


def test_redaction_masks_prose_and_audit(user_id, tmp_path):
    secret_email = "hong@orthus.example.com"
    secret_phone = "010-1234-5678"
    chat = MockChat(
        default=_distill_json(
            f"담당자 이메일은 {secret_email}.",
            f"문의는 {secret_phone} 로 연락한다.",
            page_slug="contact",
            page_title="연락처",
        )
    )
    doc_id = save_editor_document(user_id, "연락처", [{}], "담당자 연락처 문서.", chat_model=chat)
    author_from_document(doc_id, user_id, chat_model=chat, root=tmp_path)

    # written markdown is masked
    src = store.load_source(source_slug_from_title("연락처", doc_id), root=tmp_path)
    assert src is not None
    assert secret_email not in src.summary
    assert "h***@orthus.example.com" in src.summary

    claim_slug = store.list_slugs("claim", root=tmp_path)[0]
    claim = store.load_claim(claim_slug, root=tmp_path)
    assert "1234-5678" not in claim.claim
    assert "010-****-5678" in claim.claim

    # audit_log output/meta is masked
    with session() as s:
        rows = s.execute(
            select(audit_log.c.output, audit_log.c.meta).where(
                audit_log.c.node.in_(["wiki.author", "wiki.distill", "wiki.consolidate"])
            )
        ).all()
    blob = json.dumps([{"o": r.output, "m": r.meta} for r in rows], ensure_ascii=False)
    assert secret_email not in blob
    assert "1234-5678" not in blob


def test_author_is_idempotent(user_id, tmp_path):
    chat = MockChat(
        default=_distill_json(
            "요약.",
            "연차는 1년차부터 15일.",
            page_slug="vacation-policy",
            page_title="휴가 정책",
        )
    )
    doc_id = save_editor_document(
        user_id, "휴가 정책", [{}], "연차 휴가는 입사 1년차부터 15일.", chat_model=chat
    )
    author_from_document(doc_id, user_id, chat_model=chat, root=tmp_path)
    author_from_document(doc_id, user_id, chat_model=chat, root=tmp_path)

    # same slugs -> no duplication of files or rows
    assert len(store.list_slugs("claim", root=tmp_path)) == 1
    with session() as s:
        n_pages = s.execute(text("SELECT count(*) FROM wiki_pages")).scalar()
        n_chunks = s.execute(text("SELECT count(*) FROM wiki_chunks")).scalar()
    # source + 1 claim + 1 page + 1 open-question task = 4 wiki_pages rows
    assert n_pages == 4
    assert n_chunks > 0


def test_redaction_masks_pii_in_list_fields(user_id, tmp_path):
    """S1: emails/phones planted inside open_questions or terminology entries
    must be masked in both the written markdown and audit_log."""
    secret_email = "leaky@orthus.example.com"
    secret_phone = "010-9876-5432"
    chat = MockChat(
        default=json.dumps(
            {
                "summary": "요약.",
                "key_concepts": ["개념"],
                "terminology": [f"용어: {secret_email}"],
                "open_questions": [f"전화번호는 {secret_phone} 인가요?"],
                "claims": [
                    {
                        "claim": "테스트 주장.",
                        "evidence": "근거.",
                        "confidence": "low",
                        "page": {"slug": "pii-test", "title": "PII Test"},
                        "conflicting": [],
                    }
                ],
            }
        )
    )
    doc_id = save_editor_document(user_id, "개인정보 테스트", [{}], "테스트 문서.", chat_model=chat)
    author_from_document(doc_id, user_id, chat_model=chat, root=tmp_path)

    src = store.load_source(source_slug_from_title("개인정보 테스트", doc_id), root=tmp_path)
    assert src is not None
    # email masked in terminology list item
    assert secret_email not in " ".join(src.terminology)
    assert "l***@orthus.example.com" in " ".join(src.terminology)
    # phone masked in open_questions list item
    assert secret_phone not in " ".join(src.open_questions)
    assert "5432" in " ".join(src.open_questions)  # last group preserved by mask
    assert "9876" not in " ".join(src.open_questions)

    # audit_log must not contain raw PII
    with session() as s:
        rows = s.execute(
            select(audit_log.c.output, audit_log.c.meta).where(
                audit_log.c.node.in_(["wiki.author", "wiki.distill", "wiki.consolidate"])
            )
        ).all()
    blob = json.dumps([{"o": r.output, "m": r.meta} for r in rows], ensure_ascii=False)
    assert secret_email not in blob
    assert "9876" not in blob


# ---------------------------------------------------------------------------
# Regression: unique per-document source slugs (fix authoring churn)
# Design: docs/wiki-unique-source-slug.md
# ---------------------------------------------------------------------------


def _distill_json_for(summary: str, claim: str, *, page_slug: str, page_title: str):
    return json.dumps(
        {
            "summary": summary,
            "key_concepts": [page_slug],
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


def test_two_same_title_docs_get_distinct_source_slugs_and_both_authored(user_id, tmp_path):
    """Two docs with identical title must produce 2 distinct source slugs."""
    title = "Release v2.0.0"
    page_slug = "release-v2-0-0"
    chat_a = MockChat(
        default=_distill_json_for(
            "Release A summary.", "Feature A shipped.", page_slug=page_slug, page_title=title
        )
    )
    chat_b = MockChat(
        default=_distill_json_for(
            "Release B summary.", "Feature B shipped.", page_slug=page_slug, page_title=title
        )
    )

    from orthus.documents import save_editor_document

    doc_a = save_editor_document(user_id, title, [{}], "Feature A shipped.", chat_model=chat_a)
    doc_b = save_editor_document(user_id, title, [{}], "Feature B shipped.", chat_model=chat_b)
    assert doc_a != doc_b

    author_from_document(doc_a, user_id, chat_model=chat_a, root=tmp_path)
    author_from_document(doc_b, user_id, chat_model=chat_b, root=tmp_path)

    source_slugs = store.list_slugs("source", root=tmp_path)
    assert len(source_slugs) == 2, f"expected 2 distinct source slugs, got {source_slugs}"

    for slug in source_slugs:
        assert slug.startswith("src-release-v2-0-0-"), f"unexpected slug shape: {slug!r}"

    # Each source_ref points to its own doc_id
    refs = {store.load_source(slug, root=tmp_path).source_ref for slug in source_slugs}
    assert str(doc_a) in refs
    assert str(doc_b) in refs

    # _authored_source_refs sees both doc_ids
    from orthus.wiki.author import _authored_source_refs

    authored = _authored_source_refs(root=tmp_path, scope="company", owner_id=user_id)
    assert str(doc_a) in authored, "doc_a not in authored refs"
    assert str(doc_b) in authored, "doc_b not in authored refs"

    # Concept-page merge: both sources feed the same concept page
    page = store.load_page(page_slug, root=tmp_path)
    assert page is not None, "concept page not written"
    assert len(page.sources) == 2, f"expected 2 sources on page, got {page.sources}"
    for slug in source_slugs:
        assert slug in page.sources, f"{slug} missing from page.sources"


def test_same_claim_text_from_two_sources_merges_one_page(user_id, tmp_path):
    """Identical claim text from two distinct sources must fold into one wiki page.

    Guards consolidate.py:146-161 from regressing on this design change."""
    page_slug = "shared-concept"
    shared_claim = "Shared concept is true across multiple sources."
    chat_x = MockChat(
        default=_distill_json_for(
            "Doc X summary.", shared_claim, page_slug=page_slug, page_title="Shared Concept"
        )
    )
    chat_y = MockChat(
        default=_distill_json_for(
            "Doc Y summary.", shared_claim, page_slug=page_slug, page_title="Shared Concept"
        )
    )

    from orthus.documents import save_editor_document

    doc_x = save_editor_document(user_id, "Doc X Title", [{}], "Content X.", chat_model=chat_x)
    doc_y = save_editor_document(user_id, "Doc Y Title", [{}], "Content Y.", chat_model=chat_y)

    author_from_document(doc_x, user_id, chat_model=chat_x, root=tmp_path)
    author_from_document(doc_y, user_id, chat_model=chat_y, root=tmp_path)

    # Two distinct source slugs (different titles AND different doc_ids)
    source_slugs = store.list_slugs("source", root=tmp_path)
    assert len(source_slugs) == 2

    # Exactly one concept page for the shared page slug
    page_slugs = store.list_slugs("page", root=tmp_path)
    assert page_slug in page_slugs, f"{page_slug} not in pages: {page_slugs}"

    page = store.load_page(page_slug, root=tmp_path)
    assert page is not None
    # Page accumulates both source slugs
    assert len(page.sources) == 2, f"expected 2 sources on merged page, got {page.sources}"
