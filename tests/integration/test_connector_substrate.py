"""C0 connector substrate: account-aware imports for company and personal nodes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select

from orthus.connectors.base import Connector, run_parallel_import
from orthus.connectors.state import (
    ConnectorAccountSpec,
    create_connector_account,
    get_sync_state,
    save_sync_state,
)
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import InternalDocument
from orthus.tables import connector_items, corpus_chunks, documents, wiki_pages


class _MemConnector(Connector):
    def __init__(self, docs: list[InternalDocument]):
        self._docs = docs

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield from self._docs


def _doc(
    *,
    title: str = "Gmail thread",
    source: str = "gmail",
    external_id: str = "thread-1",
    body: str = "Project alpha mail body",
    edited_at: datetime | None = None,
) -> InternalDocument:
    return InternalDocument(
        title=title,
        markdown=body,
        source=source,
        source_external_id=external_id,
        source_last_edited_at=edited_at or datetime(2026, 5, 1, tzinfo=UTC),
        project="company",
    )


def _chat() -> MockChat:
    return MockChat(
        rules=[
            (
                "Project alpha mail body",
                json.dumps(
                    {
                        "summary": "mail summary",
                        "key_concepts": [],
                        "terminology": [],
                        "open_questions": [],
                        "claims": [
                            {
                                "claim": "Project alpha was discussed in mail.",
                                "evidence": "Project alpha mail body",
                                "confidence": "high",
                                "page": {"slug": "project-alpha", "title": "Project Alpha"},
                                "conflicting": [],
                            }
                        ],
                    }
                ),
            )
        ]
    )


def test_personal_account_import_sets_scope_owner_and_item_map(user_id: UUID):
    account_id = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="gmail",
            account_kind="personal",
            node_id="personal-a",
            auth_mode="oauth_composio",
            owner_id=user_id,
            account_label="ys personal gmail",
        )
    )

    report = run_parallel_import(
        _MemConnector([_doc()]),
        user_id,
        scope="personal",
        source_account_id=account_id,
        chat_model=_chat(),
    )

    assert report.created == 1
    with session() as s:
        doc = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source,
                documents.c.source_account_id,
                documents.c.scope,
            )
        ).one()
        chunk_scope = s.execute(select(corpus_chunks.c.scope)).scalar_one()
        page = s.execute(
            select(wiki_pages.c.scope, wiki_pages.c.owner_id).where(wiki_pages.c.kind == "page")
        ).one()
        item = s.execute(select(connector_items).select_from(connector_items)).one()

    assert doc.source == "gmail"
    assert doc.source_account_id == account_id
    assert doc.scope == "personal"
    assert chunk_scope == "personal"
    assert page.scope == "personal"
    assert page.owner_id == user_id
    assert item.account_id == account_id
    assert item.external_id == "thread-1"
    assert item.doc_id == doc.doc_id


def test_same_external_id_can_exist_under_two_accounts(user_id: UUID):
    account_a = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="gmail",
            account_kind="personal",
            node_id="personal-a",
            auth_mode="oauth_composio",
            owner_id=user_id,
        )
    )
    account_b = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="gmail",
            account_kind="company",
            node_id="company",
            auth_mode="oauth_composio",
        )
    )

    doc = _doc(external_id="same-upstream-id")
    first_id, first_changed = upsert_source_document(
        user_id, doc, scope="personal", source_account_id=account_a, defer_authoring=True
    )
    second_id, second_changed = upsert_source_document(
        user_id, doc, scope="company", source_account_id=account_b, defer_authoring=True
    )
    again_id, again_changed = upsert_source_document(
        user_id, doc, scope="personal", source_account_id=account_a, defer_authoring=True
    )

    assert first_changed
    assert second_changed
    assert first_id != second_id
    assert again_id == first_id
    assert not again_changed

    with session() as s:
        count = s.execute(select(func.count()).select_from(documents)).scalar_one()
    assert count == 2


def test_connector_account_validation_and_sync_state(user_id: UUID):
    with pytest.raises(ValueError, match="requires owner_id"):
        create_connector_account(
            ConnectorAccountSpec(
                connector_slug="gmail",
                account_kind="personal",
                node_id="personal-a",
                auth_mode="oauth_composio",
            )
        )

    with pytest.raises(ValueError, match="must not set owner_id"):
        create_connector_account(
            ConnectorAccountSpec(
                connector_slug="slack",
                account_kind="company",
                node_id="company",
                auth_mode="oauth_composio",
                owner_id=user_id,
            )
        )

    account_id = create_connector_account(
        ConnectorAccountSpec(
            connector_slug="slack",
            account_kind="company",
            node_id="company",
            auth_mode="oauth_composio",
            project="nova",
        )
    )
    save_sync_state(
        account_id,
        cursor_json={"ts": "2026-05-28T00:00:00Z"},
        seen_json={"ids": ["m1"]},
        daily_budget_json={"date": "2026-05-28", "used": 1},
        last_seen_id="m1",
        last_sync_at=datetime(2026, 5, 28, tzinfo=UTC),
    )

    state = get_sync_state(account_id)
    assert state is not None
    assert state.cursor_json == {"ts": "2026-05-28T00:00:00Z"}
    assert state.seen_json == {"ids": ["m1"]}
    assert state.daily_budget_json == {"date": "2026-05-28", "used": 1}
    assert state.last_seen_id == "m1"
