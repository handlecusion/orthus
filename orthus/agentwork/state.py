"""Deterministic Agent Work state machine and policy gate.

No LLM call belongs here. The gate classifies typed candidates into the P3
outcome set; later phases may add action runners after separate policy tests.
"""

from __future__ import annotations

from uuid import UUID

from orthus.audit import audit
from orthus.schemas.canonical import (
    AgentWorkCandidate,
    AgentWorkDecision,
    AgentWorkReviewAction,
    AgentWorkState,
    PolicyOutcome,
)

OUTCOME_TO_STATE: dict[PolicyOutcome, AgentWorkState] = {
    "auto_execute": "auto_execute",
    "draft_for_review": "draft_for_review",
    "request_more_data": "request_more_data",
    "reject": "rejected",
}

TERMINAL_STATES: set[AgentWorkState] = {"resolved", "dismissed"}
REVIEWABLE_STATES: set[AgentWorkState] = {"draft_for_review", "request_more_data"}

_REVIEW_TRANSITIONS: dict[AgentWorkReviewAction, AgentWorkState] = {
    "approve": "resolved",
    "dismiss": "dismissed",
    "request_more_data": "request_more_data",
}


def outcome_to_state(outcome: PolicyOutcome) -> AgentWorkState:
    return OUTCOME_TO_STATE[outcome]


def review_transition(from_state: AgentWorkState, action: AgentWorkReviewAction) -> AgentWorkState:
    if from_state not in REVIEWABLE_STATES:
        raise ValueError(f"agent work state cannot be reviewer-decided: {from_state}")
    if from_state == "request_more_data" and action == "request_more_data":
        raise ValueError("agent work item is already requesting more data")
    return _REVIEW_TRANSITIONS[action]


def classify_candidate(
    candidate: AgentWorkCandidate, *, correlation_id: UUID | None = None
) -> tuple[AgentWorkDecision, UUID, UUID]:
    """Classify and audit a candidate. Returns decision, correlation id, run id."""
    with audit("agent_work.classify", correlation_id=correlation_id) as span:
        decision = apply_policy(candidate)
        span.add_meta(
            source_kind=candidate.source_kind,
            action_family=candidate.action_family,
            source_ref_id=candidate.source_ref_id,
        )
        span.set_output(decision.model_dump())
        return decision, span.correlation_id, span.node_run_id


def apply_policy(candidate: AgentWorkCandidate) -> AgentWorkDecision:
    """Pure deterministic policy gate over canonical candidate slots."""
    family = candidate.action_family
    payload = candidate.payload or {}

    if family == "connector_sync":
        if payload.get("run_status") == "failed":
            return _decision(
                "request_more_data",
                "failed connector run needs operator triage before any retry",
                ["connector_run_failed", "operator_triage_required"],
                required_data=["connector auth/config", "redacted error review"],
            )
        if (
            payload.get("secret_state") == "configured"
            and payload.get("node_policy_allows") is True
            and payload.get("actor_role") in {"owner", "admin", "scheduler"}
        ):
            return _decision(
                "auto_execute",
                "connector account configured and node policy allows sync",
                ["connector_configured", "operator_or_scheduler", "no_new_secret"],
            )
        return _decision(
            "request_more_data",
            "connector sync needs account config, fresh auth, or operator authority",
            ["connector_missing_config_or_auth"],
            required_data=["connector config/secret", "operator authority"],
        )

    if family == "promote_review":
        if payload.get("status") != "pending":
            return _decision(
                "reject",
                "promotion stage is not pending",
                ["source_not_pending"],
            )
        if payload.get("source_scope") != "personal":
            return _decision(
                "reject",
                "promotion stage source must be personal scope",
                ["source_scope_not_personal"],
            )
        return _decision(
            "draft_for_review",
            "personal promotion stage requires central reviewer approval before import",
            ["promote_review_required", "no_auto_import"],
            required_review_role="owner",
        )

    if family == "document_draft":
        return _decision(
            "draft_for_review",
            "document draft must be reviewed in editor before publish/import",
            ["editor_review_required", "no_external_write"],
            required_review_role="owner",
        )

    if family == "email_send":
        if not (
            payload.get("recipient_hint")
            or payload.get("recipient_hash")
            or payload.get("recipient_id")
        ):
            return _decision(
                "request_more_data",
                "email draft needs a recipient before review",
                [
                    "email_draft_first",
                    "policy_memory_observation_gate_required",
                    "recipient_required",
                ],
                required_data=["recipient name, address, or thread reference"],
            )
        return _decision(
            "draft_for_review",
            "email send stays draft until deterministic policy memory computes the observed no-edit bucket",
            ["email_draft_first", "policy_memory_observation_gate_required"],
            required_review_role="owner",
        )

    if family == "personal_board_cleanup":
        if (
            payload.get("node_kind") == "personal"
            and payload.get("actor_role") in {"owner", "admin", "scheduler"}
            and payload.get("reversibility") == "reversible"
            and payload.get("external_write") is not True
            and payload.get("delete") is not True
        ):
            return _decision(
                "auto_execute",
                "personal board cleanup is reversible and has no external write",
                ["reversible", "personal_node_only", "no_external_write"],
            )
        return _decision(
            "draft_for_review",
            "board cleanup needs review when it deletes, writes externally, or has ambiguous time",
            ["board_review_required"],
            required_review_role="owner",
        )

    if family == "central_wiki_task_cleanup":
        if payload.get("resolved") is True:
            return _decision(
                "reject",
                "source wiki task is already resolved",
                ["source_task_resolved"],
            )
        if payload.get("cleanup_only") is True and payload.get("company_wiki_write") is not True:
            return _decision(
                "auto_execute",
                "central wiki pre-reflection queue cleanup is reversible",
                ["queue_cleanup_only", "no_company_wiki_write"],
            )
        return _decision(
            "draft_for_review",
            "company wiki knowledge changes require reviewer approval",
            ["company_wiki_review_required"],
            required_review_role="owner",
        )

    if family == "agent_task":
        # Delegated headless agent run dispatched to a team member's collector
        # daemon. The gate is pure code: it never runs the agent, only decides
        # whether the dispatch may auto-execute. Reject if the feature is off or
        # this is not a company node; request more data if anything required for a
        # safe dispatch is missing; auto-execute only for an operator actor with a
        # fully resolved, enrolled assignee.
        allowed_modes = {"code", "knowledge"}
        allowed_runners = {"claude", "codex", "hermes"}
        if payload.get("node_kind") != "company" or payload.get("agent_task_enabled") is not True:
            return _decision(
                "reject",
                "agent_task dispatch requires a company node with agent_task enabled",
                ["agent_task_disabled_or_wrong_node"],
            )
        missing: list[str] = []
        if not str(payload.get("instruction") or "").strip():
            missing.append("instruction")
        if not payload.get("assignee_user_id"):
            missing.append("assignee (unresolved)")
        if not payload.get("assignee_node_id"):
            missing.append("enrolled collector daemon")
        if payload.get("runner") not in allowed_runners:
            missing.append("runner (allowed: claude/codex/hermes)")
        if payload.get("mode") not in allowed_modes:
            missing.append("mode (allowed: code/knowledge)")
        if missing:
            return _decision(
                "request_more_data",
                "agent_task dispatch is missing required fields or an enrolled daemon",
                ["agent_task_dispatch_incomplete"],
                required_data=missing,
            )
        if payload.get("actor_role") not in {"owner", "admin", "scheduler"}:
            return _decision(
                "reject",
                "agent_task dispatch requires owner/admin/scheduler authority",
                ["agent_task_operator_required"],
            )
        # A delegation the LLM *inferred* from inbound mail never auto-executes.
        #
        # Every check above validates FIELDS — instruction present, assignee resolves,
        # daemon enrolled. None of them validates the one judgment that matters: "is this
        # text actually a delegation?" That judgment is made entirely by an LLM over
        # untrusted third-party content (agentwork/delegation.py), and a false positive
        # dispatches a headless agent onto a teammate's machine with full local file
        # access (AGENTS.md agent_task carve-out) — with no human in the loop, because
        # the mail path's actor is "scheduler", a machine.
        #
        # A new adversarial holdout (experiments/fugu-ko/golden/t10_holdout2.json, 24
        # non-delegation traps) fired on 2 of 24 even with the best model: a meeting-
        # minutes action item ("1. 최수민 — 테스트 커버리지 보강") and a self-assignment
        # ("제가 맡겠습니다. 오세훈님은 리뷰만") each extracted a real teammate as the
        # assignee. gpt-4o-mini fired on 7, A.X on 11. Meeting minutes are one of the most
        # common shapes of company mail, so this is not a tail risk.
        #
        # So the deterministic gate stops guessing and asks a human. The candidate still
        # reaches the queue with everything resolved; a reviewer with operator authority
        # approves it and the dispatch runs then (service.execute_reviewed_agent_task).
        # An interactive delegation — an operator typing "김철수님께 X 맡겨줘" — is not
        # affected: the human already stated the intent, so it keeps auto_execute.
        if payload.get("llm_inferred") is True:
            return _decision(
                "draft_for_review",
                "agent_task inferred by an LLM from inbound mail needs a human reviewer "
                "before a headless agent runs on the assignee's machine",
                [
                    "agent_task_llm_inferred",
                    "human_review_required",
                    "company_node_only",
                ],
            )
        return _decision(
            "auto_execute",
            "agent_task ready to dispatch to the assignee's enrolled collector daemon",
            ["agent_task_dispatch_ready", "company_node_only", "operator_or_scheduler"],
        )

    if family == "data_request":
        if payload.get("status") not in {None, "open"}:
            return _decision(
                "reject",
                "source data gap is not open",
                ["source_not_open"],
            )
        if payload.get("suggestion_status") == "ready":
            return _decision(
                "draft_for_review",
                "data-gap suggestion is ready for owner review",
                ["data_gap_suggestion_ready"],
                required_review_role="owner",
            )
        return _decision(
            "request_more_data",
            "data gap needs missing source fields or connector target before resolution",
            ["data_gap_needs_source_detail"],
            required_data=["source target", "missing fields", "connector or editor path"],
        )

    if family == "event_orchestration":
        # Phase 3-B MA.8a — an event-triggered decompose orchestration produced a
        # company-knowledge brief. It performs no external write (knowledge only);
        # it surfaces for the owner to review/act on. Deterministic draft_for_review
        # (불변식 1/9, §P3B E3 — events never auto-approve actions).
        return _decision(
            "draft_for_review",
            "event-triggered knowledge brief requires owner review; no external write",
            ["event_brief_review_required", "no_external_write"],
            required_review_role="owner",
        )

    return _decision(
        "reject",
        "unknown or unsupported action family",
        ["unsupported_action_family"],
    )


def _decision(
    outcome: PolicyOutcome,
    reason: str,
    reason_codes: list[str],
    *,
    required_review_role: str | None = None,
    required_data: list[str] | None = None,
) -> AgentWorkDecision:
    return AgentWorkDecision(
        outcome=outcome,
        state=outcome_to_state(outcome),
        policy_reason=reason,
        reason_codes=reason_codes,
        required_review_role=required_review_role,
        required_data=required_data or [],
    )
