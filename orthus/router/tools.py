"""Decompose tool registry: deterministic dispatcher + 7 named adapters.

`classify_subpart` is the ONLY entry point for routing a sub-question to a leaf.
Named adapters are a closed registry — dynamic addition and off-list calls are
forbidden (불변식 9). LLM function-call advertising is not used here; the
dispatcher is purely deterministic code (docs/company-agent-orchestration.md §6/§7).
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.router.route import classify_intent
from orthus.schemas.canonical import MailIngestRequest, MailReplyContext, ReplyDraftResult
from orthus.settings import Settings, get_settings

# Closed set of leaf names — off-list raise ValueError (불변식 9)
Leaf = Literal["action-intake", "structured", "graph", "wiki"]

# Phase 3-B MA.8a — when set, classify_subpart never routes a sub-question to the
# action-intake leaf (it routes action-shaped sub-questions as knowledge instead).
# This is how the event-orchestration worker keeps its sink read-only/knowledge-only
# (§P3B E3 / 불변식 29): an inbound mail must never spawn or auto-execute action
# AgentWork items. A ContextVar (not a param) so it propagates through fan_out's
# `copy_context().run(leaf_fn)` into the leaf threads (불변식 15) with no signature churn.
_suppress_action_intake: ContextVar[bool] = ContextVar("_suppress_action_intake", default=False)


@contextlib.contextmanager
def knowledge_only_leaves():
    """Within this block, classify_subpart suppresses the action-intake leaf."""
    token = _suppress_action_intake.set(True)
    try:
        yield
    finally:
        _suppress_action_intake.reset(token)


_VALID_LEAVES: frozenset[str] = frozenset({"action-intake", "structured", "graph", "wiki"})

# Named adapter keys — used to validate calls from fan_out
_VALID_ADAPTERS: frozenset[str] = frozenset(
    {
        "split",
        "query_wiki",
        "query_structured",
        "query_graph",
        "detect_gap",
        "queue_agent_work",
        "create_reply_draft",
    }
)


def classify_subpart(sub_q: str, *, command_split: bool = False) -> tuple[Leaf, str | None]:
    """Deterministic dispatcher: map a sub-question to (leaf, action_family | None).

    Single entry: classify_intent() unifies the old detect()+classify() two-step into one
    decision (deterministic fast-path → one LLM enum for the ambiguous middle only). When the
    knowledge-only worker suppresses action-intake (MA.8a) we pass allow_commands=False so an
    action-shaped sub-question is routed as a READ rather than spawning an AgentWork item.
    Off-list leaf names raise ValueError — this must never reach an unknown leaf.

    `command_split=True` (S3) forwards keyword_only so a command fragment's family is the
    deterministic keyword verdict only (never an LLM-enum guess) — 불변식 14.

    KNOWN GAP (intended cost of 불변식 14, documented in docs/command-split-hold-followup.md
    §S3): under command_split a command clause that `_detect_command_family` does NOT match by
    keyword is demoted to a READ leaf (not queued as an action). Reviving the LLM-enum family
    for these is forbidden (it would queue a hallucinated/wrong family); the real fix is
    widening keyword coverage in a follow-up slice.
    """
    allow_commands = not _suppress_action_intake.get()
    intent = classify_intent(sub_q, allow_commands=allow_commands, keyword_only=command_split)
    if intent.command_family is not None:
        return ("action-intake", intent.command_family)
    leaf: Leaf = intent.route  # Route = Literal["structured", "wiki", "graph"] — valid leaves
    if leaf not in _VALID_LEAVES:
        raise ValueError(f"classify_subpart: off-list leaf '{leaf}' for sub_q={sub_q!r}")
    return (leaf, None)


# ---------------------------------------------------------------------------
# Named adapters — thin wrappers around the existing backends.
# Each adapter is 1:1 with a gate; callers must use these names only.
# ---------------------------------------------------------------------------


def query_wiki(
    user_id: UUID,
    text: str,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None = None,
    page_slugs: list[str] | None = None,
):
    """Named adapter for wiki leaf — compiled wiki grounding, no gap persistence."""
    from orthus.wiki.qa import ask

    return ask(
        user_id,
        text,
        scope=scope,
        project=project,
        chat_model=chat_model,
        learn=False,
        record_gaps=False,
        context_wiki_slug=context_wiki_slug,
    )


def query_structured(
    user_id: UUID,
    text: str,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
):
    """Named adapter for structured leaf — SELECT-only validation gate."""
    from orthus.structured.query import query_structured as _qs

    return _qs(user_id, text, scope=scope, project=project, chat_model=chat_model)


def query_graph(
    user_id: UUID,
    text: str,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None = None,
):
    """Named adapter for graph leaf — K4b try_graph_answer, fail-open demote."""
    from orthus.router.graph import try_graph_answer

    return try_graph_answer(
        user_id,
        text,
        scope=scope,
        project=project,
        chat_model=chat_model,
        context_wiki_slug=context_wiki_slug,
    )


def detect_gap_from_sub_answer(sub_answer_routed) -> None:
    """Named adapter for gap detection at synthesis stage — collect, do not persist.

    The leaf wiki answer already has result.gap set (detect_gap ran inside
    answer_from_hits with record_gaps=False). This adapter is a passthrough for
    the synthesis stage traversal; no additional DB write is performed (불변식 5/6).
    """
    # Gap is already on the WikiAnswer.gap field from the leaf execution.
    # This adapter exists as a named registry entry to satisfy the 7-adapter
    # contract; actual gap collection happens in synthesize() by reading
    # sub_answer.routed.wiki.gap.


def queue_agent_work(
    user_id: UUID,
    question: str,
    *,
    actor_role: str,
    action_family: str | None = None,
    context_wiki_slug: str | None = None,
    upstream_context: str | None = None,
    chat_session_id: UUID | None = None,
    leaf_index: int | None = None,
    command_split_origin: bool = False,
    intake_source: str | None = None,
):
    """Named adapter for action-intake leaf — P3 policy gate → AgentWork candidate.

    Queuing only; no auto-execution from this layer. The policy gate inside
    create_assistant_command_work_item decides auto_execute vs draft_for_review
    vs request_more_data vs reject (불변식 1/9). `upstream_context` (Phase 2 §P2.4)
    is the redacted read→act context from upstream sub-answers; the intake stores
    it under payload.context.upstream (redaction re-applied there).

    command-split (S2): `chat_session_id` links the queued item to the chat; `leaf_index`
    is the fragment's position (enters the idempotency key only for external-write families);
    `command_split_origin=True` holds any auto-execute item for review instead of executing
    (BLOCKER C); `intake_source` marks the fan-out origin in the payload.
    """
    # Defense-in-depth (review pass 2 #5): even if classify_subpart's suppression were
    # bypassed (a future fan_out refactor that loses the ContextVar, or a caller that
    # reaches this adapter another way), the knowledge-only path must NEVER create an
    # action AgentWork item from inbound mail (§P3B E3 / 불변식 29). Guard at the actual
    # creation chokepoint. NOTE: the event-orchestration worker also keeps
    # allow_agentic_in_leaf=False so the inline-agentic engine (which does not route
    # through this adapter) cannot create items either.
    if _suppress_action_intake.get():
        return None
    from orthus.agentwork.service import create_assistant_command_work_item

    return create_assistant_command_work_item(
        user_id,
        question,
        actor_role=actor_role,
        context_wiki_slug=context_wiki_slug,
        upstream_context=upstream_context,
        action_family=action_family,
        chat_session_id=chat_session_id,
        leaf_index=leaf_index,
        command_split_origin=command_split_origin,
        intake_source=intake_source,
    )


def create_reply_draft(
    owner_user_id: UUID,
    reply_context: MailIngestRequest | None,
    *,
    owner_scoped: bool,
    settings: Settings | None = None,
) -> ReplyDraftResult:
    """Named adapter for email reply-draft — P7.1 build_reply_candidate wrapper (MA.3).

    Caller-supplied context (docs/company-agent-orchestration.md §6/§7, Option B):
    `reply_context` is the already-resolved inbound MailIngestRequest. This adapter
    does NOT resolve a mail id from storage — the decompose action-intake leaf or the
    mail-ingest event trigger that owns the inbound mail passes the request in. The
    primary `/ask` path is still queue_agent_work; this is only for a clearly-present
    reply_context.

    `owner_scoped` is REQUIRED (no default): for an individual mailbox the reply draft
    must be owner-bound so only that employee sees it, and for a shared mailbox it is
    company-visible. The ingest path derives this from the mail's resolved scope
    (personal→True); the caller must pass the same boundary here. Defaulting it would
    fail-open an owner-only mailbox reply to company-wide visibility
    (docs/p6-mail-individual-scope.md).

    Queuing only — never sends (불변식 9). The persisted payload is the typed P7.1
    reply candidate (MailReplyContext-validated at this boundary). Wrapped in an audit
    span. `required_data` lists only what the CALLER can supply: a flag-off or
    non-routable cause yields an empty list (nothing the caller provides fixes it);
    the precise diagnostic is on `reason`.
    """
    from orthus.mail.reply import build_reply_candidate

    settings = settings or get_settings()
    with audit("router.create_reply_draft") as span:
        if reply_context is None:
            # reply_context 미지정 → request_more_data (MA.3 gate)
            span.add_meta(outcome="request_more_data", reason="no_reply_context")
            return ReplyDraftResult(
                outcome="request_more_data",
                required_data=["reply_context"],
                reason="no_reply_context",
            )

        candidate = build_reply_candidate(reply_context, owner_user_id, settings)
        if candidate is None:
            # outbound / flag off / non-company node / no routable company recipient —
            # not a missing-input the caller can supply, so required_data stays empty.
            span.add_meta(outcome="request_more_data", reason="no_routable_reply")
            return ReplyDraftResult(outcome="request_more_data", reason="no_routable_reply")

        # MA.3 typed payload validation at the tool boundary. build_reply_candidate
        # already builds reply_context typed, but re-validate so a malformed payload
        # never reaches the policy gate.
        MailReplyContext.model_validate(candidate.payload["reply_context"])

        from orthus.agentwork.service import persist_reply_candidate

        item = persist_reply_candidate(
            owner_user_id, candidate, settings=settings, owner_scoped=owner_scoped
        )
        if item is None:
            span.add_meta(outcome="request_more_data", reason="persist_skipped")
            return ReplyDraftResult(outcome="request_more_data", reason="persist_skipped")
        span.add_meta(outcome="draft_for_review", work_id=str(item.work_id))
        return ReplyDraftResult(outcome="draft_for_review", item=item)
