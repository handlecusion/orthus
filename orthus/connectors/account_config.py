"""Web-managed connector account configuration.

`connector_accounts.settings_redacted` stores non-secret config and secret refs.
Actual secrets live behind `orthus.secrets`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select

from orthus.connectors.default_paths import connector_roots_setting
from orthus.connectors.manifest import AuthMode
from orthus.connectors.state import (
    AccountKind,
    ConnectorAccountSpec,
    deterministic_account_id,
    ensure_connector_account,
)
from orthus.db import session
from orthus.secrets import connector_secret_ref, get_secret, put_secret
from orthus.settings import Settings, get_settings
from orthus.tables import auth_identities, connector_accounts

FieldKind = Literal["secret", "text", "number"]

# P6.7 self-service mail mailbox connector slugs (identity-bound registration).
_MAIL_CONNECTOR_SLUGS = frozenset({"mail_nova", "mail_acme"})
# P6.7.4 Model B (§12.11): only company domains may be registered as a mailbox.
# Kept local to avoid a orthus.mail.backends import cycle (backends imports this
# module); it mirrors `backends.COMPANY_MAIL_DOMAINS`.
_COMPANY_MAIL_DOMAINS = frozenset({"nova.example", "acme.example"})
_MAIL_CONNECTOR_DOMAINS = {
    "mail_nova": "nova.example",
    "mail_acme": "acme.example",
}
# Tiered registration roles: owner/admin may register any company address.
_MAIL_MANAGER_ROLES = frozenset({"owner", "admin"})


def _enforce_mail_registration_auth(
    slug: str,
    normalized: Mapping[str, Any],
    *,
    owner_id: UUID,
    owner_email: str | None,
    actor_role: str | None,
) -> None:
    """Tiered registration auth for a self-service mail mailbox (§12.11 개정 3).

    Replaces the old single owner_addr==email binding so Model B (one user, many
    mailboxes per domain) works. Rules, fail-closed:

    - owner_addr domain MUST be `@nova.example` or `@acme.example`, else reject.
    - role owner/admin may register ANY such company address (multiple).
    - a regular member may register only an address == their verified email.
    - `owner_email is None` (JWT/demo/collector token) fails closed.

    Read/send isolation (§12.4-2/3, owner_id==session user) is enforced
    elsewhere and is unchanged.
    """

    owner_addr = _scalar_setting(normalized.get("owner_addr")).strip().lower()
    if not owner_addr:
        raise ValueError("owner_addr setting required")
    domain = owner_addr.rsplit("@", 1)[-1] if "@" in owner_addr else ""
    if domain not in _COMPANY_MAIL_DOMAINS:
        raise ValueError("owner_addr must be a company mail domain (@nova.example/@acme.example)")
    expected_domain = _MAIL_CONNECTOR_DOMAINS.get(slug)
    if expected_domain is not None and domain != expected_domain:
        raise ValueError(f"owner_addr must be an @{expected_domain} address for {slug}")

    verified = (owner_email or "").strip().lower()
    if not verified:
        raise ValueError("mail mailbox registration requires a verified session email")
    if mailbox_belongs_to_other_user(owner_addr, owner_id):
        raise ValueError("owner_addr belongs to another verified user")

    role = (actor_role or "").strip().lower()
    if role in _MAIL_MANAGER_ROLES:
        # owner/admin: any company address, multiple mailboxes.
        return
    if owner_addr != verified:
        raise ValueError("owner_addr must match the session user's verified email")


def mailbox_belongs_to_other_user(owner_addr: str, owner_id: UUID | None) -> bool:
    """True when `owner_addr` is already a verified identity for another user.

    Owner/admin may register shared aliases like info@ when no user owns that
    address, but one user must not bind another employee's verified mailbox to
    their personal connector row.
    """

    addr = owner_addr.strip().lower()
    if not addr or owner_id is None:
        return False
    with session() as s:
        rows = s.execute(
            select(auth_identities.c.user_id)
            .where(
                func.lower(auth_identities.c.email) == addr,
                auth_identities.c.email_verified.is_(True),
            )
            .distinct()
        ).all()
    user_ids = {row.user_id for row in rows}
    return bool(user_ids and owner_id not in user_ids)


def _scalar_setting(value: object) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


@dataclass(frozen=True)
class ConnectorConfigField:
    key: str
    label: str
    kind: FieldKind
    required: bool = True
    placeholder: str = ""
    default_value: str | int | list[str] | None = None


@dataclass(frozen=True)
class _ConfigSpec:
    auth_mode: AuthMode
    account_kinds: tuple[AccountKind, ...]
    fields: tuple[ConnectorConfigField, ...]


_CONFIG_SPECS: dict[str, _ConfigSpec] = {
    "notion": _ConfigSpec(
        auth_mode="token",
        account_kinds=("company", "personal"),
        fields=(
            ConnectorConfigField(
                "token",
                "Token",
                "secret",
                placeholder="secret_xxx",
            ),
        ),
    ),
    "github": _ConfigSpec(
        auth_mode="token",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField("token", "Token", "secret", required=False, placeholder="ghp_xxx"),
            ConnectorConfigField(
                "repos",
                "Repos",
                "text",
                placeholder="owner/repo, owner/repo2",
            ),
            ConnectorConfigField(
                "max_items",
                "Max items",
                "number",
                required=False,
                placeholder="200",
            ),
        ),
    ),
    "local_files": _ConfigSpec(
        auth_mode="local_path",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField("roots", "Roots", "text", placeholder="~/Documents"),
            ConnectorConfigField(
                "extensions",
                "Extensions",
                "text",
                required=False,
                placeholder=".md,.txt",
            ),
            ConnectorConfigField(
                "max_bytes",
                "Max bytes",
                "number",
                required=False,
                placeholder="1048576",
            ),
        ),
    ),
    "codex_sessions": _ConfigSpec(
        auth_mode="local_path",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField("roots", "Roots", "text", placeholder="~/.codex/sessions"),
            ConnectorConfigField(
                "max_bytes",
                "Max bytes",
                "number",
                required=False,
                placeholder="5242880",
            ),
            ConnectorConfigField(
                "max_files",
                "Max files",
                "number",
                required=False,
                placeholder="200",
            ),
            ConnectorConfigField(
                "max_messages",
                "Max messages",
                "number",
                required=False,
                placeholder="200",
            ),
            ConnectorConfigField(
                "max_message_chars",
                "Max message chars",
                "number",
                required=False,
                placeholder="4000",
            ),
        ),
    ),
    "claude_sessions": _ConfigSpec(
        auth_mode="local_path",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField("roots", "Roots", "text", placeholder="~/.claude/projects"),
            ConnectorConfigField(
                "max_bytes",
                "Max bytes",
                "number",
                required=False,
                placeholder="5242880",
            ),
            ConnectorConfigField(
                "max_files",
                "Max files",
                "number",
                required=False,
                placeholder="200",
            ),
            ConnectorConfigField(
                "max_messages",
                "Max messages",
                "number",
                required=False,
                placeholder="200",
            ),
            ConnectorConfigField(
                "max_message_chars",
                "Max message chars",
                "number",
                required=False,
                placeholder="4000",
            ),
        ),
    ),
    "gws_gmail": _ConfigSpec(
        auth_mode="local_cli",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField(
                "query",
                "Query",
                "text",
                required=False,
                placeholder="in:anywhere",
            ),
            ConnectorConfigField(
                "max_messages",
                "Max messages",
                "number",
                required=False,
                placeholder="50",
            ),
            ConnectorConfigField(
                "max_body_chars",
                "Max body chars",
                "number",
                required=False,
                placeholder="12000",
            ),
        ),
    ),
    "gws_drive": _ConfigSpec(
        auth_mode="local_cli",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField(
                "query",
                "Query",
                "text",
                required=False,
                placeholder="trashed=false",
            ),
            ConnectorConfigField(
                "max_files",
                "Max files",
                "number",
                required=False,
                placeholder="50",
            ),
            ConnectorConfigField(
                "max_bytes",
                "Max bytes",
                "number",
                required=False,
                placeholder="1048576",
            ),
        ),
    ),
    "slack": _ConfigSpec(
        auth_mode="token",
        account_kinds=("company",),
        fields=(
            ConnectorConfigField("token", "Bot token", "secret", placeholder="xoxb-xxx"),
            ConnectorConfigField("channels", "Channels", "text", placeholder="C123, C456"),
            ConnectorConfigField(
                "channel_projects",
                "Channel projects",
                "text",
                required=False,
                placeholder="C123=atlas,C456=nova",
            ),
        ),
    ),
    # P6.7 self-service mail accounts (`docs/p6-unified-mail.md` §12). One row
    # = one mailbox. `ingest_scope` is owner-only by default; the company
    # opt-in toggle and per-call identity binding are P6.7.2-.4.
    "mail_nova": _ConfigSpec(
        auth_mode="token",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField(
                "base_url",
                "Base URL",
                "text",
                required=False,
                placeholder="https://api.nova.example/v0",
            ),
            ConnectorConfigField(
                "owner_addr",
                "Mailbox address",
                "text",
                placeholder="you@nova.example",
            ),
            ConnectorConfigField(
                "ingest_scope",
                "Ingest scope",
                "text",
                required=False,
                placeholder="owner",
            ),
            ConnectorConfigField(
                "api_key",
                "API key",
                "secret",
                required=False,
                placeholder="비우면 회사 공용 nova 키 사용",
            ),
        ),
    ),
    "mail_acme": _ConfigSpec(
        auth_mode="token",
        account_kinds=("personal",),
        fields=(
            ConnectorConfigField(
                "base_url",
                "Base URL",
                "text",
                required=False,
                placeholder="https://mail-api.acme.example",
            ),
            ConnectorConfigField(
                "owner_addr",
                "Mailbox address",
                "text",
                placeholder="you@acme.example",
            ),
            ConnectorConfigField(
                "ingest_scope",
                "Ingest scope",
                "text",
                required=False,
                placeholder="owner",
            ),
            ConnectorConfigField(
                "api_token",
                "API token",
                "secret",
                required=False,
                placeholder="비우면 회사 공용 acme 토큰 사용",
            ),
        ),
    ),
}


def config_fields_for_slug(
    connector_slug: str,
    settings: Settings | None = None,
) -> tuple[ConnectorConfigField, ...]:
    settings = settings or get_settings()
    slug = connector_slug.strip().lower()
    spec = _CONFIG_SPECS.get(slug)
    if spec is None:
        return ()
    return tuple(_field_with_default(slug, field, settings) for field in spec.fields)


def configure_connector_account(
    connector_slug: str,
    user_id: UUID,
    *,
    input_settings: Mapping[str, Any],
    input_secrets: Mapping[str, str],
    account_label: str | None = None,
    settings: Settings | None = None,
    account_kind: AccountKind | None = None,
    owner_email: str | None = None,
    actor_role: str | None = None,
    device_id: str = "",
) -> UUID:
    settings = settings or get_settings()
    slug = connector_slug.strip().lower()
    spec = _CONFIG_SPECS.get(slug)
    if spec is None:
        raise ValueError(f"unsupported connector config: {connector_slug}")

    resolved_kind: AccountKind = account_kind or (
        "personal" if settings.node_kind == "personal" else "company"
    )
    if (
        resolved_kind == "personal"
        and settings.node_kind == "company"
        and not settings.owner_scope_enabled
    ):
        raise ValueError("personal connector owner scope is disabled")
    if resolved_kind not in spec.account_kinds:
        raise ValueError(f"{slug} connector is {','.join(spec.account_kinds)}-node only")

    owner_id = user_id if resolved_kind == "personal" else None
    # P6.7.4 Model B (§12.11): a mail slug keys its account_id by owner_addr so one
    # owner can hold several mailboxes per domain. Non-mail slugs keep "" -> the
    # account_id is byte-identical to before. The discriminator is derived from the
    # submitted owner_addr; a config without owner_addr can't target a mailbox.
    discriminator = ""
    if slug in _MAIL_CONNECTOR_SLUGS:
        discriminator = _scalar_setting(input_settings.get("owner_addr")).strip().lower()
        if not discriminator:
            raise ValueError("owner_addr setting required")
    base_spec = ConnectorAccountSpec(
        connector_slug=slug,
        account_kind=resolved_kind,
        node_id=settings.node_id,
        auth_mode=spec.auth_mode,
        owner_id=owner_id,
        account_label=account_label or f"{settings.node_id} {slug}",
        settings_redacted={},
        discriminator=discriminator,
        device_id=device_id,
    )
    account_id = deterministic_account_id(base_spec)
    existing = _settings_for_account(account_id)
    normalized = _normalize_settings(slug, input_settings, spec, existing, settings)
    secret_refs = _existing_secret_refs(existing)

    # Tiered registration auth (§12.11 개정 3): owner/admin may register any company
    # address; a member may register only their own verified email; non-company
    # domain or missing/None email fails closed.
    if slug in _MAIL_CONNECTOR_SLUGS:
        _enforce_mail_registration_auth(
            slug,
            normalized,
            owner_id=user_id,
            owner_email=owner_email,
            actor_role=actor_role,
        )

    for field in spec.fields:
        if field.kind != "secret":
            continue
        value = input_secrets.get(field.key)
        if value is not None and value.strip():
            ref = connector_secret_ref(account_id, field.key)
            put_secret(ref, value.strip())
            secret_refs[field.key] = ref
        if (
            field.required
            and field.key not in secret_refs
            and not _env_secret_present(slug, settings)
        ):
            raise ValueError(f"{field.key} secret required")

    settings_redacted = {
        **normalized,
        "config_source": "web",
        "secret_refs": secret_refs,
    }
    for key in secret_refs:
        settings_redacted[f"{key}_configured"] = True

    return ensure_connector_account(
        ConnectorAccountSpec(
            connector_slug=slug,
            account_kind=resolved_kind,
            node_id=settings.node_id,
            auth_mode=spec.auth_mode,
            owner_id=owner_id,
            account_label=account_label or f"{settings.node_id} {slug}",
            settings_redacted=settings_redacted,
            account_id=account_id,
            discriminator=discriminator,
            device_id=device_id,
        )
    )


def account_settings(account: Mapping[str, object]) -> dict[str, Any]:
    raw = account.get("settings_redacted")
    return raw if isinstance(raw, dict) else {}


def account_secret(account: Mapping[str, object], key: str) -> str | None:
    refs = _existing_secret_refs(account_settings(account))
    ref = refs.get(key)
    if not ref:
        return None
    return get_secret(ref)


def account_csv_setting(
    account: Mapping[str, object],
    key: str,
    fallback: str,
) -> str:
    value = account_settings(account).get(key)
    if value is None:
        return fallback
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def account_int_setting(
    account: Mapping[str, object],
    key: str,
    fallback: int,
) -> int:
    value = account_settings(account).get(key)
    if value is None or value == "":
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{key} must be positive")
    return parsed


def _normalize_settings(
    slug: str,
    values: Mapping[str, Any],
    spec: _ConfigSpec,
    existing: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in spec.fields:
        if field.kind == "secret":
            continue
        if field.key in values:
            value = values[field.key]
        elif field.key in existing:
            value = existing[field.key]
        else:
            value = _default_for_key(slug, field.key, settings)

        if field.kind == "number":
            normalized = _normalize_number(value)
        else:
            normalized = _normalize_text_list(value)
        if field.required and not normalized:
            raise ValueError(f"{field.key} setting required")
        if normalized not in (None, "", []):
            out[field.key] = normalized
    return out


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = [item.strip() for item in str(value).split(",")]
    return [item for item in items if item]


def _normalize_number(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("number setting must be integer") from exc
    if parsed <= 0:
        raise ValueError("number setting must be positive")
    return parsed


def _settings_for_account(account_id: UUID) -> dict[str, Any]:
    with session() as s:
        row = s.execute(
            select(connector_accounts.c.settings_redacted).where(
                connector_accounts.c.account_id == account_id
            )
        ).first()
    if row is None:
        return {}
    value = row.settings_redacted
    return value if isinstance(value, dict) else {}


def _existing_secret_refs(settings_json: Mapping[str, Any]) -> dict[str, str]:
    raw = settings_json.get("secret_refs")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v).strip()}


def _field_with_default(
    slug: str,
    field: ConnectorConfigField,
    settings: Settings,
) -> ConnectorConfigField:
    if field.kind == "secret":
        return field
    default = _default_for_key(slug, field.key, settings)
    return ConnectorConfigField(
        key=field.key,
        label=field.label,
        kind=field.kind,
        required=field.required,
        placeholder=field.placeholder,
        default_value=default if default not in ("", [], None) else None,
    )


def _default_for_key(slug: str, key: str, settings: Settings) -> str | int | list[str] | None:
    if slug == "github":
        return {
            "repos": _split_csv(settings.conn_github_repos),
            "max_items": settings.conn_github_max_items,
        }.get(key)
    if slug == "gws_gmail":
        return {
            "query": settings.conn_gws_gmail_query,
            "max_messages": settings.conn_gws_gmail_max_messages,
            "max_body_chars": settings.conn_gws_gmail_max_body_chars,
        }.get(key)
    if slug == "gws_drive":
        return {
            "query": settings.conn_gws_drive_query,
            "max_files": settings.conn_gws_drive_max_files,
            "max_bytes": settings.conn_gws_drive_max_bytes,
        }.get(key)
    if slug == "slack":
        return {
            "channels": _split_csv(settings.conn_slack_channels),
            "channel_projects": settings.conn_slack_channel_projects,
            "max_messages": settings.conn_slack_max_messages,
        }.get(key)
    if slug == "local_files":
        return {
            "roots": _split_csv(connector_roots_setting(settings, slug)),
            "extensions": _split_csv(settings.conn_local_files_extensions),
            "max_bytes": settings.conn_local_files_max_bytes,
        }.get(key)
    if slug in {"codex_sessions", "claude_sessions"}:
        return {
            "roots": _split_csv(connector_roots_setting(settings, slug)),
            "max_bytes": settings.conn_ai_sessions_max_bytes,
            "max_files": settings.conn_ai_sessions_max_files,
            "max_messages": settings.conn_ai_sessions_max_messages,
            "max_message_chars": settings.conn_ai_sessions_max_message_chars,
        }.get(key)
    if slug == "chat_exports":
        return {
            "roots": _split_csv(connector_roots_setting(settings, slug)),
            "max_bytes": settings.conn_chat_exports_max_bytes,
            "max_messages": settings.conn_chat_exports_max_messages,
            "max_message_chars": settings.conn_chat_exports_max_message_chars,
        }.get(key)
    if slug == "email_exports":
        return {
            "roots": _split_csv(connector_roots_setting(settings, slug)),
            "max_bytes": settings.conn_email_exports_max_bytes,
            "max_body_chars": settings.conn_email_exports_max_body_chars,
        }.get(key)
    if slug == "mail_nova":
        # P6.7: env single-account values are config defaults; ingest scope
        # defaults to owner-only.
        return {
            "base_url": settings.mail_nova_v0_base_url(),
            "owner_addr": settings.mail_nova_owner,
            "ingest_scope": "owner",
        }.get(key)
    if slug == "mail_acme":
        return {
            "base_url": settings.mail_acme_base_url,
            "owner_addr": settings.mail_acme_owner,
            "ingest_scope": "owner",
        }.get(key)
    return None


def _env_secret_present(slug: str, settings: Settings) -> bool:
    if slug == "notion":
        return bool(settings.conn_notion_token)
    if slug == "github":
        return bool(settings.conn_github_token)
    if slug == "slack":
        return bool(settings.conn_slack_bot_token)
    if slug == "mail_nova":
        # P6.7: env single-account key is the dev/legacy fallback for a row
        # whose own secret_ref is not set yet.
        return bool(settings.mail_nova_api_key or settings.mail_nova_api_key_secret_ref)
    if slug == "mail_acme":
        return bool(
            settings.mail_acme_api_token or settings.mail_acme_api_token_secret_ref
        )
    return False


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
