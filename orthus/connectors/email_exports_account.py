"""Node-local account helper for email export connector."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.email_exports import roots_from_settings
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_email_exports_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update the personal node default email export account."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError("email_exports connector is personal-node only")
    roots = roots_from_settings(connector_roots_setting(settings, "email_exports"))
    if not roots:
        raise ValueError("email export inbox not configured")

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="email_exports",
            account_kind="personal",
            node_id=settings.node_id,
            auth_mode="local_path",
            owner_id=user_id,
            account_label=f"{settings.node_id} Email Exports",
            settings_redacted={
                "config_source": "default_paths",
                "root_count": len(roots),
                "max_bytes": settings.conn_email_exports_max_bytes,
                "max_body_chars": settings.conn_email_exports_max_body_chars,
            },
        )
    )
