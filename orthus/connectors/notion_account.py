"""Notion connector account helpers.

C1 moves the existing Notion import onto the shared connector substrate while
preserving legacy rows whose `source_account_id` was NULL before C0.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select, update

from orthus.connectors.notion import notion_canonical_source_id
from orthus.connectors.state import (
    ConnectorAccountSpec,
    ensure_connector_account,
    upsert_connector_item,
)
from orthus.db import session
from orthus.settings import Settings, get_settings
from orthus.tables import documents


def ensure_notion_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update node-local default Notion account and return account_id."""
    settings = settings or get_settings()
    account_kind = "personal" if settings.node_kind == "personal" else "company"
    owner_id = user_id if account_kind == "personal" else None
    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="notion",
            account_kind=account_kind,
            node_id=settings.node_id,
            auth_mode="token",
            owner_id=owner_id,
            account_label=f"{settings.node_id} Notion",
            settings_redacted={
                "env": "ORTHUS_CONN_NOTION_TOKEN",
                "token_configured": bool(settings.conn_notion_token),
            },
        )
    )


def adopt_legacy_notion_documents(account_id: UUID) -> int:
    """Attach pre-C0 Notion rows (`source_account_id IS NULL`) to account_id.

    This prevents first C1 import from duplicating existing Notion documents. It
    also backfills connector_items for the adopted rows.
    """
    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source_external_id,
                documents.c.source_last_edited_at,
                documents.c.markdown,
            ).where(
                documents.c.source == "notion",
                documents.c.source_account_id.is_(None),
                documents.c.source_external_id.is_not(None),
            )
        ).all()
        if not rows:
            return 0
        for row in rows:
            s.execute(
                update(documents)
                .where(documents.c.doc_id == row.doc_id)
                .values(
                    source_account_id=account_id,
                    source_canonical_id=notion_canonical_source_id(row.source_external_id),
                )
            )
        s.commit()

    for row in rows:
        content_hash = hashlib.sha256(row.markdown.encode("utf-8")).hexdigest()
        external_version = (
            row.source_last_edited_at.isoformat() if row.source_last_edited_at is not None else None
        )
        upsert_connector_item(
            account_id,
            row.source_external_id,
            external_version=external_version,
            content_hash=content_hash,
            doc_id=row.doc_id,
        )
    return len(rows)
