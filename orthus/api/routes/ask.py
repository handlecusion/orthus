"""Search-only 비서 entrypoint (P2.2b, docs/architecture-v2.md §1/§7). POST /ask
auto-routes the NL question to the structured(PG) or wiki backend and returns a
`RoutedAnswer`. A structured run that the gate rejects surfaces as 422 (carrying
the compiled SQL + reason); a wiki answer is always 200.

`/ask` is pure knowledge search: it does NOT decompose compound questions, run the
inline agentic loop, or intake natural-language commands. The orchestrator (compound
decompose + single-command intake + read→act, with live SSE) lives on the agent-work
chat surface — `POST /agent-work/chats/{id}/orchestrate`
(docs/company-agent-orchestration.md §3).

GET /ask/runs/{query_id} keeps past structured runs inspectable from `query_runs`
(moved here from the removed POST /assistant/query route)."""

from __future__ import annotations

from typing import get_args
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from orthus.api.deps import get_user_id
from orthus.api.knowledge_token import (
    COLLECTOR_TOKEN_AUTH_MODE,
    enforce_token_ask_rate_limit,
    get_session_user_or_knowledge_token,
)
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.router import answer, resolve_answer_scope
from orthus.router.route import Route
from orthus.schemas.canonical import RoutedAnswer
from orthus.structured.query import query_structured
from orthus.tables import query_runs
from orthus.wiki.slug import clean_wiki_slug

router = APIRouter(prefix="/ask", tags=["ask"])

# The valid backend routes (structured | wiki | graph). Deriving this from the
# `Route` Literal keeps the /ask contract in lockstep with the router — a new
# backend added there is accepted here without editing this module.
_VALID_ROUTES: frozenset[str] = frozenset(get_args(Route))


class AskIn(BaseModel):
    question: str
    scope: str | None = None
    project: str | None = None
    context_wiki_slug: str | None = None
    # Optional backend override. None (the default and the only value the FE sends)
    # keeps the existing auto-routing exactly as-is. A valid value forces that backend
    # (e.g. the orthus-mcp `wiki_ask` tool pins "wiki" so a knowledge question is never
    # mis-routed to the structured NL→SQL backend). This is a backend *selection* only:
    # the forced backend still runs its full gate (structured keeps the sqlglot
    # SELECT-only validation), so no gate is bypassed.
    route: str | None = None

    @field_validator("context_wiki_slug", mode="before")
    @classmethod
    def _clean_context_wiki_slug(cls, value: object) -> str | None:
        return clean_wiki_slug(value)

    @field_validator("route")
    @classmethod
    def _validate_route(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _VALID_ROUTES:
            raise ValueError(
                f"route must be one of {sorted(_VALID_ROUTES)} or omitted for auto-routing"
            )
        return value


class StructuredAskIn(BaseModel):
    question: str
    scope: str | None = None
    project: str | None = None


@router.post("")
def ask_endpoint(
    body: AskIn,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
):
    enforce_token_ask_rate_limit(current)
    is_token = current.auth_mode == COLLECTOR_TOKEN_AUTH_MODE
    # Search only: decompose + the inline agentic loop are NEVER entered from /ask —
    # those belong to the agent-work chat orchestrator. The semantic answer cache stays
    # (it replays a pure grounded search answer), but token callers always recompute.
    result: RoutedAnswer = answer(
        current.user_id,
        body.question,
        scope=resolve_answer_scope(body.scope),
        project=body.project,
        context_wiki_slug=body.context_wiki_slug,
        allow_agentic=False,
        allow_decompose=False,
        allow_cache=not is_token,
        # None (default) → unchanged auto-routing. A validated value forces that
        # backend instead of running classify(); the gate on the chosen backend
        # still runs. `route` is a validated Route literal or None, so the
        # Route-typed param accepts it directly.
        route=body.route,  # type: ignore[arg-type]
    )
    payload = result.model_dump(mode="json")
    # A rejected structured query is an explicit 422 (not 200); the body still
    # carries the compiled SQL + validation reason for the UI.
    if result.mode == "structured" and result.structured is not None:
        if result.structured.status == "rejected":
            return JSONResponse(status_code=422, content=payload)
    return payload


@router.post("/structured")
def ask_structured_endpoint(
    body: StructuredAskIn,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
):
    """Recursion-free structured(PG) tool surface for orthus-mcp.

    The cli /ask agent answers NL aggregation/list/table questions by calling this
    over MCP. It runs `query_structured` directly — the sqlglot validation gate runs
    INSIDE that call, so it is never bypassed — and it does NOT call `answer()`/the
    router, so it can never recurse back into the cli agent (wiki_ask → /ask → claude
    → ∞ is impossible here). A gate-rejected query returns 200 with
    `status == "rejected"`: this is a tool endpoint, so the agent reads the rejection
    as data rather than getting the user-facing 422.
    """
    enforce_token_ask_rate_limit(current)
    result = query_structured(
        current.user_id,
        body.question,
        scope=resolve_answer_scope(body.scope),
        project=body.project,
    )
    return result.model_dump(mode="json")


@router.get("/runs/{query_id}")
def get_query_run(query_id: UUID, user_id: UUID = Depends(get_user_id)):
    with session() as s:
        r = s.execute(
            select(
                query_runs.c.query_id,
                query_runs.c.nl_question,
                query_runs.c.source_id,
                query_runs.c.compiled_sql,
                query_runs.c.validation,
                query_runs.c.status,
                query_runs.c.result_meta,
                query_runs.c.created_at,
            ).where(query_runs.c.query_id == query_id, query_runs.c.user_id == user_id)
        ).first()
    if not r:
        raise HTTPException(status_code=404, detail="query run not found")
    return {
        "query_id": str(r.query_id),
        "nl_question": r.nl_question,
        "source_id": str(r.source_id) if r.source_id else None,
        "compiled_sql": r.compiled_sql,
        "validation": r.validation,
        "status": r.status,
        "result_meta": r.result_meta,
        "created_at": r.created_at.isoformat(),
    }
