"""C2 connector registry/manifest/runner substrate."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from orthus.connectors.base import Connector
from orthus.connectors.manifest import ConnectorManifest, FactoryConnectorProvider
from orthus.connectors.registry import (
    get_connector_provider,
    list_connector_manifests,
    register_connector_provider,
)
from orthus.connectors.runner import (
    due_connector_accounts,
    run_connector_account_sync,
    run_due_connector_syncs,
)
from orthus.connectors.state import ConnectorAccountSpec, create_connector_account, get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import InternalDocument
from orthus.tables import connector_runs, documents


class _MemConnector(Connector):
    def __init__(self, docs: list[InternalDocument]):
        self._docs = docs

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield from self._docs


def _doc(source: str, external_id: str = "item-1") -> InternalDocument:
    return InternalDocument(
        title="C2 connector item",
        markdown="C2 runner body",
        source=source,
        source_external_id=external_id,
        source_last_edited_at=datetime(2026, 5, 29, tzinfo=UTC),
        project="company",
    )


def test_connector_manifest_validation():
    with pytest.raises(ValueError, match="periodic connector requires"):
        ConnectorManifest(
            slug="bad",
            label="Bad",
            source_kind="remote_api",
            account_kinds=("company",),
            auth_modes=("token",),
            capabilities=("read", "periodic"),
        )

    manifest = ConnectorManifest(
        slug="c2_manifest_ok",
        label="C2 Manifest",
        source_kind="local_file",
        account_kinds=("personal",),
        auth_modes=("local_path",),
        capabilities=("read", "manual"),
        privacy_class="personal_local",
    )

    assert manifest.supports_account_kind("personal")
    assert not manifest.supports_account_kind("company")
    assert manifest.supports_auth_mode("local_path")
    assert not manifest.can_periodic_sync()


def test_registry_and_due_runner_import_registered_account(user_id: UUID):
    slug = f"c2_runner_{uuid4().hex}"
    manifest = ConnectorManifest(
        slug=slug,
        label="C2 Runner",
        source_kind="local_app",
        account_kinds=("personal",),
        auth_modes=("local_path",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=60 * 60,
        default_daily_budget=10,
        privacy_class="personal_local",
        redaction_profile="strict",
        import_mode="parallel",
    )

    provider = FactoryConnectorProvider(
        manifest,
        lambda account, state: _MemConnector([_doc(str(account["connector_slug"]))]),
    )
    register_connector_provider(provider)

    account_id = create_connector_account(
        ConnectorAccountSpec(
            connector_slug=slug,
            account_kind="personal",
            node_id="personal-a",
            auth_mode="local_path",
            owner_id=user_id,
        )
    )

    assert get_connector_provider(slug) is provider
    assert slug in {m.slug for m in list_connector_manifests()}

    due = due_connector_accounts(
        now=datetime(2026, 5, 29, 12, tzinfo=UTC),
        node_id="personal-a",
    )
    assert [row["account_id"] for row in due] == [account_id]

    results = run_due_connector_syncs(
        user_id,
        now=datetime(2026, 5, 29, 12, tzinfo=UTC),
        node_id="personal-a",
        chat_model=MockChat(),
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.report is not None
    assert result.report.created == 1
    assert result.report.errors == 0

    with session() as s:
        run = s.execute(
            select(connector_runs).where(connector_runs.c.run_id == result.run_id)
        ).one()
        doc = s.execute(
            select(documents.c.source, documents.c.source_account_id, documents.c.scope)
        ).one()

    state = get_sync_state(account_id)
    assert state is not None
    assert state.last_sync_at is not None
    assert state.last_error is None
    assert run.status == "succeeded"
    assert run.account_id == account_id
    assert run.connector_slug == slug
    assert run.created == 1
    assert doc.source == slug
    assert doc.source_account_id == account_id
    assert doc.scope == "personal"

    assert state.last_sync_at is not None
    assert (
        due_connector_accounts(
            now=state.last_sync_at,
            node_id="personal-a",
        )
        == []
    )


def test_runner_records_provider_failure(user_id: UUID):
    slug = f"c2_fail_{uuid4().hex}"
    manifest = ConnectorManifest(
        slug=slug,
        label="C2 Failure",
        source_kind="local_app",
        account_kinds=("company",),
        auth_modes=("local_path",),
        capabilities=("read", "manual"),
    )

    def _raise(account: Mapping[str, object], state) -> Connector:
        del account, state
        raise RuntimeError("boom")

    register_connector_provider(FactoryConnectorProvider(manifest, _raise))
    account_id = create_connector_account(
        ConnectorAccountSpec(
            connector_slug=slug,
            account_kind="company",
            node_id="company",
            auth_mode="local_path",
        )
    )

    result = run_connector_account_sync(account_id, user_id)

    assert result.status == "failed"
    assert result.error_message is not None
    assert "RuntimeError: boom" in result.error_message

    state = get_sync_state(account_id)
    assert state is not None
    assert state.last_error is not None
    assert "RuntimeError: boom" in state.last_error

    with session() as s:
        run = s.execute(
            select(
                connector_runs.c.status,
                connector_runs.c.errors,
                connector_runs.c.error_message,
            ).where(connector_runs.c.run_id == result.run_id)
        ).one()
    assert run.status == "failed"
    assert run.errors == 1
    assert "RuntimeError: boom" in run.error_message
