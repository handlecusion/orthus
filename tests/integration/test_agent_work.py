"""P3.1a Agent Work substrate and data_gaps adapter."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from orthus.agentwork import (
    apply_policy,
    create_assistant_command_work_item,
    detect_assistant_command_action,
    get_agent_work_item,
    list_agent_work_items,
    sync_connector_runs_to_agent_work,
    sync_data_gaps_to_agent_work,
    sync_promote_staging_to_agent_work,
    sync_wiki_tasks_to_agent_work,
)
from orthus.agentwork.service import (
    _EMAIL_AUTO_SEND_POLICY_BUCKET,
    _policy_bucket_key_for_values,
    classify_candidate_with_policy_memory,
    execute_auto_email_send,
    ensure_email_draft_for_work_item,
    persist_agent_work_item,
)
from orthus.api.deps import get_current_user
from orthus.api.main import app
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import AgentWorkCandidate, EmailDraftPayload, WikiPage, WikiTask
from orthus.settings import get_settings
from orthus.tables import (
    agent_policy_observations,
    agent_work_decisions,
    agent_work_items,
    connector_accounts,
    audit_log,
    connector_runs,
    data_gaps,
    documents,
    email_send_log,
    promote_staging,
    users,
)
from orthus.wiki.gap import generate_suggestion, record_feedback
from orthus.wiki import store as wiki_store

client = TestClient(app)


def _orchestrate(question: str, *, headers: dict | None = None, **body):
    """Create an agent-work chat session and post a command/compound question to the
    orchestrator — the home of command intake (moved off the search-only /ask)."""
    created = client.post("/agent-work/chats", json={}, headers=headers)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    return client.post(
        f"/agent-work/chats/{sid}/orchestrate",
        json={"text": question, **body},
        headers=headers,
    )


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def test_policy_gate_is_deterministic_and_covers_all_outcomes(clean):
    candidates = [
        AgentWorkCandidate(
            source_kind="connector_run",
            source_ref_id="connector-ok",
            action_family="connector_sync",
            title="sync",
            payload={
                "secret_state": "configured",
                "node_policy_allows": True,
                "actor_role": "scheduler",
            },
        ),
        AgentWorkCandidate(
            source_kind="assistant_command",
            source_ref_id="draft-1",
            action_family="document_draft",
            title="draft",
        ),
        AgentWorkCandidate(
            source_kind="data_gap",
            source_ref_id="gap-1",
            action_family="data_request",
            title="gap",
            payload={"status": "open", "suggestion_status": "pending"},
        ),
        AgentWorkCandidate(
            source_kind="assistant_command",
            source_ref_id="bad-1",
            action_family="unsupported",
            title="bad",
        ),
    ]

    outcomes = [apply_policy(candidate).outcome for candidate in candidates]

    assert outcomes == [
        "auto_execute",
        "draft_for_review",
        "request_more_data",
        "reject",
    ]
    assert [apply_policy(candidate).outcome for candidate in candidates] == outcomes


def test_policy_gate_family_guards_are_phase_safe(clean):
    email = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id="email-1",
        action_family="email_send",
        title="send",
        payload={
            "auto_send_policy_met": True,
            "recipient_hint": "client",
            "recipient_allowed": True,
            "rate_limit_ok": True,
            "policy_memory": {
                "bucket_key": (
                    "email_send|assistant_command|draft_for_review|"
                    "email_draft_first,policy_memory_observation_gate_required"
                ),
                "total": 25,
                "approvals": 25,
                "dismissals": 0,
                "request_more_data": 0,
                "note_present_count": 0,
                "used_for_outcome": False,
            },
        },
    )
    reversible_board = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id="board-1",
        action_family="personal_board_cleanup",
        title="board",
        payload={
            "node_kind": "personal",
            "actor_role": "owner",
            "reversibility": "reversible",
        },
    )
    destructive_board = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id="board-2",
        action_family="personal_board_cleanup",
        title="board",
        payload={"reversibility": "external side effect", "delete": True},
    )
    company_board = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id="board-3",
        action_family="personal_board_cleanup",
        title="board",
        payload={
            "node_kind": "company",
            "actor_role": "owner",
            "reversibility": "reversible",
        },
    )
    queue_cleanup = AgentWorkCandidate(
        source_kind="wiki_task",
        source_ref_id="wiki-1",
        action_family="central_wiki_task_cleanup",
        title="wiki",
        payload={"cleanup_only": True},
    )
    wiki_write = AgentWorkCandidate(
        source_kind="wiki_task",
        source_ref_id="wiki-2",
        action_family="central_wiki_task_cleanup",
        title="wiki",
        payload={"company_wiki_write": True},
    )
    resolved_wiki_task = AgentWorkCandidate(
        source_kind="wiki_task",
        source_ref_id="wiki-3",
        action_family="central_wiki_task_cleanup",
        title="wiki",
        payload={"resolved": True},
    )

    assert apply_policy(email).outcome == "draft_for_review"
    assert "policy_memory_observation_gate_required" in apply_policy(email).reason_codes
    assert apply_policy(reversible_board).outcome == "auto_execute"
    assert apply_policy(destructive_board).outcome == "draft_for_review"
    assert apply_policy(company_board).outcome == "draft_for_review"
    assert apply_policy(queue_cleanup).outcome == "auto_execute"
    assert apply_policy(wiki_write).outcome == "draft_for_review"
    assert apply_policy(resolved_wiki_task).outcome == "reject"
    assert "source_task_resolved" in apply_policy(resolved_wiki_task).reason_codes

    promote = AgentWorkCandidate(
        source_kind="promote_staging",
        source_ref_id="stage-1",
        action_family="promote_review",
        title="promote",
        payload={"status": "pending", "source_scope": "personal"},
    )
    failed_run = AgentWorkCandidate(
        source_kind="connector_run",
        source_ref_id="run-1",
        action_family="connector_sync",
        title="run",
        payload={"run_status": "failed"},
    )

    assert apply_policy(promote).outcome == "draft_for_review"
    assert "no_auto_import" in apply_policy(promote).reason_codes
    assert apply_policy(failed_run).outcome == "request_more_data"
    assert "connector_run_failed" in apply_policy(failed_run).reason_codes


def test_board_create_word_is_not_cleanup_command(clean):
    assert detect_assistant_command_action("할일 만들어줘") is None
    assert detect_assistant_command_action("보드 정리해줘") == "personal_board_cleanup"


def test_summarize_and_email_compound_is_email_command(clean):
    # "요약/설명" read verb must NOT veto a compound command that also carries an
    # explicit email action signal + recipient (the real-world regression).
    assert (
        detect_assistant_command_action(
            "NOVA랑 관련된 문서 요약해서 ceo@nova.example로 메일 초안 작성해"
        )
        == "email_send"
    )
    # The directional 로/으로 particle (not just 에게) yields a recipient hint.
    assert (
        detect_assistant_command_action("보고서 정리해서 김대표로 메일 보내줘")
        == "email_send"
    )
    # No recipient → stays an ambiguous read, not an email command.
    assert detect_assistant_command_action("메일 답장 요약해줘") is None
    # Pure summarize question is still a read, not a command.
    assert detect_assistant_command_action("회사 위키에 정리된 내용 요약해줘") is None


def test_email_recipient_address_not_overcaptured_in_compound(clean, user_id):
    asked = _orchestrate(
        "NOVA랑 관련된 문서 요약해서 ceo@nova.example로 메일 초안 작성해",
        headers={"X-User-Id": str(user_id)},
    )
    assert asked.status_code == 200
    body = asked.json()
    assert body["mode"] == "agent_work"
    assert body["agent_work"]["action_family"] == "email_send"
    assert body["agent_work"]["state"] == "draft_for_review"

    item = client.get(
        f"/agent-work/{body['agent_work']['work_id']}",
        headers={"X-User-Id": str(user_id)},
    )
    payload = item.json()["payload"]
    assert payload["recipient_hint"] == "ceo@nova.example"
    assert payload["email_draft"]["recipient_hint"] == "ceo@nova.example"


def test_orchestrate_email_command_with_recipient_creates_reviewable_draft(clean, user_id):
    asked = _orchestrate(
        "john에게 이메일 초안 만들어줘", headers={"X-User-Id": str(user_id)}
    )

    assert asked.status_code == 200
    body = asked.json()
    assert body["mode"] == "agent_work"
    assert body["agent_work"]["action_family"] == "email_send"
    assert body["agent_work"]["state"] == "draft_for_review"

    item = client.get(
        f"/agent-work/{body['agent_work']['work_id']}",
        headers={"X-User-Id": str(user_id)},
    )

    assert item.status_code == 200
    payload = item.json()["payload"]
    draft = payload["email_draft"]
    assert payload["recipient_hint"] == "john"
    assert draft["recipient_hint"] == "john"
    assert draft["subject_hint"]
    assert draft["body_template"]
    assert draft["used_for_outcome"] is False
    assert not any(key.startswith("smtp") or key.startswith("send") for key in draft)
    # New mail (no reply_context) must NOT carry a "Re:" subject — that prefix is for
    # replies to a specific inbound mail.
    assert not draft["subject_hint"].startswith("Re:")
    # The composed body is a real draft, not the old reviewer-fills-everything stub.
    assert "검토자가 본문을" not in draft["body_template"] or draft["llm_drafted"] is False
    assert "llm_drafted" in draft


def test_orchestrate_email_command_keeps_real_recipient_address_unmasked(clean, user_id):
    """The recipient is extracted from the raw command BEFORE PII redaction, so a
    real address survives instead of being masked to an undeliverable j***@x.com."""
    asked = _orchestrate(
        "john@example.com에게 메일 보내줘", headers={"X-User-Id": str(user_id)}
    )

    assert asked.status_code == 200
    body = asked.json()
    assert body["agent_work"]["action_family"] == "email_send"
    assert body["agent_work"]["state"] == "draft_for_review"

    item = client.get(
        f"/agent-work/{body['agent_work']['work_id']}",
        headers={"X-User-Id": str(user_id)},
    )
    payload = item.json()["payload"]
    assert payload["recipient_hint"] == "john@example.com"
    assert payload["email_draft"]["recipient_hint"] == "john@example.com"
    # The stored question is still redacted (the recipient extraction is the only
    # pre-redaction read); the masked form must not be the recipient.
    assert "j***@example.com" not in payload["email_draft"]["recipient_hint"]


def test_orchestrate_email_command_without_recipient_requests_more_data(clean, user_id):
    asked = _orchestrate("이메일 초안 만들어줘", headers={"X-User-Id": str(user_id)})

    assert asked.status_code == 200
    body = asked.json()
    assert body["mode"] == "agent_work"
    assert body["agent_work"]["action_family"] == "email_send"
    assert body["agent_work"]["state"] == "request_more_data"
    assert "recipient_required" in body["agent_work"]["reason_codes"]

    item = client.get(
        f"/agent-work/{body['agent_work']['work_id']}",
        headers={"X-User-Id": str(user_id)},
    )

    assert item.status_code == 200
    payload = item.json()["payload"]
    assert "email_draft" not in payload
    assert "email_auto_send_gate" not in payload
    assert payload["required_data"] == ["recipient name, address, or thread reference"]


def test_email_recipient_extraction_robust(clean):
    from orthus.agentwork.service import _email_recipient_hint

    # A literal address is preferred and not over-captured from the preceding clause.
    assert _email_recipient_hint("긴 보고서 정리해서 ceo@nova.example로 메일") == "ceo@nova.example"
    # The directional 로/으로 particle yields a name recipient (not just 에게).
    assert _email_recipient_hint("보고서 정리해서 김대표로 메일 보내줘") == "김대표"
    # An address command with no read verb is an email command already.
    assert detect_assistant_command_action("ceo@nova.example로 메일 초안 작성해") == "email_send"


def test_email_draft_payload_schema_rejects_send_metadata():
    with pytest.raises(ValidationError):
        EmailDraftPayload(
            recipient_hint="john",
            subject_hint="Re: hello",
            body_template="hello",
            smtp_host="smtp.example.com",
        )


def test_data_gaps_sync_creates_idempotent_work_items(clean):
    uid = _make_user()
    gap_id = record_feedback(uid, "대리.ai 가격 정책")
    assert gap_id is not None

    first = sync_data_gaps_to_agent_work(uid)
    second = sync_data_gaps_to_agent_work(uid)

    assert first.created_or_updated == 1
    assert second.created_or_updated == 1
    assert first.items[0].work_id == second.items[0].work_id
    assert first.items[0].source_kind == "data_gap"
    assert first.items[0].source_ref_id == str(gap_id)
    assert first.items[0].policy_outcome == "request_more_data"
    assert first.items[0].policy_reason
    assert first.items[0].correlation_id is not None
    assert first.items[0].last_run_id is not None

    with session() as s:
        count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        audit_nodes = [
            row.node
            for row in s.execute(
                select(audit_log.c.node)
                .where(audit_log.c.node.in_(["agent_work.classify", "agent_work.persist"]))
                .order_by(audit_log.c.occurred_at)
            ).all()
        ]
    assert count == 1
    assert "agent_work.classify" in audit_nodes
    assert "agent_work.persist" in audit_nodes


def test_ready_data_gap_becomes_draft_for_review(clean):
    uid = _make_user()
    gap_id = record_feedback(uid, "회사 온보딩 문서")
    assert gap_id is not None
    suggestion_json = (
        '{"target": "회사 Notion 온보딩", "connector": "notion", '
        '"sections": [{"title": "필수 정보", "items": ["권한", "첫 주 업무"]}]}'
    )
    generate_suggestion(uid, gap_id, chat_model=MockChat(default=suggestion_json))

    synced = sync_data_gaps_to_agent_work(uid)

    assert synced.items[0].policy_outcome == "draft_for_review"
    assert synced.items[0].state == "draft_for_review"
    assert "data_gap_suggestion_ready" in synced.items[0].reason_codes


def test_wiki_tasks_sync_creates_idempotent_work_items(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    task = WikiTask(
        slug="conflict-nova-status",
        kind="conflict",
        description="Nova status claims disagree.",
        related=["claim-a", "claim-b"],
        created_at=datetime(2026, 6, 6, tzinfo=UTC),
        resolved=False,
    )
    wiki_store.write_task(task, user_id=user_id, scope="company", project="nova")

    first = sync_wiki_tasks_to_agent_work(user_id)
    second = sync_wiki_tasks_to_agent_work(user_id)

    assert first.created_or_updated == 1
    assert second.created_or_updated == 1
    assert first.items[0].work_id == second.items[0].work_id
    assert first.items[0].source_kind == "wiki_task"
    assert first.items[0].source_ref_id == "conflict-nova-status"
    assert first.items[0].action_family == "central_wiki_task_cleanup"
    assert first.items[0].policy_outcome == "draft_for_review"
    assert first.items[0].state == "draft_for_review"
    assert "company_wiki_review_required" in first.items[0].reason_codes
    assert first.items[0].payload["kind"] == "conflict"
    assert first.items[0].evidence[0]["kind"] == "wiki_task"

    with session() as s:
        count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        audit_nodes = [
            row.node
            for row in s.execute(
                select(audit_log.c.node)
                .where(audit_log.c.node.in_(["agent_work.classify", "agent_work.persist"]))
                .order_by(audit_log.c.occurred_at)
            ).all()
        ]
    assert count == 1
    assert "agent_work.classify" in audit_nodes
    assert "agent_work.persist" in audit_nodes


def test_wiki_tasks_sync_skips_resolved_tasks(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    wiki_store.write_task(
        WikiTask(
            slug="resolved-conflict",
            kind="conflict",
            description="Already handled.",
            related=["claim-a"],
            created_at=datetime(2026, 6, 6, tzinfo=UTC),
            resolved=True,
        ),
        user_id=user_id,
        scope="company",
    )

    synced = sync_wiki_tasks_to_agent_work(user_id)

    assert synced.created_or_updated == 0
    assert synced.items == []


def test_wiki_task_cleanup_candidate_can_be_auto_execute(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    wiki_store.write_task(
        WikiTask(
            slug="dedup-old-links",
            kind="dedup",
            description="Deduplicate stale task links.",
            related=["page-a"],
            created_at=datetime(2026, 6, 6, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
    )

    synced = sync_wiki_tasks_to_agent_work(user_id)

    assert synced.created_or_updated == 1
    assert synced.items[0].policy_outcome == "auto_execute"
    assert synced.items[0].state == "resolved"
    assert "queue_cleanup_only" in synced.items[0].reason_codes
    assert synced.items[0].payload["auto_execution"]["kind"] == "wiki_task_cleanup"
    assert synced.items[0].payload["auto_execution"]["task_slug"] == "dedup-old-links"

    task = wiki_store.load_task("dedup-old-links", root=tmp_path, scope="company")
    assert task is not None
    assert task.resolved is True
    second = sync_wiki_tasks_to_agent_work(user_id)
    assert second.created_or_updated == 0
    assert second.items == []

    with session() as s:
        audit_nodes = [
            row.node
            for row in s.execute(
                select(audit_log.c.node)
                .where(audit_log.c.node == "agent_work.auto_execute")
                .order_by(audit_log.c.occurred_at)
            ).all()
        ]
    assert audit_nodes == ["agent_work.auto_execute", "agent_work.auto_execute"]


def test_agent_work_api_sync_wiki_tasks(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    wiki_store.write_task(
        WikiTask(
            slug="open-question-onboarding",
            kind="open_question",
            description="Need onboarding source.",
            related=["company-onboarding"],
            created_at=datetime(2026, 6, 6, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
    )

    synced = client.post(
        "/agent-work/sync/wiki-tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert synced.status_code == 200
    assert synced.json()["source_kind"] == "wiki_task"
    assert synced.json()["created_or_updated"] == 1
    assert synced.json()["items"][0]["source_ref_id"] == "open-question-onboarding"
    assert synced.json()["items"][0]["state"] == "draft_for_review"


def test_promote_staging_sync_creates_pending_review_items(clean, user_id):
    stage_id = uuid.uuid4()
    source_doc_id = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(promote_staging).values(
                stage_id=stage_id,
                source_node_id="personal-a",
                source_doc_id=source_doc_id,
                source_owner_id=user_id,
                source_scope="personal",
                source_title="Personal note",
                sanitized_title="Sanitized note",
                sanitized_markdown="safe summary",
                source_meta={"target_project": "company", "source": "local_files"},
                status="pending",
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()

    first = sync_promote_staging_to_agent_work(user_id)
    second = sync_promote_staging_to_agent_work(user_id)

    assert first.created_or_updated == 1
    assert second.created_or_updated == 1
    assert first.items[0].work_id == second.items[0].work_id
    assert first.items[0].source_kind == "promote_staging"
    assert first.items[0].source_ref_id == str(stage_id)
    assert first.items[0].action_family == "promote_review"
    assert first.items[0].policy_outcome == "draft_for_review"
    assert first.items[0].state == "draft_for_review"
    assert "promote_review_required" in first.items[0].reason_codes
    assert first.items[0].payload["sanitized_markdown_chars"] == len("safe summary")
    assert "sanitized_markdown" not in first.items[0].payload
    assert first.items[0].evidence[0]["kind"] == "promote_staging"


def test_promote_staging_sync_skips_decided_and_personal_node(clean, user_id):
    settings = get_settings()
    stage_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(promote_staging).values(
                stage_id=stage_id,
                source_node_id="personal-a",
                source_doc_id=uuid.uuid4(),
                source_owner_id=user_id,
                source_scope="personal",
                source_title="Personal note",
                sanitized_title="Sanitized note",
                sanitized_markdown="safe summary",
                source_meta={"target_project": "company"},
                status="approved",
                created_by=user_id,
                approved_by=user_id,
            )
        )
        s.commit()

    assert sync_promote_staging_to_agent_work(user_id).created_or_updated == 0
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    assert sync_promote_staging_to_agent_work(user_id).created_or_updated == 0


def test_connector_runs_sync_creates_failed_run_triage_items(clean, user_id):
    account_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                scope="company",
                owner_id=None,
                auth_mode="token",
                account_label="Slack",
                status="active",
                settings_redacted={"secret_refs": {"token": "keychain://token"}},
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=run_id,
                account_id=account_id,
                connector_slug="slack",
                reason="manual",
                status="failed",
                fetched=0,
                created=0,
                updated=0,
                skipped=0,
                errors=1,
                error_message="token expired for bob@example.com",
                started_at=now,
                finished_at=now,
            )
        )
        s.commit()

    first = sync_connector_runs_to_agent_work(user_id)
    second = sync_connector_runs_to_agent_work(user_id)

    assert first.created_or_updated == 1
    assert second.created_or_updated == 1
    assert first.items[0].work_id == second.items[0].work_id
    assert first.items[0].source_kind == "connector_run"
    assert first.items[0].source_ref_id == str(run_id)
    assert first.items[0].action_family == "connector_sync"
    assert first.items[0].policy_outcome == "request_more_data"
    assert first.items[0].state == "request_more_data"
    assert "connector_run_failed" in first.items[0].reason_codes
    assert "bob@example.com" not in first.items[0].payload["error_message"]
    assert "b***@example.com" in first.items[0].payload["error_message"]
    assert first.items[0].payload["retry_guard"] == {
        "auto_retry_allowed": False,
        "reason": "failed_connector_run_triage_only",
        "requires_operator_review": True,
        "source_run_status": "failed",
        "used_for_outcome": False,
    }
    assert first.items[0].evidence[0]["kind"] == "connector_run"
    assert first.items[0].evidence[0]["retry_guard"]["auto_retry_allowed"] is False

    with session() as s:
        run_count = s.execute(select(func.count()).select_from(connector_runs)).scalar_one()
    assert run_count == 1


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_state"),
    [
        ("approve", 200, "resolved"),
        ("dismiss", 200, "dismissed"),
        ("request_more_data", 409, "request_more_data"),
    ],
)
def test_failed_connector_run_review_does_not_retry_connector(
    clean, user_id, decision, expected_status, expected_state
):
    account_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                scope="company",
                owner_id=None,
                auth_mode="token",
                account_label="Slack",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=run_id,
                account_id=account_id,
                connector_slug="slack",
                reason="manual",
                status="failed",
                errors=1,
                error_message="timeout",
                started_at=now,
                finished_at=now,
            )
        )
        s.commit()
    item = sync_connector_runs_to_agent_work(user_id).items[0]
    assert item.state == "request_more_data"
    assert item.payload["retry_guard"]["auto_retry_allowed"] is False

    decided = client.post(
        f"/agent-work/{item.work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": decision, "note": "acknowledged, no retry"},
    )

    assert decided.status_code == expected_status
    if expected_status == 200:
        body = decided.json()
        assert body["item"]["state"] == expected_state
        assert body["item"]["payload"]["retry_guard"]["auto_retry_allowed"] is False
        assert "auto_execution" not in body["item"]["payload"]
        assert "source_writeback" not in body["item"]["payload"]
    else:
        assert "already requesting more data" in decided.json()["detail"]
    with session() as s:
        run_count = s.execute(select(func.count()).select_from(connector_runs)).scalar_one()
    assert run_count == 1


def test_connector_runs_sync_skips_non_failed_and_unsupported(clean, user_id):
    ok_account = uuid.uuid4()
    stale_account = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=ok_account,
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="token",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=uuid.uuid4(),
                account_id=ok_account,
                connector_slug="slack",
                reason="manual",
                status="succeeded",
                started_at=now,
                finished_at=now,
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=stale_account,
                connector_slug="gws_drive",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="local_cli",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=uuid.uuid4(),
                account_id=stale_account,
                connector_slug="gws_drive",
                reason="manual",
                status="failed",
                errors=1,
                error_message="unsupported",
                started_at=now,
                finished_at=now,
            )
        )
        s.commit()

    synced = sync_connector_runs_to_agent_work(user_id)

    assert synced.created_or_updated == 0
    assert synced.items == []


def test_personal_node_connector_runs_are_owner_scoped(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    other_user = _make_user()
    own_account = uuid.uuid4()
    other_account = uuid.uuid4()
    own_run = uuid.uuid4()
    other_run = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        for account_id, owner_id in (
            (own_account, user_id),
            (other_account, other_user),
        ):
            s.execute(
                insert(connector_accounts).values(
                    account_id=account_id,
                    connector_slug="chat_exports",
                    account_kind="personal",
                    node_id="personal-a",
                    scope="personal",
                    owner_id=owner_id,
                    auth_mode="local_path",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
        for run_id, account_id in (
            (own_run, own_account),
            (other_run, other_account),
        ):
            s.execute(
                insert(connector_runs).values(
                    run_id=run_id,
                    account_id=account_id,
                    connector_slug="chat_exports",
                    reason="manual",
                    status="failed",
                    errors=1,
                    error_message="missing export path",
                    started_at=now,
                    finished_at=now,
                )
            )
        s.commit()

    own_items = sync_connector_runs_to_agent_work(user_id).items
    other_items = sync_connector_runs_to_agent_work(other_user).items

    assert [item.source_ref_id for item in own_items] == [str(own_run)]
    assert [item.source_ref_id for item in other_items] == [str(other_run)]


def test_agent_work_api_sync_promote_and_connector_sources(clean, user_id):
    stage_id = uuid.uuid4()
    account_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime(2026, 6, 6, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(promote_staging).values(
                stage_id=stage_id,
                source_node_id="personal-a",
                source_doc_id=uuid.uuid4(),
                source_owner_id=user_id,
                source_scope="personal",
                source_title="Personal note",
                sanitized_title="Sanitized note",
                sanitized_markdown="safe summary",
                source_meta={"target_project": "company"},
                status="pending",
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="token",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=run_id,
                account_id=account_id,
                connector_slug="slack",
                reason="manual",
                status="failed",
                errors=1,
                error_message="timeout",
                started_at=now,
                finished_at=now,
            )
        )
        s.commit()

    promoted = client.post(
        "/agent-work/sync/promote-staging",
        headers={"X-User-Id": str(user_id)},
    )
    connector = client.post(
        "/agent-work/sync/connector-runs",
        headers={"X-User-Id": str(user_id)},
    )

    assert promoted.status_code == 200
    assert promoted.json()["source_kind"] == "promote_staging"
    assert promoted.json()["items"][0]["source_ref_id"] == str(stage_id)
    assert connector.status_code == 200
    assert connector.json()["source_kind"] == "connector_run"
    assert connector.json()["items"][0]["source_ref_id"] == str(run_id)


def test_agent_work_api_sync_list_and_detail(clean, user_id):
    gap_id = record_feedback(user_id, "프로젝트별 회고 위치")
    assert gap_id is not None

    synced = client.post(
        "/agent-work/sync/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )
    assert synced.status_code == 200
    assert synced.json()["created_or_updated"] == 1

    listed = client.get("/agent-work", headers={"X-User-Id": str(user_id)})
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["source_ref_id"] == str(gap_id)
    assert rows[0]["policy_outcome"] == "request_more_data"
    assert rows[0]["policy_reason"]
    assert rows[0]["evidence"][0]["kind"] == "data_gap"
    assert rows[0]["last_run_id"]

    detail = client.get(
        f"/agent-work/{rows[0]['work_id']}",
        headers={"X-User-Id": str(user_id)},
    )
    assert detail.status_code == 200
    assert detail.json()["work_id"] == rows[0]["work_id"]


def test_agent_work_context_wiki_slug_is_projected_as_read_only_link(
    clean, user_id, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    wiki_store.write_page(
        WikiPage(
            slug="company-policy",
            title="Company Policy",
            definition="Policy page.",
            overview="Policy overview.",
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    item = create_assistant_command_work_item(
        user_id,
        "문서 초안 작성해줘",
        actor_role="owner",
        context_wiki_slug="company-policy",
    )

    assert item is not None
    assert item.source_kind == "assistant_command"
    assert item.action_family == "document_draft"
    assert item.payload["context"]["wiki_slug"] == "company-policy"
    assert item.payload["draft_document"]["status"] == "draft"
    listed = client.get(
        "/agent-work?wiki_slug=company-policy",
        headers={"X-User-Id": str(user_id)},
    )
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["work_id"] == str(item.work_id)
    assert rows[0]["wiki_slugs"] == ["company-policy"]
    assert rows[0]["payload"]["context"]["wiki_slug"] == "company-policy"

    detail = client.get(
        f"/agent-work/{item.work_id}",
        headers={"X-User-Id": str(user_id)},
    )
    assert detail.status_code == 200
    assert detail.json()["wiki_slugs"] == ["company-policy"]

    page_work = client.get(
        "/wiki/pages/company-policy/agent-work",
        headers={"X-User-Id": str(user_id)},
    )
    assert page_work.status_code == 200
    assert page_work.json()["count"] == 1
    assert page_work.json()["items"][0]["work_id"] == str(item.work_id)


def test_agent_work_wiki_projection_drops_unresolved_slugs(clean, user_id):
    item = create_assistant_command_work_item(
        user_id,
        "문서 초안 작성해줘",
        actor_role="owner",
        context_wiki_slug="missing-page",
    )

    assert item is not None
    listed = client.get(
        "/agent-work?wiki_slug=missing-page",
        headers={"X-User-Id": str(user_id)},
    )
    assert listed.status_code == 200
    assert listed.json() == []

    detail = client.get(
        f"/agent-work/{item.work_id}",
        headers={"X-User-Id": str(user_id)},
    )
    assert detail.status_code == 200
    assert detail.json()["wiki_slugs"] == []


def test_wiki_task_related_page_projects_agent_work_link(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    wiki_store.write_page(
        WikiPage(
            slug="company-onboarding",
            title="Company Onboarding",
            definition="Onboarding page.",
            overview="Onboarding overview.",
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    wiki_store.write_task(
        WikiTask(
            slug="open-question-onboarding",
            kind="open_question",
            description="Need source.",
            related=["company-onboarding", "claim-only"],
            created_at=datetime(2026, 6, 7, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    synced = client.post(
        "/agent-work/sync/wiki-tasks",
        headers={"X-User-Id": str(user_id)},
    )
    assert synced.status_code == 200
    row = synced.json()["items"][0]
    assert row["source_kind"] == "wiki_task"
    assert row["wiki_slugs"] == ["company-onboarding"]
    assert "claim-only" not in row["wiki_slugs"]

    page_work = client.get(
        "/wiki/pages/company-onboarding/agent-work",
        headers={"X-User-Id": str(user_id)},
    )
    assert page_work.status_code == 200
    assert page_work.json()["count"] == 1
    assert page_work.json()["items"][0]["source_ref_id"] == "open-question-onboarding"


def test_agent_work_decision_approve_resolves_and_records_redacted_note(clean, user_id):
    item = _sync_ready_gap(user_id)
    note = "승인. contact bob@example.com 010-1234-5678"
    with session() as s:
        before_docs = s.execute(select(func.count()).select_from(documents)).scalar_one()
        before_runs = s.execute(select(func.count()).select_from(connector_runs)).scalar_one()

    decided = client.post(
        f"/agent-work/{item['work_id']}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve", "note": note},
    )

    assert decided.status_code == 200
    body = decided.json()
    assert body["item"]["state"] == "resolved"
    assert body["decision"]["decision"] == "approve"
    assert body["decision"]["from_state"] == "draft_for_review"
    assert body["decision"]["to_state"] == "resolved"
    assert "bob@example.com" not in body["decision"]["note"]
    assert "010-1234-5678" not in body["decision"]["note"]
    assert "b***@example.com" in body["decision"]["note"]
    assert body["decision"]["node_run_id"]
    assert body["item"]["payload"]["source_writeback"]["kind"] == "data_gap"
    assert body["item"]["payload"]["source_writeback"]["status"] == "resolved"
    assert body["item"]["payload"]["source_writeback"]["updated"] is True

    with session() as s:
        row = s.execute(
            select(agent_work_decisions).where(
                agent_work_decisions.c.work_id == uuid.UUID(item["work_id"])
            )
        ).first()
        observation = s.execute(
            select(agent_policy_observations).where(
                agent_policy_observations.c.work_id == uuid.UUID(item["work_id"])
            )
        ).first()
        gap_status = s.execute(
            select(data_gaps.c.status).where(data_gaps.c.gap_id == uuid.UUID(item["source_ref_id"]))
        ).scalar_one()
        after_docs = s.execute(select(func.count()).select_from(documents)).scalar_one()
        after_runs = s.execute(select(func.count()).select_from(connector_runs)).scalar_one()
        audit_phases = [
            r.phase
            for r in s.execute(
                select(audit_log.c.phase)
                .where(audit_log.c.node == "agent_work.decision")
                .order_by(audit_log.c.occurred_at)
            ).all()
        ]
    assert row is not None
    assert "bob@example.com" not in row._mapping["note"]
    assert "010-1234-5678" not in row._mapping["note"]
    assert observation is not None
    assert observation._mapping["decision_id"] == row._mapping["decision_id"]
    assert observation._mapping["reviewer_decision"] == "approve"
    assert observation._mapping["note_present"] is True
    assert observation._mapping["bucket_key"].startswith("data_request|data_gap|")
    assert "note" not in observation._mapping["meta"]
    assert gap_status == "resolved"
    assert (after_docs, after_runs) == (before_docs, before_runs)
    assert "enter" in audit_phases
    assert "exit" in audit_phases


def test_agent_work_decision_request_more_data_from_draft(clean, user_id):
    item = _sync_ready_gap(user_id)

    decided = client.post(
        f"/agent-work/{item['work_id']}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "request_more_data", "note": "missing target"},
    )

    assert decided.status_code == 200
    body = decided.json()
    assert body["item"]["state"] == "request_more_data"
    assert body["decision"]["from_state"] == "draft_for_review"
    assert body["decision"]["to_state"] == "request_more_data"
    assert "source_writeback" not in body["item"]["payload"]
    with session() as s:
        gap_status = s.execute(
            select(data_gaps.c.status).where(data_gaps.c.gap_id == uuid.UUID(item["source_ref_id"]))
        ).scalar_one()
    assert gap_status == "open"


def test_agent_work_decision_dismisses_request_more_data_item(clean, user_id):
    gap_id = record_feedback(user_id, "정리할 수 없는 질문")
    assert gap_id is not None
    synced = sync_data_gaps_to_agent_work(user_id)
    item = synced.items[0]
    assert item.state == "request_more_data"

    decided = client.post(
        f"/agent-work/{item.work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "dismiss", "note": "not needed"},
    )

    assert decided.status_code == 200
    body = decided.json()
    assert body["item"]["state"] == "dismissed"
    assert body["item"]["payload"]["source_writeback"]["status"] == "dismissed"
    assert body["item"]["payload"]["source_writeback"]["updated"] is True
    with session() as s:
        gap_status = s.execute(
            select(data_gaps.c.status).where(data_gaps.c.gap_id == gap_id)
        ).scalar_one()
    assert gap_status == "dismissed"


def test_agent_work_decision_does_not_writeback_non_data_gap_sources(clean, user_id):
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=user_id if settings.node_kind == "personal" else None,
                source_kind="assistant_command",
                source_ref_id="document-draft-command",
                action_family="document_draft",
                title="Draft document",
                payload={},
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason="document draft requires review",
                reason_codes=["document_draft_first"],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()

    decided = client.post(
        f"/agent-work/{work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve"},
    )

    assert decided.status_code == 200
    body = decided.json()
    assert body["item"]["state"] == "resolved"
    assert "source_writeback" not in body["item"]["payload"]


def test_agent_work_decision_rejects_double_decision(clean, user_id):
    item = _sync_ready_gap(user_id)
    first = client.post(
        f"/agent-work/{item['work_id']}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve"},
    )
    second = client.post(
        f"/agent-work/{item['work_id']}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "dismiss"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "agent work item already decided"
    with session() as s:
        observations = s.execute(
            select(func.count())
            .select_from(agent_policy_observations)
            .where(agent_policy_observations.c.work_id == uuid.UUID(item["work_id"]))
        ).scalar_one()
    assert observations == 1


def test_agent_work_policy_memory_summary_counts_review_decisions(clean, user_id):
    work_ids = [_insert_review_item(user_id) for _ in range(3)]
    decisions = [
        ("approve", "looks good"),
        ("dismiss", None),
        ("request_more_data", "missing source"),
    ]
    for work_id, (decision, note) in zip(work_ids, decisions, strict=True):
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": decision, "note": note},
        )
        assert decided.status_code == 200

    listed = client.get("/agent-work/policy-memory", headers={"X-User-Id": str(user_id)})

    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    summary = rows[0]
    assert summary["bucket_key"] == (
        "data_request|data_gap|draft_for_review|data_gap_suggestion_ready"
    )
    assert summary["total"] == 3
    assert summary["approvals"] == 1
    assert summary["dismissals"] == 1
    assert summary["request_more_data"] == 1
    assert summary["note_present_count"] == 2
    assert summary["no_edit_approvals"] == 0
    assert summary["recent_total"] == 3
    assert summary["recent_no_edit_approvals"] == 0
    assert summary["recent_no_edit_approval_rate"] == 0.0
    assert summary["email_auto_send_observation_threshold_met"] is False


def test_policy_memory_context_attaches_read_only_to_new_work_item(clean, user_id):
    work_ids = [_insert_review_item(user_id) for _ in range(2)]
    decisions = [
        ("approve", "no edit"),
        ("request_more_data", None),
    ]
    for work_id, (decision, note) in zip(work_ids, decisions, strict=True):
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": decision, "note": note},
        )
        assert decided.status_code == 200

    item = _sync_ready_gap(user_id)
    context = item["payload"]["policy_memory"]

    assert item["state"] == "draft_for_review"
    assert item["policy_outcome"] == "draft_for_review"
    assert context["bucket_key"] == (
        "data_request|data_gap|draft_for_review|data_gap_suggestion_ready"
    )
    assert context["total"] == 2
    assert context["approvals"] == 1
    assert context["request_more_data"] == 1
    assert context["note_present_count"] == 1
    assert context["no_edit_approvals"] == 0
    assert context["recent_total"] == 2
    assert context["recent_no_edit_approvals"] == 0
    assert context["recent_no_edit_approval_rate"] == 0.0
    assert context["email_auto_send_observation_threshold_met"] is False
    assert context["used_for_outcome"] is False
    assert context["last_observed_at"] is not None


def test_email_policy_memory_tracks_explicit_no_edit_threshold_without_escalation(clean, user_id):
    for _ in range(20):
        work_id = _insert_email_review_item(user_id)
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": "approve", "no_edit": True},
        )
        assert decided.status_code == 200

    summary = client.get("/agent-work/policy-memory", headers={"X-User-Id": str(user_id)})
    item = create_assistant_command_work_item(
        user_id,
        "거래처에 이메일 답장 초안 보내줘",
        actor_role="owner",
    )

    assert summary.status_code == 200
    body = summary.json()[0]
    assert body["bucket_key"] == (
        "email_send|assistant_command|draft_for_review|"
        "email_draft_first,policy_memory_observation_gate_required"
    )
    assert body["total"] == 20
    assert body["no_edit_approvals"] == 20
    assert body["recent_window_days"] == 60
    assert body["recent_total"] == 20
    assert body["recent_no_edit_approvals"] == 20
    assert body["recent_no_edit_approval_rate"] == 1.0
    assert body["email_auto_send_observation_threshold_met"] is True
    assert item is not None
    assert item.action_family == "email_send"
    assert item.state == "draft_for_review"
    assert item.payload["policy_memory"]["used_for_outcome"] is False
    assert item.payload["policy_memory"]["email_auto_send_observation_threshold_met"] is True
    gate = item.payload["email_auto_send_gate"]
    assert gate["eligible"] is False
    assert gate["used_for_outcome"] is False
    assert gate["threshold_met"] is True
    assert "personal_node" in gate["missing_checks"]
    assert "recipient_present" in gate["missing_checks"]


def test_email_auto_send_policy_bucket_matches_bucket_builder(clean):
    assert _EMAIL_AUTO_SEND_POLICY_BUCKET == _policy_bucket_key_for_values(
        action_family="email_send",
        source_kind="assistant_command",
        policy_outcome="draft_for_review",
        reason_codes=[
            "email_draft_first",
            "policy_memory_observation_gate_required",
        ],
    )


def test_email_policy_memory_requires_rate_above_95_percent(clean, user_id):
    for i in range(20):
        work_id = _insert_email_review_item(user_id)
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": "approve", "no_edit": i < 19},
        )
        assert decided.status_code == 200

    summary = client.get("/agent-work/policy-memory", headers={"X-User-Id": str(user_id)})

    assert summary.status_code == 200
    body = summary.json()[0]
    assert body["recent_total"] == 20
    assert body["recent_no_edit_approvals"] == 19
    assert body["recent_no_edit_approval_rate"] == 0.95
    assert body["email_auto_send_observation_threshold_met"] is False


def test_no_edit_telemetry_requires_draft_email_approve(clean, user_id):
    email_request_more_id = _insert_email_review_item(user_id)
    non_email_draft_id = _insert_review_item(user_id)
    email_dismiss_id = _insert_email_review_item(user_id)
    email_request_decision_id = _insert_email_review_item(user_id)
    with session() as s:
        s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == email_request_more_id)
            .values(state="request_more_data")
        )
        s.commit()

    decisions = [
        (email_request_more_id, "approve"),
        (non_email_draft_id, "approve"),
        (email_dismiss_id, "dismiss"),
        (email_request_decision_id, "request_more_data"),
    ]
    for work_id, decision in decisions:
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": decision, "no_edit": True},
        )
        assert decided.status_code == 200

    with session() as s:
        rows = s.execute(
            select(agent_policy_observations.c.work_id, agent_policy_observations.c.meta).where(
                agent_policy_observations.c.work_id.in_(
                    [work_id for work_id, _decision in decisions]
                )
            )
        ).all()

    meta_by_work_id = {row.work_id: row.meta for row in rows}
    assert meta_by_work_id[email_request_more_id]["no_edit"] is None
    assert meta_by_work_id[email_request_more_id]["no_edit_approval"] is False
    assert meta_by_work_id[non_email_draft_id]["no_edit"] is None
    assert meta_by_work_id[non_email_draft_id]["no_edit_approval"] is False
    assert meta_by_work_id[email_dismiss_id]["no_edit"] is None
    assert meta_by_work_id[email_dismiss_id]["no_edit_approval"] is False
    assert meta_by_work_id[email_request_decision_id]["no_edit"] is None
    assert meta_by_work_id[email_request_decision_id]["no_edit_approval"] is False


def test_email_auto_send_gate_preflight_can_be_eligible_without_changing_outcome(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    for _ in range(20):
        work_id = _insert_email_review_item(user_id)
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": "approve", "no_edit": True},
        )
        assert decided.status_code == 200

    candidate = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id="email-ready-command",
        action_family="email_send",
        title="Email ready",
        payload={
            "node_kind": "personal",
            "actor_role": "owner",
            "recipient_hash": "recipient-hash",
            "recipient_allowed": True,
            "domain_allowed": True,
            "template_stable": True,
            "rate_limit_ok": True,
            "sensitive_content": False,
            "attachment": False,
        },
    )

    classified_candidate, decision, _correlation_id, _classify_run_id = (
        classify_candidate_with_policy_memory(candidate, user_id, settings=settings)
    )

    assert decision.outcome == "draft_for_review"
    gate = classified_candidate.payload["email_auto_send_gate"]
    assert gate["eligible"] is True
    assert gate["used_for_outcome"] is False
    assert gate["threshold_met"] is True
    assert gate["recent_total"] == 20
    assert gate["recent_no_edit_approvals"] == 20
    assert gate["missing_checks"] == []


def test_email_sender_none_keeps_eligible_draft_unlogged(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.email_sender = "none"
    work_id = _insert_auto_email_candidate(user_id)
    item = get_agent_work_item(user_id, work_id, settings=settings)
    assert item is not None

    updated = execute_auto_email_send(user_id, item, settings=settings)

    assert updated.state == "draft_for_review"
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0


def test_fake_email_sender_resolves_and_logs_hash_only(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.email_sender = "fake"
    work_id = _insert_auto_email_candidate(user_id, recipient_hint="john@example.com")
    item = get_agent_work_item(user_id, work_id, settings=settings)
    assert item is not None

    updated = execute_auto_email_send(user_id, item, settings=settings)

    assert updated.state == "resolved"
    assert updated.payload["auto_execution"]["kind"] == "email_send"
    assert updated.payload["auto_execution"]["sender_kind"] == "fake"
    assert updated.payload["email_auto_send_gate"]["used_for_outcome"] is False
    with session() as s:
        row = s.execute(select(email_send_log).where(email_send_log.c.work_id == work_id)).first()
        audit_nodes = {
            audit.node
            for audit in s.execute(
                select(audit_log.c.node).where(
                    audit_log.c.node.in_(["agent_work.auto_execute", "email.send"])
                )
            ).all()
        }
    assert row is not None
    log = row._mapping
    assert log["status"] == "sent"
    assert log["sender_kind"] == "fake"
    assert log["recipient_hash"] == _email_hash_for_test("john@example.com")
    assert log["recipient_hash"] != "john@example.com"
    assert "john@example.com" not in str(dict(log))
    assert {"agent_work.auto_execute", "email.send"} <= audit_nodes


def test_fake_email_sender_executes_after_real_gate_builder(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.email_sender = "fake"
    recipient = "john@example.com"
    for _ in range(20):
        work_id = _insert_email_review_item(user_id)
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": "approve", "no_edit": True},
        )
        assert decided.status_code == 200
    candidate = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id=f"email-ready-{uuid.uuid4()}",
        action_family="email_send",
        title="Email ready",
        payload={
            "node_kind": "personal",
            "actor_role": "owner",
            "recipient_hint": recipient,
            "recipient_hash": _email_hash_for_test(recipient),
            "recipient_allowed": True,
            "domain_allowed": True,
            "template_stable": True,
            "rate_limit_ok": True,
            "sensitive_content": False,
            "attachment": False,
        },
    )
    classified, decision, correlation_id, _classify_run_id = classify_candidate_with_policy_memory(
        candidate,
        user_id,
        settings=settings,
    )
    assert classified.payload["email_auto_send_gate"]["eligible"] is True
    assert classified.payload["email_auto_send_gate"]["reason"] == "preflight_gate"
    item = persist_agent_work_item(
        classified,
        decision,
        user_id=user_id,
        correlation_id=correlation_id,
        settings=settings,
    )
    item = ensure_email_draft_for_work_item(user_id, item, settings=settings)

    updated = execute_auto_email_send(user_id, item, settings=settings)

    assert updated.state == "resolved"
    assert updated.payload["auto_execution"]["kind"] == "email_send"
    with session() as s:
        log_count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert log_count == 1


def test_fake_email_sender_rate_limit_keeps_draft_and_logs(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.email_sender = "fake"
    recipient = "john@example.com"
    previous_work_id = _insert_auto_email_candidate(user_id, recipient_hint=recipient)
    work_id = _insert_auto_email_candidate(user_id, recipient_hint=recipient)
    recipient_hash = _email_hash_for_test(recipient)
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(email_send_log).values(
                send_id=uuid.uuid4(),
                work_id=previous_work_id,
                node_id="personal-a",
                owner_id=user_id,
                recipient_hash=recipient_hash,
                subject_hash=_email_hash_for_test("prior subject"),
                body_hash=_email_hash_for_test("prior body"),
                sender_kind="fake",
                status="sent",
                sent_at=now,
            )
        )
        s.commit()
    item = get_agent_work_item(user_id, work_id, settings=settings)
    assert item is not None

    updated = execute_auto_email_send(user_id, item, settings=settings)

    assert updated.state == "draft_for_review"
    assert "email_send_rate_limited" in updated.reason_codes
    with session() as s:
        statuses = [
            row.status
            for row in s.execute(
                select(email_send_log.c.status)
                .where(email_send_log.c.recipient_hash == recipient_hash)
                .order_by(email_send_log.c.sent_at)
            ).all()
        ]
    assert statuses == ["sent", "rate_limited"]


def test_fake_email_sender_blocks_company_and_member(clean, user_id):
    settings = get_settings()
    settings.email_sender = "fake"
    settings.node_kind = "company"
    settings.node_id = "company"
    company_work_id = _insert_auto_email_candidate(user_id)
    company_item = get_agent_work_item(user_id, company_work_id, settings=settings)
    assert company_item is not None
    assert execute_auto_email_send(user_id, company_item, settings=settings).state == (
        "draft_for_review"
    )

    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    member_work_id = _insert_auto_email_candidate(user_id, actor_role="member")
    member_item = get_agent_work_item(user_id, member_work_id, settings=settings)
    assert member_item is not None
    assert execute_auto_email_send(user_id, member_item, settings=settings).state == (
        "draft_for_review"
    )

    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0


def test_fake_email_sender_is_idempotent_for_sent_work(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    settings.email_sender = "fake"
    work_id = _insert_auto_email_candidate(user_id)
    item = get_agent_work_item(user_id, work_id, settings=settings)
    assert item is not None

    first = execute_auto_email_send(user_id, item, settings=settings)
    second = execute_auto_email_send(user_id, item, settings=settings)
    stored = get_agent_work_item(user_id, work_id, settings=settings)

    assert first.state == "resolved"
    assert second.state == "resolved"
    assert stored is not None
    assert stored.state == "resolved"
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 1


def test_policy_memory_summary_is_owner_scoped_on_personal_node(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    other_user = _make_user()
    own_work_id = _insert_review_item(user_id)
    other_work_id = _insert_review_item(other_user)

    own_decided = client.post(
        f"/agent-work/{own_work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve"},
    )
    other_decided = client.post(
        f"/agent-work/{other_work_id}/decision",
        headers={"X-User-Id": str(other_user)},
        json={"decision": "dismiss"},
    )
    own_summary = client.get("/agent-work/policy-memory", headers={"X-User-Id": str(user_id)})
    other_summary = client.get("/agent-work/policy-memory", headers={"X-User-Id": str(other_user)})

    assert own_decided.status_code == 200
    assert other_decided.status_code == 200
    assert own_summary.status_code == 200
    assert other_summary.status_code == 200
    assert own_summary.json()[0]["total"] == 1
    assert own_summary.json()[0]["approvals"] == 1
    assert other_summary.json()[0]["total"] == 1
    assert other_summary.json()[0]["dismissals"] == 1


def test_policy_memory_wiki_summary_writes_company_page(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    for decision in ["approve", "request_more_data"]:
        work_id = _insert_review_item(user_id)
        decided = client.post(
            f"/agent-work/{work_id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"decision": decision, "note": "operator note"},
        )
        assert decided.status_code == 200

    written = client.post(
        "/agent-work/policy-memory/wiki-summary",
        headers={"X-User-Id": str(user_id)},
    )

    assert written.status_code == 200
    body = written.json()
    assert body["page_slug"] == "agent-policy-memory"
    assert body["scope"] == "company"
    assert body["owner_id"] is None
    assert body["bucket_count"] == 1
    assert body["observation_count"] == 2

    page = wiki_store.load_page("agent-policy-memory", root=tmp_path, scope="company")
    assert page is not None
    assert "Used for outcome: false" in page.overview
    assert "data_request|data_gap|draft_for_review|data_gap_suggestion_ready" in page.overview
    assert "approve: 1 (50.0%)" in page.overview
    assert "request_more_data: 1 (50.0%)" in page.overview

    with session() as s:
        audit = s.execute(
            select(audit_log.c.meta).where(
                audit_log.c.node == "agent_work.policy_memory_wiki",
                audit_log.c.phase == "exit",
            )
        ).first()
    assert audit is not None
    assert audit.meta["bucket_count"] == 1
    assert audit.meta["observation_count"] == 2
    assert audit.meta["used_for_outcome"] is False


def test_policy_memory_wiki_summary_is_personal_owner_scoped(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    other_user = _make_user()
    own_work_id = _insert_review_item(user_id)
    other_work_id = _insert_review_item(other_user)

    own_decided = client.post(
        f"/agent-work/{own_work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve"},
    )
    other_decided = client.post(
        f"/agent-work/{other_work_id}/decision",
        headers={"X-User-Id": str(other_user)},
        json={"decision": "dismiss"},
    )
    written = client.post(
        "/agent-work/policy-memory/wiki-summary",
        headers={"X-User-Id": str(user_id)},
    )

    assert own_decided.status_code == 200
    assert other_decided.status_code == 200
    assert written.status_code == 200
    assert written.json()["scope"] == "personal"
    assert written.json()["owner_id"] == str(user_id)
    assert written.json()["observation_count"] == 1

    own_page = wiki_store.load_page(
        "agent-policy-memory",
        root=tmp_path,
        scope="personal",
        owner_id=user_id,
    )
    other_page = wiki_store.load_page(
        "agent-policy-memory",
        root=tmp_path,
        scope="personal",
        owner_id=other_user,
    )
    assert own_page is not None
    assert other_page is None
    assert "approve: 1 (100.0%)" in own_page.overview
    assert "dismiss: 0 (0.0%)" in own_page.overview


def test_agent_work_decision_rejects_auto_execute_state(clean, user_id):
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id="company",
                node_kind="company",
                source_kind="connector_run",
                source_ref_id="run-1",
                action_family="connector_sync",
                title="Configured sync",
                payload={},
                state="auto_execute",
                policy_outcome="auto_execute",
                policy_reason="configured",
                reason_codes=[],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()

    decided = client.post(
        f"/agent-work/{work_id}/decision",
        headers={"X-User-Id": str(user_id)},
        json={"decision": "approve"},
    )

    assert decided.status_code == 409
    assert "auto_execute" in decided.json()["detail"]
    with session() as s:
        stored = s.execute(
            select(agent_work_items.c.state).where(agent_work_items.c.work_id == work_id)
        ).scalar_one()
    assert stored == "auto_execute"


def test_agent_work_decision_requires_node_operator_in_session_mode(clean, user_id):
    item = _sync_ready_gap(user_id)

    def member_user():
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="session",
            display_name="Member",
            email="member@example.com",
            role="member",
            node_id="company",
        )

    app.dependency_overrides[get_current_user] = member_user
    try:
        decided = client.post(
            f"/agent-work/{item['work_id']}/decision",
            json={"decision": "approve"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert decided.status_code == 403
    assert decided.json()["detail"] == "node operator required"


def test_personal_node_agent_work_is_owner_scoped(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    other_user = _make_user()

    own_gap = record_feedback(user_id, "개인 Gmail 답장 규칙")
    other_gap = record_feedback(other_user, "다른 사람 보드 규칙")
    assert own_gap is not None and other_gap is not None
    sync_data_gaps_to_agent_work(user_id)
    sync_data_gaps_to_agent_work(other_user)

    own_items = list_agent_work_items(user_id)
    other_items = list_agent_work_items(other_user)

    assert [item.source_ref_id for item in own_items] == [str(own_gap)]
    assert [item.source_ref_id for item in other_items] == [str(other_gap)]


def test_personal_node_wiki_tasks_are_owner_scoped(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    other_user = _make_user()
    wiki_store.write_task(
        WikiTask(
            slug="own-personal-task",
            kind="open_question",
            description="Own personal wiki question.",
            created_at=datetime(2026, 6, 6, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="personal",
        owner_id=user_id,
        project="company",
    )
    wiki_store.write_task(
        WikiTask(
            slug="other-personal-task",
            kind="open_question",
            description="Other personal wiki question.",
            created_at=datetime(2026, 6, 6, tzinfo=UTC),
            resolved=False,
        ),
        user_id=other_user,
        scope="personal",
        owner_id=other_user,
        project="company",
    )

    sync_wiki_tasks_to_agent_work(user_id)
    sync_wiki_tasks_to_agent_work(other_user)

    own_items = list_agent_work_items(user_id)
    other_items = list_agent_work_items(other_user)

    assert [item.source_ref_id for item in own_items] == ["own-personal-task"]
    assert [item.source_ref_id for item in other_items] == ["other-personal-task"]


def _sync_ready_gap(user_id: uuid.UUID) -> dict:
    gap_id = record_feedback(user_id, f"회사 온보딩 문서 {uuid.uuid4()}")
    assert gap_id is not None
    suggestion_json = (
        '{"target": "회사 Notion 온보딩", "connector": "notion", '
        '"sections": [{"title": "필수 정보", "items": ["권한", "첫 주 업무"]}]}'
    )
    generate_suggestion(user_id, gap_id, chat_model=MockChat(default=suggestion_json))
    synced = client.post(
        "/agent-work/sync/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )
    assert synced.status_code == 200
    item = synced.json()["items"][0]
    assert item["state"] == "draft_for_review"
    return item


def _insert_review_item(user_id: uuid.UUID) -> uuid.UUID:
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=user_id if settings.node_kind == "personal" else None,
                source_kind="data_gap",
                source_ref_id=str(uuid.uuid4()),
                action_family="data_request",
                title="Ready data gap",
                payload={},
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason="data-gap suggestion is ready for owner review",
                reason_codes=["data_gap_suggestion_ready"],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()
    return work_id


def _insert_email_review_item(user_id: uuid.UUID) -> uuid.UUID:
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=user_id if settings.node_kind == "personal" else None,
                source_kind="assistant_command",
                source_ref_id=f"email-command-{work_id}",
                action_family="email_send",
                title="Email draft",
                payload={},
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason=(
                    "email send stays draft until deterministic policy memory "
                    "computes the observed no-edit bucket"
                ),
                reason_codes=[
                    "email_draft_first",
                    "policy_memory_observation_gate_required",
                ],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()
    return work_id


def _insert_auto_email_candidate(
    user_id: uuid.UUID,
    *,
    actor_role: str = "owner",
    recipient_hint: str = "john@example.com",
) -> uuid.UUID:
    settings = get_settings()
    work_id = uuid.uuid4()
    draft = EmailDraftPayload(
        recipient_hint=recipient_hint,
        subject_hint="Re: ready email",
        body_template="검토된 이메일 본문 초안입니다.",
    ).model_dump(mode="json")
    checks = {
        "source_kind_assistant_command": True,
        "base_policy_bucket": True,
        "personal_node": True,
        "operator_role": actor_role in {"owner", "admin"},
        "observation_threshold": True,
        "recipient_present": True,
        "recipient_allowed": True,
        "domain_allowed": True,
        "template_stable": True,
        "rate_limit_ok": True,
        "sensitive_content_absent": True,
        "attachment_absent": True,
    }
    payload = {
        "question": f"{recipient_hint}에게 이메일 초안 만들어줘",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
        "actor_role": actor_role,
        "recipient_hint": recipient_hint,
        "recipient_hash": _email_hash_for_test(recipient_hint),
        "recipient_allowed": True,
        "domain_allowed": True,
        "template_stable": True,
        "rate_limit_ok": True,
        "sensitive_content": False,
        "attachment": False,
        "policy_memory": {
            "bucket_key": _EMAIL_AUTO_SEND_POLICY_BUCKET,
            "total": 20,
            "recent_total": 20,
            "recent_no_edit_approvals": 20,
            "recent_no_edit_approval_rate": 1.0,
            "email_auto_send_observation_threshold_met": True,
            "used_for_outcome": False,
        },
        "email_draft": draft,
        "email_auto_send_gate": {
            "eligible": True,
            "used_for_outcome": False,
            "reason": "preflight_gate",
            "bucket_key": _EMAIL_AUTO_SEND_POLICY_BUCKET,
            "recent_window_days": 60,
            "recent_total": 20,
            "recent_no_edit_approvals": 20,
            "recent_no_edit_approval_rate": 1.0,
            "threshold_met": True,
            "checks": checks,
            "missing_checks": [],
        },
    }
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=user_id if settings.node_kind == "personal" else None,
                source_kind="assistant_command",
                source_ref_id=f"email-ready-{work_id}",
                action_family="email_send",
                title="Email draft ready for auto send",
                payload=payload,
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason="email send draft reached deterministic review threshold",
                reason_codes=[
                    "email_draft_first",
                    "policy_memory_observation_gate_required",
                ],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()
    return work_id


def _email_hash_for_test(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
