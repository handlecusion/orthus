"""C8 GitHub personal connector."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from orthus.connectors import github_provider
from orthus.connectors.github import GitHubConnector, parse_github_repos
from orthus.connectors.github_account import ensure_github_account
from orthus.connectors.registry import get_connector_provider, register_default_connector_providers
from orthus.connectors.runner import run_connector_account_sync
from orthus.connectors.state import get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import InternalDocument
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents


class _FakeGitHubConnector:
    def __init__(self, token: str, repos: tuple[str, ...], *, max_items: int) -> None:
        self.token = token
        self.repos = repos
        self.max_items = max_items

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield InternalDocument(
            title="GitHub acme/app #1: Runner task",
            markdown="GitHub task body",
            source="github",
            source_external_id="github:acme/app:issue:1",
            source_last_edited_at=datetime(2026, 5, 31, tzinfo=UTC),
            project="company",
        )


def test_parse_github_repos_accepts_slugs_and_urls():
    assert parse_github_repos("acme/app, https://github.com/AcmeInc/api/issues") == (
        "acme/app",
        "acmeinc/api",
    )


def test_github_connector_reads_issues_and_pull_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/app/issues"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 12,
                    "title": "Fix connector token",
                    "body": "Secret token=ghp_abcdefghijklmnopqrstuvwxyz",
                    "state": "open",
                    "html_url": "https://github.com/acme/app/issues/12",
                    "updated_at": "2026-05-31T00:00:00Z",
                    "user": {"login": "ys"},
                    "labels": [{"name": "bug"}],
                },
                {
                    "number": 13,
                    "title": "Add runner PR",
                    "body": "PR body",
                    "state": "closed",
                    "html_url": "https://github.com/acme/app/pull/13",
                    "updated_at": "2026-05-31T00:01:00Z",
                    "user": {"login": "ys"},
                    "labels": [],
                    "pull_request": {"url": "https://api.github.com/repos/acme/app/pulls/13"},
                },
            ],
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    docs = list(GitHubConnector("token", ["acme/app"], client=client).iter_documents(None))

    assert len(docs) == 2
    assert docs[0].source == "github"
    assert docs[0].source_external_id == "github:acme/app:issue:12"
    assert "Kind: issue" in docs[0].markdown
    assert "[REDACTED_SECRET]" in docs[0].markdown
    assert docs[1].source_external_id == "github:acme/app:pull_request:13"
    assert "Kind: pull_request" in docs[1].markdown


def test_ensure_github_account_uses_node_policy(user_id: UUID, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "conn_github_token", "secret-token")
    monkeypatch.setattr(settings, "conn_github_repos", "acme/app")
    monkeypatch.setattr(settings, "conn_github_max_items", 25)

    with pytest.raises(ValueError, match="personal-node only"):
        ensure_github_account(user_id, settings)

    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    personal_id = ensure_github_account(user_id, settings)
    with session() as s:
        personal = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == personal_id)
        ).one()
    assert personal.account_kind == "personal"
    assert personal.scope == "personal"
    assert personal.owner_id == user_id


def test_github_runner_imports_personal_docs(user_id: UUID, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_github_token", "secret-token")
    monkeypatch.setattr(settings, "conn_github_repos", "acme/app")
    monkeypatch.setattr(settings, "conn_github_max_items", 25)
    monkeypatch.setattr(github_provider, "GitHubConnector", _FakeGitHubConnector)
    register_default_connector_providers(replace=True)

    provider = get_connector_provider("github")
    assert provider is not None
    assert not provider.manifest.supports_account_kind("company")
    assert provider.manifest.supports_account_kind("personal")

    account_id = ensure_github_account(user_id, settings)
    first = run_connector_account_sync(account_id, user_id, chat_model=MockChat())

    assert first.status == "succeeded"
    assert first.report is not None
    assert first.report.created == 1
    assert first.report.errors == 0

    with session() as s:
        doc = s.execute(
            select(documents.c.source, documents.c.scope, documents.c.source_account_id)
        ).one()
        item_count = s.execute(select(func.count()).select_from(connector_items)).scalar_one()

    assert doc.source == "github"
    assert doc.scope == "personal"
    assert doc.source_account_id == account_id
    assert item_count == 1

    state = get_sync_state(account_id)
    assert state is not None
    assert state.last_sync_at is not None
    assert state.last_error is None
