"""P7.5 mail auto-send candidacy evaluator (deterministic, fail-closed).

agentwork owns the bounded policy decision; mail owns transport. This module is
import-cycle safe: it depends only on canonical schemas + settings, never on
`orthus.mail`.

There is intentionally NO code path in this slice that actually auto-sends real
mail. `evaluate_auto_send_candidacy` only produces `MailAutoSendCandidacy`
evidence with `used_for_outcome=False`. Real activation is operator-gated future
P7.5 work that must add a bucket opt-in store, the same policy-memory threshold
(60d / 20 obs / >95% no-edit rate), and the recipient/domain/template/rate-limit
guards before any send.
"""

from __future__ import annotations

from orthus.schemas.canonical import (
    AgentPolicyMemoryContext,
    AgentWorkItem,
    MailAutoSendCandidacy,
)
from orthus.settings import Settings

# Mirror of agentwork.service thresholds; kept local so this module imports no
# service code (avoids an agentwork-internal cycle).
_MIN_RECENT_OBSERVATIONS = 20
_MIN_NO_EDIT_APPROVAL_RATE = 0.95


def evaluate_auto_send_candidacy(
    item: AgentWorkItem,
    settings: Settings,
    *,
    policy_memory: AgentPolicyMemoryContext | None = None,
) -> MailAutoSendCandidacy:
    """Deterministic, fail-closed auto-send candidacy evidence.

    Always returns `eligible=False` in this slice: there is no bucket opt-in store
    yet, so `bucket_opted_in` can never be true. The function still records the
    individual gate signals so the reviewer (and future P7.5 work) can see why a
    draft is not auto-send-eligible. `used_for_outcome` is always False.
    """

    reasons: list[str] = []

    kill_switch_enabled = bool(settings.mail_auto_send_enabled)
    if not kill_switch_enabled:
        reasons.append("auto_send_kill_switch_off")

    # v1: no opt-in store exists, so a bucket can never be opted in.
    bucket_opted_in = False
    reasons.append("no_bucket_opt_in_store")

    threshold_met = _threshold_met(policy_memory)
    if not threshold_met:
        reasons.append("observation_threshold_not_met")

    guards_passed = _guards_passed(item)
    if not guards_passed:
        reasons.append("guards_not_passed")

    # Fail-closed: eligibility requires every signal, and bucket_opted_in is
    # structurally False, so eligible is always False this slice.
    eligible = kill_switch_enabled and bucket_opted_in and threshold_met and guards_passed
    if not eligible and "auto_send_not_eligible" not in reasons:
        reasons.append("auto_send_not_eligible")

    return MailAutoSendCandidacy(
        eligible=eligible,
        kill_switch_enabled=kill_switch_enabled,
        bucket_opted_in=bucket_opted_in,
        threshold_met=threshold_met,
        guards_passed=guards_passed,
        reasons=reasons,
    )


def _threshold_met(policy_memory: AgentPolicyMemoryContext | None) -> bool:
    if policy_memory is None:
        return False
    return (
        policy_memory.recent_total >= _MIN_RECENT_OBSERVATIONS
        and policy_memory.recent_no_edit_approval_rate > _MIN_NO_EDIT_APPROVAL_RATE
    )


def _guards_passed(item: AgentWorkItem) -> bool:
    """Recipient/domain/template/rate-limit/sensitive/attachment guards from the
    existing email_auto_send_gate evidence, if present. Absent gate => not passed."""
    gate = (item.payload or {}).get("email_auto_send_gate")
    if not isinstance(gate, dict):
        return False
    return gate.get("eligible") is True and gate.get("missing_checks") in ([], None)
