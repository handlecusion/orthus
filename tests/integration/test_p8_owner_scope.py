"""P8.1 acceptance: central owner-scope flag + fail-closed owner boundary.

docs/p8-central-consolidation.md §3/§6 (P8.1 row).

Two boundaries are exercised on the COMPANY (central) node (the `clean` fixture
sets node_kind="company"):

1. `ORTHUS_OWNER_SCOPE_ENABLED` (Settings.owner_scope_enabled), default false,
   gates whether central /ask honors an "all"/"personal" scope request. OFF =
   the central node stays company-only; ON = a logged-in user can merge their own
   personal rows with company knowledge. A blank request stays "company" either
   way.
2. The Agent Work / data-gap read filters are NOT gated on that flag — they are
   the permanent fail-closed owner boundary `(owner_id IS NULL) OR (owner_id ==
   caller)`. User B must never see user A's owned personal rows, regardless of the
   flag.

The seeded personal-owned rows belong to user A. Today company rows carry
owner_id NULL, so existing behavior is unchanged for those; these tests assert
that future owner_id-stamped personal rows stay invisible to non-owners.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from orthus.agentwork import get_agent_work_item, list_agent_work_items
from orthus.agentwork.service import (
    build_inbox_summary,
    get_policy_memory_context_for_decision,
    list_policy_memory_summaries,
)
from orthus.router import resolve_answer_scope as _resolve_scope
from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import AgentWorkCandidate, AgentWorkDecision
from orthus.settings import get_settings
from orthus.structured.query import query_structured
from orthus.tables import (
    agent_policy_observations,
    agent_work_decisions,
    agent_work_items,
    data_gaps,
    notion_rows,
    users,
    documents,
)
from orthus.wiki.gap import get_gap, list_gaps, list_gaps_for_wiki_page, set_gap_status
from orthus.wiki.retrieve import retrieve


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


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


def _seed_personal_wiki(owner_id: uuid.UUID, topic: str) -> None:
    """Author a personal-scope (owner=owner_id) wiki page so retrieve()/ask have a
    hit. This stamps scope/owner on documents+corpus+embeddings+wiki_pages."""
    save_editor_document(
        owner_id,
        topic,
        [{}],
        f"{topic}: 개인 비밀 메모 한 줄.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic} 요약", f"{topic}: 개인 메모.", page_slug=topic, page_title=topic
            )
        ),
        scope="personal",
    )


def _seed_personal_notion_row(owner_id: uuid.UUID, db_name: str) -> None:
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id=f"db-{db_name}",
                db_name=db_name,
                properties={"상태": "진행중"},
                scope="personal",
                owner_id=owner_id,
                user_id=owner_id,
            )
        )
        s.commit()


def _seed_company_agent_work() -> uuid.UUID:
    """A normal company-scope Agent Work row (owner_id NULL)."""
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind="company",
                owner_id=None,
                source_kind="data_gap",
                source_ref_id=f"company-gap-{work_id}",
                action_family="data_request",
                title="회사 데이터 갭",
                state="draft_for_review",
            )
        )
        s.commit()
    return work_id


def _seed_owned_agent_work(owner_id: uuid.UUID) -> uuid.UUID:
    """A personal-owned Agent Work row stored on the central (company) node."""
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind="company",
                owner_id=owner_id,
                source_kind="assistant_command",
                source_ref_id=f"owned-cmd-{work_id}",
                action_family="document_draft",
                title="개인 소유 작업",
                state="draft_for_review",
            )
        )
        s.commit()
    return work_id


def _seed_owned_data_gap(owner_id: uuid.UUID, question: str) -> uuid.UUID:
    gap_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(data_gaps).values(
                gap_id=gap_id,
                scope="personal",
                owner_id=owner_id,
                node_id=get_settings().node_id,
                question_norm=question.lower(),
                question=question,
                reason="no_data",
                hit_count=1,
                status="open",
                source="auto",
            )
        )
        s.commit()
    return gap_id


# bucket-key shape comes from _policy_bucket_key_for_values: action|source|outcome|reasons
_POLICY_BUCKET_KEY = "email_send|assistant_command|draft_for_review|none"


def _seed_policy_observation(owner_id: uuid.UUID | None, reviewer_decision: str) -> uuid.UUID:
    """An approve-style observation row in the email auto-send bucket. owner_id None
    is a company-scope (shared) row; a UUID is a personal-owned row. The FK parents
    (agent_work_items, agent_work_decisions) are created with the same owner_id."""
    settings = get_settings()
    observation_id = uuid.uuid4()
    work_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    reviewer_id = owner_id or _make_user()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind="company",
                owner_id=owner_id,
                source_kind="assistant_command",
                source_ref_id=f"obs-item-{work_id}",
                action_family="email_send",
                title="이메일 발송 작업",
                state="resolved",
            )
        )
        s.execute(
            insert(agent_work_decisions).values(
                decision_id=decision_id,
                work_id=work_id,
                node_id=settings.node_id,
                reviewer_id=reviewer_id,
                decision=reviewer_decision,
                from_state="draft_for_review",
                to_state="resolved",
            )
        )
        s.execute(
            insert(agent_policy_observations).values(
                observation_id=observation_id,
                node_id=settings.node_id,
                node_kind="company",
                owner_id=owner_id,
                work_id=work_id,
                decision_id=decision_id,
                reviewer_id=reviewer_id,
                source_kind="assistant_command",
                source_ref_id=f"obs-{observation_id}",
                action_family="email_send",
                policy_outcome="draft_for_review",
                reason_codes=[],
                reviewer_decision=reviewer_decision,
                from_state="draft_for_review",
                to_state="resolved",
                note_present=False,
                bucket_key=_POLICY_BUCKET_KEY,
                meta={},
                observed_at=datetime.now(UTC),
            )
        )
        s.commit()
    return observation_id


def _policy_context_for_bucket(user_id: uuid.UUID):
    candidate = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id=f"cand-{uuid.uuid4()}",
        action_family="email_send",
        title="이메일 발송 후보",
    )
    decision = AgentWorkDecision(
        outcome="draft_for_review",
        state="draft_for_review",
        policy_reason="needs review",
        reason_codes=[],
    )
    return get_policy_memory_context_for_decision(user_id, candidate, decision)


# --- /ask scope resolution (the flag) -------------------------------------------


def test_resolve_scope_company_node_flag_off_clamps_to_company(clean):
    settings = get_settings()
    assert settings.node_kind == "company"
    assert settings.owner_scope_enabled is False
    assert _resolve_scope("all") == "company"
    assert _resolve_scope("personal") == "company"
    assert _resolve_scope(None) == "company"
    assert _resolve_scope("company") == "company"


def test_resolve_scope_company_node_flag_on_honors_request(clean):
    settings = get_settings()
    settings.owner_scope_enabled = True
    assert _resolve_scope("all") == "all"
    assert _resolve_scope("personal") == "personal"
    # No explicit scope still stays company-only even with the flag on.
    assert _resolve_scope(None) == "company"
    assert _resolve_scope("company") == "company"


def test_resolve_scope_personal_node_unchanged(clean):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.owner_scope_enabled = False
    assert _resolve_scope(None) == "all"
    assert _resolve_scope("company") == "company"
    assert _resolve_scope("personal") == "personal"


# --- wiki retrieve / structured boundary ----------------------------------------


def test_flag_off_company_scope_hides_owned_personal_for_all(clean, user_id):
    """Flag OFF: scope resolves to company, so A's personal page is invisible to
    both A and B through the central /ask scope."""
    user_b = _make_user()
    topic = f"p8-off-{uuid.uuid4().hex[:8]}"
    _seed_personal_wiki(user_id, topic)

    resolved = _resolve_scope("all")  # company-clamped
    assert resolved == "company"
    hits_a = retrieve(user_id, f"{topic} 개인 메모", k=10, scope=resolved)
    hits_b = retrieve(user_b, f"{topic} 개인 메모", k=10, scope=resolved)
    assert not any(topic in h.page_slug for h in hits_a)
    assert not any(topic in h.page_slug for h in hits_b)


def test_flag_on_owner_sees_own_personal_others_do_not(clean, user_id):
    """Flag ON: scope='all' as A includes A's personal page; as B it does not."""
    settings = get_settings()
    settings.owner_scope_enabled = True
    user_b = _make_user()
    topic = f"p8-on-{uuid.uuid4().hex[:8]}"
    _seed_personal_wiki(user_id, topic)

    resolved = _resolve_scope("all")  # honored
    assert resolved == "all"
    hits_a = retrieve(user_id, f"{topic} 개인 메모", k=10, scope=resolved)
    hits_b = retrieve(user_b, f"{topic} 개인 메모", k=10, scope=resolved)
    assert any(topic in h.page_slug for h in hits_a), "owner sees own personal page"
    assert not any(topic in h.page_slug for h in hits_b), "non-owner never sees it"


def test_flag_on_personal_scope_excludes_other_users_rows(clean, user_id):
    """Flag ON: scope='personal' as B returns none of A's personal rows."""
    settings = get_settings()
    settings.owner_scope_enabled = True
    user_b = _make_user()
    topic = f"p8-onp-{uuid.uuid4().hex[:8]}"
    _seed_personal_wiki(user_id, topic)

    resolved = _resolve_scope("personal")
    assert resolved == "personal"
    hits_b = retrieve(user_b, f"{topic} 개인 메모", k=10, scope=resolved)
    assert not any(topic in h.page_slug for h in hits_b)


def test_flag_on_structured_owner_isolation(clean, user_id):
    """Flag ON: structured 'all' as A sees A's personal notion row; B does not."""
    settings = get_settings()
    settings.owner_scope_enabled = True
    user_b = _make_user()
    db_name = f"p8보드{uuid.uuid4().hex[:6]}"
    _seed_personal_notion_row(user_id, db_name)

    sql = f"SELECT count(*) AS n FROM notion_rows WHERE db_name = '{db_name}'"
    chat = MockChat(rules=[("개수", '{"sql": "%s"}' % sql)])

    res_a = query_structured(user_id, f"{db_name} 개수", scope="all", chat_model=chat)
    res_b = query_structured(user_b, f"{db_name} 개수", scope="all", chat_model=chat)
    assert res_a.status == "executed"
    assert res_a.rows[0][0] == 1, "owner sees their own personal row"
    assert res_b.status == "executed"
    assert res_b.rows[0][0] == 0, "non-owner never sees A's personal row"


# --- Agent Work owner boundary (permanent, not flag-gated) ----------------------


@pytest.mark.parametrize("flag", [False, True])
def test_agent_work_owner_boundary_is_unconditional(clean, user_id, flag):
    """B never sees A's owned Agent Work row; A sees own + company(NULL) rows.
    Independent of owner_scope_enabled."""
    settings = get_settings()
    settings.owner_scope_enabled = flag
    user_b = _make_user()
    company_work_id = _seed_company_agent_work()
    owned_work_id = _seed_owned_agent_work(user_id)

    a_ids = {item.work_id for item in list_agent_work_items(user_id)}
    b_ids = {item.work_id for item in list_agent_work_items(user_b)}

    assert company_work_id in a_ids and owned_work_id in a_ids
    assert company_work_id in b_ids, "company NULL-owner rows stay shared"
    assert owned_work_id not in b_ids, "B must not see A's owned Agent Work"


def test_agent_work_scope_personal_returns_only_caller_owned(clean, user_id):
    """scope='personal' narrows to the caller's own rows — never company
    NULL-owner rows and never another user's owned rows."""
    user_b = _make_user()
    company_work_id = _seed_company_agent_work()
    owned_work_id = _seed_owned_agent_work(user_id)

    a_personal = {i.work_id for i in list_agent_work_items(user_id, scope="personal")}
    assert owned_work_id in a_personal, "owner sees own personal row"
    assert company_work_id not in a_personal, "company NULL-owner row excluded from personal scope"

    b_personal = {i.work_id for i in list_agent_work_items(user_b, scope="personal")}
    assert owned_work_id not in b_personal, "B never sees A's owned row in personal scope"
    assert company_work_id not in b_personal


def test_agent_work_scope_company_returns_only_shared(clean, user_id):
    """scope='company' returns shared NULL-owner rows only — no personal-owned rows."""
    company_work_id = _seed_company_agent_work()
    owned_work_id = _seed_owned_agent_work(user_id)

    a_company = {i.work_id for i in list_agent_work_items(user_id, scope="company")}
    assert company_work_id in a_company
    assert owned_work_id not in a_company, "owned personal row excluded from company scope"


def test_get_agent_work_item_owned_row_not_found_for_other_user(clean, user_id):
    user_b = _make_user()
    owned_work_id = _seed_owned_agent_work(user_id)

    assert get_agent_work_item(user_id, owned_work_id) is not None
    assert get_agent_work_item(user_b, owned_work_id) is None


def test_inbox_summary_agent_work_excludes_other_owner(clean, user_id):
    user_b = _make_user()
    company_work_id = _seed_company_agent_work()
    owned_work_id = _seed_owned_agent_work(user_id)

    b_summary = build_inbox_summary(user_b)
    b_ids = {item.work_id for item in b_summary.buckets.agent_work.items}
    assert company_work_id in b_ids
    assert owned_work_id not in b_ids


def test_policy_memory_summaries_owner_scoped_smoke(clean, user_id):
    """Owner predicate applies without error on the company node (no rows seeded;
    asserts the query runs and returns the empty boundary)."""
    user_b = _make_user()
    assert list_policy_memory_summaries(user_id) == []
    assert list_policy_memory_summaries(user_b) == []


@pytest.mark.parametrize("flag", [False, True])
def test_policy_memory_summaries_exclude_other_owner(clean, user_id, flag):
    """Company node: B's bucket summary aggregates the shared NULL-owner observation
    but never A's owned observation; A counts both. Not flag-gated."""
    settings = get_settings()
    settings.owner_scope_enabled = flag
    user_b = _make_user()
    _seed_policy_observation(None, "approve")  # company-shared
    _seed_policy_observation(user_id, "approve")  # A-owned

    def _total(rows) -> int:
        bucket = next((r for r in rows if r.bucket_key == _POLICY_BUCKET_KEY), None)
        return bucket.total if bucket is not None else 0

    a_total = _total(list_policy_memory_summaries(user_id))
    b_total = _total(list_policy_memory_summaries(user_b))
    assert a_total == 2, "owner sees shared + own observation"
    assert b_total == 1, "non-owner sees only the shared NULL-owner observation"


def test_policy_memory_context_excludes_other_owner(clean, user_id):
    """get_policy_memory_context_for_decision aggregates the shared NULL-owner
    observation for everyone but A's owned observation only for A."""
    user_b = _make_user()
    _seed_policy_observation(None, "approve")  # company-shared
    _seed_policy_observation(user_id, "approve")  # A-owned

    ctx_a = _policy_context_for_bucket(user_id)
    ctx_b = _policy_context_for_bucket(user_b)
    assert ctx_a.bucket_key == _POLICY_BUCKET_KEY
    assert ctx_a.total == 2, "owner sees shared + own observation"
    assert ctx_b.total == 1, "non-owner sees only the shared NULL-owner observation"


# --- data gaps owner boundary (permanent, not flag-gated) -----------------------


@pytest.mark.parametrize("flag", [False, True])
def test_data_gaps_owner_boundary_is_unconditional(clean, user_id, flag):
    settings = get_settings()
    settings.owner_scope_enabled = flag
    user_b = _make_user()
    a_gap = _seed_owned_data_gap(user_id, f"A 개인 갭 {uuid.uuid4().hex[:6]}")

    a_ids = {g.gap_id for g in list_gaps(user_id)}
    b_ids = {g.gap_id for g in list_gaps(user_b)}
    assert a_gap in a_ids
    assert a_gap not in b_ids, "B must not see A's owned data gap"


def test_data_gaps_for_wiki_page_owner_boundary(clean, user_id):
    user_b = _make_user()
    slug = f"p8-gap-page-{uuid.uuid4().hex[:6]}"
    gap_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(data_gaps).values(
                gap_id=gap_id,
                scope="personal",
                owner_id=user_id,
                node_id=get_settings().node_id,
                question_norm=f"q-{gap_id}",
                question="A의 페이지 연결 갭",
                reason="no_data",
                context_wiki_slug=slug,
                hit_count=1,
                status="open",
                source="auto",
            )
        )
        s.commit()

    a_ids = {g.gap_id for g in list_gaps_for_wiki_page(user_id, slug)}
    b_ids = {g.gap_id for g in list_gaps_for_wiki_page(user_b, slug)}
    assert gap_id in a_ids
    assert gap_id not in b_ids


def test_get_gap_owned_row_not_found_for_other_user(clean, user_id):
    """By-id fetch: B cannot read A's owned personal gap; A can; the shared
    company NULL-owner gap stays readable by both."""
    user_b = _make_user()
    a_gap = _seed_owned_data_gap(user_id, f"A 개인 갭 byid {uuid.uuid4().hex[:6]}")

    assert get_gap(user_id, a_gap) is not None, "owner reads own gap by id"
    assert get_gap(user_b, a_gap) is None, "B must not read A's owned gap by id"


def test_set_gap_status_owned_row_denied_for_other_user(clean, user_id):
    """By-id resolve: B cannot mutate A's owned personal gap; A can resolve own."""
    user_b = _make_user()
    a_gap = _seed_owned_data_gap(user_id, f"A 개인 갭 set {uuid.uuid4().hex[:6]}")

    assert set_gap_status(user_b, a_gap, "resolved") is None, "B cannot resolve A's gap"
    # The row stays open after B's denied attempt.
    assert get_gap(user_id, a_gap).status == "open"
    # The owner can resolve their own gap.
    resolved = set_gap_status(user_id, a_gap, "resolved")
    assert resolved is not None and resolved.status == "resolved"


# --- personal Gmail inbox owner boundary ----------------------------------------


def _seed_gmail_doc(owner_id: uuid.UUID, subject: str) -> uuid.UUID:
    """Insert a gws_gmail personal document for owner_id."""
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=owner_id,
                title=f"Gmail: {subject}",
                block_json=[],
                markdown=f"# {subject}\n\nSource: gws_gmail\nMessage ID: msg-{doc_id}\nThread ID: thr-{doc_id}\n\n본문 내용입니다.",
                source="gws_gmail",
                source_external_id=f"gmail:{doc_id}",
                source_last_edited_at=None,
                schema_version=1,
                scope="personal",
                project="atlas",
            )
        )
        s.commit()
    return doc_id


def test_personal_gmail_list_owner_sees_own(clean, user_id):
    """Owner's listing includes their own gws_gmail document."""
    from orthus.mail.personal import list_personal_gmail

    _seed_gmail_doc(user_id, "오너 메일")
    result = list_personal_gmail(user_id)
    assert result.total >= 1
    subjects = [item.subject for item in result.items]
    assert any("오너 메일" in s for s in subjects)


def test_personal_gmail_list_other_user_sees_nothing(clean, user_id):
    """User B's listing does NOT include user A's gws_gmail documents."""
    from orthus.mail.personal import list_personal_gmail

    _seed_gmail_doc(user_id, "A의 비밀 메일")
    user_b = _make_user()
    result_b = list_personal_gmail(user_b)
    subjects = [item.subject for item in result_b.items]
    assert not any("A의 비밀 메일" in s for s in subjects)


def test_personal_gmail_detail_owner_can_read(clean, user_id):
    """Owner can fetch their own gws_gmail doc detail."""
    from orthus.mail.personal import get_personal_gmail

    doc_id = _seed_gmail_doc(user_id, "상세 조회 메일")
    detail = get_personal_gmail(user_id, doc_id)
    assert detail is not None
    assert detail.subject == "상세 조회 메일"
    assert detail.doc_id == doc_id


def test_personal_gmail_detail_other_user_gets_none(clean, user_id):
    """User B cannot read user A's gws_gmail doc by id (returns None)."""
    from orthus.mail.personal import get_personal_gmail

    doc_id = _seed_gmail_doc(user_id, "A의 상세 메일")
    user_b = _make_user()
    detail_b = get_personal_gmail(user_b, doc_id)
    assert detail_b is None, "non-owner must not read A's personal gmail doc"
