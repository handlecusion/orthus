"""P8.7b: dual-auth knowledge endpoints + owner boundary + rate limit + wiki task.

A scope-`knowledge` collector token authenticates the read knowledge surface and
`/ask`; scope `knowledge:write` authenticates `POST /wiki/tasks`. Session/demo/jwt
behavior stays unchanged. Tokens never reach promote/allowlist/command-create/
decision. The owner-only row boundary still hides user B's personal rows from
user A's token. The per-token `/ask` rate limit returns 429 beyond 30/5min.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.knowledge_token import (
    ASK_RATE_LIMIT_CALLS,
    WIKI_TASK_MAX_NOTE_CHARS,
    WIKI_TASK_RATE_LIMIT_CALLS,
    reset_token_ask_rate_limit,
    reset_token_dashboard_write_rate_limit,
    reset_token_wiki_task_rate_limit,
)
from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import agent_work_items, collector_tokens, data_gaps, users
from orthus.wiki import store

client = TestClient(app)

COMPANY_NODE = "company"


def _enable(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    reset_token_ask_rate_limit()
    reset_token_wiki_task_rate_limit()
    reset_token_dashboard_write_rate_limit()


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _make_token(user_id, *, scopes: list[str], node_id: str = COMPANY_NODE) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=node_id,
                name="mcp-test",
                token_hash=hash_collector_token(token),
                scopes=scopes,
            )
        )
        s.commit()
    return token


def _revoke(token: str) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import update

    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_hash == hash_collector_token(token))
            .values(revoked_at=datetime.now(UTC))
        )
        s.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _distill_json(summary: str, claim: str, *, page_slug: str, page_title: str) -> str:
    import json

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


def _seed_owned_agent_work(owner_id: uuid.UUID) -> uuid.UUID:
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


# --- dual-auth: knowledge token works on the read surface + /ask ----------------


def test_knowledge_token_can_ask(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.post(
        "/ask", json={"question": "회사 정보", "scope": "all"}, headers=_headers(token)
    )
    assert res.status_code == 200, res.text
    assert res.json()["mode"] in {"wiki", "structured"}


def test_knowledge_token_lists_own_agent_work(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    work_id = _seed_owned_agent_work(user_id)
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/agent-work", headers=_headers(token))
    assert res.status_code == 200, res.text
    assert {item["work_id"] for item in res.json()} == {str(work_id)}

    detail = client.get(f"/agent-work/{work_id}", headers=_headers(token))
    assert detail.status_code == 200
    assert detail.json()["work_id"] == str(work_id)


def test_knowledge_token_wiki_search_and_page(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    _seed_personal_wiki(user_id, "토픽알파")
    token = _make_token(user_id, scopes=["knowledge"])

    search = client.get(
        "/wiki/search", params={"query": "토픽알파", "scope": "all"}, headers=_headers(token)
    )
    assert search.status_code == 200, search.text
    slugs = {item["slug"] for item in search.json()["items"]}
    assert "토픽알파" in slugs

    page = client.get("/wiki/pages/토픽알파", headers=_headers(token))
    assert page.status_code == 200, page.text
    assert page.json()["slug"] == "토픽알파"


def test_knowledge_token_reads_team_calendar(clean, monkeypatch):
    # The shared team calendar is company internal knowledge: a knowledge-scoped
    # token (the owner's orthus-mcp) can read it for schedule-aware reply drafting.
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/dashboard/calendar", headers=_headers(token))
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)


# --- slice 2: inbox_summary + data_gaps read tools ------------------------------


def test_knowledge_token_reads_inbox_summary(clean, monkeypatch):
    # The orthus-mcp `inbox_summary` tool reads the caller's Agent Work inbox via
    # the knowledge token; the bucket counts reflect the token owner's own rows.
    _enable(monkeypatch)
    user_id = _make_user()
    _seed_owned_agent_work(user_id)
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/agent-work/inbox-summary", headers=_headers(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["node_kind"] == "company"
    assert body["buckets"]["agent_work"]["count"] >= 1


def test_knowledge_token_reads_data_gaps(clean, monkeypatch):
    from orthus.wiki.gap import record_feedback

    _enable(monkeypatch)
    user_id = _make_user()
    record_feedback(user_id, "셀러스 정산 주기는?")
    token = _make_token(user_id, scopes=["knowledge"])

    res = client.get("/gaps", params={"status": "open"}, headers=_headers(token))
    assert res.status_code == 200, res.text
    questions = {row["question"] for row in res.json()}
    assert "셀러스 정산 주기는?" in questions


def test_knowledge_token_reads_page_data_gaps(clean, monkeypatch):
    from orthus.wiki.gap import record_feedback

    _enable(monkeypatch)
    user_id = _make_user()
    _seed_personal_wiki(user_id, "토픽베타")
    record_feedback(user_id, "토픽베타 추가 질문?", context_wiki_slug="토픽베타")
    token = _make_token(user_id, scopes=["knowledge"])

    # _seed_personal_wiki makes a personal-scope page, so resolve the page in the
    # personal scope; the open gap (linked by context_wiki_slug) is owner-readable.
    res = client.get(
        "/wiki/pages/토픽베타/data-gaps", params={"scope": "personal"}, headers=_headers(token)
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 1
    assert any(item["question"] == "토픽베타 추가 질문?" for item in body["items"])


def test_data_gaps_owner_boundary_token_b_blind(clean, monkeypatch):
    # User B's knowledge token must not see user A's personal-scope gap (owner-only
    # row boundary). A personal-scope gap is owner-bound; B's token gets an empty list
    # for it, while A's own token does see it.
    from datetime import UTC, datetime

    _enable(monkeypatch)
    user_a = _make_user()
    user_b = _make_user()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(data_gaps).values(
                gap_id=uuid.uuid4(),
                scope="personal",
                owner_id=user_a,
                node_id=COMPANY_NODE,
                reason="insufficient_grounding",
                question="A 전용 갭 질문?",
                question_norm="a 전용 갭 질문",
                suggestion_status="pending",
                hit_count=1,
                status="open",
                source="feedback",
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
        s.commit()

    token_a = _make_token(user_a, scopes=["knowledge"])
    token_b = _make_token(user_b, scopes=["knowledge"])

    res_a = client.get("/gaps", params={"status": "open"}, headers=_headers(token_a))
    assert res_a.status_code == 200, res_a.text
    assert any(row["question"] == "A 전용 갭 질문?" for row in res_a.json())

    res_b = client.get("/gaps", params={"status": "open"}, headers=_headers(token_b))
    assert res_b.status_code == 200, res_b.text
    assert all(row["question"] != "A 전용 갭 질문?" for row in res_b.json())


def test_inbox_and_gaps_reject_ingest_only_token_403(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["ingest"])  # no knowledge scope
    for path in ["/agent-work/inbox-summary", "/gaps"]:
        res = client.get(path, headers=_headers(token))
        assert res.status_code == 403, f"{path}: {res.status_code} {res.text}"


# --- knowledge:write token can add/update/delete team + personal schedule -------


def test_knowledge_write_token_team_calendar_crud(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    created = client.post(
        "/dashboard/calendar/events",
        json={"title": "주간회의", "event_date": "2026-07-01", "all_day": True},
        headers=_headers(token),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["event_id"]

    patched = client.patch(
        f"/dashboard/calendar/events/{event_id}",
        json={"title": "주간회의", "event_date": "2026-07-01", "location": "회의실A"},
        headers=_headers(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["location"] == "회의실A"

    listed = client.get(
        "/dashboard/calendar",
        params={"from": "2026-07-01", "to": "2026-07-01"},
        headers=_headers(token),
    )
    assert listed.status_code == 200, listed.text
    assert any(e["event_id"] == event_id for e in listed.json())

    deleted = client.delete(f"/dashboard/calendar/events/{event_id}", headers=_headers(token))
    assert deleted.status_code == 204, deleted.text


def test_read_only_knowledge_token_cannot_write_team_calendar(clean, monkeypatch):
    # scope `knowledge` (read) lacks `knowledge:write`: create must 403, not 201.
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.post(
        "/dashboard/calendar/events",
        json={"title": "x", "event_date": "2026-07-01"},
        headers=_headers(token),
    )
    assert res.status_code == 403, res.text


def test_knowledge_write_token_team_members_add_and_list(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    write = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    added = client.post(
        "/dashboard/team-members",
        json={"name": "신입민수"},
        headers=_headers(write),
    )
    assert added.status_code == 201, added.text
    listed = client.get("/dashboard/team-members", headers=_headers(write))
    assert listed.status_code == 200, listed.text
    assert any(m["name"] == "신입민수" for m in listed.json())


def test_knowledge_write_token_personal_schedule_crud(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "치과",
            "starts_at": "2026-07-01T14:00:00+09:00",
            "ends_at": "2026-07-01T15:00:00+09:00",
        },
        headers=_headers(token),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["event_id"]

    listed = client.get(
        "/personal-board/fixed-events",
        params={"from": "2026-07-01", "to": "2026-07-01"},
        headers=_headers(token),
    )
    assert listed.status_code == 200, listed.text
    assert any(e["event_id"] == event_id for e in listed.json())

    patched = client.patch(
        f"/personal-board/fixed-events/{event_id}",
        json={"title": "치과 정기검진"},
        headers=_headers(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "치과 정기검진"


def test_read_only_knowledge_token_cannot_write_personal_schedule(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "x",
            "starts_at": "2026-07-01T14:00:00+09:00",
            "ends_at": "2026-07-01T15:00:00+09:00",
        },
        headers=_headers(token),
    )
    assert res.status_code == 403, res.text


def test_owner_boundary_personal_schedule_not_visible_to_other_token(clean, monkeypatch):
    # User A's personal event must not appear in user B's token list.
    _enable(monkeypatch)
    user_a = _make_user()
    user_b = _make_user()
    token_a = _make_token(user_a, scopes=["knowledge", "knowledge:write"])
    token_b = _make_token(user_b, scopes=["knowledge"])
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "A의 비밀 일정",
            "starts_at": "2026-07-02T09:00:00+09:00",
            "ends_at": "2026-07-02T10:00:00+09:00",
        },
        headers=_headers(token_a),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["event_id"]
    listed_b = client.get(
        "/personal-board/fixed-events",
        params={"from": "2026-07-02", "to": "2026-07-02"},
        headers=_headers(token_b),
    )
    assert listed_b.status_code == 200, listed_b.text
    assert all(e["event_id"] != event_id for e in listed_b.json())


# --- ingest-only token -> 403; revoked -> 401 -----------------------------------


def test_ingest_only_token_rejected_403(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["ingest"])
    for method, path in [
        ("POST", "/ask"),
        ("GET", "/agent-work"),
        ("GET", "/wiki/search"),
        ("GET", "/dashboard/calendar"),
    ]:
        if method == "POST":
            res = client.post(path, json={"question": "x"}, headers=_headers(token))
        else:
            params = {"query": "x"} if "search" in path else None
            res = client.get(path, params=params, headers=_headers(token))
        assert res.status_code == 403, f"{path}: {res.status_code} {res.text}"


def test_revoked_token_rejected_401(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    _revoke(token)
    res = client.get("/agent-work", headers=_headers(token))
    assert res.status_code == 401, res.text


# --- token must never reach promote/allowlist/command-create/decision -----------


def _session_mode(monkeypatch) -> None:
    """Production auth mode: a `dct_` bearer never satisfies a session route.

    In demo mode `get_current_user` ignores Authorization entirely, so the
    no-token-path assertions must run under session mode to be meaningful."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_company")
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)


def test_knowledge_token_cannot_reach_command_or_promote_or_decision(clean, monkeypatch):
    _enable(monkeypatch)
    _session_mode(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    # promote stage list: session-only operator surface (no token path) -> 401
    promote = client.get("/promote/stage", headers=_headers(token))
    assert promote.status_code == 401, promote.text

    # command create: session-only operator -> 401 (token never satisfies it)
    cmd = client.post(
        "/collector/commands",
        json={"kind": "connector_sync", "payload": {"connector": "gws_gmail"}},
        headers=_headers(token),
    )
    assert cmd.status_code == 401, cmd.text

    # agent-work decision: session-only -> 401, and no token path
    decision = client.post(
        f"/agent-work/{uuid.uuid4()}/decision",
        json={"decision": "approve"},
        headers=_headers(token),
    )
    assert decision.status_code == 401, decision.text


def test_knowledge_token_cannot_reach_allowlist(clean, monkeypatch):
    _enable(monkeypatch)
    _session_mode(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/auth/allowlist", headers=_headers(token))
    # Either no token path (401) or the route is session-only; never 200.
    assert res.status_code in {401, 403, 404}, res.text


# --- owner boundary: token of B cannot see A's personal rows --------------------


def test_owner_boundary_token_b_cannot_see_a_personal(clean, monkeypatch):
    _enable(monkeypatch)
    user_a = _make_user()
    user_b = _make_user()
    _seed_personal_wiki(user_a, "에이비밀")
    token_b = _make_token(user_b, scopes=["knowledge"])

    search = client.get(
        "/wiki/search", params={"query": "에이비밀", "scope": "all"}, headers=_headers(token_b)
    )
    assert search.status_code == 200, search.text
    assert all(item["scope"] != "personal" for item in search.json()["items"])
    assert "에이비밀" not in {item["slug"] for item in search.json()["items"]}

    # B's own personal page (owner A) is not visible via /wiki/pages either.
    page = client.get("/wiki/pages/에이비밀", headers=_headers(token_b))
    assert page.status_code == 404, page.text


# --- rate limit: 31st token /ask in window -> 429 -------------------------------


def test_token_ask_rate_limit(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    for i in range(ASK_RATE_LIMIT_CALLS):
        res = client.post("/ask", json={"question": "회사"}, headers=_headers(token))
        assert res.status_code == 200, f"call {i}: {res.status_code} {res.text}"
    over = client.post("/ask", json={"question": "회사"}, headers=_headers(token))
    assert over.status_code == 429, over.text


# --- wiki_update_candidate: company-scope review task (owner + admin reviewable) -


def test_wiki_update_candidate_creates_company_review_task(clean, monkeypatch):
    _enable(monkeypatch)  # node_kind="company"
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge:write"])
    res = client.post(
        "/wiki/tasks",
        json={
            "slug": "회사-개요",
            "note": "이 페이지 업데이트 필요",
            "evidence_urls": ["https://x"],
        },
        headers=_headers(token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "open_question"
    # On a company node the candidate is a COMPANY-scope review task so owner AND
    # admin operators see it in the shared /wiki/tasks queue (the actual content
    # change is still gated to owner/admin at resolve time).
    assert body["scope"] == "company"
    assert body["target_slug"] == "회사-개요"

    task = store.load_task(body["slug"], scope="company", owner_id=None)
    assert task is not None
    assert task.kind == "open_question"
    assert task.resolved is False
    assert "회사-개요" in task.related
    # It is NOT buried in the caller's personal scope.
    assert store.load_task(body["slug"], scope="personal", owner_id=user_id) is None


def test_knowledge_read_token_cannot_write_wiki_task_403(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])  # read-only
    res = client.post(
        "/wiki/tasks",
        json={"slug": "회사-개요", "note": "x"},
        headers=_headers(token),
    )
    assert res.status_code == 403, res.text


# --- M1: token candidate rate-limit + size caps (anti-spam) ---------------------


def test_wiki_task_token_rate_limited(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge:write"])
    body = {"slug": "회사-개요", "note": "후보"}
    for i in range(WIKI_TASK_RATE_LIMIT_CALLS):
        res = client.post("/wiki/tasks", json=body, headers=_headers(token))
        assert res.status_code == 200, f"call {i}: {res.text}"
    over = client.post("/wiki/tasks", json=body, headers=_headers(token))
    assert over.status_code == 429, over.text


def test_wiki_task_token_note_too_long_413(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge:write"])
    res = client.post(
        "/wiki/tasks",
        json={"slug": "회사-개요", "note": "x" * (WIKI_TASK_MAX_NOTE_CHARS + 1)},
        headers=_headers(token),
    )
    assert res.status_code == 413, res.text


def test_wiki_task_session_user_not_rate_limited(clean, monkeypatch):
    # A session/demo operator (X-User-Id) is exempt from the token rate limit.
    _enable(monkeypatch)
    user_id = _make_user()
    hdr = {"X-User-Id": str(user_id)}
    for i in range(WIKI_TASK_RATE_LIMIT_CALLS + 3):
        res = client.post("/wiki/tasks", json={"slug": "회사-개요", "note": "후보"}, headers=hdr)
        assert res.status_code == 200, f"call {i}: {res.text}"


# --- whoami: identity + node role for the calling token -------------------------


def test_whoami_returns_identity_and_node(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])
    res = client.get("/collector/whoami", headers=_headers(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == str(user_id)
    assert body["node_id"] == COMPANY_NODE
    assert body["scopes"] == ["knowledge"]
    # A bare token user with no auth_identity/allowlist row has no email/role.
    assert body["email"] is None
    assert body["role"] is None


def test_whoami_returns_effective_token_scopes(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["ingest", "knowledge", "agent_task"])
    res = client.get("/collector/whoami", headers=_headers(token))
    assert res.status_code == 200, res.text
    assert res.json()["scopes"] == ["agent_task", "commands", "ingest", "knowledge"]


def test_whoami_requires_knowledge_scope(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["ingest"])  # no knowledge scope
    res = client.get("/collector/whoami", headers=_headers(token))
    assert res.status_code == 403, res.text


# --- session/demo behavior unchanged (no token) ---------------------------------


def test_demo_user_unchanged_no_token(clean, monkeypatch):
    """A normal demo identity (X-User-Id) still reaches the endpoints with no
    token and is never rate limited by the token limiter."""
    _enable(monkeypatch)
    user_id = _make_user()
    _seed_owned_agent_work(user_id)
    hdr = {"X-User-Id": str(user_id)}

    ask = client.post("/ask", json={"question": "회사"}, headers=hdr)
    assert ask.status_code == 200, ask.text

    work = client.get("/agent-work", headers=hdr)
    assert work.status_code == 200, work.text

    # Far more than the token window; demo identity is not rate limited.
    for _ in range(ASK_RATE_LIMIT_CALLS + 5):
        res = client.post("/ask", json={"question": "회사"}, headers=hdr)
        assert res.status_code == 200, res.text


# --- orthus ticket surface: dual-auth board task CRUD (P9 ticket CLI) ---------------


def _create_ticket(token: str, title: str = "티켓", **extra) -> dict:
    payload = {"title": title, "scheduled_date": "2026-07-10", **extra}
    created = client.post("/personal-board/tasks", json=payload, headers=_headers(token))
    assert created.status_code == 201, created.text
    return created.json()


def test_knowledge_write_token_ticket_crud_roundtrip(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    task = _create_ticket(token, "로그인 500 오류 조사", priority="urgent")
    task_id = task["task_id"]
    assert task["status"] == "open"
    assert task["priority"] == "urgent"

    listed = client.get("/personal-board/tasks", params={"status": "open"}, headers=_headers(token))
    assert listed.status_code == 200, listed.text
    assert any(t["task_id"] == task_id for t in listed.json())

    # git-SHA style short-id prefix (dashless) resolves to exactly this ticket.
    prefix = task_id.replace("-", "")[:8]
    by_prefix = client.get(
        "/personal-board/tasks", params={"id_prefix": prefix}, headers=_headers(token)
    )
    assert by_prefix.status_code == 200, by_prefix.text
    assert [t["task_id"] for t in by_prefix.json()] == [task_id]

    done = client.patch(
        f"/personal-board/tasks/{task_id}", json={"status": "done"}, headers=_headers(token)
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"

    commented = client.post(
        f"/personal-board/tasks/{task_id}/comments",
        json={"body": "원인: 세션 쿠키 만료 처리 누락"},
        headers=_headers(token),
    )
    assert commented.status_code == 201, commented.text
    comments = client.get(f"/personal-board/tasks/{task_id}/comments", headers=_headers(token))
    assert comments.status_code == 200, comments.text
    assert any("세션 쿠키" in c["body"] for c in comments.json())

    # done is reversible (P3.3c: no destructive transition).
    reopened = client.patch(
        f"/personal-board/tasks/{task_id}", json={"status": "open"}, headers=_headers(token)
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "open"


def test_ticket_create_validation_maps_to_422(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    both = client.post(
        "/personal-board/tasks",
        json={
            "title": "x",
            "scheduled_date": "2026-07-10",
            "backlog_bucket_id": str(uuid.uuid4()),
        },
        headers=_headers(token),
    )
    assert both.status_code == 422, both.text
    assert "placement" in both.json()["detail"]

    neither = client.post("/personal-board/tasks", json={"title": "x"}, headers=_headers(token))
    assert neither.status_code == 422, neither.text

    bad_status = client.get(
        "/personal-board/tasks", params={"status": "opened"}, headers=_headers(token)
    )
    assert bad_status.status_code == 422, bad_status.text

    bad_prefix = client.get(
        "/personal-board/tasks", params={"id_prefix": "zz!"}, headers=_headers(token)
    )
    assert bad_prefix.status_code == 422, bad_prefix.text


def test_ticket_idempotent_create_with_client_id(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    client_id = str(uuid.uuid4())
    first = _create_ticket(token, "재시도 안전", task_id=client_id)
    second = _create_ticket(token, "재시도 안전", task_id=client_id)
    assert first["task_id"] == second["task_id"] == client_id

    listed = client.get(
        "/personal-board/tasks",
        params={"id_prefix": client_id.replace("-", "")},
        headers=_headers(token),
    )
    assert len(listed.json()) == 1


def test_read_only_knowledge_token_cannot_write_tickets(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    write = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    read_only = _make_token(user_id, scopes=["knowledge"])
    task = _create_ticket(write)

    created = client.post(
        "/personal-board/tasks",
        json={"title": "x", "scheduled_date": "2026-07-10"},
        headers=_headers(read_only),
    )
    assert created.status_code == 403, created.text
    patched = client.patch(
        f"/personal-board/tasks/{task['task_id']}",
        json={"status": "done"},
        headers=_headers(read_only),
    )
    assert patched.status_code == 403, patched.text
    commented = client.post(
        f"/personal-board/tasks/{task['task_id']}/comments",
        json={"body": "x"},
        headers=_headers(read_only),
    )
    assert commented.status_code == 403, commented.text
    listed = client.get("/personal-board/tasks", headers=_headers(read_only))
    assert listed.status_code == 200, listed.text


def test_owner_boundary_tickets_not_visible_or_writable_to_other_token(clean, monkeypatch):
    _enable(monkeypatch)
    user_a = _make_user()
    user_b = _make_user()
    token_a = _make_token(user_a, scopes=["knowledge", "knowledge:write"])
    token_b = _make_token(user_b, scopes=["knowledge", "knowledge:write"])
    task = _create_ticket(token_a, "A의 비밀 티켓")

    listed_b = client.get("/personal-board/tasks", headers=_headers(token_b))
    assert all(t["task_id"] != task["task_id"] for t in listed_b.json())

    patched_b = client.patch(
        f"/personal-board/tasks/{task['task_id']}",
        json={"status": "done"},
        headers=_headers(token_b),
    )
    assert patched_b.status_code == 404, patched_b.text

    comments_b = client.get(
        f"/personal-board/tasks/{task['task_id']}/comments", headers=_headers(token_b)
    )
    assert comments_b.status_code == 404, comments_b.text


def test_token_has_no_ticket_delete_path(clean, monkeypatch):
    # P3.3c: the agent surface only gets reversible transitions; DELETE stays
    # session-only. A token DELETE must never remove the row. Demo auth mode
    # would resolve a single-user DB to that same user (the bearer is ignored,
    # not honored), so assert under the production `session` mode where the
    # non-token fallback demands a session cookie.
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    task = _create_ticket(token)

    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    deleted = client.delete(f"/personal-board/tasks/{task['task_id']}", headers=_headers(token))
    assert deleted.status_code == 401, deleted.text

    listed = client.get(
        "/personal-board/tasks",
        params={"id_prefix": task["task_id"].replace("-", "")},
        headers=_headers(token),
    )
    assert [t["task_id"] for t in listed.json()] == [task["task_id"]]


def test_ticket_write_rate_limit_returns_429(clean, monkeypatch):
    _enable(monkeypatch)
    from orthus.api import knowledge_token as kt

    monkeypatch.setattr(kt._dashboard_write_rate_limiter, "_calls", 2)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    _create_ticket(token, "1")
    _create_ticket(token, "2")
    third = client.post(
        "/personal-board/tasks",
        json={"title": "3", "scheduled_date": "2026-07-10"},
        headers=_headers(token),
    )
    assert third.status_code == 429, third.text


def test_token_ticket_projects_and_buckets_lists(clean, monkeypatch):
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge"])

    projects = client.get("/personal-board/projects", headers=_headers(token))
    assert projects.status_code == 200, projects.text

    buckets = client.get("/personal-board/backlog-buckets", headers=_headers(token))
    assert buckets.status_code == 200, buckets.text
    keys = [b["key"] for b in buckets.json()]
    assert "next_week" in keys and "someday" in keys


def test_company_channel_ticket_derives_company_scope(clean, monkeypatch):
    # An assigned company project appears as a company channel on the owner's
    # board (synced by GET /personal-board/projects, no FE bootstrap needed) and
    # a ticket created on it becomes team-visible scope=company.
    _enable(monkeypatch)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    from orthus.tables import dashboard_projects, project_assignments, team_members

    project_id = uuid.uuid4()
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=COMPANY_NODE, name="Nova"
            )
        )
        s.execute(
            insert(team_members).values(
                member_id=member_id, node_id=COMPANY_NODE, user_id=user_id, name="담당자"
            )
        )
        s.execute(
            insert(project_assignments).values(
                assignment_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                project_id=project_id,
                member_id=member_id,
            )
        )
        s.commit()

    channels = client.get("/personal-board/projects", headers=_headers(token))
    assert channels.status_code == 200, channels.text
    company = [c for c in channels.json() if c["kind"] == "company"]
    assert len(company) == 1 and company[0]["name"] == "Nova"

    task = _create_ticket(token, "회사 채널 티켓", project_id=company[0]["project_id"])
    assert task["scope"] == "company"
    assert task["company_project_id"] == str(project_id)


def test_ticket_writes_publish_owner_scoped_board_ping(clean, monkeypatch):
    # Realtime slice: create/update/comment publish a content-free change ping
    # carrying only the owner_id (the /dashboard/stream generator drops it for
    # any other subscriber). No ticket content may ride on the event.
    _enable(monkeypatch)
    events: list[dict] = []
    from orthus import realtime

    monkeypatch.setattr(realtime, "publish", events.append)
    user_id = _make_user()
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    task = _create_ticket(token, "핑 검증")
    client.patch(
        f"/personal-board/tasks/{task['task_id']}",
        json={"status": "done"},
        headers=_headers(token),
    )
    client.post(
        f"/personal-board/tasks/{task['task_id']}/comments",
        json={"body": "메모"},
        headers=_headers(token),
    )

    assert len(events) == 3
    for event in events:
        assert event["type"] == "personal_board_changed"
        assert event["owner_id"] == str(user_id)
        assert "title" not in event and "body" not in event


def test_knowledge_token_reads_project_board_tasks(clean, monkeypatch):
    # `orthus ticket project-list`: a read-only knowledge token can view the
    # project-level company-ticket aggregation (company internal knowledge,
    # same rationale as the team calendar). Personal-channel rows never appear.
    _enable(monkeypatch)
    user_id = _make_user()
    write = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    read_only = _make_token(user_id, scopes=["knowledge"])

    from orthus.tables import dashboard_projects, project_assignments, team_members

    project_id = uuid.uuid4()
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=COMPANY_NODE, name="Nova"
            )
        )
        s.execute(
            insert(team_members).values(
                member_id=member_id, node_id=COMPANY_NODE, user_id=user_id, name="담당자"
            )
        )
        s.execute(
            insert(project_assignments).values(
                assignment_id=uuid.uuid4(),
                node_id=COMPANY_NODE,
                project_id=project_id,
                member_id=member_id,
            )
        )
        s.commit()

    channels = client.get("/personal-board/projects", headers=_headers(write))
    company = [c for c in channels.json() if c["kind"] == "company"]
    company_ticket = _create_ticket(write, "회사 티켓", project_id=company[0]["project_id"])
    _create_ticket(write, "개인 티켓")  # 채널 없음 → scope=personal, 집계 제외

    listed = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_headers(read_only)
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [r["task_id"] for r in rows] == [company_ticket["task_id"]]
    assert rows[0]["owner_user_id"] == str(user_id)


def test_knowledge_token_kanban_board_row_crud(clean, monkeypatch):
    # `orthus ticket board|board-add|board-move`: knowledge token reads the project
    # kanban database and a knowledge:write token adds/moves cards. Row DELETE and
    # database schema mutation stay session-only.
    _enable(monkeypatch)
    user_id = _make_user()
    write = _make_token(user_id, scopes=["knowledge", "knowledge:write"])
    read_only = _make_token(user_id, scopes=["knowledge"])

    from orthus.tables import dashboard_projects

    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=COMPANY_NODE, name="탕수육"
            )
        )
        s.commit()
    ensured = client.post(
        f"/dashboard/projects/{project_id}/board/ensure",
        headers={"X-User-Id": str(user_id)},
    )
    assert ensured.status_code == 200, ensured.text
    database_id = ensured.json()["database_id"]

    listed = client.get(f"/dashboard/projects/{project_id}/databases", headers=_headers(read_only))
    assert listed.status_code == 200, listed.text
    schema = listed.json()[0]
    title_prop = next(p for p in schema["properties"] if p["type"] == "title")
    status_prop = next(p for p in schema["properties"] if p["type"] == "status")
    first_option = status_prop["options"][0]
    second_option = status_prop["options"][1]

    created = client.post(
        f"/dashboard/databases/{database_id}/rows",
        json={"props": {title_prop["id"]: "경쟁사 분석", status_prop["id"]: first_option["id"]}},
        headers=_headers(write),
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["props"][status_prop["id"]] == first_option["id"]

    denied = client.post(
        f"/dashboard/databases/{database_id}/rows",
        json={"props": {title_prop["id"]: "x"}},
        headers=_headers(read_only),
    )
    assert denied.status_code == 403, denied.text

    moved = client.patch(
        f"/dashboard/databases/{database_id}/rows/{row['row_id']}",
        json={"props": {**row["props"], status_prop["id"]: second_option["id"]}},
        headers=_headers(write),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["props"][status_prop["id"]] == second_option["id"]
    assert moved.json()["props"][title_prop["id"]] == "경쟁사 분석"

    bundle = client.get(f"/dashboard/databases/{database_id}", headers=_headers(read_only))
    assert bundle.status_code == 200, bundle.text
    assert len(bundle.json()["rows"]) == 1

    # 토큰으로 행 삭제 불가 — 세션 전용(P3.3c 동형). session mode에서 bearer는 401.
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    deleted = client.delete(
        f"/dashboard/databases/{database_id}/rows/{row['row_id']}", headers=_headers(write)
    )
    assert deleted.status_code == 401, deleted.text


def test_kanban_row_writes_publish_board_ping(clean, monkeypatch):
    # 칸반 실시간: row create/update가 database_id만 담은 content-free 핑을 발행해
    # 열려 있는 보드가 재조회한다 (project_board_changed — owner 필터 없음, 회사 데이터).
    _enable(monkeypatch)
    events: list[dict] = []
    from orthus import realtime

    monkeypatch.setattr(realtime, "publish", events.append)
    user_id = _make_user()
    write = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    from orthus.tables import dashboard_projects

    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=COMPANY_NODE, name="아틀라스"
            )
        )
        s.commit()
    ensured = client.post(
        f"/dashboard/projects/{project_id}/board/ensure", headers={"X-User-Id": str(user_id)}
    )
    database_id = ensured.json()["database_id"]
    schema = ensured.json()
    title_prop = next(p for p in schema["properties"] if p["type"] == "title")

    created = client.post(
        f"/dashboard/databases/{database_id}/rows",
        json={"props": {title_prop["id"]: "핑 검증"}},
        headers=_headers(write),
    )
    assert created.status_code == 201, created.text
    client.patch(
        f"/dashboard/databases/{database_id}/rows/{created.json()['row_id']}",
        json={"props": created.json()["props"]},
        headers=_headers(write),
    )

    board_pings = [e for e in events if e.get("type") == "project_board_changed"]
    assert len(board_pings) == 2
    for event in board_pings:
        assert event["database_id"] == database_id
        assert "props" not in event and "title" not in event
