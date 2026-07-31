"""W4 acceptance: the T2 compounding trigger (docs/llm-wiki.md §7/§9).

After a grounded wiki Q&A, `ask(learn=True)` persists the answer + grounding back
into the wiki as a low-confidence `qa_session` source + claim, so the wiki grows
from usage. Deterministic (no extra LLM call); dedup by normalized-question hash.

P2.1: T2 learning is PERSONAL — the qa source/claim are authored under the asking
user's personal scope (owner=user_id), not the shared company layer — so the store
reads here target scope="personal", owner_id=user_id.

The `_isolate_wiki_store` fixture is session-scoped, so the markdown store is shared
across tests (only Postgres is wiped per-test by `clean`). Each test therefore uses a
UNIQUE question and asserts on the slug DERIVED from its own question hash, never on
global file/row counts — keeping assertions immune to cross-test store pollution."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.tables import wiki_pages
from orthus.wiki import ask, store
from orthus.wiki.author import _qa_hash

# Scripted distill output so the editor save authors a real wiki page whose body
# carries "연차/15일", which retrieve then grounds the answer on.
_DISTILL_JSON = json.dumps(
    {
        "summary": "연차 휴가 정책 요약: 입사 1년차부터 15일 부여.",
        "key_concepts": ["연차 휴가"],
        "terminology": ["연차"],
        "open_questions": [],
        "claims": [
            {
                "claim": "연차 휴가는 입사 1년차부터 15일이 부여된다.",
                "evidence": "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
                "confidence": "high",
                "page": {"slug": "연차-휴가", "title": "연차 휴가"},
                "conflicting": [],
            }
        ],
    }
)

_ANSWER = "연차는 1년차부터 15일입니다."


def _seed_corpus_page(user_id):
    save_editor_document(
        user_id,
        "휴가",
        [{}],
        "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
        chat_model=MockChat(default=_DISTILL_JSON),
    )


def _slugs_for(question: str) -> tuple[str, str]:
    qa_slug = f"qa-{_qa_hash(question)}"
    return qa_slug, f"{qa_slug}-claim"


def test_grounded_ask_authors_qa_claim(user_id):
    question = "W4 연차 며칠 부여돼?"
    qa_slug, claim_slug = _slugs_for(question)
    _seed_corpus_page(user_id)

    out = ask(user_id, question, k=3, chat_model=MockChat(default=_ANSWER), learn=True)
    assert out.sources, "answer must be grounded for T2 to fire"

    # P2.1: T2 learning is PERSONAL (owner=asker) — qa source/claim land under the
    # user's personal scope, not the shared company layer.
    # the source is a qa_session typed source summarizing the answer.
    src = store.load_source(qa_slug, root=None, scope="personal", owner_id=user_id)
    assert src is not None
    assert src.source_type == "qa_session"
    assert src.summary == _ANSWER
    assert src.key_claims == [claim_slug]

    # its claim exists and is low-confidence, evidenced by the answer.
    assert store.exists("claim", claim_slug, scope="personal", owner_id=user_id)
    claim = store.load_claim(claim_slug, root=None, scope="personal", owner_id=user_id)
    assert claim is not None
    assert claim.confidence == "low"
    assert claim.evidence == _ANSWER
    assert qa_slug in claim.supporting

    # wiki_pages rows exist for the qa source + claim.
    with session() as s:
        slugs = {r.slug for r in s.execute(select(wiki_pages.c.slug)).all()}
    assert qa_slug in slugs
    assert claim_slug in slugs


def test_re_ask_is_idempotent_no_duplicate_no_conflict(user_id):
    question = "W4-idem 연차 며칠?"
    qa_slug, claim_slug = _slugs_for(question)
    _seed_corpus_page(user_id)

    def _count_rows(slug: str) -> int:
        with session() as s:
            return s.execute(
                select(func.count()).select_from(wiki_pages).where(wiki_pages.c.slug == slug)
            ).scalar()

    ask(user_id, question, k=3, chat_model=MockChat(default=_ANSWER), learn=True)
    claim_first = store.load_claim(claim_slug, root=None, scope="personal", owner_id=user_id)
    assert claim_first is not None
    # exactly one row per slug (upsert, not insert).
    assert _count_rows(qa_slug) == 1
    assert _count_rows(claim_slug) == 1

    # same question, same answer -> overwrite in place, no new files/rows.
    ask(user_id, question, k=3, chat_model=MockChat(default=_ANSWER), learn=True)
    assert _count_rows(qa_slug) == 1, "no duplicate qa_session row"
    assert _count_rows(claim_slug) == 1, "no duplicate qa claim row"

    # Content is byte-stable across re-asks: related_pages is taken from concept-page
    # hits only (the self-amplification guard), so the now-richer wiki — which also
    # surfaces the qa claim itself — does not drift this claim's provenance.
    claim_second = store.load_claim(claim_slug, root=None, scope="personal", owner_id=user_id)
    assert claim_second == claim_first
    assert claim_second.confidence == "low"

    # same slug + same claim text => consolidate must NOT emit a conflict task.
    conflict_slug = f"conflict-{claim_slug}"
    assert not store.exists("task", conflict_slug, scope="personal", owner_id=user_id), (
        "re-asking the same question must not trip the conflict rule against itself"
    )


def test_empty_grounding_learns_nothing(user_id):
    question = "W4-empty 존재하지 않는 주제에 대한 질문?"
    qa_slug, claim_slug = _slugs_for(question)

    # No corpus page authored -> empty wiki -> ungrounded answer.
    out = ask(user_id, question, k=3, chat_model=MockChat(), learn=True)
    assert out.sources == []
    assert not store.exists("source", qa_slug, scope="personal", owner_id=user_id), (
        "empty-grounding must author no source"
    )
    assert not store.exists("claim", claim_slug, scope="personal", owner_id=user_id), (
        "empty-grounding must author no claim"
    )


def test_learn_false_does_not_author(user_id):
    question = "W4-nolearn 연차 며칠?"
    qa_slug, claim_slug = _slugs_for(question)
    _seed_corpus_page(user_id)

    out = ask(user_id, question, k=3, chat_model=MockChat(default=_ANSWER), learn=False)
    assert out.sources, "answer is grounded"
    assert not store.exists("source", qa_slug, scope="personal", owner_id=user_id), (
        "learn=False must author no source"
    )
    assert not store.exists("claim", claim_slug, scope="personal", owner_id=user_id), (
        "learn=False must author no claim"
    )
