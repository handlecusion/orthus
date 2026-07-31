"""Data-gap backlog endpoints (docs/architecture-v2.md §7).

The /ask wiki path auto-records poorly grounded answers into the backlog (no LLM
call). These endpoints let the FE turn a weak answer into an actionable 'add this
data here' suggestion (`/gaps/feedback`, one LLM call) and let a data owner review
and resolve the backlog (`GET /gaps`, `PATCH /gaps/{id}`)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from orthus.api.deps import get_user_id
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.auth import AuthenticatedUser
from orthus.schemas.canonical import DataGap
from orthus.wiki.gap import (
    generate_suggestion,
    list_gaps,
    record_feedback,
    set_gap_status,
)

router = APIRouter(prefix="/gaps", tags=["gaps"])

_ALLOWED_STATUS = {"open", "resolved", "dismissed"}


class FeedbackIn(BaseModel):
    question: str


class StatusPatchIn(BaseModel):
    status: str


@router.get("", response_model=list[DataGap])
def list_data_gaps(
    status: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[DataGap]:
    """Backlog rows in the caller's node scope (company or own personal), busiest first.

    Dual-auth (session/demo/jwt OR a knowledge-scoped collector token) so the
    orthus-mcp `data_gaps` tool can read the owner's open backlog. Read-only; the
    token resolves to its owner so the existing owner-scope filter still applies.
    """
    if status is not None and status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail=f"invalid status: {status}")
    return list_gaps(current.user_id, status=status)


@router.post("/feedback", response_model=DataGap)
def submit_feedback(body: FeedbackIn, user_id: UUID = Depends(get_user_id)) -> DataGap:
    """'이 답변 부족해요': record the question as a gap and generate an LLM suggestion of
    what to add where. Synchronous — the user pressed the button and expects the result."""
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    gap_id = record_feedback(user_id, question)
    if gap_id is None:
        raise HTTPException(status_code=400, detail="question is empty after normalization")
    result = generate_suggestion(user_id, gap_id)
    if result is None:
        raise HTTPException(status_code=404, detail="gap not found")
    return result


@router.post("/{gap_id}/suggest", response_model=DataGap)
def suggest_for_gap(gap_id: UUID, user_id: UUID = Depends(get_user_id)) -> DataGap:
    """Generate (or refresh) the LLM suggestion for an existing backlog row."""
    result = generate_suggestion(user_id, gap_id)
    if result is None:
        raise HTTPException(status_code=404, detail="gap not found")
    return result


@router.patch("/{gap_id}", response_model=DataGap)
def patch_gap_status(
    gap_id: UUID, body: StatusPatchIn, user_id: UUID = Depends(get_user_id)
) -> DataGap:
    """Resolve / dismiss / reopen a backlog row."""
    if body.status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail=f"invalid status: {body.status}")
    result = set_gap_status(user_id, gap_id, body.status)
    if result is None:
        raise HTTPException(status_code=404, detail="gap not found")
    return result
