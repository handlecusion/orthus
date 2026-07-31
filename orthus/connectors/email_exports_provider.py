"""Registry provider for the personal email export connector."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_csv_setting, account_int_setting
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.email_exports import EmailExportsConnector, roots_from_settings
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class EmailExportsConnectorProvider:
    manifest = ConnectorManifest(
        slug="email_exports",
        label="Email Exports",
        source_kind="export",
        account_kinds=("personal",),
        auth_modes=("local_path",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=15 * 60,
        default_daily_budget=500,
        privacy_class="personal_sensitive",
        redaction_profile="strict",
        import_mode="parallel",
        description="Node-local .eml, .mbox, and JSON email exports.",
        settings_keys=(
            "ORTHUS_CONN_EMAIL_EXPORTS_ROOTS",
            "ORTHUS_CONN_EMAIL_EXPORTS_MAX_BYTES",
            "ORTHUS_CONN_EMAIL_EXPORTS_MAX_BODY_CHARS",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> EmailExportsConnector:
        del state
        settings = get_settings()
        roots = roots_from_settings(
            account_csv_setting(
                account,
                "roots",
                connector_roots_setting(settings, "email_exports"),
            )
        )
        if not roots:
            raise RuntimeError("email export roots not configured")
        return EmailExportsConnector(
            roots,
            max_bytes=account_int_setting(
                account,
                "max_bytes",
                settings.conn_email_exports_max_bytes,
            ),
            max_body_chars=account_int_setting(
                account,
                "max_body_chars",
                settings.conn_email_exports_max_body_chars,
            ),
        )
