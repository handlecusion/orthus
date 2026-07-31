"""C1 Notion import uses connector account substrate."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import uuid
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy import func, select

from orthus.connectors.base import Connector, run_import
from orthus.connectors.notion import notion_canonical_source_id
from orthus.connectors.notion_account import (
    adopt_legacy_notion_documents,
    ensure_notion_account,
)
from orthus.connectors.state import ConnectorAccountSpec, create_connector_account
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.schemas.canonical import InternalDocument
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents, users


class _MemConnector(Connector):
    def __init__(self, docs: list[InternalDocument]):
        self._docs = docs

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield from self._docs


def _notion_doc(page_id: str = "page-1") -> InternalDocument:
    return InternalDocument(
        title="Notion page",
        markdown="Company notion body",
        source="notion",
        source_external_id=page_id,
        source_canonical_id=notion_canonical_source_id(page_id),
        source_last_edited_at=datetime(2026, 5, 1, tzinfo=UTC),
        project="company",
    )


def test_ensure_notion_account_uses_node_policy(user_id: UUID, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_notion_token", "secret-token")

    first = ensure_notion_account(user_id, settings)
    second = ensure_notion_account(user_id, settings)

    assert first == second
    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == first)
        ).one()
    account = row._mapping
    assert account["connector_slug"] == "notion"
    assert account["account_kind"] == "personal"
    assert account["scope"] == "personal"
    assert account["owner_id"] == user_id
    assert account["node_id"] == "personal-a"
    assert account["auth_mode"] == "token"
    assert account["settings_redacted"]["token_configured"] is True


def test_adopt_legacy_notion_docs_prevents_duplicate_import(user_id: UUID):
    legacy_doc = _notion_doc("same-page").model_copy(update={"source_canonical_id": None})
    legacy_id, changed = upsert_source_document(
        user_id,
        legacy_doc,
        scope="company",
        defer_authoring=True,
    )
    assert changed

    account_id = ensure_notion_account(user_id)
    adopted = adopt_legacy_notion_documents(account_id)
    assert adopted == 1

    report = run_import(
        _MemConnector([_notion_doc("same-page")]),
        user_id,
        scope="company",
        source_account_id=account_id,
    )

    assert report.created == 0
    assert report.skipped == 1
    assert report.doc_ids == [legacy_id]

    with session() as s:
        doc_count = s.execute(select(func.count()).select_from(documents)).scalar_one()
        adopted_doc = s.execute(
            select(documents.c.source_account_id, documents.c.source_canonical_id).where(
                documents.c.doc_id == legacy_id
            )
        ).one()
        item = s.execute(select(connector_items).select_from(connector_items)).one()

    assert doc_count == 1
    assert adopted_doc.source_account_id == account_id
    assert adopted_doc.source_canonical_id == "notion:page:same-page"
    assert item.account_id == account_id
    assert item.external_id == "same-page"
    assert item.doc_id == legacy_id


def test_notion_canonical_dedupes_same_page_across_company_accounts(user_id: UUID):
    account_a = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="notion",
            account_kind="company",
            node_id="company",
            auth_mode="token",
            account_label="company notion A",
        )
    )
    account_b = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="notion",
            account_kind="company",
            node_id="company",
            auth_mode="token",
            account_label="company notion B",
        )
    )
    page_id = "550e8400e29b41d4a716446655440000"

    first = run_import(
        _MemConnector([_notion_doc(page_id)]),
        user_id,
        scope="company",
        source_account_id=account_a,
    )
    second = run_import(
        _MemConnector([_notion_doc("550e8400-e29b-41d4-a716-446655440000")]),
        user_id,
        scope="company",
        source_account_id=account_b,
    )

    assert first.created == 1
    assert second.created == 0
    assert second.skipped == 1
    assert second.doc_ids == first.doc_ids

    with session() as s:
        doc_rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source_account_id,
                documents.c.source_external_id,
                documents.c.source_canonical_id,
            )
        ).all()
        items = s.execute(
            select(
                connector_items.c.account_id,
                connector_items.c.external_id,
                connector_items.c.doc_id,
            )
            .select_from(connector_items)
            .order_by(connector_items.c.account_id)
        ).all()

    assert len(doc_rows) == 1
    doc = doc_rows[0]
    assert doc.source_account_id == account_a
    assert doc.source_external_id == page_id
    assert doc.source_canonical_id == "notion:page:550e8400-e29b-41d4-a716-446655440000"
    assert {item.account_id for item in items} == {account_a, account_b}
    assert {item.doc_id for item in items} == {doc.doc_id}


def test_notion_canonical_keeps_personal_owners_separate(user_id: UUID):
    other_user_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user_id, display_name="Other User"))
        s.commit()
    account_a = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="notion",
            account_kind="personal",
            node_id="personal-a",
            auth_mode="token",
            owner_id=user_id,
        )
    )
    account_b = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="notion",
            account_kind="personal",
            node_id="personal-a",
            auth_mode="token",
            owner_id=other_user_id,
        )
    )
    page_id = "550e8400-e29b-41d4-a716-446655440000"

    first = run_import(
        _MemConnector([_notion_doc(page_id)]),
        user_id,
        scope="personal",
        source_account_id=account_a,
    )
    second = run_import(
        _MemConnector([_notion_doc(page_id)]),
        other_user_id,
        scope="personal",
        source_account_id=account_b,
    )

    assert first.created == 1
    assert second.created == 1
    assert first.doc_ids != second.doc_ids

    with session() as s:
        doc_count = s.execute(select(func.count()).select_from(documents)).scalar_one()
        item_count = s.execute(select(func.count()).select_from(connector_items)).scalar_one()

    assert doc_count == 2
    assert item_count == 2
