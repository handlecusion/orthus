"""Read-only company federation endpoints for personal nodes."""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.db import session
from orthus.schemas.canonical import AssistantResult, WikiPage, WikiSourceRef
from orthus.settings import get_settings
from orthus.structured.query import query_structured
from orthus.tables import users
from orthus.wiki.retrieve import retrieve
from orthus.wiki.store import load_page

router = APIRouter(prefix="/federation", tags=["federation"])


class WikiRetrieveIn(BaseModel):
    question: str
    k: int = 5
    project: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    source: str | None = None


class StructuredQueryIn(BaseModel):
    question: str
    project: str | None = None


@router.post("/wiki/retrieve", response_model=list[WikiSourceRef])
def retrieve_company_wiki(
    body: WikiRetrieveIn,
    authorization: str | None = Header(default=None),
) -> list[WikiSourceRef]:
    _require_federation_token(authorization)
    user_id = _ensure_service_user()
    return retrieve(
        user_id,
        body.question,
        k=body.k,
        scope="company",
        project=body.project,
        since=body.since,
        until=body.until,
        source=body.source,
    )


@router.get("/wiki/pages/{slug:path}", response_model=WikiPage)
def get_company_wiki_page(
    slug: str,
    authorization: str | None = Header(default=None),
) -> WikiPage:
    _require_federation_token(authorization)
    page = load_page(slug, scope="company", owner_id=None)
    if page is None:
        raise HTTPException(status_code=404, detail=f"wiki page not found: {slug}")
    return page


@router.post("/structured/query", response_model=AssistantResult)
def query_company_structured(
    body: StructuredQueryIn,
    authorization: str | None = Header(default=None),
) -> AssistantResult:
    _require_federation_token(authorization)
    user_id = _ensure_service_user()
    return query_structured(user_id, body.question, scope="company", project=body.project)


def _require_federation_token(authorization: str | None) -> None:
    settings = get_settings()
    if settings.node_kind != "company" or not settings.federation_service_token:
        raise HTTPException(status_code=404, detail="federation disabled")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing federation token")
    if not secrets.compare_digest(token, settings.federation_service_token):
        raise HTTPException(status_code=401, detail="invalid federation token")


def _ensure_service_user() -> UUID:
    settings = get_settings()
    try:
        user_id = UUID(settings.federation_service_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="invalid federation service user id") from exc
    with session() as s:
        s.execute(
            pg_insert(users)
            .values(
                user_id=user_id,
                external_id=f"federation:{settings.node_id}",
                display_name="Federation Service",
            )
            .on_conflict_do_nothing(index_elements=[users.c.user_id])
        )
        s.commit()
    return user_id
