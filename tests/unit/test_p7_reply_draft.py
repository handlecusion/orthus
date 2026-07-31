"""P7.1 mail reply draft + P7.5 auto-send candidacy unit tests.

These exercise the pure builder/policy functions without a DB. DB-backed flow
lives in tests/integration/test_p7_reply_flow.py.
"""

from __future__ import annotations

import uuid

from orthus.agentwork.autosend import evaluate_auto_send_candidacy
from orthus.mail.reply import build_reply_candidate
from orthus.agentwork.state import apply_policy
from orthus.schemas.canonical import (
    AgentPolicyMemoryContext,
    AgentWorkItem,
    MailIngestRequest,
)
from orthus.settings import Settings


def _company_settings(**overrides) -> Settings:
    base = dict(
        node_kind="company",
        node_id="company",
        mail_reply_draft_enabled=True,
        mail_auto_send_enabled=False,
        mail_send_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _inbound(**overrides) -> MailIngestRequest:
    base = dict(
        backend="nova",
        owner_addr="owner01@nova.example",
        external_id="ext-1",
        message_id="<msg-1@example.com>",
        direction="inbound",
        from_addr="client@partner.com",
        to_addr=["owner01@nova.example"],
        subject="견적 문의드립니다",
        body_text="안녕하세요, 견적 부탁드립니다." * 200,
    )
    base.update(overrides)
    return MailIngestRequest(**base)


def test_build_reply_candidate_inbound_routable():
    owner = uuid.uuid4()
    candidate = build_reply_candidate(_inbound(), owner, _company_settings())
    assert candidate is not None
    assert candidate.source_kind == "mail_reply"
    assert candidate.source_ref_id == "mail:nova:<msg-1@example.com>"
    assert candidate.action_family == "email_send"
    # recipient_hint is the original sender so the gate yields draft_for_review.
    assert candidate.payload["recipient_hint"] == "client@partner.com"
    ctx = candidate.payload["reply_context"]
    assert ctx["backend"] == "nova"
    assert ctx["from_addr"] == "owner01@nova.example"
    assert ctx["to_addr"] == "client@partner.com"
    assert ctx["subject"].startswith("Re: ")
    # thread excerpt is truncated to 2000 chars (no redaction on the P6 mail path).
    assert len(ctx["thread_excerpt"]) <= 2000


def test_build_reply_candidate_outbound_returns_none():
    owner = uuid.uuid4()
    assert build_reply_candidate(_inbound(direction="outbound"), owner, _company_settings()) is None


def test_build_reply_candidate_flag_off_returns_none():
    owner = uuid.uuid4()
    settings = _company_settings(mail_reply_draft_enabled=False)
    assert build_reply_candidate(_inbound(), owner, settings) is None


def test_build_reply_candidate_non_routable_to_addr_returns_none():
    owner = uuid.uuid4()
    payload = _inbound(to_addr=["someone@gmail.com"], owner_addr="someone@gmail.com")
    assert build_reply_candidate(payload, owner, _company_settings()) is None


def test_build_reply_candidate_personal_node_returns_none():
    owner = uuid.uuid4()
    settings = _company_settings(node_kind="personal", node_id="personal-a")
    assert build_reply_candidate(_inbound(), owner, settings) is None


def test_build_reply_candidate_picks_routable_from_among_recipients():
    owner = uuid.uuid4()
    payload = _inbound(to_addr=["list@gmail.com", "team@acme.example"])
    candidate = build_reply_candidate(payload, owner, _company_settings())
    assert candidate is not None
    assert candidate.payload["reply_context"]["from_addr"] == "team@acme.example"
    assert candidate.payload["reply_context"]["backend"] == "acme"


def test_reply_candidate_gate_is_draft_for_review():
    owner = uuid.uuid4()
    candidate = build_reply_candidate(_inbound(), owner, _company_settings())
    assert candidate is not None
    decision = apply_policy(candidate)
    assert decision.outcome == "draft_for_review"
    assert decision.state == "draft_for_review"


def _reply_item(payload: dict) -> AgentWorkItem:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return AgentWorkItem(
        work_id=uuid.uuid4(),
        node_id="company",
        node_kind="company",
        owner_id=None,
        source_kind="mail_reply",
        source_ref_id="mail:nova:<msg-1@example.com>",
        action_family="email_send",
        title="답장 초안",
        payload=payload,
        state="draft_for_review",
        policy_outcome="draft_for_review",
        created_at=now,
        updated_at=now,
    )


def test_auto_send_candidacy_fail_closed_kill_switch_off():
    item = _reply_item({})
    candidacy = evaluate_auto_send_candidacy(item, _company_settings(mail_auto_send_enabled=False))
    assert candidacy.eligible is False
    assert candidacy.kill_switch_enabled is False
    assert candidacy.bucket_opted_in is False
    assert candidacy.used_for_outcome is False
    assert "auto_send_kill_switch_off" in candidacy.reasons


def test_auto_send_candidacy_fail_closed_even_with_kill_switch_and_threshold():
    # Kill switch on + threshold met + guards present, but no bucket opt-in store
    # exists, so eligibility stays False.
    policy_memory = AgentPolicyMemoryContext(
        bucket_key="x",
        recent_total=30,
        recent_no_edit_approvals=30,
        recent_no_edit_approval_rate=1.0,
    )
    item = _reply_item({"email_auto_send_gate": {"eligible": True, "missing_checks": []}})
    candidacy = evaluate_auto_send_candidacy(
        item,
        _company_settings(mail_auto_send_enabled=True),
        policy_memory=policy_memory,
    )
    assert candidacy.kill_switch_enabled is True
    assert candidacy.threshold_met is True
    assert candidacy.guards_passed is True
    assert candidacy.bucket_opted_in is False
    assert candidacy.eligible is False
    assert candidacy.used_for_outcome is False
    assert "no_bucket_opt_in_store" in candidacy.reasons
