"""Explicit personal→central promotion API."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.api.deps import get_current_user, get_user_id
from orthus.auth import AuthenticatedUser, require_node_operator
from orthus.db import session
from orthus.promote import (
    approve_promotion_stage,
    export_personal_document,
    list_promotion_stages,
    reject_promotion_stage,
    stage_promotion_package,
)
from orthus.schemas.canonical import PromotionPackage, PromotionStage
from orthus.settings import get_settings
from orthus.tables import users

router = APIRouter(prefix="/promote", tags=["promote"])


class PromotionExportIn(BaseModel):
    doc_id: UUID


@router.post("/export", response_model=PromotionPackage)
def export_promotion(
    body: PromotionExportIn,
    user_id: UUID = Depends(get_user_id),
) -> PromotionPackage:
    try:
        return export_personal_document(body.doc_id, user_id, get_settings())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stage", response_model=PromotionStage)
def stage_promotion(
    body: PromotionPackage,
    current: AuthenticatedUser = Depends(get_current_user),
) -> PromotionStage:
    require_node_operator(current)
    user_id = current.user_id
    _ensure_demo_user_row(user_id)
    try:
        return stage_promotion_package(body, user_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/stage", response_model=list[PromotionStage])
def list_staged_promotions(
    status: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[PromotionStage]:
    require_node_operator(current)
    user_id = current.user_id
    _ensure_demo_user_row(user_id)
    try:
        return list_promotion_stages(status=status, settings=get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stage/{stage_id}/approve", response_model=PromotionStage)
def approve_promotion(
    stage_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> PromotionStage:
    require_node_operator(current)
    user_id = current.user_id
    _ensure_demo_user_row(user_id)
    try:
        return approve_promotion_stage(stage_id, user_id, get_settings())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stage/{stage_id}/reject", response_model=PromotionStage)
def reject_promotion(
    stage_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> PromotionStage:
    require_node_operator(current)
    user_id = current.user_id
    _ensure_demo_user_row(user_id)
    try:
        return reject_promotion_stage(stage_id, user_id, get_settings())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ensure_demo_user_row(user_id: UUID) -> None:
    with session() as s:
        s.execute(
            pg_insert(users)
            .values(
                user_id=user_id,
                external_id=f"demo:{user_id}",
                display_name="Demo User",
                preferred_timezone="Asia/Seoul",
            )
            .on_conflict_do_nothing(index_elements=[users.c.user_id])
        )
        s.commit()
