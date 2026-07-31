"""Registry providers for personal AI session connectors."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import account_csv_setting, account_int_setting
from orthus.connectors.ai_sessions import AiSessionsConnector, roots_from_settings
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class AiSessionsConnectorProvider:
    def __init__(self, *, slug: str, label: str, roots_attr: str) -> None:
        self.slug = slug
        self.label = label
        self.roots_attr = roots_attr
        self.manifest = ConnectorManifest(
            slug=slug,
            label=label,
            source_kind="local_app",
            account_kinds=("personal",),
            auth_modes=("local_path",),
            capabilities=("read", "manual", "periodic"),
            default_interval_seconds=15 * 60,
            default_daily_budget=1000,
            privacy_class="personal_sensitive",
            redaction_profile="strict",
            import_mode="parallel",
            description=f"{label} local session logs with tool payloads filtered out.",
            settings_keys=(
                f"ORTHUS_{roots_attr.upper()}",
                "ORTHUS_CONN_AI_SESSIONS_MAX_BYTES",
                "ORTHUS_CONN_AI_SESSIONS_MAX_FILES",
                "ORTHUS_CONN_AI_SESSIONS_MAX_MESSAGES",
                "ORTHUS_CONN_AI_SESSIONS_MAX_MESSAGE_CHARS",
            ),
        )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> AiSessionsConnector:
        del state
        settings = get_settings()
        roots = roots_from_settings(
            account_csv_setting(
                account,
                "roots",
                connector_roots_setting(settings, self.slug),
            )
        )
        if not roots:
            raise RuntimeError(f"{self.slug} roots not configured")
        return AiSessionsConnector(
            source=self.slug,
            label=self.label,
            roots=roots,
            max_bytes=account_int_setting(
                account,
                "max_bytes",
                settings.conn_ai_sessions_max_bytes,
            ),
            max_files=account_int_setting(
                account,
                "max_files",
                settings.conn_ai_sessions_max_files,
            ),
            max_messages=account_int_setting(
                account,
                "max_messages",
                settings.conn_ai_sessions_max_messages,
            ),
            max_message_chars=account_int_setting(
                account,
                "max_message_chars",
                settings.conn_ai_sessions_max_message_chars,
            ),
        )


def default_ai_session_providers() -> tuple[AiSessionsConnectorProvider, ...]:
    return (
        AiSessionsConnectorProvider(
            slug="codex_sessions",
            label="Codex Sessions",
            roots_attr="conn_codex_sessions_roots",
        ),
        AiSessionsConnectorProvider(
            slug="claude_sessions",
            label="Claude Sessions",
            roots_attr="conn_claude_sessions_roots",
        ),
    )
