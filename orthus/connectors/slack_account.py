"""Node-local account helper for Slack connector."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.slack import parse_slack_channel_projects, parse_slack_channels
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_slack_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update company node default Slack account."""
    settings = settings or get_settings()
    if settings.node_kind != "company":
        raise ValueError("slack connector is company-node only")
    if not settings.conn_slack_bot_token:
        raise ValueError("ORTHUS_CONN_SLACK_BOT_TOKEN not set")
    channels = parse_slack_channels(settings.conn_slack_channels)
    if not channels:
        raise ValueError("ORTHUS_CONN_SLACK_CHANNELS not set")
    channel_projects = parse_slack_channel_projects(settings.conn_slack_channel_projects)

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="slack",
            account_kind="company",
            node_id=settings.node_id,
            auth_mode="token",
            owner_id=None,
            account_label=f"{settings.node_id} Slack",
            settings_redacted={
                "env": "ORTHUS_CONN_SLACK_BOT_TOKEN",
                "token_configured": True,
                "channel_count": len(channels),
                "channel_projects": settings.conn_slack_channel_projects,
                "channel_project_count": len(channel_projects),
                "max_messages": settings.conn_slack_max_messages,
            },
        )
    )
