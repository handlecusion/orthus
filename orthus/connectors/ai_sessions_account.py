"""Node-local account helpers for AI session connectors."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.ai_sessions import roots_from_settings
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings

AI_SESSION_CONNECTORS = {
    "codex_sessions": ("conn_codex_sessions_roots", "Codex Sessions"),
    "claude_sessions": ("conn_claude_sessions_roots", "Claude Sessions"),
}


def ensure_ai_sessions_account(
    connector_slug: str,
    user_id: UUID,
    settings: Settings | None = None,
) -> UUID:
    """Create/update the personal node default AI session account."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError(f"{connector_slug} connector is personal-node only")
    if connector_slug not in AI_SESSION_CONNECTORS:
        raise ValueError(f"unsupported AI session connector: {connector_slug}")

    _roots_attr, label = AI_SESSION_CONNECTORS[connector_slug]
    roots = roots_from_settings(connector_roots_setting(settings, connector_slug))
    if not roots:
        raise ValueError(f"{connector_slug} roots not configured")

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug=connector_slug,
            account_kind="personal",
            node_id=settings.node_id,
            auth_mode="local_path",
            owner_id=user_id,
            account_label=f"{settings.node_id} {label}",
            settings_redacted={
                "config_source": "default_paths",
                "root_count": len(roots),
                "max_bytes": settings.conn_ai_sessions_max_bytes,
                "max_files": settings.conn_ai_sessions_max_files,
                "max_messages": settings.conn_ai_sessions_max_messages,
                "max_message_chars": settings.conn_ai_sessions_max_message_chars,
            },
        )
    )
