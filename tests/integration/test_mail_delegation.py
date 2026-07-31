"""Delegation slice 4 (mail -> agent_task) + slice 5 (completion surface).

DB-backed. The LLM is mocked: a stub chat model returns the delegation extraction
JSON (or a non-delegation verdict). No real claude/codex/hermes process runs — the
daemon side is simulated by completing the queued collector command directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select

import orthus.agentwork.delegation as delegation_mod
from orthus.agentwork import (
    execute_reviewed_agent_task,
    list_agent_work_items,
    resolve_agent_task_work_item_result,
)
from orthus.collector.commands import claim_command, complete_command, list_pending_commands
from orthus.db import session
from orthus.mail.delegation import build_delegation_candidate
from orthus.mail.ingest import ingest_mail
from orthus.schemas.canonical import MailIngestRequest
from orthus.settings import get_settings
from orthus.tables import (
    agent_work_items,
    auth_identities,
    collector_commands,
    collector_tokens,
    users,
)

COMPANY_NODE = "company"


# --- fixtures-style helpers ---------------------------------------------------


class _DelegationChat:
    """Returns a fixed delegation-extraction JSON for chat.complete()."""

    model_id = "stub:delegation"

    def __init__(self, response: str):
        self.response = response

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.response


def _delegation_json(
    *,
    is_delegation: bool = True,
    assignee: str = "dev@example.com",
    mode: str = "code",
    runner: str = "claude",
    instruction: str = "리포 테스트를 고쳐줘",
) -> str:
    import json

    return json.dumps(
        {
            "is_delegation": is_delegation,
            "assignee": assignee,
            "mode": mode,
            "runner": runner,
            "instruction": instruction,
        }
    )


def _enable_delegation(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "mail_agent_task_delegation_enabled", True)
    monkeypatch.setattr(settings, "agent_task_enabled", True)
    # Reply-draft hook stays off so the ingest flow only exercises delegation.
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", False)


def _make_user(email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        if email is not None:
            s.execute(
                insert(auth_identities).values(
                    identity_id=uuid.uuid4(),
                    user_id=uid,
                    provider="test",
                    provider_subject=uuid.uuid4().hex,
                    email=email,
                    email_verified=True,
                )
            )
        s.commit()
    return uid


def _enroll_daemon(user_id: uuid.UUID) -> None:
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=uuid.uuid4().hex,
                scopes=["commands"],
            )
        )
        s.commit()


def _inbound(**overrides) -> MailIngestRequest:
    base = dict(
        backend="nova",
        owner_addr="owner01@nova.example",
        external_id=f"ext-{uuid.uuid4()}",
        message_id=f"<{uuid.uuid4()}@example.com>",
        direction="inbound",
        from_addr="boss@partner.com",
        to_addr=["owner01@nova.example"],
        subject="작업 위임",
        body_text="dev@example.com 에게 리포 테스트를 고쳐달라고 전해줘.",
    )
    base.update(overrides)
    return MailIngestRequest(**base)


# --- slice 4: build_delegation_candidate --------------------------------------


def test_build_delegation_candidate_auto_dispatches(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    item = build_delegation_candidate(_inbound(), actor, get_settings())

    assert item is not None
    assert item.action_family == "agent_task"
    # ★ 메일發 위임은 **auto_execute하지 않는다**. "이건 위임"이라는 판단은 LLM이 신뢰할 수 없는
    # 외부 메일 본문 위에서 내린 것이고, 오탐 1건이면 팀원 Mac에서 샌드박스 없는 full-access
    # 에이전트가 돈다. 적대적 홀드아웃에서 최선 모델도 24개 함정 중 2개를 오탐했다
    # (experiments/fugu-ko/golden/t10_holdout2.json). 그래서 사람이 먼저 본다.
    assert item.state == "draft_for_review"
    assert item.payload["llm_inferred"] is True
    assert item.payload["assignee_user_id"] == str(assignee)
    assert item.payload["assignee_node_id"] == COMPANY_NODE
    assert item.payload["runner"] == "claude"
    assert item.payload["mode"] == "code"
    # mail origin is recorded for slice 5 completion surfacing.
    origin = item.payload["mail_origin"]
    assert origin["sender_hint"] == "boss@partner.com"
    assert origin["source_canonical_id"].startswith("mail:nova:")
    # 승인 전에는 daemon으로 아무것도 안 나간다 — 이게 이 슬라이스의 핵심이다.
    assert list_pending_commands(assignee).count == 0

    # 리뷰어가 승인하면 그때 dispatch된다.
    dispatched = _approve_and_dispatch(item, actor)
    pending = list_pending_commands(assignee)
    assert pending.count == 1
    command = pending.items[0]
    assert command.kind == "agent_task"
    assert command.payload["work_item_id"] == str(dispatched.work_id)
    assert command.payload["instruction"] == "리포 테스트를 고쳐줘"


def _approve_and_dispatch(item, reviewer):
    """리뷰어 승인 → dispatch. `POST /agent-work/{id}/decision`이 하는 일 그대로.

    메일發 위임은 **auto_execute되지 않는다**(agentwork/state.py): "이 텍스트가 위임인가"는
    LLM이 신뢰할 수 없는 외부 메일 본문 위에서 내린 판단이고, 오탐 1건이면 팀원 Mac에서
    샌드박스 없는 에이전트가 돈다. 그래서 dispatch는 사람이 승인한 뒤에 일어난다.
    """
    from orthus.agentwork import decide_agent_work_item

    result = decide_agent_work_item(reviewer, item.work_id, "approve")
    return execute_reviewed_agent_task(reviewer, result.item, reviewer_role="owner")


def test_build_delegation_candidate_flag_off_noop(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_agent_task_delegation_enabled", False)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # Even a delegation-shaped extraction must not run when the flag is off.
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    item = build_delegation_candidate(_inbound(), actor, get_settings())

    assert item is None
    with session() as s:
        work_count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert work_count == 0
    assert cmd_count == 0


def test_build_delegation_candidate_not_company_noop(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    monkeypatch.setattr(get_settings(), "node_kind", "personal")
    actor = _make_user()
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    assert build_delegation_candidate(_inbound(), actor, get_settings()) is None


def test_build_delegation_candidate_non_delegation_returns_none(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    monkeypatch.setattr(
        delegation_mod,
        "get_chat_model_for",
        lambda _task: _DelegationChat(_delegation_json(is_delegation=False)),
    )

    item = build_delegation_candidate(_inbound(), actor, get_settings())

    assert item is None
    with session() as s:
        work_count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
    assert work_count == 0


def test_build_delegation_candidate_unenrolled_requests_more_data(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    # assignee resolves but has NO collector daemon enrolled.
    _make_user(email="dev@example.com")
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    item = build_delegation_candidate(_inbound(), actor, get_settings())

    assert item is not None
    assert item.state == "request_more_data"
    assert item.payload.get("auto_execution") is None
    with session() as s:
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert cmd_count == 0


def test_extraction_failure_does_not_delegate(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # Non-JSON completion -> _parse_extraction returns None -> no delegation.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat("이건 위임이 아닙니다")
    )

    assert build_delegation_candidate(_inbound(), actor, get_settings()) is None


# --- slice 4: ingest hook -----------------------------------------------------


def _register_mailbox_owner(email: str = "owner01@nova.example") -> uuid.UUID:
    """Bind a mailbox address to a real owner so the individual-mailbox boundary
    does not refuse ingest under the service user."""
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Mailbox Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="test",
                provider_subject=f"sub-{uid}",
                email=email,
                email_verified=True,
            )
        )
        s.commit()
    return uid


def test_ingest_inbound_creates_delegation_work_item(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    _register_mailbox_owner()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    result = ingest_mail(_inbound(message_id="<deleg-1@example.com>"), get_settings())

    assert result.ingested is True
    with session() as s:
        row = s.execute(
            select(agent_work_items).where(agent_work_items.c.action_family == "agent_task")
        ).first()
    assert row is not None
    # 메일 ingest가 만든 위임 후보는 **검토 대기**다(auto_execute 아님).
    # ingest만으로는 daemon에 아무것도 안 나간다 — 사람이 승인해야 dispatch된다.
    assert row._mapping["state"] == "draft_for_review"
    assert list_pending_commands(assignee).count == 0


def test_ingest_with_delegation_flag_off_creates_no_item(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_agent_task_delegation_enabled", False)
    _register_mailbox_owner()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )

    result = ingest_mail(_inbound(message_id="<deleg-2@example.com>"), get_settings())

    assert result.ingested is True
    with session() as s:
        count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.action_family == "agent_task")
        ).scalar_one()
    assert count == 0


# --- slice 5: completion surface ----------------------------------------------


def _complete_done(work_item, *, assignee, stdout: str = "작업 완료"):
    command_id = uuid.UUID(work_item.payload["auto_execution"]["command_id"])
    claim_command(assignee, command_id)
    command = complete_command(
        assignee,
        command_id,
        status="done",
        result_payload={"exit_code": 0, "stdout": stdout, "runner": "claude"},
    )
    return resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )


def test_complete_mail_origin_creates_completion_email_draft(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )
    item = build_delegation_candidate(_inbound(), actor, get_settings())
    assert item is not None and item.state == "draft_for_review"
    item = _approve_and_dispatch(item, actor)  # 승인해야 dispatch된다

    resolved = _complete_done(item, assignee=assignee)
    assert resolved is not None and resolved.state == "resolved"

    # a completion email_send draft_for_review work item was created for the sender.
    completion = next(
        i
        for i in list_agent_work_items(actor, source_kind="agent_task")
        if i.action_family == "email_send"
    )
    assert completion.state == "draft_for_review"
    assert completion.source_ref_id == f"agent-task-complete-{item.work_id}"
    draft = completion.payload["email_draft"]
    assert draft["recipient_hint"] == "boss@partner.com"
    assert "작업 완료" in draft["body_template"]
    assert completion.payload["completion_origin"]["agent_task_work_id"] == str(item.work_id)


def test_complete_operator_origin_creates_no_completion_email(clean, monkeypatch):
    # An operator direct enqueue (no mail_origin) must not create a completion email.
    from orthus.agentwork.service import create_agent_task_work_item

    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev@example.com",
        mode="knowledge",
        runner="codex",
        instruction="조사해줘",
    )
    assert item is not None and item.state == "auto_execute"
    assert "mail_origin" not in item.payload

    resolved = _complete_done(item, assignee=assignee)
    assert resolved is not None and resolved.state == "resolved"

    emails = [
        i
        for i in list_agent_work_items(actor, source_kind="agent_task")
        if i.action_family == "email_send"
    ]
    assert emails == []


def test_complete_failed_creates_no_completion_email(clean, monkeypatch):
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)
    # delegation_extract now resolves its model through the assignment layer
    # (EXAONE — safety pick, see orchestration.ASSIGNMENTS). Patch that resolver so the
    # test still injects its canned extraction instead of calling a real model.
    monkeypatch.setattr(
        delegation_mod, "get_chat_model_for", lambda _task: _DelegationChat(_delegation_json())
    )
    item = build_delegation_candidate(_inbound(), actor, get_settings())
    assert item is not None and item.state == "draft_for_review"
    item = _approve_and_dispatch(item, actor)  # 승인해야 dispatch된다

    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])
    claim_command(assignee, command_id)
    command = complete_command(
        assignee, command_id, status="failed", result_payload={"reason": "runner not installed"}
    )
    resolved = resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )

    assert resolved is not None and resolved.state == "request_more_data"
    emails = [
        i
        for i in list_agent_work_items(actor, source_kind="agent_task")
        if i.action_family == "email_send"
    ]
    assert emails == []


def test_meeting_minutes_mail_never_reaches_the_llm(clean, monkeypatch):
    """★ R2: 회의록 메일은 결정론 프리필터가 잘라 **LLM에 도달하지 않는다**.

    이 메일(`h-09`)이 정확히 EXAONE이 오탐한 것이다 — 액션아이템의 "최수민"을 assignee로 뽑았고,
    플래그가 켜져 있었다면 최수민의 Mac에서 샌드박스 없는 에이전트가 돌았을 것이다.
    이제는 LLM을 부르지도 않는다.
    """
    _enable_delegation(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)

    called: list[str] = []

    def _tripwire(_task):
        called.append(_task)
        return _DelegationChat(_delegation_json())

    monkeypatch.setattr(delegation_mod, "get_chat_model_for", _tripwire)

    minutes = _inbound()
    minutes = minutes.model_copy(
        update={
            "subject": "[회의록] 주간 스프린트",
            "body_text": (
                "[회의록] 2026-07-10 주간 스프린트\n\n"
                "액션 아이템\n"
                "1. 최수민 — 테스트 커버리지 70%까지 보강\n"
                "2. 오세훈 — 마이그레이션 스크립트 초안\n"
            ),
        }
    )

    item = build_delegation_candidate(minutes, actor, get_settings())

    assert item is None, "회의록은 위임 후보가 되면 안 된다"
    assert called == [], "프리필터가 잘랐어야 하는데 LLM이 호출됐다"
    assert list_pending_commands(assignee).count == 0
