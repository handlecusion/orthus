"""전용 코퍼스 브라우저: company-scope `documents`에 대한 읽기/검색/인라인 저장.

`/editor`는 로그인 사용자 본인 문서만 보여주지만, 회사 central 아카식은
모든 사내 지식의 단일 저장소라 인증된 사용자라면 `scope='company'` 문서를
전부 열람·편집할 수 있어야 한다(의도된 접근 모델). 저장은 editor와 동일하게
`save_editor_document`(doc_id 키 업데이트 → corpus 재색인 + wiki authoring)를
재사용하므로 별도 central write path를 만들지 않는다. corpus chunk/embedding에는
접근하지 않는다(설계 원칙 #7)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from orthus.api.deps import get_user_id
from orthus.api.routes.documents import DocumentIn, DocumentSaved
from orthus.audit import audit
from orthus.db import session
from orthus.documents import save_editor_document
from orthus.tables import documents, users

router = APIRouter(prefix="/corpus", tags=["corpus"])

_PREVIEW_LEN = 160
# 목록 preview는 서버에서 left()로 잘라 full markdown detoast를 피한다.
# strip 후에도 _PREVIEW_LEN을 채울 여유분으로 넉넉히 자른다.
_PREVIEW_FETCH_LEN = 400
_MAX_LIMIT = 200


class CorpusListItem(BaseModel):
    doc_id: UUID
    title: str
    source: str
    project: str
    owner_name: str | None
    updated_at: datetime
    preview: str


class CorpusListResponse(BaseModel):
    items: list[CorpusListItem]
    total: int
    counts_by_source: dict[str, int]


class CorpusDocumentOut(BaseModel):
    doc_id: UUID
    title: str
    block_json: list[dict] = Field(default_factory=list)
    markdown: str
    source: str
    project: str
    owner_name: str | None
    updated_at: datetime


def _preview(markdown: str) -> str:
    text = markdown.strip()
    return text[:_PREVIEW_LEN]


@router.get("/documents", response_model=CorpusListResponse)
def list_corpus_documents(
    source: str | None = None,
    q: str | None = None,
    sort: Literal["recent", "title"] = "recent",
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user_id: UUID = Depends(get_user_id),
) -> CorpusListResponse:
    """company-scope 문서 목록 + 검색 + per-source counts.

    `counts_by_source`는 q 필터는 반영하되 source 필터는 무시해, 현재 검색에 대한
    각 source별 개수를 필터 칩에 항상 보여준다. `total`은 source 포함 모든 활성
    필터를 만족하는 행 수다."""
    q_clean = (q or "").strip()
    like = f"%{q_clean}%" if q_clean else None
    with audit("corpus.list") as span:
        with session() as s:
            base = [documents.c.scope == "company"]
            if like is not None:
                base.append(or_(documents.c.title.ilike(like), documents.c.markdown.ilike(like)))
            active = list(base)
            if source:
                active.append(documents.c.source == source)

            order = documents.c.title.asc() if sort == "title" else documents.c.updated_at.desc()
            rows = s.execute(
                select(
                    documents.c.doc_id,
                    documents.c.title,
                    documents.c.source,
                    documents.c.project,
                    func.left(documents.c.markdown, _PREVIEW_FETCH_LEN).label("markdown_head"),
                    documents.c.updated_at,
                    users.c.display_name.label("owner_name"),
                )
                .select_from(documents.outerjoin(users, documents.c.user_id == users.c.user_id))
                .where(*active)
                .order_by(order)
                .limit(limit)
                .offset(offset)
            ).all()

            total = s.execute(
                select(func.count()).select_from(documents).where(*active)
            ).scalar_one()

            count_rows = s.execute(
                select(documents.c.source, func.count().label("count"))
                .where(*base)
                .group_by(documents.c.source)
            ).all()

        span.add_meta(source=source or "", q=bool(q_clean), sort=sort, returned=len(rows))

    items = [
        CorpusListItem(
            doc_id=r.doc_id,
            title=r.title,
            source=r.source,
            project=r.project,
            owner_name=r.owner_name,
            updated_at=r.updated_at,
            preview=_preview(r.markdown_head),
        )
        for r in rows
    ]
    counts_by_source = {row.source: row.count for row in count_rows}
    return CorpusListResponse(items=items, total=total, counts_by_source=counts_by_source)


@router.get("/documents/{doc_id}", response_model=CorpusDocumentOut)
def get_corpus_document(doc_id: UUID, _user_id: UUID = Depends(get_user_id)) -> CorpusDocumentOut:
    with session() as s:
        r = s.execute(
            select(
                documents.c.doc_id,
                documents.c.title,
                documents.c.block_json,
                documents.c.markdown,
                documents.c.source,
                documents.c.project,
                documents.c.updated_at,
                users.c.display_name.label("owner_name"),
            )
            .select_from(documents.outerjoin(users, documents.c.user_id == users.c.user_id))
            .where(documents.c.doc_id == doc_id, documents.c.scope == "company")
        ).first()
    if not r:
        raise HTTPException(status_code=404, detail="document not found")
    return CorpusDocumentOut(
        doc_id=r.doc_id,
        title=r.title,
        block_json=r.block_json,
        markdown=r.markdown,
        source=r.source,
        project=r.project,
        owner_name=r.owner_name,
        updated_at=r.updated_at,
    )


@router.put("/documents/{doc_id}", response_model=DocumentSaved)
def update_corpus_document(
    doc_id: UUID, body: DocumentIn, user_id: UUID = Depends(get_user_id)
) -> DocumentSaved:
    """company 문서 인라인 저장. 원래 owner/source/project를 보존한 채
    `save_editor_document`(doc_id 키 업데이트 → corpus 재색인 + wiki authoring)를
    호출한다. `agent_draft`는 draft/publish 경계를 지키기 위해 여기서 거부한다."""
    with session() as s:
        row = s.execute(
            select(documents.c.user_id, documents.c.source, documents.c.project).where(
                documents.c.doc_id == doc_id,
                documents.c.scope == "company",
            )
        ).first()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    if row.source == "agent_draft":
        raise HTTPException(status_code=400, detail="agent drafts must be published via /editor")
    with audit("corpus.edit") as span:
        span.add_meta(
            doc_id=str(doc_id),
            editor_user_id=str(user_id),
            owner_user_id=str(row.user_id),
            source=row.source,
        )
        save_editor_document(
            row.user_id,
            body.title,
            body.block_json,
            body.markdown,
            doc_id=doc_id,
            scope="company",
            project=row.project,
        )
    return DocumentSaved(doc_id=doc_id)
