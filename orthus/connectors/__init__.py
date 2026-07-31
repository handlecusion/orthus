"""Connector package — source-agnostic import interface (prompt §4.1)."""

from orthus.connectors.base import (
    Connector,
    ImportReport,
    run_import,
    run_parallel_import,
)
from orthus.connectors.ai_sessions import AiSessionsConnector
from orthus.connectors.ai_sessions_account import ensure_ai_sessions_account
from orthus.connectors.chat_exports import ChatExportsConnector
from orthus.connectors.chat_exports_account import ensure_chat_exports_account
from orthus.connectors.email_exports import EmailExportsConnector
from orthus.connectors.email_exports_account import ensure_email_exports_account
from orthus.connectors.github import GitHubConnector
from orthus.connectors.github_account import ensure_github_account
from orthus.connectors.gws_account import (
    ensure_gws_drive_account,
    ensure_gws_gmail_account,
)
from orthus.connectors.gws_cli import GwsDriveConnector, GwsGmailConnector
from orthus.connectors.default_accounts import (
    can_ensure_default_account,
    default_account_config_status,
    ensure_default_connector_account,
)
from orthus.connectors.manifest import (
    ConnectorManifest,
    FactoryConnectorProvider,
)
from orthus.connectors.local_files import LocalFilesConnector
from orthus.connectors.local_files_account import ensure_local_files_account
from orthus.connectors.notion import NotionConnector
from orthus.connectors.notion_account import (
    adopt_legacy_notion_documents,
    ensure_notion_account,
)
from orthus.connectors.slack import SlackConnector
from orthus.connectors.slack_account import ensure_slack_account
from orthus.connectors.registry import (
    get_connector_provider,
    list_connector_manifests,
    register_default_connector_providers,
    register_connector_provider,
)
from orthus.connectors.runner import (
    due_connector_accounts,
    run_connector_account_sync,
    run_due_connector_syncs,
)
from orthus.connectors.state import ConnectorAccountSpec, create_connector_account

__all__ = [
    "Connector",
    "ConnectorAccountSpec",
    "ConnectorManifest",
    "FactoryConnectorProvider",
    "ImportReport",
    "AiSessionsConnector",
    "ChatExportsConnector",
    "EmailExportsConnector",
    "GitHubConnector",
    "GwsDriveConnector",
    "GwsGmailConnector",
    "LocalFilesConnector",
    "SlackConnector",
    "adopt_legacy_notion_documents",
    "can_ensure_default_account",
    "create_connector_account",
    "default_account_config_status",
    "ensure_ai_sessions_account",
    "ensure_chat_exports_account",
    "ensure_email_exports_account",
    "ensure_github_account",
    "ensure_gws_drive_account",
    "ensure_gws_gmail_account",
    "ensure_slack_account",
    "ensure_default_connector_account",
    "ensure_local_files_account",
    "due_connector_accounts",
    "ensure_notion_account",
    "get_connector_provider",
    "list_connector_manifests",
    "register_default_connector_providers",
    "register_connector_provider",
    "run_connector_account_sync",
    "run_due_connector_syncs",
    "run_import",
    "run_parallel_import",
    "NotionConnector",
]
