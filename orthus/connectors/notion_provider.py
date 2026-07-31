"""Default registry provider for the existing Notion connector."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_secret
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.notion import NotionConnector
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class NotionConnectorProvider:
    manifest = ConnectorManifest(
        slug="notion",
        label="Notion",
        source_kind="remote_api",
        account_kinds=("company", "personal"),
        auth_modes=("token",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=30 * 60,
        default_daily_budget=500,
        privacy_class="mixed",
        redaction_profile="standard",
        import_mode="parallel",
        description="Notion pages and database rows via the existing Notion API connector.",
        settings_keys=("ORTHUS_CONN_NOTION_TOKEN",),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> NotionConnector:
        del state
        token = account_secret(account, "token") or get_settings().conn_notion_token
        if not token:
            raise RuntimeError("Notion token not configured")
        return NotionConnector(token)
