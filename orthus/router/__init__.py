"""Unified entrypoint for the 비서 (P2.2b, docs/architecture-v2.md §1/§7).

`answer()` classifies the question, dispatches to the structured(PG) or wiki
backend, and wraps the result in the canonical `RoutedAnswer` envelope. One
`router.answer` audit span carries the chosen mode.

Priority: decompose > agentic > legacy  (docs/company-agent-orchestration.md §3)
- decompose: ORTHUS_ASK_DECOMPOSE_ENABLED + not should_federate(scope)
- agentic:   decompose off OR should_federate(scope)=True일 때만 독립 동작
- legacy:    agentic/decompose 둘 다 미진입"""

from __future__ import annotations

from uuid import UUID

from orthus.audit import audit
from orthus.federation.query import (
    federated_structured_answer,
    federated_wiki_answer,
    should_federate,
)
from orthus.models.base import ChatModel
from orthus.models.registry import get_agent_chat_model
from orthus.router.agentic import run_agentic_answer
from orthus.router.cache import (
    cache_lookup,
    cache_store,
    is_cacheable_result,
    knowledge_watermark,
)
from orthus.router.graph import _CONFLICT_VIEW_UNAVAILABLE, try_graph_answer
from orthus.router.route import Route, classify
from orthus.schemas.canonical import ChatTurn, RoutedAnswer
from orthus.settings import get_settings
from orthus.structured.query import query_structured
from orthus.wiki.qa import ask

__all__ = ["answer", "classify", "resolve_answer_scope"]


def resolve_answer_scope(requested_scope: str | None) -> str:
    """Resolve the effective answer scope for the current node.

    Personal node: honor the requested scope (default 'all') unchanged.

    Company (central) node: company-only by default. With owner-scope enabled
    (P8.1), an explicit request for 'all' or 'personal' is honored so a logged-in
    user can merge their own personal rows with company knowledge; the existing
    `retrieve()` / `query_structured()` scope filters still keep that to company +
    own-personal. A blank/no-scope request stays 'company' even with the flag on.
    `should_federate()` returns False on the company node, so 'all' here resolves
    inside the single central runtime rather than fanning out to federation.

    Shared by the search-only `/ask` endpoint and the agent-work orchestrate chat.
    """
    settings = get_settings()
    if settings.node_kind == "company":
        if settings.owner_scope_enabled and requested_scope in {"all", "personal"}:
            return requested_scope
        return "company"
    return requested_scope or "all"


# Shown to the asker (WikiView renders wiki.warnings verbatim) in place of raw internal
# gate tokens on a degraded-KG demote. The precise token (kg_unavailable, driver_error:...,
# mapping_error:..., timeout, internal_error) is operator/telemetry signal and stays on the
# audit span / fallback_reason only — it must not leak into the asker-facing banner.
_GRAPH_DEGRADED_MESSAGE = "그래프 조회를 일시적으로 사용할 수 없어 위키 답변으로 대체했습니다."


def _phase_publisher(user_id: UUID, stream_id: str | None):
    """Tier i: best-effort phase-frame publisher for the orchestrate live SSE.

    Returns None when no stream is attached (`/ask` search-only never sets one, and
    decompose fan-out leaves call answer() without a stream_id, so leaves never emit
    duplicate phase frames). The returned callback publishes
    ``{"type": "phase", "stage": ..., **extra}`` onto the same `{user_id}:{stream_id}`
    channel the decompose/agentic frames use, and NEVER raises — stream feedback must
    not affect the answer (orthus/agentwork/stream.py contract)."""
    if stream_id is None:
        return None
    stream_key = f"{user_id}:{stream_id}"

    def _publish(stage: str, **extra: object) -> None:
        try:
            from orthus.agentwork import stream as agent_stream

            agent_stream.publish_threadsafe(stream_key, {"type": "phase", "stage": stage, **extra})
        except Exception:  # noqa: BLE001 — 진행 표시 실패가 답변에 영향을 주면 안 된다
            pass

    return _publish


def answer(
    user_id: UUID,
    question: str,
    *,
    scope: str = "all",
    project: str | None = None,
    chat_model: ChatModel | None = None,
    context_wiki_slug: str | None = None,
    context_mail_id: str | None = None,
    history: list[ChatTurn] | None = None,
    stream_id: str | None = None,
    allow_agentic: bool = True,
    allow_decompose: bool = True,
    allow_cache: bool = True,
    learn: bool = True,
    record_gaps: bool = True,
    route: Route | None = None,
    actor_role: str | None = None,
    auth_mode: str | None = None,
    chat_session_id: UUID | None = None,
) -> RoutedAnswer:
    """Route, with the Phase 3-A semantic answer cache wrapped around the ladder.

    Consults the cache (MA.7a, docs/company-agent-orchestration.md §P3A) ONLY for
    company-scope, non-federated, opted-in, non-token requests and replays a previously
    grounded answer when the company wiki knowledge is unchanged (불변식 22/24). The cache
    is not a grounding bypass — it replays an already-compiled-wiki-grounded RoutedAnswer.
    With ORTHUS_ASK_SEMANTIC_CACHE_ENABLED off both hooks are no-ops and output is
    byte-identical to the pre-cache ladder (불변식 27).

    `allow_cache=False` (knowledge-token callers, same `not is_token` pattern as
    allow_agentic/allow_decompose) forces a full recompute with no store.
    """
    settings = get_settings()
    # §P3A.2: company-scope + non-federated + opted-in + non-token only. Cheap partition
    # checks first so non-company answers never even read the flag. The cache key is
    # (question, scope, project) only — so requests whose grounding depends on extra
    # request-scoped inputs are NOT cached: a wiki-page context (context_wiki_slug), a mail
    # context (context_mail_id, MA.3b), a conversation history, or a forced backend route
    # (route) would otherwise let an answer grounded/served for one input be replayed for a
    # different one (e.g. a wiki-forced answer served for a later structured-forced request).
    cache_on = (
        allow_cache
        and scope == "company"
        and not should_federate(scope)
        and context_wiki_slug is None
        and context_mail_id is None
        and not history
        and route is None
        and settings.ask_semantic_cache_enabled
    )
    store_watermark: str | None = None
    if cache_on:
        cached = cache_lookup(question, scope=scope, project=project)
        if cached is not None:
            # Hit accounting lives on the ask.cache(result=hit) span emitted inside
            # cache_lookup — we deliberately do NOT emit a router.answer span here. That span
            # is already non-uniform across the ladder (decompose/agentic priority paths return
            # before the legacy _route_answer tail that owns it), so adding it only on hits would
            # be a partial band-aid plus an audit_log write on the fast replay path. Served-mode
            # dashboards sum ask.cache hits + router.answer misses (docs §P3A.9).
            #
            # F4: a hit bypasses the agentic/decompose engine that an open
            # GET /ask/{stream_id}/stream expects to emit a terminal frame, so that SSE would
            # hang until keepalive/disconnect. Publish the terminal frame ourselves so the live
            # bubble closes immediately. Best-effort: publish_threadsafe is a no-op with no loop/
            # subscriber and never raises (feedback-only). Lazy import avoids an import cycle.
            if stream_id is not None:
                from orthus.agentwork import stream as agent_stream

                agent_stream.publish_threadsafe(f"{user_id}:{stream_id}", {"type": "turn_complete"})
            return cached
        # F1: snapshot the knowledge watermark BEFORE grounding. A store-time read would let a
        # wiki edit landing in the answer-compute window (seconds, incl. the LLM call) stamp the
        # row with a watermark NEWER than the knowledge it was grounded on, making the stale
        # answer HIT (defeating 불변식 24). Stamping the pre-grounding snapshot only ever causes a
        # future MISS (conservative recompute) if knowledge changed mid-compute, never a stale HIT.
        store_watermark = knowledge_watermark(scope, project)
    routed = _route_answer(
        user_id,
        question,
        scope=scope,
        project=project,
        chat_model=chat_model,
        context_wiki_slug=context_wiki_slug,
        context_mail_id=context_mail_id,
        history=history,
        stream_id=stream_id,
        allow_agentic=allow_agentic,
        allow_decompose=allow_decompose,
        learn=learn,
        record_gaps=record_gaps,
        route=route,
        actor_role=actor_role,
        auth_mode=auth_mode,
        chat_session_id=chat_session_id,
    )
    if cache_on and is_cacheable_result(routed) and not _is_transient_graph_demote(routed):
        cache_store(question, routed, scope=scope, project=project, watermark=store_watermark)
    return routed


# F2b — wiki-빈근거 폴백의 결정론 전제 어휘 (f2-wiki-empty-recover-prereg.md §5).
# v1(어휘 조건 없음)은 실측 기각: fixture에서 지식 질문도 근거 0 공답이 흔해
# 파손 15건이 났다. 집계·열거 신호가 있는 질문만 폴백 후보로 연다.
_AGG_SIGNAL_TERMS: tuple[str, ...] = (
    "세어",
    "세줘",
    "세 줘",
    "건수",
    "행 수",
    "항목 수",
    "개수",
    "집계",
    "몇 곳",
    "몇 개",
    "몇 건",
)
_AGG_SHOW_TERM = "보여줘"
_AGG_SHOW_QUALIFIERS: tuple[str, ...] = ("포함된", "항목")


def _has_aggregate_signal(question: str) -> bool:
    q = question or ""
    if any(t in q for t in _AGG_SIGNAL_TERMS):
        return True
    return _AGG_SHOW_TERM in q and any(t in q for t in _AGG_SHOW_QUALIFIERS)


def _is_transient_graph_demote(routed: RoutedAnswer) -> bool:
    """True when a graph question demoted to a wiki answer because the KG was unavailable
    (the answer carries the degraded-KG banner, or the K8.3 conflict-unavailable token).

    That state is transient — caching it would freeze a 'graph temporarily unavailable' /
    '모순 확인 불가' answer past KG recovery (the D6 silence-as-all-clear failure the banner
    exists to prevent), because the wiki_pages watermark does not track KG availability."""
    return (
        _GRAPH_DEGRADED_MESSAGE in routed.warnings or _CONFLICT_VIEW_UNAVAILABLE in routed.warnings
    )


def _route_answer(
    user_id: UUID,
    question: str,
    *,
    scope: str = "all",
    project: str | None = None,
    chat_model: ChatModel | None = None,
    context_wiki_slug: str | None = None,
    context_mail_id: str | None = None,
    history: list[ChatTurn] | None = None,
    stream_id: str | None = None,
    allow_agentic: bool = True,
    allow_decompose: bool = True,
    learn: bool = True,
    record_gaps: bool = True,
    route: Route | None = None,
    actor_role: str | None = None,
    auth_mode: str | None = None,
    chat_session_id: UUID | None = None,
) -> RoutedAnswer:
    """Route the question to a backend and return a unified envelope.

    `scope` ('all' | 'company' | 'personal') is forwarded to whichever backend
    runs. `project` (None = all projects) narrows the answer to a single
    company→project bucket on both backends (P2). The wiki path keeps `learn=True`
    (default) so T2 compounding still fires; the structured path has no learning step.

    Priority: decompose > agentic > legacy (docs/company-agent-orchestration.md §3).
    - `allow_decompose=False`: skips decompose — set on leaf calls to fix depth at 1.
    - `allow_agentic=False`: forces legacy ladder — set by knowledge-token callers so
      the orthus-mcp `wiki_ask` tool can NEVER re-enter the cli agent and recurse.
    - `learn=False` / `record_gaps=False`: suppress T2 writes and gap persistence on
      leaf sub-question calls inside the decompose fan-out (불변식 5).
    """
    settings = get_settings()

    # Priority 1: decompose (compound question split + parallel fan-out)
    # Guarded: flag on + not federated + allow_decompose (depth-1 recursion guard)
    if allow_decompose and settings.ask_decompose_enabled and not should_federate(scope):
        from orthus.auth import MANAGER_ROLES
        from orthus.router.decompose import answer_or_decompose, command_split_signal
        from orthus.settings import command_split_active

        # command_split (BLOCKER F/G): a command fragment is escalated to a typed action only
        # for a verified session owner/admin on a compound (connective/enum) input, and only
        # when the command-split feature is active. Non-session / non-operator / missing role
        # degrades to False (byte-identical to the pre-command-split decompose path).
        cmd_split = (
            command_split_active(settings)
            and command_split_signal(question)
            and actor_role in MANAGER_ROLES
            and auth_mode == "session"
        )
        # Strict flag-off byte-identical: the pre-command-split decompose path always stamped
        # queued action-intake leaf items with actor_role="owner". Preserve that literal on the
        # non-command-split path (flag off, or a pure-question decompose) so no policy-gate input
        # changes outside the feature; only a real command-split fan-out uses the caller's role.
        leaf_actor_role = actor_role if cmd_split else "owner"
        return answer_or_decompose(
            user_id,
            question,
            scope=scope,
            project=project,
            chat_model=chat_model,
            context_wiki_slug=context_wiki_slug,
            history=history,
            stream_id=stream_id,
            actor_role=leaf_actor_role,
            allow_agentic_in_leaf=settings.agentic_ask_enabled and allow_agentic,
            learn=learn,
            record_gaps=record_gaps,
            context_mail_id=context_mail_id,
            command_split=cmd_split,
            chat_session_id=chat_session_id,
        )

    # Priority 2: inline agentic loop
    if allow_agentic and settings.agentic_ask_enabled and not should_federate(scope):
        agent_chat = get_agent_chat_model()
        if agent_chat is not None:
            return run_agentic_answer(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
                stream_id=stream_id,
                agent_chat=agent_chat,
            )
    # Tier i: legacy-ladder phase frames (검색 중 → 근거 N개 → 답변 작성 중). None without a
    # stream, so the /ask search path and decompose leaves stay byte-identical.
    on_phase = _phase_publisher(user_id, stream_id)
    with audit("router.answer") as span:
        # `route` is the caller's pre-computed read route (ask.py classify_intent) — reuse it
        # so the legacy ladder does not run a SECOND classify; the prod LLM is a codex
        # subprocess. None → classify here (back-compat for direct answer() callers).
        mode: Route = route if route is not None else classify(question, chat_model=chat_model)
        # `mode` tracks the SERVED backend and is overwritten to 'wiki' if a graph
        # candidate demotes; `classified_route` is the immutable classifier verdict, so a
        # dashboard counting graph-classified questions stays accurate across demotes.
        span.add_meta(mode=mode, classified_route=mode)
        if mode == "structured":
            if should_federate(scope):
                return federated_structured_answer(
                    user_id, question, project=project, chat_model=chat_model
                )
            result = query_structured(
                user_id, question, scope=scope, project=project, chat_model=chat_model
            )
            # C3-a cross-plane recovery (docs/routing-holdout-plan.md §13): a GATE-REJECTED
            # structured result means the SQL compiled to an invalid/hallucinated shape — the
            # plane choice itself was likely wrong, and the structured answer is just an error
            # string. Re-query the wiki plane, which may hold the answer and fails safe to
            # "정보 없음". ONLY on gate rejection, never on 0-row: a real "0건" is a correct
            # structured answer, and re-querying wiki would fabricate it (TN hallucination,
            # C3-c rejected). We are past the federate early-return, so ask() is direct.
            #   `scope_rewrite_failed:<ExcType>` is EXCLUDED (code review #4): it is a
            #   fail-closed SECURITY reject (the tenant scope predicate could not be injected),
            #   not a wrong-plane signal. Masking it behind a wiki answer would hide the
            #   fail-closed reject from the asker; keep serving the reject.
            reject = result.validation.rejected_reason or ""
            recoverable = not result.validation.passed and not reject.startswith(
                "scope_rewrite_failed"
            )
            if settings.routing_gate_reject_wiki_recover and recoverable:
                span.add_meta(structured_gate_reject_recover=result.validation.rejected_reason)
                wiki_recover = ask(
                    user_id,
                    question,
                    scope=scope,
                    project=project,
                    chat_model=chat_model,
                    learn=learn,
                    record_gaps=record_gaps,
                    context_wiki_slug=context_wiki_slug,
                    history=history,
                    on_phase=on_phase,
                )
                if wiki_recover.sources:
                    span.add_meta(mode="wiki", routed_as="structured")
                    return RoutedAnswer(question=question, mode="wiki", wiki=wiki_recover)
            return RoutedAnswer(question=question, mode=mode, structured=result)
        # K4b graph branch: a candidate only. try_graph_answer never raises and never
        # fires under federation (it requires a company node + kg_available). On any miss
        # it returns answer=None; we demote to "wiki" and fall through to the SINGLE
        # existing wiki dispatch below (no duplicated dispatch, no recursion). The reject
        # reason rides along as telemetry on the demoted wiki answer (kg-impl §7.2).
        graph_warnings: list[str] = []
        if mode == "graph":
            outcome = try_graph_answer(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
            )
            if outcome.answer is not None:
                return outcome.answer
            mode = "wiki"
            graph_warnings = outcome.fallback_warnings
            # Record the PRECISE demote token (bind_miss vs no_groundable_pages vs …) on the
            # top router.answer span so a served-mode dashboard can bucket demotes without a
            # correlation_id join to the nested router.graph span. `demote_reason` is already
            # produced inside try_graph_answer; thread it up rather than re-deriving a generic
            # catch-all from the (often empty) fallback_warnings.
            span.add_meta(
                mode="wiki",
                routed_as="graph",
                fallback_reason=outcome.demote_reason or "bind_or_unavailable",
            )
        if should_federate(scope):
            wiki_answer = federated_wiki_answer(
                user_id,
                question,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
            )
        else:
            wiki_answer = ask(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                learn=learn,
                record_gaps=record_gaps,
                context_wiki_slug=context_wiki_slug,
                history=history,
                on_phase=on_phase,
            )
        # F2 — C3-a의 대칭 (analysis/f2-wiki-empty-recover-prereg.md): wiki로 서빙하려는
        # 답이 근거 0 공답이면 structured를 시도한다. 게이트 전부 통과일 때만 갈아타고,
        # 리젝이면 공답 유지 — 지식형 질문의 환각 SQL은 게이트가 걸러 낸다. wiki 근거가
        # 하나라도 있으면 이 분기는 아예 열리지 않으므로 기존 wiki 답변은 불변이다.
        # federated 경로는 제외(연합 structured 병합은 별도 계약).
        if (
            settings.routing_wiki_empty_structured_recover
            and not should_federate(scope)
            and not wiki_answer.sources
            and _has_aggregate_signal(question)
        ):
            recover = query_structured(
                user_id, question, scope=scope, project=project, chat_model=chat_model
            )
            if recover.validation.passed:
                span.add_meta(mode="structured", routed_as="wiki", wiki_empty_recover=True)
                return RoutedAnswer(question=question, mode="structured", structured=recover)
        if graph_warnings:
            # Surface the degraded-KG signal in the wiki answer's own warnings (the served
            # mode is 'wiki', and the FE renders a wiki/graph body through WikiView, which
            # shows wiki.warnings, while suppressing the outer banner for those modes). But
            # the asker must NOT see the raw internal gate tokens (kg_unavailable,
            # driver_error:..., mapping_error:..., …) — those are operator/telemetry signal
            # kept on the audit span / fallback_reason only. Show one fixed friendly Korean
            # message instead.
            served_warnings = [_GRAPH_DEGRADED_MESSAGE]
            # K8.3 (D6) — the conflict-honesty token is an FE-CONTRACT token (ask/page.tsx keys
            # on it to render "모순 확인 불가"), not a raw gate token, so it is preserved verbatim
            # alongside the friendly message. Without this a conflict question during a KG
            # outage / gate-reject would demote to a plain-wiki answer the asker reads as "no
            # conflicts" — the exact silence-as-all-clear failure D6 exists to prevent.
            if _CONFLICT_VIEW_UNAVAILABLE in graph_warnings:
                served_warnings.append(_CONFLICT_VIEW_UNAVAILABLE)
            wiki_answer = wiki_answer.model_copy(
                update={"warnings": [*served_warnings, *wiki_answer.warnings]}
            )
        return RoutedAnswer(
            question=question,
            mode=mode,
            wiki=wiki_answer,
            warnings=wiki_answer.warnings,
        )
