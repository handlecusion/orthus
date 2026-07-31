"""Process-local connector provider registry."""

from __future__ import annotations

from collections.abc import Iterable

from orthus.connectors.manifest import ConnectorManifest, ConnectorProvider

_REGISTRY: dict[str, ConnectorProvider] = {}


def register_connector_provider(provider: ConnectorProvider, *, replace: bool = False) -> None:
    """Register one provider keyed by manifest slug."""
    slug = provider.manifest.slug
    if slug in _REGISTRY and not replace:
        raise ValueError(f"connector provider already registered: {slug}")
    _REGISTRY[slug] = provider


def get_connector_provider(slug: str) -> ConnectorProvider | None:
    return _REGISTRY.get(slug.strip().lower())


def require_connector_provider(slug: str) -> ConnectorProvider:
    provider = get_connector_provider(slug)
    if provider is None:
        raise KeyError(f"connector provider not registered: {slug}")
    return provider


def list_connector_providers() -> list[ConnectorProvider]:
    return [_REGISTRY[slug] for slug in sorted(_REGISTRY)]


def list_connector_manifests() -> list[ConnectorManifest]:
    return [provider.manifest for provider in list_connector_providers()]


def registered_connector_slugs() -> set[str]:
    return set(_REGISTRY)


def register_connector_providers(
    providers: Iterable[ConnectorProvider], *, replace: bool = False
) -> None:
    for provider in providers:
        register_connector_provider(provider, replace=replace)


def register_default_connector_providers(*, replace: bool = False) -> None:
    """Register built-in providers that have real connector implementations."""
    from orthus.connectors.ai_sessions_provider import default_ai_session_providers
    from orthus.connectors.chat_exports_provider import ChatExportsConnectorProvider
    from orthus.connectors.email_exports_provider import EmailExportsConnectorProvider
    from orthus.connectors.github_provider import GitHubConnectorProvider
    from orthus.connectors.gws_provider import GwsDriveConnectorProvider, GwsGmailConnectorProvider
    from orthus.connectors.local_files_provider import LocalFilesConnectorProvider
    from orthus.connectors.mail_provider import (
        MailAcmeConnectorProvider,
        MailNovaConnectorProvider,
    )
    from orthus.connectors.notion_provider import NotionConnectorProvider
    from orthus.connectors.slack_provider import SlackConnectorProvider

    register_connector_provider(NotionConnectorProvider(), replace=replace)
    register_connector_provider(LocalFilesConnectorProvider(), replace=replace)
    register_connector_providers(default_ai_session_providers(), replace=replace)
    register_connector_provider(ChatExportsConnectorProvider(), replace=replace)
    register_connector_provider(EmailExportsConnectorProvider(), replace=replace)
    register_connector_provider(GitHubConnectorProvider(), replace=replace)
    register_connector_provider(SlackConnectorProvider(), replace=replace)
    register_connector_provider(GwsGmailConnectorProvider(), replace=replace)
    register_connector_provider(GwsDriveConnectorProvider(), replace=replace)
    # P6.7 self-service mail accounts (`docs/p6-unified-mail.md` §12).
    register_connector_provider(MailNovaConnectorProvider(), replace=replace)
    register_connector_provider(MailAcmeConnectorProvider(), replace=replace)
