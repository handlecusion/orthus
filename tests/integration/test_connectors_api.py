"""Generic connector API for FE attach/sync flow."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.connectors import default_accounts
from orthus.connectors import github_provider
from orthus.connectors import gws_account
from orthus.connectors import gws_provider
from orthus.connectors import notion_provider
from orthus.schemas.canonical import InternalDocument
from orthus.schemas.canonical import WikiClaim, WikiPage, WikiSource
from orthus.secrets import clear_memory_secrets
from orthus.settings import get_settings
from orthus.db import session
from orthus.tables import (
    connector_accounts,
    connector_runs,
    connector_sync_state,
    documents,
    users,
)
import orthus.wiki.store as wiki_store
from orthus.wiki.retrieve import retrieve
from orthus.wiki.slugs import source_slug_from_title

client = TestClient(app)


class _FakeNotionConnector:
    def __init__(self, token: str) -> None:
        self.token = token

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield InternalDocument(
            title="Generic Notion sync",
            markdown="Generic Notion body",
            source="notion",
            source_external_id="generic-notion-page",
            source_last_edited_at=datetime(2026, 5, 31, tzinfo=UTC),
            project="company",
        )


class _FakeGitHubConnector:
    def __init__(self, token: str, repos, *, max_items: int = 200) -> None:
        self.token = token
        self.repos = tuple(repos)
        self.max_items = max_items

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        assert self.token == "ghp_web_config_token"
        assert self.repos == ("example-org/orthus",)
        assert self.max_items == 200
        yield InternalDocument(
            title="GitHub web config issue",
            markdown="GitHub web config body",
            source="github",
            source_external_id="github-web-config",
            source_last_edited_at=datetime(2026, 5, 31, tzinfo=UTC),
            project="company",
        )


class _FailingGitHubConnector:
    def __init__(self, token: str, repos, *, max_items: int = 200) -> None:
        del token, repos, max_items

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        del since
        raise RuntimeError("github sync failed for policy evidence")


class _FakeGwsGmailConnector:
    def __init__(
        self,
        runner,
        *,
        query: str,
        max_messages: int,
        max_body_chars: int,
    ) -> None:
        self.query = query
        self.max_messages = max_messages
        self.max_body_chars = max_body_chars

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        assert self.query == "newer_than:7d"
        assert self.max_messages == 5
        assert self.max_body_chars == 1024
        yield InternalDocument(
            title="Gmail gws sync",
            markdown="Gmail gws body",
            source="gws_gmail",
            source_external_id="gmail:gws-message",
            source_last_edited_at=datetime(2026, 5, 31, tzinfo=UTC),
            project="company",
        )


class _FakeLegacyGwsGmailConnector(_FakeGwsGmailConnector):
    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        assert self.query == "in:anywhere"
        assert self.max_messages == 5
        yield InternalDocument(
            title="Gmail legacy query sync",
            markdown="Gmail legacy query body",
            source="gws_gmail",
            source_external_id="gmail:legacy-query-message",
            source_last_edited_at=datetime(2026, 5, 31, tzinfo=UTC),
            project="company",
        )


PERSONAL_ONLY_CONNECTOR_SLUGS = (
    "local_files",
    "codex_sessions",
    "claude_sessions",
    "chat_exports",
    "email_exports",
    "github",
    "gws_gmail",
    "gws_drive",
)


def test_connectors_api_lists_manifests_and_syncs_chat_exports(
    user_id,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "chat-exports"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "api-conv",
                    "title": "API connector sync",
                    "mapping": {
                        "u": {
                            "message": {
                                "author": {"role": "user"},
                                "content": {"parts": ["API sync user message"]},
                            }
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_chat_exports_roots", str(root))
    monkeypatch.setattr(settings, "conn_chat_exports_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_chat_exports_max_messages", 25)
    monkeypatch.setattr(settings, "conn_chat_exports_max_message_chars", 512)

    listed = client.get("/connectors")
    assert listed.status_code == 200
    body = listed.json()
    manifests = {m["slug"]: m for m in body["manifests"]}
    assert body["node_kind"] == "personal"
    assert manifests["chat_exports"]["can_ensure_default"] is True
    assert manifests["chat_exports"]["default_configured"] is True
    assert manifests["chat_exports"]["config_error"] is None
    assert manifests["chat_exports"]["source_kind"] == "export"
    assert "ORTHUS_CONN_CHAT_EXPORTS_ROOTS" in manifests["chat_exports"]["settings_keys"]
    assert "slack" not in manifests
    assert body["runs"] == []

    ensured = client.post("/connectors/chat_exports/ensure")
    assert ensured.status_code == 200
    assert ensured.json()["connector_slug"] == "chat_exports"
    assert ensured.json()["settings_redacted"]["root_count"] == 1

    synced = client.post("/connectors/chat_exports/sync", json={"concurrency": 2})
    assert synced.status_code == 200
    sync_body = synced.json()
    assert sync_body["status"] == "succeeded"
    assert sync_body["report"]["created"] == 1

    relisted = client.get("/connectors")
    assert relisted.status_code == 200
    relisted_body = relisted.json()
    account = next(a for a in relisted_body["accounts"] if a["connector_slug"] == "chat_exports")
    assert account["last_sync_at"] is not None
    assert account["last_error"] is None
    run = next(r for r in relisted_body["runs"] if r["connector_slug"] == "chat_exports")
    assert run["account_id"] == account["account_id"]
    assert run["reason"] == "manual"
    assert run["status"] == "succeeded"
    assert run["created"] == 1
    assert run["errors"] == 0
    assert run["finished_at"] is not None
    doc_item = next(d for d in relisted_body["documents"] if d["connector_slug"] == "chat_exports")
    assert doc_item["account_id"] == account["account_id"]
    assert doc_item["title"] == "Chat export: API connector sync"
    assert doc_item["source_external_id"] is not None
    assert doc_item["source_external_id"].startswith("chat-export:")

    with session() as s:
        doc = s.execute(select(documents.c.source, documents.c.scope)).one()
    assert doc.source == "chat_exports"
    assert doc.scope == "personal"


def test_connectors_api_filters_manifests_by_node_kind(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"

    listed = client.get("/connectors")

    assert listed.status_code == 200
    body = listed.json()
    manifests = {m["slug"]: m for m in body["manifests"]}
    assert body["node_kind"] == "company"
    assert {"notion", "slack"}.issubset(manifests)
    assert "chat_exports" not in manifests
    assert "claude_sessions" not in manifests
    assert "codex_sessions" not in manifests
    assert "email_exports" not in manifests
    assert "github" not in manifests
    assert "gws_drive" not in manifests
    assert "gws_gmail" not in manifests
    assert "local_files" not in manifests


def test_connectors_api_rejects_personal_connectors_on_company_node(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.conn_github_token = "secret-token"
    settings.conn_github_repos = "example-org/orthus"

    github_config = client.put(
        "/connectors/github/config",
        json={
            "secrets": {"token": "ghp_web_config_token"},
            "settings": {"repos": "example-org/orthus"},
        },
    )
    assert github_config.status_code == 400
    assert "personal-node only" in github_config.json()["detail"]

    github_ensure = client.post("/connectors/github/ensure")
    assert github_ensure.status_code == 400
    assert "personal-node only" in github_ensure.json()["detail"]

    gws_ensure = client.post("/connectors/gws_gmail/ensure")
    assert gws_ensure.status_code == 400
    assert "personal-node only" in gws_ensure.json()["detail"]

    for slug in (
        "local_files",
        "codex_sessions",
        "claude_sessions",
        "chat_exports",
        "email_exports",
        "gws_drive",
    ):
        ensured = client.post(f"/connectors/{slug}/ensure")
        assert ensured.status_code == 400, slug
        assert "personal-node only" in ensured.json()["detail"]


def test_owner_scope_personal_connectors_on_company_node(user_id, monkeypatch):
    clear_memory_secrets()
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.owner_scope_enabled = True
    monkeypatch.setattr(settings, "secret_backend", "memory")
    headers = {"X-User-Id": str(user_id)}

    listed = client.get("/connectors?scope=personal", headers=headers)

    assert listed.status_code == 200
    body = listed.json()
    manifests = {m["slug"]: m for m in body["manifests"]}
    assert body["node_kind"] == "company"
    assert {"notion", "github", "gws_gmail", "gws_drive"}.issubset(manifests)
    assert "slack" not in manifests
    assert manifests["github"]["can_ensure_default"] is False
    assert manifests["github"]["default_configured"] is False
    assert body["accounts"] == []

    configured = client.put(
        "/connectors/github/config?scope=personal",
        headers=headers,
        json={
            "secrets": {"token": "ghp_web_config_token"},
            "settings": {"repos": "example-org/orthus"},
        },
    )

    assert configured.status_code == 200
    account = configured.json()
    assert account["connector_slug"] == "github"
    assert account["account_kind"] == "personal"
    assert account["scope"] == "personal"
    assert account["node_id"] == "company"
    assert account["owner_id"] == str(user_id)
    assert account["settings_redacted"]["repos"] == ["example-org/orthus"]
    assert account["settings_redacted"]["token_configured"] is True

    relisted = client.get("/connectors?scope=personal", headers=headers)
    assert relisted.status_code == 200
    relisted_body = relisted.json()
    relisted_manifests = {m["slug"]: m for m in relisted_body["manifests"]}
    assert relisted_manifests["github"]["default_configured"] is True
    assert relisted_manifests["github"]["config_error"] is None
    assert [a["connector_slug"] for a in relisted_body["accounts"]] == ["github"]

    other_user = uuid.uuid4()
    other_listed = client.get(
        "/connectors?scope=personal",
        headers={"X-User-Id": str(other_user)},
    )
    assert other_listed.status_code == 200
    assert other_listed.json()["accounts"] == []

    synced = client.post("/connectors/github/sync?scope=personal", headers=headers)
    assert synced.status_code == 400
    assert "desktop collector" in synced.json()["detail"]
    clear_memory_secrets()


def test_personal_connector_list_projects_collector_null_account_docs(user_id, monkeypatch):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.owner_scope_enabled = True
    headers = {"X-User-Id": str(user_id)}
    now = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="collector local note",
                block_json={},
                markdown="collector local body",
                source="local_files",
                source_account_id=None,
                source_external_id="local:/tmp/note.md",
                source_last_edited_at=now,
                schema_version=1,
                scope="personal",
                project="company",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()

    listed = client.get("/connectors?scope=personal", headers=headers)
    assert listed.status_code == 200
    docs = {doc["doc_id"]: doc for doc in listed.json()["documents"]}
    assert str(doc_id) in docs
    assert docs[str(doc_id)]["connector_slug"] == "local_files"
    assert docs[str(doc_id)]["account_id"] is None

    other_user = uuid.uuid4()
    other = client.get("/connectors?scope=personal", headers={"X-User-Id": str(other_user)})
    assert other.status_code == 200
    assert str(doc_id) not in {doc["doc_id"] for doc in other.json()["documents"]}


def test_company_node_rejects_personal_connector_sync_routes(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.conn_github_token = "secret-token"
    settings.conn_github_repos = "example-org/orthus"

    for slug in PERSONAL_ONLY_CONNECTOR_SLUGS:
        synced = client.post(f"/connectors/{slug}/sync")
        assert synced.status_code == 400, slug
        assert "personal-node only" in synced.json()["detail"]


def test_company_node_ignores_stale_personal_only_company_accounts(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    account_id = uuid.uuid4()
    run_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    now = datetime(2026, 6, 2, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="gws_drive",
                account_kind="company",
                node_id="company",
                scope="company",
                owner_id=None,
                auth_mode="local_cli",
                account_label="stale company gws account",
                status="active",
                settings_redacted={"config_source": "legacy-bug"},
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_sync_state).values(
                account_id=account_id,
                cursor_json={},
                seen_json={},
                daily_budget_json={},
                last_sync_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_runs).values(
                run_id=run_id,
                account_id=account_id,
                connector_slug="gws_drive",
                reason="manual",
                status="succeeded",
                fetched=1,
                created=1,
                updated=0,
                skipped=0,
                errors=0,
                started_at=now,
                finished_at=now,
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="Stale GWS document",
                block_json={},
                markdown="stale",
                source="gws_drive",
                source_account_id=account_id,
                source_external_id="drive:stale",
                source_last_edited_at=now,
                schema_version=1,
                scope="company",
                project="company",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()

    listed = client.get("/connectors")

    assert listed.status_code == 200
    body = listed.json()
    manifest_slugs = {m["slug"] for m in body["manifests"]}
    assert {"notion", "slack"}.issubset(manifest_slugs)
    assert "gws_drive" not in manifest_slugs
    assert all(a["connector_slug"] != "gws_drive" for a in body["accounts"])
    assert all(r["connector_slug"] != "gws_drive" for r in body["runs"])
    assert all(d["connector_slug"] != "gws_drive" for d in body["documents"])

    synced = client.post("/connectors/gws_drive/sync")
    assert synced.status_code == 400
    assert "personal-node only" in synced.json()["detail"]


def test_company_wiki_retrieval_ignores_stale_personal_only_connector_pages(user_id):
    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    now = datetime(2026, 6, 2, tzinfo=UTC)
    gws_account_id = uuid.uuid4()
    notion_account_id = uuid.uuid4()
    gws_doc_id = uuid.uuid4()
    notion_doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=gws_account_id,
                connector_slug="gws_drive",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="local_cli",
                account_label="stale company gws account",
                status="active",
                settings_redacted={},
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=notion_account_id,
                connector_slug="notion",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="token",
                account_label="company notion",
                status="active",
                settings_redacted={},
                created_at=now,
                updated_at=now,
            )
        )
        for doc_id, account_id, source, title, markdown in [
            (
                gws_doc_id,
                gws_account_id,
                "gws_drive",
                "central-boundary-gws",
                "central-boundary-gws-secret should not ground central ask.",
            ),
            (
                notion_doc_id,
                notion_account_id,
                "notion",
                "central-boundary-notion",
                "central-boundary-notion-policy should ground central ask.",
            ),
        ]:
            s.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=user_id,
                    title=title,
                    block_json={},
                    markdown=markdown,
                    source=source,
                    source_account_id=account_id,
                    source_external_id=f"{source}:{doc_id}",
                    source_last_edited_at=now,
                    schema_version=1,
                    scope="company",
                    project="company",
                    created_at=now,
                    updated_at=now,
                )
            )
        s.commit()

    gws_source_slug = source_slug_from_title("central-boundary-gws", gws_doc_id)
    notion_source_slug = source_slug_from_title("central-boundary-notion", notion_doc_id)
    _write_company_wiki_chain(
        user_id,
        doc_id=gws_doc_id,
        source_slug=gws_source_slug,
        claim_slug="central-boundary-gws-claim",
        page_slug="central-boundary-gws-secret",
        text="central-boundary-gws-secret must stay hidden from company retrieval.",
    )
    _write_company_wiki_chain(
        user_id,
        doc_id=notion_doc_id,
        source_slug=notion_source_slug,
        claim_slug="central-boundary-notion-claim",
        page_slug="central-boundary-notion-policy",
        text="central-boundary-notion-policy is valid company grounding.",
    )

    hits = retrieve(
        user_id,
        "central-boundary-gws-secret central-boundary-notion-policy",
        k=10,
        scope="company",
    )
    slugs = {hit.page_slug for hit in hits}

    assert "central-boundary-notion-policy" in slugs
    assert "central-boundary-gws-secret" not in slugs
    assert "central-boundary-gws-claim" not in slugs


def test_company_wiki_retrieval_keeps_clean_source_on_contaminated_page(user_id):
    """A broad page can mention both stale GWS and clean promoted sources.

    Unsupported source blocking must hide the stale branch without walking back
    through that shared page and hiding every clean source page attached to it.
    """

    settings = get_settings()
    settings.node_kind = "company"
    settings.node_id = "company"
    now = datetime(2026, 6, 2, tzinfo=UTC)
    gws_account_id = uuid.uuid4()
    gws_doc_id = uuid.uuid4()
    promoted_doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=gws_account_id,
                connector_slug="gws_drive",
                account_kind="company",
                node_id="company",
                scope="company",
                auth_mode="local_cli",
                account_label="stale company gws account",
                status="active",
                settings_redacted={},
                created_at=now,
                updated_at=now,
            )
        )
        for doc_id, source, title, source_account_id, markdown in [
            (
                gws_doc_id,
                "gws_drive",
                "overblock-gws",
                gws_account_id,
                "overblock-gws-secret must not ground central ask.",
            ),
            (
                promoted_doc_id,
                "promoted_personal",
                "overblock-promoted",
                None,
                "overblock-promoted-policy is clean central promoted knowledge.",
            ),
        ]:
            s.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=user_id,
                    title=title,
                    block_json={},
                    markdown=markdown,
                    source=source,
                    source_account_id=source_account_id,
                    source_external_id=f"{source}:{doc_id}",
                    source_last_edited_at=now,
                    schema_version=1,
                    scope="company",
                    project="company",
                    created_at=now,
                    updated_at=now,
                )
            )
        s.commit()

    overblock_gws_slug = source_slug_from_title("overblock-gws", gws_doc_id)
    overblock_promoted_slug = source_slug_from_title("overblock-promoted", promoted_doc_id)
    _write_company_wiki_source_and_claim(
        user_id,
        doc_id=gws_doc_id,
        source_slug=overblock_gws_slug,
        claim_slug="overblock-gws-claim",
        page_slug="doc",
        text="overblock-gws-secret must stay hidden.",
    )
    _write_company_wiki_source_and_claim(
        user_id,
        doc_id=promoted_doc_id,
        source_slug=overblock_promoted_slug,
        claim_slug="overblock-promoted-claim",
        page_slug="doc",
        text="overblock-promoted-policy is clean central promoted knowledge.",
    )
    wiki_store.write_page(
        WikiPage(
            slug="doc",
            title="doc",
            definition="Shared generic doc page.",
            overview="Shared generic doc page.",
            evidence=["overblock-gws-claim", "overblock-promoted-claim"],
            sources=[overblock_gws_slug, overblock_promoted_slug],
            backlinks=["overblock-gws-claim", "overblock-promoted-claim"],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )

    hits = retrieve(
        user_id,
        "overblock-gws-secret overblock-promoted-policy",
        k=10,
        scope="company",
    )
    slugs = {hit.page_slug for hit in hits}

    assert "overblock-promoted-claim" in slugs
    assert "overblock-gws-claim" not in slugs
    assert overblock_gws_slug not in slugs
    assert "doc" not in slugs


def _write_company_wiki_chain(
    user_id: UUID,
    *,
    doc_id: UUID,
    source_slug: str,
    claim_slug: str,
    page_slug: str,
    text: str,
) -> None:
    wiki_store.write_source(
        WikiSource(
            slug=source_slug,
            title=source_slug,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime(2026, 6, 2, tzinfo=UTC),
            summary=text,
            key_claims=[claim_slug],
            related_pages=[page_slug],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    wiki_store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim=text,
            supporting=[source_slug],
            confidence="high",
            last_reviewed=date(2026, 6, 2),
            related_pages=[page_slug],
            evidence=text,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    wiki_store.write_page(
        WikiPage(
            slug=page_slug,
            title=page_slug,
            definition=text,
            overview=text,
            evidence=[claim_slug],
            sources=[source_slug],
            backlinks=[claim_slug],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )


def _write_company_wiki_source_and_claim(
    user_id: UUID,
    *,
    doc_id: UUID,
    source_slug: str,
    claim_slug: str,
    page_slug: str,
    text: str,
) -> None:
    wiki_store.write_source(
        WikiSource(
            slug=source_slug,
            title=source_slug,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime(2026, 6, 2, tzinfo=UTC),
            summary=text,
            key_claims=[claim_slug],
            related_pages=[page_slug],
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )
    wiki_store.write_claim(
        WikiClaim(
            slug=claim_slug,
            claim=text,
            supporting=[source_slug],
            confidence="high",
            last_reviewed=date(2026, 6, 2),
            related_pages=[page_slug],
            evidence=text,
        ),
        user_id=user_id,
        scope="company",
        project="company",
    )


def test_connectors_api_rejects_unsupported_default_account(user_id):
    res = client.post("/connectors/unknown/ensure")
    assert res.status_code == 400
    assert "unsupported connector" in res.json()["detail"]


def test_connectors_api_queues_background_sync(user_id, tmp_path, monkeypatch):
    root = tmp_path / "chat-exports"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "api-bg-conv",
                    "title": "API background connector sync",
                    "mapping": {
                        "u": {
                            "message": {
                                "author": {"role": "user"},
                                "content": {"parts": ["API background sync user message"]},
                            }
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_chat_exports_roots", str(root))

    synced = client.post(
        "/connectors/chat_exports/sync",
        json={"concurrency": 2, "background": True},
    )

    assert synced.status_code == 200
    sync_body = synced.json()
    assert sync_body["status"] == "running"
    assert sync_body["report"] is None

    relisted = client.get("/connectors")
    assert relisted.status_code == 200
    run = next(r for r in relisted.json()["runs"] if r["run_id"] == sync_body["run_id"])
    assert run["status"] == "succeeded"
    assert run["created"] == 1


def test_connectors_api_rejects_notion_without_token(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_notion_token", "")

    res = client.post("/connectors/notion/ensure")

    assert res.status_code == 400
    assert res.json()["detail"] == "ORTHUS_CONN_NOTION_TOKEN not set"


def test_connectors_api_syncs_notion_through_generic_surface(
    user_id,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_notion_token", "secret-token")
    monkeypatch.setattr(notion_provider, "NotionConnector", _FakeNotionConnector)

    listed = client.get("/connectors")
    assert listed.status_code == 200
    manifest = next(m for m in listed.json()["manifests"] if m["slug"] == "notion")
    assert manifest["can_ensure_default"] is True
    assert manifest["default_configured"] is True
    assert manifest["config_error"] is None

    ensured = client.post("/connectors/notion/ensure")
    assert ensured.status_code == 200
    assert ensured.json()["connector_slug"] == "notion"
    assert ensured.json()["settings_redacted"]["token_configured"] is True

    synced = client.post("/connectors/notion/sync")
    assert synced.status_code == 200
    assert synced.json()["status"] == "succeeded"
    assert synced.json()["report"]["created"] == 1

    with session() as s:
        doc = s.execute(select(documents.c.source, documents.c.scope)).one()
    assert doc.source == "notion"
    assert doc.scope == "personal"


def test_connectors_api_configures_github_from_web_secret(
    user_id,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_github_token", "")
    monkeypatch.setattr(settings, "conn_github_repos", "")
    monkeypatch.setattr(settings, "secret_backend", "memory")
    monkeypatch.setattr(github_provider, "GitHubConnector", _FakeGitHubConnector)
    clear_memory_secrets()

    listed = client.get("/connectors")
    assert listed.status_code == 200
    manifest = next(m for m in listed.json()["manifests"] if m["slug"] == "github")
    assert manifest["default_configured"] is False
    assert {field["key"] for field in manifest["config_fields"]} == {
        "token",
        "repos",
        "max_items",
    }

    configured = client.put(
        "/connectors/github/config",
        json={
            "secrets": {"token": "ghp_web_config_token"},
            "settings": {"repos": "Example-Org/Orthus"},
        },
    )
    assert configured.status_code == 200
    account = configured.json()
    assert account["connector_slug"] == "github"
    settings_redacted = account["settings_redacted"]
    assert "ghp_web_config_token" not in json.dumps(settings_redacted)
    assert settings_redacted["config_source"] == "web"
    assert settings_redacted["token_configured"] is True
    assert settings_redacted["repos"] == ["Example-Org/Orthus"]
    assert settings_redacted["secret_refs"]["token"].startswith("orthus/connectors/")

    relisted = client.get("/connectors")
    assert relisted.status_code == 200
    manifest = next(m for m in relisted.json()["manifests"] if m["slug"] == "github")
    assert manifest["default_configured"] is True
    assert manifest["config_error"] is None

    synced = client.post("/connectors/github/sync")
    assert synced.status_code == 200
    assert synced.json()["status"] == "succeeded"
    assert synced.json()["report"]["created"] == 1

    with session() as s:
        doc = s.execute(select(documents.c.source, documents.c.scope)).one()
    assert doc.source == "github"
    assert doc.scope == "personal"


def test_failed_sync_run_surfaces_in_connectors_list(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_github_token", "")
    monkeypatch.setattr(settings, "conn_github_repos", "")
    monkeypatch.setattr(settings, "secret_backend", "memory")
    monkeypatch.setattr(github_provider, "GitHubConnector", _FailingGitHubConnector)
    clear_memory_secrets()

    configured = client.put(
        "/connectors/github/config",
        json={
            "secrets": {"token": "ghp_web_config_token"},
            "settings": {"repos": "example-org/orthus"},
        },
    )
    assert configured.status_code == 200
    account_id = configured.json()["account_id"]

    synced = client.post("/connectors/github/sync")

    assert synced.status_code == 200
    sync_body = synced.json()
    assert sync_body["account_id"] == account_id
    assert sync_body["connector_slug"] == "github"
    assert sync_body["status"] == "failed"
    assert sync_body["report"] is None
    assert "RuntimeError: github sync failed for policy evidence" in sync_body["error_message"]

    relisted = client.get("/connectors")
    assert relisted.status_code == 200
    body = relisted.json()
    account = next(a for a in body["accounts"] if a["connector_slug"] == "github")
    assert account["account_id"] == account_id
    assert "RuntimeError: github sync failed for policy evidence" in account["last_error"]
    assert account["last_sync_at"] is None
    run = next(r for r in body["runs"] if r["run_id"] == sync_body["run_id"])
    assert run["account_id"] == account_id
    assert run["connector_slug"] == "github"
    assert run["reason"] == "manual"
    assert run["status"] == "failed"
    assert run["errors"] == 1
    assert "RuntimeError: github sync failed for policy evidence" in run["error_message"]
    assert run["finished_at"] is not None


def test_connectors_api_syncs_gws_gmail_without_web_secret(
    user_id,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_gws_command", "gws")
    monkeypatch.setattr(settings, "conn_gws_gmail_query", "newer_than:7d")
    monkeypatch.setattr(settings, "conn_gws_gmail_max_messages", 5)
    monkeypatch.setattr(settings, "conn_gws_gmail_max_body_chars", 1024)
    monkeypatch.setattr(default_accounts, "gws_command_available", lambda command: True)
    monkeypatch.setattr(gws_account, "gws_command_available", lambda command: True)
    monkeypatch.setattr(gws_provider, "GwsGmailConnector", _FakeGwsGmailConnector)

    listed = client.get("/connectors")
    assert listed.status_code == 200
    manifest = next(m for m in listed.json()["manifests"] if m["slug"] == "gws_gmail")
    assert manifest["default_configured"] is True
    assert {field["key"] for field in manifest["config_fields"]} == {
        "query",
        "max_messages",
        "max_body_chars",
    }
    assert manifest["auth_modes"] == ["local_cli"]

    ensured = client.post("/connectors/gws_gmail/ensure")
    assert ensured.status_code == 200
    account = ensured.json()
    assert account["auth_mode"] == "local_cli"
    assert account["settings_redacted"]["config_source"] == "gws_cli"
    assert account["settings_redacted"]["command"] == "gws"

    synced = client.post("/connectors/gws_gmail/sync")
    assert synced.status_code == 200
    assert synced.json()["status"] == "succeeded"
    assert synced.json()["report"]["created"] == 1

    with session() as s:
        doc = s.execute(select(documents.c.source, documents.c.scope)).one()
    assert doc.source == "gws_gmail"
    assert doc.scope == "personal"


def test_connectors_api_gws_gmail_legacy_30d_query_falls_back_to_anywhere(
    user_id,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_gws_command", "gws")
    monkeypatch.setattr(settings, "conn_gws_gmail_query", "in:anywhere newer_than:30d")
    monkeypatch.setattr(settings, "conn_gws_gmail_max_messages", 5)
    monkeypatch.setattr(settings, "conn_gws_gmail_max_body_chars", 1024)
    monkeypatch.setattr(default_accounts, "gws_command_available", lambda command: True)
    monkeypatch.setattr(gws_account, "gws_command_available", lambda command: True)
    monkeypatch.setattr(gws_provider, "GwsGmailConnector", _FakeLegacyGwsGmailConnector)

    ensured = client.post("/connectors/gws_gmail/ensure")
    assert ensured.status_code == 200
    assert ensured.json()["settings_redacted"]["query"] == "in:anywhere newer_than:30d"

    synced = client.post("/connectors/gws_gmail/sync")
    assert synced.status_code == 200
    assert synced.json()["status"] == "succeeded"
    assert synced.json()["report"]["created"] == 1


def test_connectors_api_defaults_codex_sessions_roots(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_codex_sessions_roots", "")

    listed = client.get("/connectors")
    assert listed.status_code == 200
    manifest = next(m for m in listed.json()["manifests"] if m["slug"] == "codex_sessions")
    assert manifest["default_configured"] is True
    assert manifest["config_error"] is None
    assert {field["key"] for field in manifest["config_fields"]} == {
        "roots",
        "max_bytes",
        "max_files",
        "max_messages",
        "max_message_chars",
    }

    ensured = client.post("/connectors/codex_sessions/ensure")
    assert ensured.status_code == 200
    settings_redacted = ensured.json()["settings_redacted"]
    assert settings_redacted["config_source"] == "default_paths"
    assert settings_redacted["root_count"] >= 2


def test_connectors_api_bootstraps_demo_user_for_fe_header(
    clean,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "chat-exports"
    root.mkdir()

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_chat_exports_roots", str(root))

    demo_user = UUID("00000000-0000-4000-8000-000000000001")
    res = client.post(
        "/connectors/chat_exports/ensure",
        headers={"X-User-Id": str(demo_user)},
    )

    assert res.status_code == 200
    assert res.json()["owner_id"] == str(demo_user)
    with session() as s:
        row = s.execute(select(users.c.display_name).where(users.c.user_id == demo_user)).one()
    assert row.display_name == "Demo User"


def test_session_member_cannot_trigger_connector_command(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8830")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3830")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_personal_ys")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "personal_owner_email", "owner@example.com")

    owner_client = TestClient(app)
    assert (
        owner_client.post("/auth/dev-login", json={"email": "owner@example.com"}).status_code == 200
    )
    invited = owner_client.post(
        "/auth/allowlist",
        json={"email": "member@example.com", "role": "member"},
    )
    assert invited.status_code == 201

    member_client = TestClient(app)
    login = member_client.post("/auth/dev-login", json={"email": "member@example.com"})
    assert login.status_code == 200
    assert login.json()["role"] == "member"

    res = member_client.post("/connectors/chat_exports/ensure")

    assert res.status_code == 403
    assert res.json()["detail"] == "node operator required"

    config = member_client.put(
        "/connectors/chat_exports/config",
        json={"settings": {}, "secrets": {}},
    )
    assert config.status_code == 403
    assert config.json()["detail"] == "node operator required"

    sync = member_client.post("/connectors/chat_exports/sync")
    assert sync.status_code == 403
    assert sync.json()["detail"] == "node operator required"

    notion_import = member_client.post("/connectors/notion/import", json={})
    assert notion_import.status_code == 403
    assert notion_import.json()["detail"] == "node operator required"
