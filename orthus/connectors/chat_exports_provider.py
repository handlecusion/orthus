"""Registry provider for personal chat exports."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_csv_setting, account_int_setting
from orthus.connectors.chat_exports import ChatExportsConnector, roots_from_settings
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class ChatExportsConnectorProvider:
    manifest = ConnectorManifest(
        slug="chat_exports",
        label="Chat Exports",
        source_kind="export",
        account_kinds=("personal",),
        auth_modes=("local_path",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=15 * 60,
        default_daily_budget=1000,
        privacy_class="personal_sensitive",
        redaction_profile="strict",
        import_mode="parallel",
        description="Official ChatGPT/Claude export files from a node-local inbox.",
        settings_keys=(
            "ORTHUS_CONN_CHAT_EXPORTS_ROOTS",
            "ORTHUS_CONN_CHAT_EXPORTS_MAX_BYTES",
            "ORTHUS_CONN_CHAT_EXPORTS_MAX_MESSAGES",
            "ORTHUS_CONN_CHAT_EXPORTS_MAX_MESSAGE_CHARS",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> ChatExportsConnector:
        del state
        settings = get_settings()
        roots = roots_from_settings(
            account_csv_setting(
                account,
                "roots",
                connector_roots_setting(settings, "chat_exports"),
            )
        )
        if not roots:
            raise RuntimeError("chat export roots not configured")
        return ChatExportsConnector(
            roots,
            max_bytes=account_int_setting(
                account,
                "max_bytes",
                settings.conn_chat_exports_max_bytes,
            ),
            max_messages=account_int_setting(
                account,
                "max_messages",
                settings.conn_chat_exports_max_messages,
            ),
            max_message_chars=account_int_setting(
                account,
                "max_message_chars",
                settings.conn_chat_exports_max_message_chars,
            ),
        )
