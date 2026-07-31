"""Editor document save/read. Saving (re)runs the corpus pipeline (prompt §8)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from orthus.api.deps import get_user_id
from orthus.db import session
from orthus.documents import (
    publish_agent_draft_document,
    save_agent_draft_document,
    save_editor_document,
)
from orthus.tables import documents

router = APIRouter(prefix="/documents", tags=["documents"])


class DocumentIn(BaseModel):
    title: str
    block_json: list[dict] = Field(default_factory=list)
    markdown: str


class DocumentSaved(BaseModel):
    doc_id: UUID


class DocumentOut(BaseModel):
    doc_id: UUID
    title: str
    block_json: list[dict]
    markdown: str
    source: str
    updated_at: datetime


class DocumentListItem(BaseModel):
    doc_id: UUID
    title: str
    source: str
    updated_at: datetime


@router.post("", response_model=DocumentSaved, status_code=201)
def create_document(body: DocumentIn, user_id: UUID = Depends(get_user_id)) -> DocumentSaved:
    doc_id = save_editor_document(user_id, body.title, body.block_json, body.markdown)
    return DocumentSaved(doc_id=doc_id)


@router.put("/{doc_id}", response_model=DocumentSaved)
def update_document(
    doc_id: UUID, body: DocumentIn, user_id: UUID = Depends(get_user_id)
) -> DocumentSaved:
    with session() as s:
        row = s.execute(
            select(documents.c.source, documents.c.scope, documents.c.project).where(
                documents.c.doc_id == doc_id,
                documents.c.user_id == user_id,
            )
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    if row.source == "agent_draft":
        try:
            save_agent_draft_document(user_id, doc_id, body.title, body.block_json, body.markdown)
        except LookupError:
            raise HTTPException(status_code=404, detail="document not found") from None
    else:
        save_editor_document(
            user_id,
            body.title,
            body.block_json,
            body.markdown,
            doc_id=doc_id,
            scope=row.scope,
            project=row.project,
        )
    return DocumentSaved(doc_id=doc_id)


@router.post("/{doc_id}/publish", response_model=DocumentSaved)
def publish_document(
    doc_id: UUID, body: DocumentIn, user_id: UUID = Depends(get_user_id)
) -> DocumentSaved:
    with session() as s:
        row = s.execute(
            select(documents.c.source).where(
                documents.c.doc_id == doc_id,
                documents.c.user_id == user_id,
            )
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    if row.source != "agent_draft":
        raise HTTPException(status_code=409, detail="document is not an agent draft")
    try:
        publish_agent_draft_document(user_id, doc_id, body.title, body.block_json, body.markdown)
    except LookupError:
        raise HTTPException(status_code=404, detail="document not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="document is not an agent draft") from None
    return DocumentSaved(doc_id=doc_id)


@router.get("", response_model=list[DocumentListItem])
def list_documents(user_id: UUID = Depends(get_user_id)) -> list[DocumentListItem]:
    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.title,
                documents.c.source,
                documents.c.updated_at,
            )
            .where(documents.c.user_id == user_id)
            .order_by(documents.c.updated_at.desc())
        ).all()
    return [
        DocumentListItem(doc_id=r.doc_id, title=r.title, source=r.source, updated_at=r.updated_at)
        for r in rows
    ]


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: UUID, user_id: UUID = Depends(get_user_id)) -> DocumentOut:
    with session() as s:
        r = s.execute(
            select(
                documents.c.doc_id,
                documents.c.title,
                documents.c.block_json,
                documents.c.markdown,
                documents.c.source,
                documents.c.updated_at,
            ).where(documents.c.doc_id == doc_id, documents.c.user_id == user_id)
        ).first()
    if not r:
        raise HTTPException(status_code=404, detail="document not found")
    return DocumentOut(
        doc_id=r.doc_id,
        title=r.title,
        block_json=r.block_json,
        markdown=r.markdown,
        source=r.source,
        updated_at=r.updated_at,
    )
