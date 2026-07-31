"""Board endpoints — kanban view over the Nova 개발 로드맵 Notion DB.

GET  /board          → flat list of BoardCard (4 fixed columns)
PATCH /board/{row_id} → update local status overlay, then attempt Notion
                        write-back; 422 on invalid status, 404 if row_id not
                        in the target DB / scope.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orthus.api.deps import get_user_id
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.auth import AuthenticatedUser
from orthus.board import STATUS_COLUMNS, list_board, set_status
from orthus.schemas.canonical import BoardCard

router = APIRouter(prefix="/board", tags=["board"])


class BoardResponse(BaseModel):
    columns: list[str]
    cards: list[BoardCard]


class StatusPatch(BaseModel):
    status: str


@router.get("", response_model=BoardResponse)
def get_board(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> BoardResponse:
    # Dual-auth so the owner's orthus-mcp (knowledge token) + Solar agentic /ask
    # can read the company kanban board — company internal knowledge, low-risk.
    cards = list_board(current.user_id)
    return BoardResponse(columns=STATUS_COLUMNS, cards=cards)


@router.patch("/{row_id}", response_model=BoardCard)
def patch_card_status(
    row_id: UUID,
    body: StatusPatch,
    user_id: UUID = Depends(get_user_id),
) -> BoardCard:
    try:
        return set_status(user_id, row_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
