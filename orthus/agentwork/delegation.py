"""Assignee + enrolled-daemon resolution for delegated agent_task work.

A company AgentWork item dispatches an `agent_task` collector command to a team
member's collector daemon. Before the deterministic policy gate can auto-execute
that dispatch it must resolve two things from node-local rows:

- the assignee user (by email or user_id), and
- whether that assignee has an enrolled collector daemon (an active, non-revoked
  collector token on this node that can run commands).

Both lookups are pure reads. They never create rows, never send anything, and
never run an agent — the dispatch itself is a separate explicit step.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from orthus.audit import audit
from orthus.collector.auth import effective_scopes
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_DELEGATION_EXTRACT, get_chat_model_for
from orthus.settings import Settings, get_settings
from orthus.tables import auth_identities, collector_tokens, users

# A daemon may carry the dedicated `agent_task` scope, but pre-existing daemon
# tokens only have `commands` (or `ingest`, which implies `commands`). Either is
# enough to claim/run a queued agent_task command, so both count as enrolled.
_DAEMON_RUN_SCOPES = frozenset({"agent_task", "commands"})

# Mail-side delegation extraction (used by the inbound-mail path only — the /ask
# chat path is deterministic, see parse_chat_delegation below). The LLM only
# EXTRACTS the delegation fields — it never decides execution. The deterministic
# policy gate decides auto_execute vs request_more_data, and the dispatch still
# fails closed unless `agent_task_enabled` is set on both processes. Keeping it
# here (not in orthus.mail) preserves the one-way mail -> agentwork import direction.
_EXTRACT_EXCERPT_MAX = 4000
_ALLOWED_MODES = frozenset({"code", "knowledge"})
_ALLOWED_RUNNERS = frozenset({"claude", "codex", "hermes"})
# Conservative defaults when the text does not name them explicitly: knowledge
# mode is read-only (safest), codex is the default runner.
_DEFAULT_MODE = "knowledge"
_DEFAULT_RUNNER = "codex"

# Deterministic chat-delegation leads (slice 6, /ask path). Chat delegation is
# intake by EXPLICIT prefix only — NO LLM is called on the chat request path.
# A delegation is recognized only when the (stripped) question starts with one of
# these anchored leads (case-insensitive). Anything else falls through to a normal
# /ask answer. The first matching lead in declaration order wins; more-specific
# leads are listed first so e.g. "내 에이전트한테" wins over "에이전트한테".
_CHAT_DELEGATION_LEADS: tuple[str, ...] = (
    "/위임 ",
    "위임:",
    "위임 ",
    "내 에이전트한테",
    "내 에이전트에게",
    "에이전트한테",
    "에이전트에게",
)
# Instruction words that imply a code-editing (write) task. Absent these, chat
# delegation defaults to read-only knowledge mode.
_CHAT_CODE_MODE_TERMS: tuple[str, ...] = ("코드", "파일", "수정", "구현")
# Runner names the instruction may explicitly request; otherwise codex default.
_CHAT_RUNNER_TERMS: tuple[str, ...] = ("claude", "codex", "hermes")
# Optional leading "@<email>" teammate marker placed before the delegation lead.
_CHAT_ASSIGNEE_RE = re.compile(r"^@(?P<email>\S+@\S+)\s+(?P<rest>.*)$", re.DOTALL)
# Optional leading "cwd=<path>" marker (right after the delegation lead) that pins
# the daemon run directory for this one command. Quote the path for spaces
# (cwd="/a b/c"); "~" is expanded daemon-side. Parsed out before mode detection so
# a path containing a code-mode word (코드/파일/...) never flips the mode.
_CHAT_CWD_RE = re.compile(
    r"""^cwd=(?:"([^"]*)"|'([^']*)'|(\S+))(?:\s+(?P<rest>.*))?$""",
    re.DOTALL,
)

_EXTRACT_SYSTEM_PROMPT = (
    "당신은 텍스트에서 '특정 팀원에게 위임할 작업'을 추출하는 분류기다. "
    "텍스트가 누군가에게 작업을 맡기는 위임 지시인지 판단하고, 맞다면 위임 정보를 "
    "JSON으로만 출력한다. 위임 작업이 아니거나 확실하지 않으면 위임하지 않는다. "
    "사실을 지어내지 않는다.\n\n"
    "출력은 JSON 객체 하나뿐이며 다른 텍스트는 출력하지 않는다. 스키마:\n"
    '{"is_delegation": true|false, '
    '"assignee": "<배정 대상 이름 또는 이메일, 없으면 빈 문자열>", '
    '"mode": "code"|"knowledge", '
    '"runner": "claude"|"codex"|"hermes", '
    '"instruction": "<맡길 작업 한국어 한 문장, 없으면 빈 문자열>"}\n'
    "is_delegation이 false면 나머지는 빈 값이어도 된다. mode/runner를 텍스트에서 "
    "확정할 수 없으면 비워 둔다(코드가 보수적 기본값을 채운다). assignee를 "
    "텍스트에서 확정할 수 없으면 빈 문자열로 둔다(호출자가 기본값을 정한다)."
)


def extract_delegation_intent(
    text: str,
    settings: Settings,
    *,
    backend: str | None = None,
    chat_model: ChatModel | None = None,
) -> dict[str, str] | None:
    """LLM-extract a delegation intent from free text. Returns the fields or None.

    Returns ``{"assignee", "mode", "runner", "instruction"}`` when the text is a
    delegation request, or None when it is not / is uncertain / extraction fails.
    ``assignee`` may be an empty string when the text does not name one — the
    caller decides the default (mail = treat as "not a delegation"; chat = self).
    ``instruction`` is always non-empty in a returned dict. mode/runner fall back
    to conservative defaults when not stated. The LLM never decides execution.
    """
    excerpt = (text or "").strip()[:_EXTRACT_EXCERPT_MAX]
    if not excerpt:
        return None
    try:
        with audit("agent_work.delegation_extract") as span:
            span.add_meta(backend=backend, has_excerpt=bool(excerpt))
            # EXAONE, not the primary. A false positive here dispatches a headless agent
            # onto a teammate's machine with full local file access, so the metric is the
            # false-positive count, not accuracy: on 12 non-delegation traps EXAONE fired
            # 0 times, Solar 1, A.X 4 (it read the sender's OWN plan as a delegation).
            #
            # That 0 was in-sample — the same golden that picked EXAONE. A later adversarial
            # holdout (experiments/fugu-ko/golden/t10_holdout2.json, 24 traps) fires it on 2:
            # a meeting-minutes action item and a self-assignment, both naming a real
            # teammate (Solar 6, gpt-4o-mini 7, A.X 11). The ranking holds; the zero does not.
            # Two deterministic guards stand behind this call, not one model:
            #   - mail/delegation_prefilter.py drops machine-shaped mail before we ask at all
            #   - state.py routes what remains to draft_for_review, never auto_execute
            chat = chat_model or get_chat_model_for(TASK_DELEGATION_EXTRACT)
            # F1 glue: k-way self-consistency (ORTHUS_DELEGATION_SC_K, default 1 =
            # single call, byte-identical to pre-F1). The flaky failure mode this
            # targets is a one-off miss on a true delegation (vendor rerun
            # variance); strict-majority voting also suppresses one-off false
            # positives, so it cannot loosen the FP posture. Aggregation is
            # deterministic: strict majority on is_delegation, then per-field
            # most-common among the delegation votes (tie -> earliest call).
            sc_k = _sc_k()
            votes: list[dict[str, Any]] = []
            for _ in range(sc_k):
                completion = (
                    chat.complete(_EXTRACT_SYSTEM_PROMPT, excerpt, json_only=True) or ""
                ).strip()
                one = _parse_extraction(completion)
                if one is not None:
                    votes.append(one)
            parsed = _majority_extraction(votes, sc_k)
            span.add_meta(sc_k=sc_k, sc_votes=len(votes))
            span.set_output({"is_delegation": bool(parsed) and parsed.get("is_delegation") is True})
    except Exception:  # noqa: BLE001 — any LLM/parse failure means "do not delegate"
        return None

    if not parsed or parsed.get("is_delegation") is not True:
        return None
    instruction = str(parsed.get("instruction") or "").strip()
    if not instruction:
        return None
    mode = str(parsed.get("mode") or "").strip().lower()
    if mode not in _ALLOWED_MODES:
        mode = _DEFAULT_MODE
    runner = str(parsed.get("runner") or "").strip().lower()
    if runner not in _ALLOWED_RUNNERS:
        runner = _DEFAULT_RUNNER
    return {
        "assignee": str(parsed.get("assignee") or "").strip(),
        "mode": mode,
        "runner": runner,
        "instruction": instruction,
    }


def _sc_k() -> int:
    """F1 self-consistency fan-out; 1 (default) = single call, exact legacy path."""
    try:
        k = int(os.environ.get("ORTHUS_DELEGATION_SC_K", "1"))
    except ValueError:
        return 1
    return min(max(k, 1), 5)


def _majority_extraction(votes: list[dict[str, Any]], k: int) -> dict[str, Any] | None:
    """Deterministic strict-majority aggregation over k extraction attempts.

    is_delegation needs > k/2 True votes; fields take the most-common value
    among the True votes (tie -> earliest vote). k=1 degrades to the single
    parsed dict, preserving pre-F1 behavior bit-for-bit.
    """
    if not votes:
        return None
    if k <= 1:
        return votes[0]
    yes = [v for v in votes if v.get("is_delegation") is True]
    if len(yes) * 2 <= k:
        return {"is_delegation": False}
    merged: dict[str, Any] = {"is_delegation": True}
    for field in ("assignee", "mode", "runner", "instruction"):
        vals = [str(v.get(field) or "").strip() for v in yes]
        counts: dict[str, int] = {}
        for val in vals:
            counts[val] = counts.get(val, 0) + 1
        best = max(counts.values())
        merged[field] = next(v for v in vals if counts[v] == best)
    return merged


def _parse_extraction(completion: str) -> dict[str, Any] | None:
    if not completion:
        return None
    try:
        data = json.loads(completion)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_chat_delegation(question: str) -> dict[str, str | None] | None:
    """Deterministically parse a chat (/ask) delegation request. NO LLM call.

    Recognizes a delegation only when the stripped question begins with an
    explicit lead (`위임:`, `위임 `, `/위임 `, `내 에이전트한테/에게`, `에이전트한테/에게`),
    optionally prefixed with an `@<email>` teammate marker. Returns

        {"assignee", "mode", "runner", "instruction", "cwd"}

    or None when the text is not an explicit delegation / has no instruction.
    ``assignee`` is the `@email` when present, else None (the caller defaults it
    to self). ``mode`` is "code" when the instruction names a code-editing word,
    else "knowledge". ``runner`` is an explicitly named runner, else "codex".
    ``cwd`` is the `cwd=<path>` directory when given right after the lead, else
    None (the daemon then falls back to ORTHUS_AGENT_TASK_WORKSPACE). Pure string
    parsing — never calls the LLM, the DB, or any external service.
    """
    stripped = (question or "").strip()
    if not stripped:
        return None

    assignee: str | None = None
    assignee_match = _CHAT_ASSIGNEE_RE.match(stripped)
    if assignee_match is not None:
        assignee = assignee_match.group("email").strip() or None
        stripped = assignee_match.group("rest").strip()
        if not stripped:
            return None

    lowered = stripped.lower()
    matched_lead: str | None = None
    for lead in _CHAT_DELEGATION_LEADS:
        if lowered.startswith(lead.lower()):
            matched_lead = lead
            break
    if matched_lead is None:
        return None

    instruction = stripped[len(matched_lead) :].strip()
    if not instruction:
        return None

    cwd: str | None = None
    cwd_match = _CHAT_CWD_RE.match(instruction)
    if cwd_match is not None:
        cwd = (cwd_match.group(1) or cwd_match.group(2) or cwd_match.group(3) or "").strip() or None
        instruction = (cwd_match.group("rest") or "").strip()
        if not instruction:
            return None

    instruction_lower = instruction.lower()
    mode = "code" if any(term in instruction for term in _CHAT_CODE_MODE_TERMS) else "knowledge"
    runner = _DEFAULT_RUNNER
    for candidate in _CHAT_RUNNER_TERMS:
        if candidate in instruction_lower:
            runner = candidate
            break

    return {
        "assignee": assignee,
        "mode": mode,
        "runner": runner,
        "instruction": instruction,
        "cwd": cwd,
    }


def resolve_assignee(email_or_id: str) -> UUID | None:
    """Resolve a user_id from a raw user_id string or a login email.

    Tries user_id first (exact match), then a verified auth identity email
    (case-insensitive). Returns None when neither matches.
    """
    value = (email_or_id or "").strip()
    if not value:
        return None

    try:
        candidate = UUID(value)
    except ValueError:
        candidate = None
    if candidate is not None:
        with session() as s:
            row = s.execute(select(users.c.user_id).where(users.c.user_id == candidate)).first()
        if row is not None:
            return row.user_id

    lowered = value.lower()
    with session() as s:
        row = s.execute(
            select(auth_identities.c.user_id)
            .where(func.lower(auth_identities.c.email) == lowered)
            .order_by(auth_identities.c.created_at.desc())
        ).first()
    return row.user_id if row is not None else None


def resolve_enrolled_daemon(
    user_id: UUID,
    *,
    device_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, str] | None:
    """Resolve the assignee's enrolled collector daemon as (node_id, device_id).

    Enrolled = an active (not revoked) collector token on this node whose
    effective scopes allow running commands. ``device_id`` in the returned tuple
    is the matched token's device_id ("" for a legacy deviceless token).

    When ``device_id`` is a non-empty string, only a run-scope token whose own
    device_id equals it qualifies (this is how a device the assignee does not own
    is rejected). When ``device_id`` is None/empty, the most recently created
    run-scope token wins (today's behavior). Returns None when nothing qualifies.
    """
    settings = settings or get_settings()
    requested_device = (device_id or "").strip()
    with session() as s:
        rows = s.execute(
            select(
                collector_tokens.c.node_id,
                collector_tokens.c.scopes,
                collector_tokens.c.device_id,
            )
            .where(
                collector_tokens.c.user_id == user_id,
                collector_tokens.c.node_id == settings.node_id,
                collector_tokens.c.revoked_at.is_(None),
            )
            .order_by(collector_tokens.c.created_at.desc())
        ).all()
    for row in rows:
        scopes = effective_scopes(frozenset(row.scopes or []))
        if not (scopes & _DAEMON_RUN_SCOPES):
            continue
        token_device = row.device_id or ""
        if requested_device:
            if token_device == requested_device:
                return (row.node_id, token_device)
            continue
        return (row.node_id, token_device)
    return None
