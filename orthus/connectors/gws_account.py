"""Node-local account helpers for gws CLI connectors."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.gws_cli import (
    gws_command_available,
    gws_command_label,
    gws_install_message,
)
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_gws_gmail_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    settings = settings or get_settings()
    return _ensure_gws_account(
        "gws_gmail",
        "Gmail (gws)",
        user_id,
        settings,
        extra={
            "query": settings.conn_gws_gmail_query,
            "max_messages": settings.conn_gws_gmail_max_messages,
        },
    )


def ensure_gws_drive_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    settings = settings or get_settings()
    return _ensure_gws_account(
        "gws_drive",
        "Drive (gws)",
        user_id,
        settings,
        extra={
            "query": settings.conn_gws_drive_query,
            "max_files": settings.conn_gws_drive_max_files,
        },
    )


def _ensure_gws_account(
    slug: str,
    label: str,
    user_id: UUID,
    settings: Settings | None,
    *,
    extra: dict,
) -> UUID:
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError(f"{slug} connector is personal-node only")
    if not gws_command_available(settings.conn_gws_command):
        raise ValueError(gws_install_message(settings.conn_gws_command))

    account_kind = "personal"
    owner_id = user_id
    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug=slug,
            account_kind=account_kind,
            node_id=settings.node_id,
            auth_mode="local_cli",
            owner_id=owner_id,
            account_label=f"{settings.node_id} {label}",
            settings_redacted={
                "config_source": "gws_cli",
                "command": gws_command_label(settings.conn_gws_command),
                **extra,
            },
        )
    )
