"""Registry provider for Slack connector."""

from __future__ import annotations

from collections.abc import Mapping

from orthus.connectors.account_config import (
    account_csv_setting,
    account_int_setting,
    account_secret,
)
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.slack import (
    SlackConnector,
    parse_slack_channel_projects,
    parse_slack_channels,
)
from orthus.connectors.state import SyncState
from orthus.settings import get_settings


class SlackConnectorProvider:
    manifest = ConnectorManifest(
        slug="slack",
        label="Slack",
        source_kind="remote_api",
        account_kinds=("company",),
        auth_modes=("token",),
        capabilities=("read", "manual", "periodic"),
        default_interval_seconds=15 * 60,
        default_daily_budget=500,
        privacy_class="company",
        redaction_profile="standard",
        import_mode="parallel",
        description="Slack channel messages and threads via Web API.",
        settings_keys=(
            "ORTHUS_CONN_SLACK_BOT_TOKEN",
            "ORTHUS_CONN_SLACK_CHANNELS",
            "ORTHUS_CONN_SLACK_CHANNEL_PROJECTS",
            "ORTHUS_CONN_SLACK_MAX_MESSAGES",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> SlackConnector:
        del state
        settings = get_settings()
        token = account_secret(account, "token") or settings.conn_slack_bot_token
        channels = parse_slack_channels(
            account_csv_setting(account, "channels", settings.conn_slack_channels)
        )
        if not token:
            raise RuntimeError("Slack bot token not configured")
        if not channels:
            raise RuntimeError("Slack channels not configured")
        return SlackConnector(
            token,
            channels,
            max_messages=account_int_setting(
                account,
                "max_messages",
                settings.conn_slack_max_messages,
            ),
            channel_projects=parse_slack_channel_projects(
                account_csv_setting(
                    account,
                    "channel_projects",
                    settings.conn_slack_channel_projects,
                )
            ),
        )
