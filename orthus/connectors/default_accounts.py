"""Default node-local connector account ensure dispatch.

FE/API/CLI all use this module so adding a connector means registering one
provider plus one account-ensure function.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from orthus.connectors.ai_sessions import roots_from_settings as ai_session_roots
from orthus.connectors.ai_sessions_account import ensure_ai_sessions_account
from orthus.connectors.chat_exports import roots_from_settings as chat_export_roots
from orthus.connectors.chat_exports_account import ensure_chat_exports_account
from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.email_exports import roots_from_settings as email_export_roots
from orthus.connectors.email_exports_account import ensure_email_exports_account
from orthus.connectors.github import parse_github_repos
from orthus.connectors.github_account import ensure_github_account
from orthus.connectors.gws_account import (
    ensure_gws_drive_account,
    ensure_gws_gmail_account,
)
from orthus.connectors.gws_cli import gws_command_available, gws_install_message
from orthus.connectors.local_files_account import ensure_local_files_account
from orthus.connectors.local_files import (
    parse_local_file_extensions,
    parse_local_file_roots,
)
from orthus.connectors.notion_account import (
    adopt_legacy_notion_documents,
    ensure_notion_account,
)
from orthus.connectors.slack import parse_slack_channels
from orthus.connectors.slack_account import ensure_slack_account
from orthus.settings import Settings, get_settings

DEFAULT_ACCOUNT_SLUGS = {
    "notion",
    "local_files",
    "codex_sessions",
    "claude_sessions",
    "chat_exports",
    "email_exports",
    "github",
    "slack",
    "gws_gmail",
    "gws_drive",
}


@dataclass(frozen=True)
class DefaultAccountConfigStatus:
    configured: bool
    error: str | None = None


def can_ensure_default_account(connector_slug: str) -> bool:
    return connector_slug.strip().lower() in DEFAULT_ACCOUNT_SLUGS


def default_account_config_status(
    connector_slug: str,
    settings: Settings | None = None,
) -> DefaultAccountConfigStatus:
    settings = settings or get_settings()
    slug = connector_slug.strip().lower()
    if slug not in DEFAULT_ACCOUNT_SLUGS:
        return DefaultAccountConfigStatus(False, "unsupported connector")
    if slug == "notion":
        return _has_env(settings.conn_notion_token, "ORTHUS_CONN_NOTION_TOKEN not set")
    if slug == "local_files":
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(False, "local_files connector is personal-node only")
        if not parse_local_file_roots(connector_roots_setting(settings, slug)):
            return DefaultAccountConfigStatus(False, "local file roots not configured")
        if not parse_local_file_extensions(settings.conn_local_files_extensions):
            return DefaultAccountConfigStatus(
                False, "ORTHUS_CONN_LOCAL_FILES_EXTENSIONS allows no files"
            )
        return DefaultAccountConfigStatus(True)
    if slug in {"codex_sessions", "claude_sessions"}:
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(False, f"{slug} connector is personal-node only")
        if not ai_session_roots(connector_roots_setting(settings, slug)):
            return DefaultAccountConfigStatus(False, f"{slug} roots not configured")
        return DefaultAccountConfigStatus(True)
    if slug == "chat_exports":
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(False, "chat_exports connector is personal-node only")
        return _has_roots(
            chat_export_roots(connector_roots_setting(settings, slug)),
            "chat export inbox not configured",
        )
    if slug == "email_exports":
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(
                False, "email_exports connector is personal-node only"
            )
        return _has_roots(
            email_export_roots(connector_roots_setting(settings, slug)),
            "email export inbox not configured",
        )
    if slug == "github":
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(False, "github connector is personal-node only")
        if not settings.conn_github_token:
            return DefaultAccountConfigStatus(False, "ORTHUS_CONN_GITHUB_TOKEN not set")
        if not parse_github_repos(settings.conn_github_repos):
            return DefaultAccountConfigStatus(False, "ORTHUS_CONN_GITHUB_REPOS not set")
        return DefaultAccountConfigStatus(True)
    if slug == "slack":
        if settings.node_kind != "company":
            return DefaultAccountConfigStatus(False, "slack connector is company-node only")
        if not settings.conn_slack_bot_token:
            return DefaultAccountConfigStatus(False, "ORTHUS_CONN_SLACK_BOT_TOKEN not set")
        if not parse_slack_channels(settings.conn_slack_channels):
            return DefaultAccountConfigStatus(False, "ORTHUS_CONN_SLACK_CHANNELS not set")
        return DefaultAccountConfigStatus(True)
    if slug in {"gws_gmail", "gws_drive"}:
        if settings.node_kind != "personal":
            return DefaultAccountConfigStatus(False, f"{slug} connector is personal-node only")
        if not gws_command_available(settings.conn_gws_command):
            return DefaultAccountConfigStatus(
                False,
                gws_install_message(settings.conn_gws_command),
            )
        return DefaultAccountConfigStatus(True)
    return DefaultAccountConfigStatus(False, f"unsupported connector: {connector_slug}")


def ensure_default_connector_account(
    connector_slug: str,
    user_id: UUID,
    settings: Settings | None = None,
) -> UUID:
    settings = settings or get_settings()
    slug = connector_slug.strip().lower()
    if slug == "notion":
        if not settings.conn_notion_token:
            raise ValueError("ORTHUS_CONN_NOTION_TOKEN not set")
        account_id = ensure_notion_account(user_id, settings)
        adopt_legacy_notion_documents(account_id)
        return account_id
    if slug == "local_files":
        return ensure_local_files_account(user_id, settings)
    if slug in {"codex_sessions", "claude_sessions"}:
        return ensure_ai_sessions_account(slug, user_id, settings)
    if slug == "chat_exports":
        return ensure_chat_exports_account(user_id, settings)
    if slug == "email_exports":
        return ensure_email_exports_account(user_id, settings)
    if slug == "github":
        return ensure_github_account(user_id, settings)
    if slug == "slack":
        return ensure_slack_account(user_id, settings)
    if slug == "gws_gmail":
        return ensure_gws_gmail_account(user_id, settings)
    if slug == "gws_drive":
        return ensure_gws_drive_account(user_id, settings)
    raise ValueError(f"unsupported connector: {connector_slug}")


def _has_env(value: str, error: str) -> DefaultAccountConfigStatus:
    return DefaultAccountConfigStatus(True) if value else DefaultAccountConfigStatus(False, error)


def _has_roots(roots: object, error: str) -> DefaultAccountConfigStatus:
    return DefaultAccountConfigStatus(True) if roots else DefaultAccountConfigStatus(False, error)
