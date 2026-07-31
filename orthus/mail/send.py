"""P6.3 manual mail compose/send adapters.

orthus owns no SMTP/SES/Resend provider; it is an HTTP client of the two approved
mail-server APIs (nova-server, acme-mail) per docs/p6-unified-mail.md
§5-C. Send is routed by the from_addr domain: acme.example -> acme,
nova.example -> nova, anything else is rejected. Gmail send stays out of scope.

P6.7.2 (§12.4-3): when multi-account is on and the session user owns mailbox
rows, the from_addr must resolve to a `connector_accounts` row they own and that
row's secret/base_url is used. A from_addr matching none of their rows rejects;
it never falls through to env routing. Flag off or zero rows keeps env routing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from orthus.connectors.account_config import (
    account_secret,
    account_settings,
    mailbox_belongs_to_other_user,
)
from orthus.mail.backends import (
    _MAIL_ACCOUNT_BACKENDS,
    MAIL_HTTP_USER_AGENT,
    _mail_account_rows,
    _normalize_backend_base_url,
    _owner_matches_backend,
    _scalar_setting,
    env_backend_defaults,
)
from orthus.schemas.canonical import (
    MailSendBackend,
    MailSendRequest,
    MailSendResult,
)
from orthus.secrets import SecretStoreError, get_secret
from orthus.settings import Settings

_DOMAIN_BACKEND: dict[str, MailSendBackend] = {
    "acme.example": "acme",
    "nova.example": "nova",
}


class MailSendRoutingError(ValueError):
    """Raised when the from_addr domain does not map to a send backend."""


class MailSendUnconfiguredError(RuntimeError):
    """Raised when the resolved backend has no send credentials configured."""


class MailSendOwnershipError(PermissionError):
    """Raised when the session user does not own the requested from_addr mailbox.

    `docs/p6-unified-mail.md` §12.4-3: with multi-account on and the user owning
    mail rows, send-as is restricted to a mailbox row they own. A from_addr that
    matches none of their rows must reject, never fall through to env routing.
    """


@dataclass(frozen=True)
class MailSendBackendConfig:
    backend: MailSendBackend
    base_url: str
    owner: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.token.strip())


def backend_for_from_addr(from_addr: str) -> MailSendBackend:
    domain = from_addr.rsplit("@", 1)[-1].strip().lower()
    backend = _DOMAIN_BACKEND.get(domain)
    if backend is None:
        raise MailSendRoutingError(f"from domain not routable for send: {domain or '(none)'}")
    return backend


def send_backend_config(backend: MailSendBackend, settings: Settings) -> MailSendBackendConfig:
    if backend == "acme":
        # D2: scoped M2M send token, distinct from the read session token.
        return MailSendBackendConfig(
            backend="acme",
            base_url=settings.mail_acme_base_url,
            owner=settings.mail_acme_owner,
            token=_secret_or_inline(
                settings.mail_acme_send_token_secret_ref,
                settings.mail_acme_send_token,
            ),
        )
    # D1: nova reuses the existing read APP_API_KEY.
    return MailSendBackendConfig(
        backend="nova",
        base_url=settings.mail_nova_v0_base_url(),
        owner=settings.mail_nova_owner,
        token=_secret_or_inline(
            settings.mail_nova_api_key_secret_ref,
            settings.mail_nova_api_key,
        ),
    )


# Mail account connector slug -> send backend name. Mirrors the read-side map in
# `backends._MAIL_ACCOUNT_BACKENDS` but for the send (token-only) shape.
_MAIL_SLUG_SEND_BACKEND: dict[str, MailSendBackend] = {
    "mail_nova": "nova",
    "mail_acme": "acme",
}


def resolve_send_backend_config(
    request_from_addr: str,
    settings: Settings,
    owner_id: UUID | None,
) -> MailSendBackendConfig:
    """Resolve the send config for a from_addr, honoring per-mailbox ownership.

    Multi-account on (§12.4-3): the from_addr must match an active mail account
    row this owner owns; that row's secret/base_url is used. No match (including
    an owner with zero rows) -> `MailSendOwnershipError` (do NOT fall through to
    env routing). Flag off / no owner -> domain-routed env `send_backend_config`.
    """

    backend = backend_for_from_addr(request_from_addr)
    if owner_id is None or not settings.mail_multi_account_enabled:
        return send_backend_config(backend, settings)

    wanted = request_from_addr.strip().lower()
    for row in _mail_account_rows(settings, owner_id):
        slug = str(row.get("connector_slug"))
        send_backend = _MAIL_SLUG_SEND_BACKEND.get(slug)
        if send_backend is None or send_backend != backend:
            continue
        cfg = _account_to_send_config(send_backend, row, settings)
        if cfg is not None and cfg.owner.strip().lower() == wanted:
            return cfg
    raise MailSendOwnershipError(f"from_addr not owned by session user: {request_from_addr}")


def _account_to_send_config(
    backend: MailSendBackend,
    account: Mapping[str, Any],
    settings: Settings,
) -> MailSendBackendConfig | None:
    spec = _MAIL_ACCOUNT_BACKENDS.get(str(account.get("connector_slug")))
    if spec is None:
        return None
    _read_backend, secret_keys = spec
    token = ""
    for key in secret_keys:
        if key == "session":
            # Session tokens are read-only; send uses the bearer/api token only.
            continue
        value = account_secret(account, key) or ""
        if value:
            token = value
            break
    redacted = account_settings(account)
    owner = _scalar_setting(redacted.get("owner_addr"))
    if not _owner_matches_backend(backend, owner):
        return None
    owner_id = account.get("owner_id")
    if isinstance(owner_id, UUID) and mailbox_belongs_to_other_user(owner, owner_id):
        return None
    # A self-service mailbox row may omit base_url/token and rely on the shared
    # company app key (`ORTHUS_MAIL_*`) so it can be registered with just its
    # address; fill those in here when the row has none of its own.
    env_base, env_token, _ = env_backend_defaults(backend, settings)
    return MailSendBackendConfig(
        backend=backend,
        base_url=_normalize_backend_base_url(
            backend,
            _scalar_setting(redacted.get("base_url")) or env_base,
        ),
        owner=owner,
        token=token or env_token,
    )


def backend_send_configured(backend: MailSendBackend, settings: Settings) -> bool:
    return send_backend_config(backend, settings).configured


async def send_mail(
    request: MailSendRequest,
    settings: Settings,
    *,
    owner_id: UUID | None = None,
    client: httpx.AsyncClient | None = None,
) -> MailSendResult:
    """Route by from-domain (and per-mailbox ownership) and POST to the send API.

    When `owner_id` owns mail rows under multi-account, the from_addr is resolved
    to that owner's mailbox row (§12.4-3); otherwise env/domain routing is used.
    """

    cfg = resolve_send_backend_config(request.from_addr, settings, owner_id)
    if not cfg.configured:
        raise MailSendUnconfiguredError(f"{cfg.backend} send not configured")

    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=settings.mail_timeout_seconds)
    try:
        if cfg.backend == "acme":
            return await _send_acme(cfg, request, http_client)
        return await _send_nova(cfg, request, http_client)
    finally:
        if close_client:
            await http_client.aclose()


async def _send_acme(
    cfg: MailSendBackendConfig,
    request: MailSendRequest,
    client: httpx.AsyncClient,
) -> MailSendResult:
    # Verified contract (approved mail-server routes.ts):
    # POST {base_url}/mail/send, Bearer token, JSON body. orthus omits attachments.
    body: dict[str, object] = {
        "from_addr": request.from_addr,
        "to": request.to,
        "subject": request.subject,
        "text": request.text,
    }
    if request.html:
        body["html"] = request.html
    # cc/bcc/reply_to_id는 값이 있을 때만 싣는다 — 기존(미지정) 발송 바디는 불변.
    # acme worker가 미지원 필드를 무시하면 답장은 비스레드로 발송된다(실패 아님).
    _apply_optional_send_fields(body, request)
    try:
        response = await client.post(
            f"{cfg.base_url.rstrip('/')}/mail/send",
            json=body,
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "User-Agent": MAIL_HTTP_USER_AGENT,
            },
        )
    except httpx.HTTPError as exc:
        return MailSendResult(backend="acme", status="failed", error=_transport_error(exc))
    if response.status_code == 200:
        payload = _safe_json(response)
        return MailSendResult(
            backend="acme",
            status="sent",
            provider_message_id=_provider_id(payload),
        )
    return MailSendResult(
        backend="acme",
        status="failed",
        error=_status_error(response.status_code),
    )


def _apply_optional_send_fields(body: dict[str, object], request: MailSendRequest) -> None:
    """cc/bcc/reply_to_id를 값이 있을 때만 전송 바디에 추가한다.

    이렇게 하면 참조/답장을 쓰지 않는 기존 발송 바디는 바이트 동일하게 유지돼
    회귀가 없다(원칙: 새 기능이 기존 경로를 바꾸지 않는다).
    """
    if request.cc:
        body["cc"] = request.cc
    if request.bcc:
        body["bcc"] = request.bcc
    if request.reply_to_id:
        body["reply_to_id"] = request.reply_to_id


def _nova_send_body(cfg: MailSendBackendConfig, request: MailSendRequest) -> dict[str, object]:
    # Verified contract (nova-ai/nova-server server/mail/routes.py ComposeRequest):
    # {from_addr, to, cc?, bcc?, subject, text, html?, reply_to_id?, attachments?}
    # -> {"id", "resend_id"}. API-key callers may send as any from_addr; no owner param.
    body: dict[str, object] = {
        "from_addr": request.from_addr,
        "to": request.to,
        "subject": request.subject,
        "text": request.text,
    }
    if request.html:
        body["html"] = request.html
    _apply_optional_send_fields(body, request)
    return body


async def _send_nova(
    cfg: MailSendBackendConfig,
    request: MailSendRequest,
    client: httpx.AsyncClient,
) -> MailSendResult:
    try:
        response = await client.post(
            f"{cfg.base_url.rstrip('/')}/mail/send",
            json=_nova_send_body(cfg, request),
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "User-Agent": MAIL_HTTP_USER_AGENT,
            },
        )
    except httpx.HTTPError as exc:
        return MailSendResult(backend="nova", status="failed", error=_transport_error(exc))
    if response.status_code == 200:
        payload = _safe_json(response)
        return MailSendResult(
            backend="nova",
            status="sent",
            provider_message_id=_provider_id(payload),
        )
    return MailSendResult(
        backend="nova",
        status="failed",
        error=_status_error(response.status_code),
    )


def _secret_or_inline(secret_ref: str, inline_value: str) -> str:
    ref = secret_ref.strip()
    if not ref:
        return inline_value
    try:
        return get_secret(ref) or ""
    except (SecretStoreError, ValueError):
        return ""


def _safe_json(response: httpx.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _provider_id(payload: dict[str, object]) -> str | None:
    for key in ("resend_id", "id", "message_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _status_error(status_code: int) -> str:
    # Surface the status class only; never echo raw provider payloads.
    reason = {403: "forbidden", 429: "rate limited"}.get(status_code, "delivery failed")
    return f"{status_code} {reason}"


def _transport_error(exc: httpx.HTTPError) -> str:
    return f"transport error: {type(exc).__name__}"
