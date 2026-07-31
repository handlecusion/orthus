"""Personal Gmail inbox projection (P6.1b).

Read-only query against already-ingested gws_gmail documents.
No gws CLI calls, no wiki writes, no AgentWork — only SELECT on the
documents table filtered to the caller's own rows.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select

from orthus.db import session
from orthus.schemas.canonical import PersonalMailDetail, PersonalMailItem, PersonalMailResponse
from orthus.tables import documents

_GMAIL_TITLE_PREFIX = "Gmail: "
_SNIPPET_LEN = 200
# Header block that gws_gmail connector prefixes to every markdown body:
# "# <subject>\n\nSource: gws_gmail\nMessage ID: ...\nThread ID: ...\n\n"
_HEADER_RE = re.compile(
    r"^#[^\n]*\n\nSource:\s*gws_gmail\nMessage ID:[^\n]*\nThread ID:[^\n]*\n\n",
    re.MULTILINE,
)


def _derive_subject(title: str) -> str:
    if title.startswith(_GMAIL_TITLE_PREFIX):
        return title[len(_GMAIL_TITLE_PREFIX) :]
    return title


def _derive_snippet(markdown: str) -> str:
    """Strip the gws_gmail header block, then return first 200 chars."""
    body = _HEADER_RE.sub("", markdown, count=1)
    body = body.strip()
    if len(body) > _SNIPPET_LEN:
        return body[:_SNIPPET_LEN]
    return body


def list_personal_gmail(
    user_id: UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
) -> PersonalMailResponse:
    """Return owner-only gws_gmail documents, newest first."""
    base = (
        select(
            documents.c.doc_id,
            documents.c.title,
            documents.c.source_external_id,
            documents.c.source_last_edited_at,
            documents.c.markdown,
        )
        .where(documents.c.source == "gws_gmail")
        .where(documents.c.scope == "personal")
        .where(documents.c.user_id == user_id)
    )
    if search:
        base = base.where(documents.c.title.ilike(f"%{search}%"))

    count_q = select(func.count()).select_from(base.subquery())
    list_q = (
        base.order_by(documents.c.source_last_edited_at.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )

    with session() as s:
        total = s.execute(count_q).scalar_one()
        rows = s.execute(list_q).fetchall()

    items = [
        PersonalMailItem(
            doc_id=row.doc_id,
            title=row.title,
            subject=_derive_subject(row.title),
            sent_at=row.source_last_edited_at,
            external_id=row.source_external_id,
            snippet=_derive_snippet(row.markdown or ""),
        )
        for row in rows
    ]
    return PersonalMailResponse(items=items, total=total)


def get_personal_gmail(user_id: UUID, doc_id: UUID) -> PersonalMailDetail | None:
    """Return full body for one owned gws_gmail document; None if not found/not owned."""
    q = (
        select(
            documents.c.doc_id,
            documents.c.title,
            documents.c.source_last_edited_at,
            documents.c.markdown,
        )
        .where(documents.c.doc_id == doc_id)
        .where(documents.c.source == "gws_gmail")
        .where(documents.c.scope == "personal")
        .where(documents.c.user_id == user_id)
    )
    with session() as s:
        row = s.execute(q).first()
    if row is None:
        return None
    return PersonalMailDetail(
        doc_id=row.doc_id,
        title=row.title,
        subject=_derive_subject(row.title),
        sent_at=row.source_last_edited_at,
        body=row.markdown or "",
    )
