"""Registry provider for the GitHub connector."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import (
    account_csv_setting,
    account_int_setting,
    account_secret,
)
from orthus.connectors.github import GitHubConnector, parse_github_repos
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class GitHubConnectorProvider:
    manifest = ConnectorManifest(
        slug="github",
        label="GitHub",
        source_kind="remote_api",
        account_kinds=("personal",),
        auth_modes=("token",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=30 * 60,
        default_daily_budget=500,
        privacy_class="mixed",
        redaction_profile="standard",
        import_mode="parallel",
        description="GitHub repository issues and pull requests via REST API.",
        settings_keys=(
            "ORTHUS_CONN_GITHUB_TOKEN",
            "ORTHUS_CONN_GITHUB_REPOS",
            "ORTHUS_CONN_GITHUB_MAX_ITEMS",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> GitHubConnector:
        del state
        settings = get_settings()
        token = account_secret(account, "token") or settings.conn_github_token
        repos = parse_github_repos(
            account_csv_setting(account, "repos", settings.conn_github_repos)
        )
        if not token:
            raise RuntimeError("GitHub token not configured")
        if not repos:
            raise RuntimeError("GitHub repos not configured")
        return GitHubConnector(
            token,
            repos,
            max_items=account_int_setting(
                account,
                "max_items",
                settings.conn_github_max_items,
            ),
        )
