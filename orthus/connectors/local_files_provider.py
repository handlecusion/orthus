"""Registry provider for the personal local-files connector."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_csv_setting, account_int_setting
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.local_files import (
    LocalFilesConnector,
    parse_local_file_extensions,
    parse_local_file_roots,
)
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class LocalFilesConnectorProvider:
    manifest = ConnectorManifest(
        slug="local_files",
        label="Local Files",
        source_kind="local_file",
        account_kinds=("personal",),
        auth_modes=("local_path",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=15 * 60,
        default_daily_budget=1000,
        privacy_class="personal_local",
        redaction_profile="strict",
        import_mode="parallel",
        description="Text files under node-local allowlisted directories.",
        settings_keys=(
            "ORTHUS_CONN_LOCAL_FILES_ROOTS",
            "ORTHUS_CONN_LOCAL_FILES_EXTENSIONS",
            "ORTHUS_CONN_LOCAL_FILES_MAX_BYTES",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> LocalFilesConnector:
        del state
        settings = get_settings()
        roots = parse_local_file_roots(
            account_csv_setting(
                account,
                "roots",
                connector_roots_setting(settings, "local_files"),
            )
        )
        if not roots:
            raise RuntimeError("local file roots not configured")
        extensions = parse_local_file_extensions(
            account_csv_setting(account, "extensions", settings.conn_local_files_extensions)
        )
        if not extensions:
            raise RuntimeError("local file extensions allow no files")
        return LocalFilesConnector(
            roots,
            extensions=extensions,
            max_bytes=account_int_setting(
                account,
                "max_bytes",
                settings.conn_local_files_max_bytes,
            ),
        )
