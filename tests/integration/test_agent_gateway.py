"""P10.4b gated commit-action bridge — security boundary regressions.

The new collector-token router (`/collector/gateway/actions[/…/decision]`) opens
the ONE token path to agent-work creation/decisions that the codebase otherwise
keeps session-only. These tests fix its guards: fail-closed flag, agent_task
scope, EXPLICIT owner/admin role (require_node_operator no-ops for tokens), and
strict owner-scope (a daemon can never act on another owner's rows).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.collector.rate_limit import reset_collector_owner_rate_limits
from orthus.db import session
from orthus.secrets import clear_memory_secrets
from orthus.settings import get_settings
from orthus.tables import (
    agent_work_items,
    auth_allowlist,
    auth_identities,
    collector_tokens,
    users,
)

client = TestClient(app)
COMPANY_NODE = "company"


def _enable(monkeypatch, *, actions=True, agent_task=True) -> None:
    reset_collector_owner_rate_limits()
    clear_memory_secrets()
    s = get_settings()
    monkeypatch.setattr(s, "collector_api_enabled", True)
    monkeypatch.setattr(s, "node_kind", "company")
    monkeypatch.setattr(s, "node_id", COMPANY_NODE)
    monkeypatch.setattr(s, "agent_gateway_actions_enabled", actions)
    monkeypatch.setattr(s, "agent_task_enabled", agent_task)
    monkeypatch.setattr(s, "secret_backend", "memory")


def _make_operator(role: str | None = "owner") -> uuid.UUID:
    uid = uuid.uuid4()
    email = f"{uid.hex[:8]}@example.com"
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="google",
                provider_subject=uuid.uuid4().hex,
                email=email,
                email_verified=True,
            )
        )
        if role is not None:
            s.execute(
                insert(auth_allowlist).values(
                    allowlist_id=uuid.uuid4(),
                    node_id=COMPANY_NODE,
                    email=email,
                    role=role,
                )
            )
        s.commit()
    return uid


def _token(user_id: uuid.UUID, *, scopes: list[str] | None = None) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=hash_collector_token(token),
                scopes=scopes if scopes is not None else ["agent_task", "commands"],
            )
        )
        s.commit()
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _delegate_body(instruction="검토해줘"):
    return {"kind": "delegate", "assignee": "", "instruction": instruction, "mode": "knowledge"}


# --- flag / scope / role gates ------------------------------------------------


def test_flag_off_is_404(clean, monkeypatch):
    _enable(monkeypatch, actions=False)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 404
    # 신규 kind=email_draft slot도 같은 kill-switch 404를 상속한다 (명시 커버리지).
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "email_draft", "recipient": "a@b.co", "instruction": "x"},
        headers=_hdr(token),
    )
    assert r.status_code == 404


def test_missing_agent_task_scope_is_403(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner, scopes=["commands"])  # no agent_task scope
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 403


def test_non_operator_owner_is_403(clean, monkeypatch):
    """A token whose owner is NOT on the allowlist (role None) → 403, even though
    require_node_operator would silently no-op for a token."""
    _enable(monkeypatch)
    non_op = _make_operator(role=None)  # user + email, but no allowlist role
    token = _token(non_op)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 403


def test_bad_token_is_401(clean, monkeypatch):
    _enable(monkeypatch)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr("dct_bogus"))
    assert r.status_code == 401


# --- submit --------------------------------------------------------------------


def test_owner_can_submit_delegate(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["action_family"] == "agent_task"
    assert item["state"] in {"auto_execute", "request_more_data", "draft_for_review", "reject"}


def test_empty_instruction_is_422(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "delegate", "instruction": "  "},
        headers=_hdr(token),
    )
    assert r.status_code == 422


def test_admin_role_also_allowed(clean, monkeypatch):
    _enable(monkeypatch)
    admin = _make_operator("admin")
    token = _token(admin)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 200


def test_multi_identity_owner_resolves_operator_role(clean, monkeypatch):
    """Reviewer Finding 1: an owner with a SECOND non-allowlisted identity must
    still resolve to their operator role (not falsely 403)."""
    _enable(monkeypatch)
    owner = _make_operator("owner")  # first email is allowlisted
    with session() as s:
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=owner,
                provider="dev",
                provider_subject=uuid.uuid4().hex,
                email=f"{uuid.uuid4().hex[:8]}@notallowed.example",  # not on allowlist
                email_verified=True,
            )
        )
        s.commit()
    token = _token(owner)
    r = client.post("/collector/gateway/actions", json=_delegate_body(), headers=_hdr(token))
    assert r.status_code == 200, r.text  # resolved via the allowlisted email


def test_command_kind_is_wired(clean, monkeypatch):
    """PO#1: the kind=command branch is reachable + owner-gated (routes through
    create_assistant_command_work_item → P3 gate). Exact email classification is
    covered by existing P3 tests; here we prove the endpoint wiring + no crash."""
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "command", "text": "bob@example.com 에게 회의 요약 메일 초안 작성해줘"},
        headers=_hdr(token),
    )
    # command intake either creates an item (200) or declines a non-command (422);
    # never 500/403 for an operator.
    assert r.status_code in {200, 422}, r.text


def test_command_kind_non_operator_403_before_classification(clean, monkeypatch):
    _enable(monkeypatch)
    non_op = _make_operator(role=None)
    token = _token(non_op)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "command", "text": "email bob a summary"},
        headers=_hdr(token),
    )
    assert r.status_code == 403  # role checked before any intake


def test_empty_command_text_is_422(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "command", "text": "   "},
        headers=_hdr(token),
    )
    assert r.status_code == 422


def test_delegate_to_another_user(clean, monkeypatch):
    """PO#4: delegation to a non-empty assignee routes through the existing
    delegation substrate (create_agent_task_work_item resolves the assignee)."""
    _enable(monkeypatch)
    owner = _make_operator("owner")
    other = _make_operator("admin")  # a real teammate (has email)
    with session() as s:
        other_email = (
            s.execute(auth_identities.select().where(auth_identities.c.user_id == other))
            .first()
            .email
        )
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "delegate", "assignee": other_email, "instruction": "백로그 추가"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["action_family"] == "agent_task"


# --- decision owner-scope ------------------------------------------------------


def _owner_bound_review_item(owner_id: uuid.UUID) -> uuid.UUID:
    """Insert an OWNER-BOUND (owner_id set) draft_for_review item — the kind the
    owner-scope predicate protects (vs company-shared owner_id NULL items, which
    any operator may decide, matching the session route)."""
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=COMPANY_NODE,
                node_kind="company",
                owner_id=owner_id,
                source_kind="test",
                source_ref_id=uuid.uuid4().hex,
                action_family="data_request",
                title="owner-bound review",
                state="draft_for_review",
            )
        )
        s.commit()
    return work_id


def test_reviews_endpoint_owner_scoped_and_agent_task_scoped(clean, monkeypatch):
    """Reviewer Finding 2: the daemon reads pending reviews from this endpoint
    (agent_task scope, NOT the knowledge-scoped /agent-work). Owner-scoped."""
    _enable(monkeypatch)
    owner_b = _make_operator("owner")
    _owner_bound_review_item(owner_b)  # B's draft_for_review
    # B (agent_task token) sees exactly their own draft
    tok_b = _token(owner_b)
    r = client.get("/collector/gateway/reviews", headers=_hdr(tok_b))
    assert r.status_code == 200, r.text
    states = {it["state"] for it in r.json()}
    assert states == {"draft_for_review"}

    # A does not see B's owner-bound review
    owner_a = _make_operator("owner")
    r_a = client.get("/collector/gateway/reviews", headers=_hdr(_token(owner_a)))
    assert r_a.status_code == 200 and r_a.json() == []

    # a token without agent_task scope → 403
    r_scope = client.get(
        "/collector/gateway/reviews", headers=_hdr(_token(owner_b, scopes=["commands"]))
    )
    assert r_scope.status_code == 403


def test_decision_cannot_touch_another_owners_bound_item(clean, monkeypatch):
    """Owner A's token must not see/decide owner B's OWNER-BOUND item — the P8
    owner-scope boundary the endpoint inherits via _agent_work_owner_predicate."""
    _enable(monkeypatch)
    owner_b = _make_operator("owner")
    work_id = _owner_bound_review_item(owner_b)

    owner_a = _make_operator("owner")
    r_a = client.post(
        f"/collector/gateway/actions/{work_id}/decision",
        json={"decision": "dismiss"},
        headers=_hdr(_token(owner_a)),
    )
    assert r_a.status_code == 404  # A cannot even see B's owner-bound item

    # B's own token can decide it.
    r_b = client.post(
        f"/collector/gateway/actions/{work_id}/decision",
        json={"decision": "dismiss"},
        headers=_hdr(_token(owner_b)),
    )
    assert r_b.status_code == 200, r_b.text
    assert r_b.json()["item"]["state"] == "dismissed"


# --- P10 구조화 kind=email_draft slot (spec §13 Finding 3) ----------------------


def _forbid_nl_classifiers(monkeypatch):
    """구조화 slot 경로는 NL 분류기(command detect / recipient 정규식)를 호출하면
    안 된다 — 호출되는 순간 테스트가 실패하도록 둘 다 지뢰로 바꾼다."""
    import orthus.agentwork.service as service

    def _boom(*_args, **_kwargs):
        raise AssertionError("NL classifier must not run for the structured email_draft slot")

    monkeypatch.setattr(service, "detect_assistant_command_action", _boom)
    monkeypatch.setattr(service, "_email_recipient_hint", _boom)


def test_email_draft_slot_creates_draft_without_classifier(clean, monkeypatch):
    """recipient가 있는 구조화 slot → 분류기 미호출로 email_send draft_for_review +
    typed payload.email_draft. outcome은 기존 결정론 P3 gate 그대로다."""
    _enable(monkeypatch)
    _forbid_nl_classifiers(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={
            "kind": "email_draft",
            "recipient": "bob@example.com",
            "instruction": "회의 요약 공유",
        },
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["action_family"] == "email_send"
    assert item["state"] == "draft_for_review"
    payload = item["payload"]
    # recipient는 slot에서 그대로 온다 (정규식 추출 없음, unmasked 운영 데이터).
    assert payload["recipient_hint"] == "bob@example.com"
    draft = payload["email_draft"]
    assert draft["recipient_hint"] == "bob@example.com"
    assert draft["body_template"]
    assert draft["status"] == "draft"
    assert draft["used_for_outcome"] is False
    # P3.5 exact email auto-send policy bucket 불변 (source_kind=assistant_command).
    assert item["source_kind"] == "assistant_command"
    assert sorted(item["reason_codes"]) == [
        "email_draft_first",
        "policy_memory_observation_gate_required",
    ]


def test_email_draft_slot_without_recipient_requests_more_data(clean, monkeypatch):
    """recipient 부재 → P3.4c 계약 그대로 request_more_data + payload.required_data.
    slot 경로는 instruction 본문에서 recipient를 재추출하지 않는다(에이전트 책임)."""
    _enable(monkeypatch)
    _forbid_nl_classifiers(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "email_draft", "instruction": "김대표에게 회의 요약 메일 보내줘"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["action_family"] == "email_send"
    assert item["state"] == "request_more_data"
    payload = item["payload"]
    assert payload["required_data"] == ["recipient name, address, or thread reference"]
    assert "recipient_hint" not in payload
    assert "email_draft" not in payload


def test_email_draft_slot_empty_instruction_is_422(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "email_draft", "recipient": "bob@example.com", "instruction": "  "},
        headers=_hdr(token),
    )
    assert r.status_code == 422


def test_email_draft_slot_non_operator_is_403(clean, monkeypatch):
    _enable(monkeypatch)
    non_op = _make_operator(role=None)
    token = _token(non_op)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "email_draft", "recipient": "bob@example.com", "instruction": "메일"},
        headers=_hdr(token),
    )
    assert r.status_code == 403


def test_email_draft_slot_extras_cannot_smuggle_send_metadata(clean, monkeypatch):
    """P3.4d allowlist: request body의 extra key(smtp/send/provider·email_draft
    통짜 주입)는 서버가 무시하고, payload.email_draft는 서버가 직접 만든 typed
    EmailDraftPayload(extra=forbid)만 담긴다."""
    from orthus.schemas.canonical import EmailDraftPayload

    _enable(monkeypatch)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={
            "kind": "email_draft",
            "recipient": "bob@example.com",
            "instruction": "회의 요약 공유",
            "smtp_host": "smtp.attacker.example",
            "email_draft": {"body_template": "주입", "smtp_host": "smtp.attacker.example"},
        },
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert "smtp_host" not in payload
    draft = payload["email_draft"]
    assert draft["body_template"] != "주입"
    assert "smtp_host" not in draft
    # 저장된 draft가 allowlist 스키마를 그대로 통과해야 한다 (extra 없음 증명).
    EmailDraftPayload.model_validate(draft)


def test_legacy_command_nl_path_still_uses_classifier(clean, monkeypatch):
    """하위 호환: 구버전 데몬의 kind=command NL 페이로드는 기존 분류 경로
    (detect_assistant_command_action + recipient 정규식)를 그대로 탄다."""
    import orthus.agentwork.service as service

    _enable(monkeypatch)
    calls: list[str] = []
    orig = service.detect_assistant_command_action

    def _spy(question):
        calls.append(question)
        return orig(question)

    monkeypatch.setattr(service, "detect_assistant_command_action", _spy)
    owner = _make_operator("owner")
    token = _token(owner)
    r = client.post(
        "/collector/gateway/actions",
        json={"kind": "command", "text": "bob@example.com 에게 회의 요약 메일 초안 작성해줘"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    assert calls, "legacy NL path must consult the deterministic classifier"
    item = r.json()
    assert item["action_family"] == "email_send"
    assert item["payload"]["recipient_hint"] == "bob@example.com"
