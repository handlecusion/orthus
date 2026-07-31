"""Connector registry metadata.

C2 keeps provider capabilities explicit before adding more company/personal
connectors. The manifest is operational policy, not UI copy only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from orthus.connectors.base import Connector
from orthus.connectors.state import AccountKind, SyncState

AuthMode = Literal["token", "oauth", "composio", "local_path", "local_cli"]
ConnectorCapability = Literal["read", "write", "periodic", "manual", "profile"]
PrivacyClass = Literal["company", "personal_local", "personal_sensitive", "mixed"]
RedactionProfile = Literal["none", "standard", "strict"]
SourceKind = Literal["remote_api", "local_app", "local_file", "export"]
ImportMode = Literal["serial", "parallel"]


@dataclass(frozen=True)
class ConnectorManifest:
    """Static connector metadata used by runner, API, and future FE."""

    slug: str
    label: str
    source_kind: SourceKind
    account_kinds: tuple[AccountKind, ...]
    auth_modes: tuple[AuthMode, ...]
    capabilities: tuple[ConnectorCapability, ...] = ("read", "manual")
    default_interval_seconds: int | None = None
    default_daily_budget: int | None = None
    privacy_class: PrivacyClass = "company"
    redaction_profile: RedactionProfile = "none"
    import_mode: ImportMode = "serial"
    description: str = ""
    settings_keys: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.slug.strip():
            raise ValueError("manifest slug required")
        if self.slug != self.slug.strip().lower():
            raise ValueError("manifest slug must be lowercase and trimmed")
        if not self.label.strip():
            raise ValueError("manifest label required")
        if not self.account_kinds:
            raise ValueError("manifest account_kinds required")
        if not self.auth_modes:
            raise ValueError("manifest auth_modes required")
        if "periodic" in self.capabilities and self.default_interval_seconds is None:
            raise ValueError("periodic connector requires default_interval_seconds")
        if self.default_interval_seconds is not None and self.default_interval_seconds <= 0:
            raise ValueError("default_interval_seconds must be positive")
        if self.default_daily_budget is not None and self.default_daily_budget <= 0:
            raise ValueError("default_daily_budget must be positive")

    def supports_account_kind(self, account_kind: str) -> bool:
        return account_kind in self.account_kinds

    def supports_auth_mode(self, auth_mode: str) -> bool:
        return auth_mode in self.auth_modes

    def can_periodic_sync(self) -> bool:
        return "periodic" in self.capabilities and self.default_interval_seconds is not None


class ConnectorProvider(Protocol):
    """Provider contract behind a manifest entry."""

    manifest: ConnectorManifest

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> Connector:
        """Build a connector instance for one account sync."""


ConnectorFactory = Callable[[Mapping[str, object], SyncState | None], Connector]


@dataclass(frozen=True)
class FactoryConnectorProvider:
    """Small provider adapter for pure factory-backed connectors."""

    manifest: ConnectorManifest
    factory: ConnectorFactory

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> Connector:
        return self.factory(account, state)
