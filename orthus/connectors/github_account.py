"""Node-local account helper for GitHub connector."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.github import parse_github_repos
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_github_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update node-local default GitHub account."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError("github connector is personal-node only")
    if not settings.conn_github_token:
        raise ValueError("ORTHUS_CONN_GITHUB_TOKEN not set")
    repos = parse_github_repos(settings.conn_github_repos)
    if not repos:
        raise ValueError("ORTHUS_CONN_GITHUB_REPOS not set")

    account_kind = "personal"
    owner_id = user_id
    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="github",
            account_kind=account_kind,
            node_id=settings.node_id,
            auth_mode="token",
            owner_id=owner_id,
            account_label=f"{settings.node_id} GitHub",
            settings_redacted={
                "env": "ORTHUS_CONN_GITHUB_TOKEN",
                "token_configured": True,
                "repo_count": len(repos),
                "max_items": settings.conn_github_max_items,
            },
        )
    )
