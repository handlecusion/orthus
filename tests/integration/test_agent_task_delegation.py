"""Delegated agent_task loop: company AgentWork -> collector command -> result.

These DB-backed tests exercise the deterministic policy gate, the work-item
dispatch (which enqueues a collector command without running any agent), and the
/complete write-back. No real claude/codex/hermes process is ever spawned: the
daemon side is simulated by completing the queued collector command directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert, select

from orthus.agentwork import (
    apply_policy,
    create_agent_task_work_item,
    get_agent_work_item,
    resolve_agent_task_work_item_result,
)
from orthus.agentwork.chat import create_session as create_chat_session
from orthus.agentwork.delegation import resolve_assignee, resolve_enrolled_daemon
from orthus.agentwork.service import execute_auto_agent_task
from orthus.collector.commands import complete_command, list_pending_commands
from orthus.db import session
from orthus.personal_board import create_delegation_task
from orthus.schemas.canonical import AgentWorkCandidate
from orthus.settings import get_settings
from orthus.tables import (
    auth_identities,
    collector_commands,
    collector_tokens,
    personal_board_backlog_buckets,
    personal_board_tasks,
    users,
)

COMPANY_NODE = "company"


def _make_user(email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        if email is not None:
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
        s.commit()
    return uid


def _enroll_daemon(
    user_id: uuid.UUID,
    *,
    scopes: list[str] | None = None,
    device_id: str | None = None,
) -> uuid.UUID:
    token_id = uuid.uuid4()
    values = dict(
        token_id=token_id,
        user_id=user_id,
        node_id=COMPANY_NODE,
        name="daemon",
        token_hash=uuid.uuid4().hex,
    )
    if scopes is not None:
        values["scopes"] = scopes
    if device_id is not None:
        values["device_id"] = device_id
    with session() as s:
        s.execute(insert(collector_tokens).values(**values))
        s.commit()
    return token_id


def _enable_agent_task() -> None:
    settings = get_settings()
    settings.agent_task_enabled = True


# --- policy gate (deterministic) ----------------------------------------------


def test_policy_gate_agent_task_outcomes(clean):
    base = {
        "node_kind": "company",
        "agent_task_enabled": True,
        "actor_role": "owner",
        "assignee_user_id": str(uuid.uuid4()),
        "assignee_node_id": "company",
        "mode": "code",
        "runner": "claude",
        "instruction": "fix the bug",
    }
    auto = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="task-auto",
        action_family="agent_task",
        title="auto",
        payload=dict(base),
    )
    flag_off = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="task-off",
        action_family="agent_task",
        title="off",
        payload={**base, "agent_task_enabled": False},
    )
    not_company = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="task-personal",
        action_family="agent_task",
        title="personal",
        payload={**base, "node_kind": "personal"},
    )
    no_daemon = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="task-no-daemon",
        action_family="agent_task",
        title="no daemon",
        payload={**base, "assignee_node_id": None},
    )
    not_operator = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id="task-non-op",
        action_family="agent_task",
        title="non op",
        payload={**base, "actor_role": "member"},
    )

    assert apply_policy(auto).outcome == "auto_execute"
    assert apply_policy(flag_off).outcome == "reject"
    assert apply_policy(not_company).outcome == "reject"
    assert apply_policy(no_daemon).outcome == "request_more_data"
    assert apply_policy(not_operator).outcome == "reject"
    # determinism
    assert apply_policy(auto).outcome == "auto_execute"


# --- resolution helpers -------------------------------------------------------


def test_resolve_assignee_by_email_and_id(clean):
    uid = _make_user(email="Member@example.com")
    assert resolve_assignee("member@example.com") == uid
    assert resolve_assignee(str(uid)) == uid
    assert resolve_assignee("missing@example.com") is None
    assert resolve_assignee("") is None


def test_resolve_enrolled_daemon(clean):
    uid = _make_user()
    assert resolve_enrolled_daemon(uid) is None
    _enroll_daemon(uid, scopes=["commands"])
    # Deviceless token -> (node, "") tuple, no device requested.
    assert resolve_enrolled_daemon(uid) == (COMPANY_NODE, "")


def test_resolve_enrolled_daemon_device_targeting(clean):
    uid = _make_user()
    _enroll_daemon(uid, scopes=["commands"], device_id="device-a")
    _enroll_daemon(uid, scopes=["commands"], device_id="device-b")

    # No device requested -> most-recently created run-scope token wins (device-b).
    assert resolve_enrolled_daemon(uid) == (COMPANY_NODE, "device-b")
    # A specific owned device resolves to that device.
    assert resolve_enrolled_daemon(uid, device_id="device-a") == (COMPANY_NODE, "device-a")
    assert resolve_enrolled_daemon(uid, device_id="device-b") == (COMPANY_NODE, "device-b")
    # A device the assignee does not own -> None (reject).
    assert resolve_enrolled_daemon(uid, device_id="device-zzz") is None


# --- create + dispatch --------------------------------------------------------


def test_create_agent_task_auto_dispatches_command(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee, scopes=["commands"])

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev@example.com",
        mode="code",
        runner="claude",
        instruction="fix the failing test",
        cwd="/repo",
    )

    assert item is not None
    assert item.action_family == "agent_task"
    assert item.state == "auto_execute"
    assert item.payload["assignee_user_id"] == str(assignee)
    assert item.payload["assignee_node_id"] == COMPANY_NODE
    dispatched_command_id = item.payload["auto_execution"]["command_id"]
    assert item.payload["auto_execution"]["status"] == "dispatched"

    pending = list_pending_commands(assignee)
    assert pending.count == 1
    command = pending.items[0]
    assert str(command.command_id) == dispatched_command_id
    assert command.kind == "agent_task"
    assert command.payload["work_item_id"] == str(item.work_id)
    assert command.payload["mode"] == "code"
    assert command.payload["runner"] == "claude"
    assert command.payload["instruction"] == "fix the failing test"
    assert command.payload["cwd"] == "/repo"


def test_create_agent_task_pins_requested_device(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev-pinned@example.com")
    _enroll_daemon(assignee, scopes=["commands"], device_id="device-a")
    _enroll_daemon(assignee, scopes=["commands"], device_id="device-b")

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev-pinned@example.com",
        mode="knowledge",
        runner="codex",
        instruction="run on device a",
        device_id="device-a",
    )

    assert item is not None and item.state == "auto_execute"
    assert item.payload["assignee_device_id"] == "device-a"
    # The dispatched collector command is pinned to that device.
    with session() as s:
        rows = s.execute(
            select(collector_commands).where(
                collector_commands.c.user_id == assignee,
                collector_commands.c.kind == "agent_task",
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].device_id == "device-a"


def test_create_agent_task_without_device_leaves_command_unpinned(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev-unpinned@example.com")
    _enroll_daemon(assignee, scopes=["commands"], device_id="device-a")

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev-unpinned@example.com",
        mode="knowledge",
        runner="codex",
        instruction="run anywhere",
    )

    assert item is not None and item.state == "auto_execute"
    assert "assignee_device_id" not in item.payload
    with session() as s:
        rows = s.execute(
            select(collector_commands).where(
                collector_commands.c.user_id == assignee,
                collector_commands.c.kind == "agent_task",
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].device_id is None


def test_create_agent_task_unowned_device_creates_no_row(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev-nodevice@example.com")
    _enroll_daemon(assignee, scopes=["commands"], device_id="device-a")

    # A device the assignee does not own is a hard stop (no queue row).
    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev-nodevice@example.com",
        mode="knowledge",
        runner="codex",
        instruction="run on a device they lack",
        device_id="device-zzz",
    )

    assert item is None
    with session() as s:
        rows = s.execute(
            select(collector_commands).where(collector_commands.c.user_id == assignee)
        ).all()
    assert rows == []


def test_pinned_command_is_device_isolated(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev-isolated@example.com")
    _enroll_daemon(assignee, scopes=["commands"], device_id="device-a")

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev-isolated@example.com",
        mode="knowledge",
        runner="codex",
        instruction="device-a only",
        device_id="device-a",
    )
    assert item is not None and item.state == "auto_execute"

    # device-a sees it; device-b does not; an unpinned/deviceless poll does not.
    assert list_pending_commands(assignee, device_id="device-a").count == 1
    assert list_pending_commands(assignee, device_id="device-b").count == 0
    assert list_pending_commands(assignee).count == 0


def test_create_agent_task_unenrolled_assignee_requests_more_data(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="nodaemon@example.com")  # no collector token

    item = create_agent_task_work_item(
        actor,
        actor_role="admin",
        assignee="nodaemon@example.com",
        mode="knowledge",
        runner="codex",
        instruction="summarize the repo",
    )

    assert item is not None
    assert item.state == "request_more_data"
    assert item.payload.get("auto_execution") is None
    # no collector command was enqueued
    with session() as s:
        rows = s.execute(
            select(collector_commands).where(collector_commands.c.user_id == assignee)
        ).all()
    assert rows == []


def test_create_agent_task_non_operator_creates_no_row(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev2@example.com")
    _enroll_daemon(assignee, scopes=["commands"])

    item = create_agent_task_work_item(
        actor,
        actor_role="member",
        assignee="dev2@example.com",
        mode="code",
        runner="claude",
        instruction="do thing",
        cwd="/repo",
    )

    assert item is None
    with session() as s:
        work_rows = s.execute(
            select(collector_commands).where(collector_commands.c.user_id == assignee)
        ).all()
    assert work_rows == []


# --- delegation board task (dispatch -> assignee backlog, PR-N2) ---------------


def _enable_owner_scope() -> None:
    # create_delegation_task는 회사 노드 + owner-scope off면 fail-closed로 거부한다
    # (그 상태의 개인 보드 read 표면이 404라 유령 row 방지 — operator 리뷰 반영).
    get_settings().owner_scope_enabled = True


def test_create_delegation_task_inserts_backlog_row(clean):
    """Helper unit: bucket ensure + insert with source_kind='delegation'."""
    _enable_owner_scope()
    uid = _make_user()

    task_id = create_delegation_task(
        uid,
        COMPANY_NODE,
        title="위임: 보고서 작성",
        note="full instruction\n\nagent work: x",
        source_label="위임 — 홍길동",
    )

    with session() as s:
        row = s.execute(
            select(personal_board_tasks).where(personal_board_tasks.c.task_id == task_id)
        ).one()
        bucket_key = s.execute(
            select(personal_board_backlog_buckets.c.key).where(
                personal_board_backlog_buckets.c.bucket_id == row.backlog_bucket_id
            )
        ).scalar_one()
    assert row.user_id == uid
    assert row.source_kind == "delegation"
    assert row.status == "open"
    assert row.scheduled_date is None  # backlog placement, not calendar
    assert bucket_key == "next_week"
    assert row.title == "위임: 보고서 작성"
    assert row.note == "full instruction\n\nagent work: x"
    assert row.source_label == "위임 — 홍길동"

    # 재호출은 새 row를 추가한다(멱등은 work-item payload marker가 담당).
    second = create_delegation_task(uid, COMPANY_NODE, title="위임: 2")
    assert second != task_id


def test_dispatch_creates_assignee_board_task_and_is_idempotent(clean):
    _enable_agent_task()
    _enable_owner_scope()
    actor = _make_user()
    assignee = _make_user(email="board-task@example.com")
    _enroll_daemon(assignee, scopes=["commands"])

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="board-task@example.com",
        mode="code",
        runner="claude",
        instruction="write the weekly report",
        cwd="/repo",
    )

    assert item is not None and item.state == "auto_execute"
    marker = item.payload.get("delegation_board_task")
    assert isinstance(marker, dict) and marker.get("task_id")

    with session() as s:
        rows = s.execute(
            select(personal_board_tasks).where(
                personal_board_tasks.c.user_id == assignee,
                personal_board_tasks.c.source_kind == "delegation",
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert str(row.task_id) == marker["task_id"]
    assert row.title.startswith("위임: ")
    assert "write the weekly report" in row.note
    assert f"agent work: {item.work_id}" in row.note
    assert row.source_label == "위임 — U"  # delegator display_name

    # 두 번째 execute 호출은 payload marker로 board task를 다시 만들지 않는다.
    again = execute_auto_agent_task(actor, item)
    assert again.payload["delegation_board_task"] == marker
    with session() as s:
        count = s.execute(
            select(personal_board_tasks).where(
                personal_board_tasks.c.user_id == assignee,
                personal_board_tasks.c.source_kind == "delegation",
            )
        ).all()
    assert len(count) == 1


def test_owner_scope_off_skips_board_task_but_dispatch_succeeds(clean):
    """회사 노드 + owner-scope off: 보드 row 미생성(fail-closed), dispatch는 정상."""
    _enable_agent_task()
    assert get_settings().owner_scope_enabled is False
    actor = _make_user()
    assignee = _make_user(email="scope-off@example.com")
    _enroll_daemon(assignee, scopes=["commands"])

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="scope-off@example.com",
        mode="code",
        runner="claude",
        instruction="scope off case",
        cwd="/repo",
    )

    assert item is not None and item.state == "auto_execute"
    assert "delegation_board_task" not in item.payload
    with session() as s:
        rows = s.execute(
            select(personal_board_tasks).where(personal_board_tasks.c.source_kind == "delegation")
        ).all()
    assert rows == []


def test_self_delegation_creates_no_board_task(clean):
    _enable_agent_task()
    _enable_owner_scope()
    actor = _make_user(email="self@example.com")
    _enroll_daemon(actor, scopes=["commands"])

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="self@example.com",
        mode="knowledge",
        runner="codex",
        instruction="summarize my inbox",
    )

    assert item is not None and item.state == "auto_execute"
    assert item.payload.get("auto_execution", {}).get("status") == "dispatched"
    assert "delegation_board_task" not in item.payload
    with session() as s:
        rows = s.execute(
            select(personal_board_tasks).where(
                personal_board_tasks.c.user_id == actor,
                personal_board_tasks.c.source_kind == "delegation",
            )
        ).all()
    assert rows == []


def test_board_task_failure_does_not_break_dispatch(clean, monkeypatch):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="board-fail@example.com")
    _enroll_daemon(assignee, scopes=["commands"])

    def boom(*args, **kwargs):
        raise RuntimeError("board is down")

    monkeypatch.setattr("orthus.agentwork.service.create_delegation_task", boom)

    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="board-fail@example.com",
        mode="code",
        runner="claude",
        instruction="fix the flaky test",
        cwd="/repo",
    )

    # dispatch(SoR)는 그대로 성공한다.
    assert item is not None and item.state == "auto_execute"
    assert item.payload["auto_execution"]["status"] == "dispatched"
    assert "delegation_board_task" not in item.payload
    pending = list_pending_commands(assignee)
    assert pending.count == 1
    with session() as s:
        rows = s.execute(
            select(personal_board_tasks).where(
                personal_board_tasks.c.user_id == assignee,
                personal_board_tasks.c.source_kind == "delegation",
            )
        ).all()
    assert rows == []


# --- /complete write-back -----------------------------------------------------


def test_complete_done_resolves_work_item(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev3@example.com")
    _enroll_daemon(assignee, scopes=["commands"])
    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev3@example.com",
        mode="knowledge",
        runner="codex",
        instruction="answer the question",
    )
    assert item is not None and item.state == "auto_execute"
    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])

    # Simulate the daemon claiming + completing the command successfully.
    from orthus.collector.commands import claim_command

    claim_command(assignee, command_id)
    result_payload = {"exit_code": 0, "stdout": "all good", "runner": "codex"}
    command = complete_command(assignee, command_id, status="done", result_payload=result_payload)
    resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )

    refreshed = get_agent_work_item(actor, item.work_id)
    assert refreshed is not None
    assert refreshed.state == "resolved"
    assert refreshed.payload["auto_execution"]["status"] == "done"
    assert refreshed.payload["auto_execution"]["result"] == result_payload


def test_complete_done_that_asks_user_input_marks_request_more_data(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_daemon(actor, scopes=["commands"])
    chat = create_chat_session(actor, get_settings().node_id)
    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee=str(actor),
        mode="knowledge",
        runner="claude",
        instruction="작업해줘",
        chat_session_id=uuid.UUID(chat.session_id),
    )
    assert item is not None and item.state == "auto_execute"
    command = list_pending_commands(actor).items[0]
    result_payload = {
        "exit_code": 0,
        "stdout": "진행 방식 골라 주세요.\n1. 빠르게 처리\n2. 테스트 먼저",
        "runner_session_id": item.payload["thread_id"],
    }

    updated = resolve_agent_task_work_item_result(
        command.payload,
        command_status="done",
        result=result_payload,
    )

    assert updated is not None
    assert updated.state == "request_more_data"
    assert updated.payload["auto_execution"]["status"] == "awaiting_user_input"
    assert updated.payload["pending_user_input"]["status"] == "waiting"
    assert updated.payload["pending_user_input"]["chat_session_id"] == chat.session_id
    assert "awaiting_user_input" in updated.reason_codes


def test_complete_failed_marks_work_item_request_more_data(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev4@example.com")
    _enroll_daemon(assignee, scopes=["commands"])
    item = create_agent_task_work_item(
        actor,
        actor_role="owner",
        assignee="dev4@example.com",
        mode="code",
        runner="claude",
        instruction="break it",
        cwd="/repo",
    )
    assert item is not None and item.state == "auto_execute"
    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])

    from orthus.collector.commands import claim_command

    claim_command(assignee, command_id)
    command = complete_command(
        assignee,
        command_id,
        status="failed",
        result_payload={"reason": "runner not installed: claude"},
    )
    resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )

    refreshed = get_agent_work_item(actor, item.work_id)
    assert refreshed is not None
    assert refreshed.state == "request_more_data"
    assert refreshed.payload["auto_execution"]["status"] == "failed"
    assert "auto_execute_failed" in refreshed.reason_codes


def test_complete_without_work_item_id_is_noop(clean):
    # A non-delegated agent_task command (no work_item_id) does not raise.
    assert (
        resolve_agent_task_work_item_result({}, command_status="done", result={"ok": True}) is None
    )
    assert (
        resolve_agent_task_work_item_result(
            {"work_item_id": str(uuid.uuid4())}, command_status="done", result={}
        )
        is None
    )
