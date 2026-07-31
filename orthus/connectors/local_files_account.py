"""Node-local account helper for the local-files connector."""

from __future__ import annotations

from uuid import UUID

from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.local_files import (
    parse_local_file_extensions,
    parse_local_file_roots,
)
from orthus.connectors.state import ConnectorAccountSpec, ensure_connector_account
from orthus.settings import Settings, get_settings


def ensure_local_files_account(user_id: UUID, settings: Settings | None = None) -> UUID:
    """Create/update the personal node default local-files account."""
    settings = settings or get_settings()
    if settings.node_kind != "personal":
        raise ValueError("local_files connector is personal-node only")
    roots = parse_local_file_roots(connector_roots_setting(settings, "local_files"))
    if not roots:
        raise ValueError("local file roots not configured")
    extensions = parse_local_file_extensions(settings.conn_local_files_extensions)
    if not extensions:
        raise ValueError("ORTHUS_CONN_LOCAL_FILES_EXTENSIONS allows no files")

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug="local_files",
            account_kind="personal",
            node_id=settings.node_id,
            auth_mode="local_path",
            owner_id=user_id,
            account_label=f"{settings.node_id} Local Files",
            settings_redacted={
                "config_source": "default_paths",
                "root_count": len(roots),
                "extensions": sorted(extensions),
                "max_bytes": settings.conn_local_files_max_bytes,
            },
        )
    )
