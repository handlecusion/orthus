"""Node-local account helper for chat export connector."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.chat_exports import roots_from_settings
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_chat_exports_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update the personal node default chat export account."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError("chat_exports connector is personal-node only")
    roots = roots_from_settings(connector_roots_setting(settings, "chat_exports"))
    if not roots:
        raise ValueError("chat export inbox not configured")

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="chat_exports",
            account_kind="personal",
            node_id=settings.node_id,
            auth_mode="local_path",
            owner_id=user_id,
            account_label=f"{settings.node_id} Chat Exports",
            settings_redacted={
                "config_source": "default_paths",
                "root_count": len(roots),
                "max_bytes": settings.conn_chat_exports_max_bytes,
                "max_messages": settings.conn_chat_exports_max_messages,
                "max_message_chars": settings.conn_chat_exports_max_message_chars,
            },
        )
    )
