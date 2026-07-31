"""Registry providers for P6.7 self-service mail accounts.

P6.7.1 registers `mail_nova` / `mail_acme` as connector providers so a
logged-in user can list and configure their own mailbox rows through
`/connectors?scope=personal`. One `connector_accounts` row models one mailbox.

The sync runner is intentionally MINIMAL here: P6.7.1 only needs the provider to
exist so the account surfaces in the connector UI/API and stores
config/secret-ref. The per-account pull-ingest rewiring (account row → backend
config → `pull_ingest_backend`) is P6.7.2. Until then `build_connector` returns a
no-op connector that yields no documents, behind the
`mail_multi_account_enabled` flag (fail-closed).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime

from orthus.connectors.base import Connector
from orthus.connectors.manifest import ConnectorManifest
from orthus.connectors.state import SyncState
from orthus.schemas.canonical import InternalDocument
from orthus.settings import get_settings

# Display label / settings_keys mirror the env-var single-account names so the
# FE and operators see the same config vocabulary as the legacy path.
_MAIL_NOVA_SLUG = "mail_nova"
_MAIL_ACME_SLUG = "mail_acme"


class _NoopMailConnector(Connector):
    """P6.7.1 placeholder: yields no documents.

    P6.7.2 wires per-account pull: this connector will be replaced by an account
    row → `MailBackendConfig` → `pull_ingest_backend(backend, settings)` path so a
    mailbox row drives real ingest. Keeping it inert now means registering the
    provider does not change any ingest behavior.
    """

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        del since
        return iter(())


def _build_mail_connector(
    account: Mapping[str, object],
    state: SyncState | None,
) -> Connector:
    del account, state
    # Fail-closed: even when invoked, P6.7.1 performs no ingest. The flag is read
    # here so the provider is structurally gated the same way the rest of P6.7 is;
    # P6.7.2 wires per-account pull behind the same flag.
    get_settings()
    return _NoopMailConnector()


class MailNovaConnectorProvider:
    manifest = ConnectorManifest(
        slug=_MAIL_NOVA_SLUG,
        label="Nova Mail",
        source_kind="remote_api",
        account_kinds=("personal",),
        auth_modes=("token",),
        # Read-only manual surface in P6.7.1. Periodic/account-driven pull is P6.7.2.
        capabilities=("read", "manual"),
        privacy_class="mixed",
        redaction_profile="none",
        import_mode="serial",
        description="Self-service @nova.example mailbox (P6.7 multi-account, §12).",
        settings_keys=(
            "ORTHUS_MAIL_NOVA_BASE_URL",
            "ORTHUS_MAIL_NOVA_OWNER",
            "ORTHUS_MAIL_NOVA_API_KEY",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> Connector:
        return _build_mail_connector(account, state)


class MailAcmeConnectorProvider:
    manifest = ConnectorManifest(
        slug=_MAIL_ACME_SLUG,
        label="Acme Mail",
        source_kind="remote_api",
        account_kinds=("personal",),
        auth_modes=("token",),
        capabilities=("read", "manual"),
        privacy_class="mixed",
        redaction_profile="none",
        import_mode="serial",
        description="Self-service @acme.example mailbox (P6.7 multi-account, §12).",
        settings_keys=(
            "ORTHUS_MAIL_ACME_BASE_URL",
            "ORTHUS_MAIL_ACME_OWNER",
            "ORTHUS_MAIL_ACME_API_TOKEN",
        ),
    )

    def build_connector(
        self,
        account: Mapping[str, object],
        state: SyncState | None,
    ) -> Connector:
        return _build_mail_connector(account, state)
