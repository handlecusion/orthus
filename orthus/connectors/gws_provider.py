"""Registry providers for Google Workspace sources through the gws CLI."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_int_setting, account_settings
from orthus.connectors.gws_cli import (
    DEFAULT_GWS_DRIVE_MAX_BYTES,
    DEFAULT_GWS_DRIVE_MAX_FILES,
    DEFAULT_GWS_DRIVE_QUERY,
    DEFAULT_GWS_GMAIL_MAX_BODY_CHARS,
    DEFAULT_GWS_GMAIL_MAX_MESSAGES,
    DEFAULT_GWS_GMAIL_QUERY,
    GwsCliRunner,
    GwsDriveConnector,
    GwsGmailConnector,
)
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings

_LEGACY_GMAIL_QUERY = "in:anywhere newer_than:30d"


class GwsGmailConnectorProvider:
    manifest = ConnectorManifest(
        slug="gws_gmail",
        label="Gmail (gws)",
        source_kind="remote_api",
        account_kinds=("personal",),
        auth_modes=("local_cli",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=15 * 60,
        default_daily_budget=200,
        privacy_class="personal_sensitive",
        redaction_profile="strict",
        import_mode="parallel",
        description="Gmail messages via the node-local gws CLI; no API key stored in orthus.",
        settings_keys=(
            "ORTHUS_CONN_GWS_COMMAND",
            "ORTHUS_CONN_GWS_GMAIL_QUERY",
            "ORTHUS_CONN_GWS_GMAIL_MAX_MESSAGES",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> GwsGmailConnector:
        del state
        settings = get_settings()
        stored = account_settings(account)
        query = _effective_gmail_query(
            str(stored.get("query") or settings.conn_gws_gmail_query or DEFAULT_GWS_GMAIL_QUERY)
        )
        return GwsGmailConnector(
            _runner(),
            query=query,
            max_messages=account_int_setting(
                account,
                "max_messages",
                settings.conn_gws_gmail_max_messages or DEFAULT_GWS_GMAIL_MAX_MESSAGES,
            ),
            max_body_chars=settings.conn_gws_gmail_max_body_chars
            or DEFAULT_GWS_GMAIL_MAX_BODY_CHARS,
        )


class GwsDriveConnectorProvider:
    manifest = ConnectorManifest(
        slug="gws_drive",
        label="Drive (gws)",
        source_kind="remote_api",
        account_kinds=("personal",),
        auth_modes=("local_cli",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=30 * 60,
        default_daily_budget=200,
        privacy_class="mixed",
        redaction_profile="standard",
        import_mode="parallel",
        description="Google Drive text/docs metadata and content via the node-local gws CLI.",
        settings_keys=(
            "ORTHUS_CONN_GWS_COMMAND",
            "ORTHUS_CONN_GWS_DRIVE_QUERY",
            "ORTHUS_CONN_GWS_DRIVE_MAX_FILES",
            "ORTHUS_CONN_GWS_DRIVE_MAX_BYTES",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> GwsDriveConnector:
        del state
        settings = get_settings()
        stored = account_settings(account)
        query = str(stored.get("query") or settings.conn_gws_drive_query or DEFAULT_GWS_DRIVE_QUERY)
        return GwsDriveConnector(
            _runner(),
            query=query,
            max_files=account_int_setting(
                account,
                "max_files",
                settings.conn_gws_drive_max_files or DEFAULT_GWS_DRIVE_MAX_FILES,
            ),
            max_bytes=settings.conn_gws_drive_max_bytes or DEFAULT_GWS_DRIVE_MAX_BYTES,
        )


def _runner() -> GwsCliRunner:
    settings = get_settings()
    return GwsCliRunner(
        command=settings.conn_gws_command,
        timeout_seconds=settings.conn_gws_timeout_seconds,
    )


def _effective_gmail_query(query: str) -> str:
    normalized = " ".join(query.split())
    if normalized == _LEGACY_GMAIL_QUERY:
        return DEFAULT_GWS_GMAIL_QUERY
    return normalized or DEFAULT_GWS_GMAIL_QUERY
