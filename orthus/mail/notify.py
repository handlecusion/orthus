"""New-mail signal for desktop notifications.

Read-only, local-only: a count/max over already-ingested mail documents visible
to the caller. No gws/provider calls, no wiki writes, no AgentWork — only SELECT
on the documents table. Safe to poll frequently (the desktop shell polls this to
decide whether to raise a native notification).

Visibility mirrors the existing document boundary: company-scope mail is
company-wide; personal-scope mail (individual mailboxes) is owner-only. `gws_gmail`
personal Gmail is intentionally excluded (source prefix `mail_` only covers the P6
unified company/individual mail backends `mail_nova` / `mail_acme`).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, or_, select

from orthus.db import session
from orthus.schemas.canonical import MailNotifyState
from orthus.tables import documents


def mail_notify_state(user_id: UUID) -> MailNotifyState:
    """Return total/latest-ingest-time/latest-subject for the caller's mail."""
    mail_src = documents.c.source.startswith("mail_", autoescape=True)
    visible = or_(
        documents.c.scope == "company",
        and_(documents.c.scope == "personal", documents.c.user_id == user_id),
    )
    with session() as s:
        agg = s.execute(
            select(
                func.count().label("total"),
                func.max(documents.c.created_at).label("latest_at"),
            ).where(mail_src, visible)
        ).one()
        total = int(agg.total or 0)
        latest_at = agg.latest_at
        latest_title: str | None = None
        if total:
            latest_title = s.execute(
                select(documents.c.title)
                .where(mail_src, visible)
                .order_by(documents.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
    return MailNotifyState(total=total, latest_at=latest_at, latest_title=latest_title)
