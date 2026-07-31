"""Read-only mail backend adapters for P6 unified inbox."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx

from orthus.connectors.gws_cli import (
    GwsCliError,
    GwsCliRunner,
    GwsGmailConnector,
    GwsGmailMessage,
    gws_command_available,
)
from orthus.schemas.canonical import (
    CanonicalEmail,
    EmailAttachmentRef,
    MailBackendName,
    MailBackendStatus,
    MailDirection,
    MailInboxResponse,
    MailScope,
)
from sqlalchemy import and_, select

from orthus.connectors.account_config import (
    account_secret,
    account_settings,
    mailbox_belongs_to_other_user,
)
from orthus.db import session
from orthus.secrets import SecretStoreError, get_secret
from orthus.settings import Settings
from orthus.tables import connector_accounts

COMPANY_MAIL_DOMAINS = {"nova.example", "acme.example"}

# The acme mail worker sits behind Cloudflare, which blocks the default
# `python-httpx/x` User-Agent with a 403 (error 1010, "banned browser
# signature") BEFORE auth — making that whole mailbox invisible. A browser-like
# UA is accepted. nova is unaffected but harmless to send it there too, so every
# provider call carries it. See `docs/p6-unified-mail.md` and the mail diagnosis.
MAIL_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class MailBackendConfig:
    backend: MailBackendName
    base_url: str
    owner: str
    bearer_token: str = ""
    session_token: str = ""
    account_id: UUID | None = None

    @property
    def configured(self) -> bool:
        if not self.base_url.strip() or not self.owner.strip():
            return False
        return bool(self.bearer_token.strip() or self.session_token.strip())


def backend_configs_from_settings(settings: Settings) -> list[MailBackendConfig]:
    return [
        MailBackendConfig(
            backend="nova",
            base_url=settings.mail_nova_v0_base_url(),
            owner=settings.mail_nova_owner,
            bearer_token=_secret_or_inline(
                settings.mail_nova_api_key_secret_ref,
                settings.mail_nova_api_key,
            ),
        ),
        MailBackendConfig(
            backend="acme",
            base_url=settings.mail_acme_base_url,
            owner=settings.mail_acme_owner,
            bearer_token=_secret_or_inline(
                settings.mail_acme_api_token_secret_ref,
                settings.mail_acme_api_token,
            ),
            session_token=_secret_or_inline(
                settings.mail_acme_session_secret_ref,
                settings.mail_acme_session,
            ),
        ),
        MailBackendConfig(backend="gmail", base_url="", owner=""),
    ]


# P6.7 self-service mailbox connector slugs -> read backend name + the secret
# field keys to resolve from the account row (first present wins).
_MAIL_ACCOUNT_BACKENDS: dict[str, tuple[MailBackendName, tuple[str, ...]]] = {
    "mail_nova": ("nova", ("api_key",)),
    "mail_acme": ("acme", ("api_token", "session")),
}
_MAIL_BACKEND_DOMAINS: dict[MailBackendName, str] = {
    "nova": "nova.example",
    "acme": "acme.example",
}


def backend_configs_from_accounts(
    settings: Settings,
    owner_id: UUID,
) -> list[MailBackendConfig]:
    """P6.7 multi-account backend configs for one owner.

    Flag off (`mail_multi_account_enabled` is false) keeps the legacy env
    single-account list byte-identical (`backend_configs_from_settings`,
    gmail appended). This is the only remaining path that reads the
    `ORTHUS_MAIL_*_OWNER`/secret env backends.

    Flag on: the company inbox is strictly self-service. Each active
    `mail_nova`/`mail_acme` row owned by `owner_id` on this node becomes a
    `MailBackendConfig` built from `settings_redacted` (`base_url`, `owner_addr`)
    plus the per-mailbox secret resolved from the account `secret_ref` (never the
    raw value in DB/logs). Zero rows -> empty list (no env single-account
    fallback). Gmail is NOT appended: gmail is the personal-mail page's concern
    (`GET /mail/personal`), not the company unified inbox.

    P6.7.2 wires this into `GET /mail/inbox` read isolation and into send-as
    ownership (`send.resolve_send_backend_config`). P6.7.3 wires per-mailbox
    `ingest_scope` routing and the `pull_ingest_all` account loop
    (`mail.pull`/`mail.ingest`). See `docs/p6-unified-mail.md` §12.
    """

    if not settings.mail_multi_account_enabled:
        return backend_configs_from_settings(settings)

    rows = _mail_account_rows(settings, owner_id)
    return [cfg for cfg in (_account_to_config(row, settings) for row in rows) if cfg is not None]


def _mail_account_rows(settings: Settings, owner_id: UUID) -> list[Mapping[str, Any]]:
    """Active mail account rows for one owner on this node, slug-ordered."""

    conditions = [
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(tuple(_MAIL_ACCOUNT_BACKENDS)),
        connector_accounts.c.account_kind == "personal",
        connector_accounts.c.owner_id == owner_id,
        connector_accounts.c.status == "active",
    ]
    with session() as s:
        rows = s.execute(
            select(connector_accounts)
            .where(and_(*conditions))
            .order_by(
                connector_accounts.c.connector_slug,
                connector_accounts.c.created_at,
            )
        ).all()
    return [dict(row._mapping) for row in rows]


def env_backend_defaults(backend: MailBackendName, settings: Settings) -> tuple[str, str, str]:
    """`(base_url, bearer, session)` company-shared env defaults for a mail backend.

    These are the company app-key env values (`ORTHUS_MAIL_*`). A self-service
    mailbox row may omit its own base_url/secret and register with just its
    address; the company app key is shared across every mailbox on that domain,
    so these fill in for an account row that has no per-mailbox secret. Unknown
    backend -> empty (gmail never routes here).
    """

    if backend == "nova":
        return (
            settings.mail_nova_v0_base_url(),
            _secret_or_inline(settings.mail_nova_api_key_secret_ref, settings.mail_nova_api_key),
            "",
        )
    if backend == "acme":
        return (
            settings.mail_acme_base_url,
            _secret_or_inline(
                settings.mail_acme_api_token_secret_ref,
                settings.mail_acme_api_token,
            ),
            _secret_or_inline(
                settings.mail_acme_session_secret_ref,
                settings.mail_acme_session,
            ),
        )
    return ("", "", "")


def _account_to_config(account: Mapping[str, Any], settings: Settings) -> MailBackendConfig | None:
    spec = _MAIL_ACCOUNT_BACKENDS.get(str(account.get("connector_slug")))
    if spec is None:
        return None
    backend, secret_keys = spec
    redacted = account_settings(account)
    owner = _scalar_setting(redacted.get("owner_addr"))
    if not _owner_matches_backend(backend, owner):
        return None
    owner_id = account.get("owner_id")
    if isinstance(owner_id, UUID) and mailbox_belongs_to_other_user(owner, owner_id):
        return None
    bearer_token = ""
    session_token = ""
    for key in secret_keys:
        value = account_secret(account, key) or ""
        if not value:
            continue
        if key == "session":
            session_token = value
        else:
            bearer_token = value
    env_base, env_bearer, env_session = env_backend_defaults(backend, settings)
    base_url = _normalize_backend_base_url(
        backend,
        _scalar_setting(redacted.get("base_url")) or env_base,
    )
    raw_account_id = account.get("account_id")
    return MailBackendConfig(
        backend=backend,
        base_url=base_url,
        owner=owner,
        bearer_token=bearer_token or env_bearer,
        session_token=session_token or env_session,
        account_id=raw_account_id if isinstance(raw_account_id, UUID) else None,
    )


def _owner_matches_backend(backend: MailBackendName, owner_addr: str) -> bool:
    """Reject stale/self-service rows registered under the wrong mail provider.

    `mail_nova` rows must point at `@nova.example` and `mail_acme` rows at
    `@acme.example`. Older rows created before this guard can otherwise render
    as duplicate mailbox labels and route provider calls with an invalid owner.
    """

    expected = _MAIL_BACKEND_DOMAINS.get(backend)
    if expected is None:
        return True
    addr = owner_addr.strip().lower()
    return bool(addr and addr.rsplit("@", 1)[-1] == expected)


def _normalize_backend_base_url(backend: MailBackendName, base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if backend == "nova" and base and not base.endswith("/v0"):
        return f"{base}/v0"
    return base


def _scalar_setting(value: object) -> str:
    """Coerce a connector text setting to a scalar.

    `account_config._normalize_settings` stores text fields as lists (csv ->
    list[str]); a single-value mailbox field round-trips as a one-element list.
    """

    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def account_ingest_scope(account: Mapping[str, Any]) -> str:
    """The per-mailbox `ingest_scope` setting ("owner" default) for a mail row."""

    raw = _scalar_setting(account_settings(account).get("ingest_scope")).strip().lower()
    return raw or "owner"


async def list_unified_inbox(
    settings: Settings,
    *,
    owner_id: UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
    search: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> MailInboxResponse:
    """Fan out to configured mail backends and return canonical rows.

    Unconfigured backends are reported, not treated as failures. This keeps P6.1
    fail-closed while external repo deploys and secrets are still staged.

    Read isolation (`docs/p6-unified-mail.md` §12.4-2): when multi-account is on,
    the company inbox is strictly self-service — the fan-out enumerates ONLY this
    owner's configured mail account rows (zero rows -> empty inbox, no env
    single-account fallback). Flag off keeps the legacy env fan-out unchanged.
    """

    requested_limit = max(1, min(limit or settings.mail_inbox_default_limit, 500))
    if owner_id is not None and settings.mail_multi_account_enabled:
        configs = backend_configs_from_accounts(settings, owner_id)
    else:
        configs = backend_configs_from_settings(settings)
    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=settings.mail_timeout_seconds)
    search_query = (search or "").strip()
    try:
        tasks = [
            (
                _list_gmail_inbox(settings, limit=requested_limit, offset=offset)
                if cfg.backend == "gmail"
                # Company backends: pull the whole mailbox (all folders) so the FE
                # 받은/보낸/별표/휴지통 filters are complete, not inbound-only.
                # `search` goes to the provider (LIKE over the FULL mailbox, not
                # just this fetch window) — both backends accept it on list routes.
                else _list_backend_folders(
                    cfg,
                    http_client,
                    limit=requested_limit,
                    offset=offset,
                    search=search_query or None,
                )
            )
            for cfg in configs
        ]
        backend_results = await asyncio.gather(*tasks)
    finally:
        if close_client:
            await http_client.aclose()

    items = [item for result in backend_results for item in result.items]
    if search_query:
        # Company rows are already provider-filtered (full-mailbox LIKE, possibly
        # matching beyond the truncated body_text we hold) — re-filtering here
        # would drop valid hits. Only gmail rows still need the local matcher.
        q = search_query.lower()
        items = [item for item in items if item.backend != "gmail" or _matches_search(item, q)]
    items.sort(key=_email_sort_key, reverse=True)
    items = items[:requested_limit]
    statuses = [_with_send_state(result.status, settings) for result in backend_results]
    total = len(items) if search_query else sum(status.total for status in statuses)
    unread = (
        sum(1 for item in items if not item.read)
        if search_query
        else sum(status.unread for status in statuses)
    )
    return MailInboxResponse(
        items=items,
        total=total,
        unread=unread,
        backends=statuses,
        send_enabled=bool(settings.mail_send_enabled),
    )


@dataclass(frozen=True)
class _BackendResult:
    items: list[CanonicalEmail]
    status: MailBackendStatus


_MAIL_FOLDERS = frozenset({"inbox", "sent", "all", "starred", "trash"})


async def _list_backend_inbox(
    cfg: MailBackendConfig,
    client: httpx.AsyncClient,
    *,
    limit: int,
    offset: int,
    folder: str = "inbox",
    search: str | None = None,
) -> _BackendResult:
    if not cfg.configured:
        return _BackendResult(
            items=[],
            status=MailBackendStatus(
                backend=cfg.backend,
                account_id=cfg.account_id,
                configured=False,
                ok=True,
            ),
        )
    route = folder if folder in _MAIL_FOLDERS else "inbox"

    params: dict[str, str | int] = {"owner": cfg.owner, "limit": limit, "offset": offset}
    if search:
        # Both providers filter list routes by `search` (subject/from/body LIKE)
        # over the whole mailbox — full-history search, not window-bound.
        params["search"] = search
    headers = _headers_for_config(cfg)
    try:
        response = await client.get(
            f"{cfg.base_url.rstrip('/')}/mail/{route}",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("emails", [])
        if not isinstance(rows, list):
            raise ValueError("mail backend response missing emails[]")
        items = [
            normalize_backend_email(
                cfg.backend,
                row,
                owner_addr=cfg.owner,
                account_id=cfg.account_id,
            )
            for row in rows
            if isinstance(row, Mapping)
        ]
        return _BackendResult(
            items=items,
            status=MailBackendStatus(
                backend=cfg.backend,
                account_id=cfg.account_id,
                configured=True,
                ok=True,
                total=_int(payload.get("total"), default=len(items)),
                unread=_int(payload.get("unread"), default=0),
                # Multi-account: carry this mailbox's address so the FE can label
                # and send-as per mailbox (env single-account owner may be empty).
                owner_addr=cfg.owner.strip() or None,
            ),
        )
    except (httpx.HTTPError, ValueError) as exc:
        return _BackendResult(
            items=[],
            status=MailBackendStatus(
                backend=cfg.backend,
                account_id=cfg.account_id,
                configured=True,
                ok=False,
                error=f"{type(exc).__name__}: {str(exc)[:180]}",
                owner_addr=cfg.owner.strip() or None,
            ),
        )


async def _list_backend_folders(
    cfg: MailBackendConfig,
    client: httpx.AsyncClient,
    *,
    limit: int,
    offset: int,
    search: str | None = None,
) -> _BackendResult:
    """Fetch a backend's full mailbox: `/mail/all` (inbound+outbound, non-trash)
    plus `/mail/trash`, merged. This is what lets the unified FE folders
    (받은/보낸/별표/휴지통) all populate instead of inbound-only."""
    all_res, trash_res = await asyncio.gather(
        _list_backend_inbox(cfg, client, limit=limit, offset=offset, folder="all", search=search),
        _list_backend_inbox(cfg, client, limit=limit, offset=offset, folder="trash", search=search),
    )
    if not all_res.status.ok:
        return all_res
    seen = {(item.backend, item.external_id) for item in all_res.items}
    merged = list(all_res.items)
    for item in trash_res.items:
        if (item.backend, item.external_id) not in seen:
            # The trash route is authoritative: acme's list SELECT omits the
            # `trashed` column, so without this override a trashed mail reappears
            # as a normal inbox row on the next full reload.
            merged.append(item if item.trashed else item.model_copy(update={"trashed": True}))
    return _BackendResult(items=merged, status=all_res.status)


def resolve_backend_config(
    settings: Settings,
    backend: MailBackendName,
    *,
    owner_id: UUID | None = None,
    account_id: UUID | None = None,
) -> MailBackendConfig | None:
    """Return the configured config for one company-mail backend (or None).

    Mirrors `list_unified_inbox`'s source selection (multi-account rows vs env)
    so single-message detail/mutation proxies stay owner-isolated. gmail has no
    base_url here, so it never resolves through this company-backend path.
    """
    if owner_id is not None and settings.mail_multi_account_enabled:
        configs = backend_configs_from_accounts(settings, owner_id)
    else:
        configs = backend_configs_from_settings(settings)
    for cfg in configs:
        if account_id is not None and cfg.account_id != account_id:
            continue
        if cfg.backend == backend and cfg.configured:
            return cfg
    return None


def _detail_row(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("email"), Mapping):
        return payload["email"]
    if isinstance(payload, Mapping):
        return payload
    raise ValueError("mail backend detail missing email body")


async def fetch_backend_email_detail(
    cfg: MailBackendConfig,
    external_id: str,
    client: httpx.AsyncClient,
) -> CanonicalEmail | None:
    """GET one company-mail message (the backend auto-marks it read).

    Returns None on backend 404 (so the route maps it to 404). Credentials are
    server-injected via `_headers_for_config`; the orthus caller never holds the
    backend session/app key. Inline `cid:` images that the backend did not already
    rewrite (acme) are resolved to bounded `data:` URIs so the HTML body
    renders standalone in the FE sandbox.
    """
    resp = await client.get(
        f"{cfg.base_url.rstrip('/')}/mail/{external_id}",
        headers=_headers_for_config(cfg),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    email = normalize_backend_email(
        cfg.backend,
        _detail_row(resp.json()),
        owner_addr=cfg.owner,
        account_id=cfg.account_id,
    )
    return await _inline_cid_images(email, cfg, client)


class MailAttachmentError(RuntimeError):
    """Raised when an attachment ref is not a safe provider-relative key."""


class MailAttachmentTooLargeError(RuntimeError):
    """Raised when an attachment exceeds the proxy size cap (avoid buffering huge
    files into memory)."""


_ATTACHMENT_KEY_PREFIX = "mail/attachments/"
# Cap the bytes the proxy will buffer for one attachment download. Real mail
# attachments are well under this; the cap stops a giant object from exhausting
# server memory.
_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
_CID_REF_RE = re.compile(r"""cid:([^"'\)\s>]+)""", re.IGNORECASE)
# Cap a single inline image we base64-inline into the HTML body. Bigger inline
# parts (rare) are left as cid: refs rather than bloating the detail payload.
_INLINE_IMAGE_MAX_BYTES = 2_000_000


def _attachment_key_ok(ref: str | None) -> bool:
    """Only provider-relative attachment keys (no scheme/host/traversal)."""
    if not ref:
        return False
    if "://" in ref or ref.startswith("//") or ".." in ref:
        return False
    return ref.startswith(_ATTACHMENT_KEY_PREFIX)


async def fetch_backend_attachment(
    cfg: MailBackendConfig,
    ref: str,
    client: httpx.AsyncClient,
) -> tuple[bytes, str] | None:
    """Fetch raw attachment bytes for a provider-relative key. Returns
    `(content, content_type)` or None on a backend 404.

    nova presigns (`/mail/attachments/url?s3_key=`) then we stream the signed S3
    URL; acme streams directly (`/mail/attachments/get?r2_key=`). The key is
    validated to a `mail/attachments/` prefix so a client cannot turn this proxy
    into an SSRF/arbitrary-object reader.
    """
    if not _attachment_key_ok(ref):
        raise MailAttachmentError("invalid attachment key")
    base = cfg.base_url.rstrip("/")
    headers = _headers_for_config(cfg)
    if cfg.backend == "nova":
        # Nova app-key auth intentionally does not allow attachment presign
        # endpoints. Do not attempt `/mail/attachments/url` with the shared key.
        raise MailAttachmentError("nova attachment proxy unsupported with app key")
    return await _stream_capped(
        client, f"{base}/mail/attachments/get", headers, params={"r2_key": ref}
    )


async def _stream_capped(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, str] | None = None,
) -> tuple[bytes, str] | None:
    """GET `url` streaming into memory with a hard byte cap (no full buffering of
    a giant object). Returns None on 404, raises MailAttachmentTooLargeError past
    the cap."""
    async with client.stream("GET", url, headers=headers, params=params) as resp:
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _ATTACHMENT_MAX_BYTES:
                    raise MailAttachmentTooLargeError("attachment too large")
            except ValueError:
                pass
        buffer = bytearray()
        async for chunk in resp.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > _ATTACHMENT_MAX_BYTES:
                raise MailAttachmentTooLargeError("attachment too large")
        content_type = resp.headers.get("content-type") or "application/octet-stream"
        return bytes(buffer), content_type.split(";", 1)[0].strip() or "application/octet-stream"


async def _inline_cid_images(
    email: CanonicalEmail,
    cfg: MailBackendConfig,
    client: httpx.AsyncClient,
) -> CanonicalEmail:
    """Rewrite remaining `cid:` refs in the HTML body to bounded data: URIs.

    No-op when the body has no cid refs (nova already rewrites inline images to
    presigned URLs server-side). Strictly best-effort: a failed fetch leaves the
    cid ref untouched and never breaks detail rendering.
    """
    html = email.body_html
    if not html or "cid:" not in html.lower():
        return email
    # Exact content_id wins; the local-part-only base is a non-clobbering
    # fallback so one attachment's base ("abc") can't shadow another's exact
    # match ("abc@host").
    by_cid: dict[str, EmailAttachmentRef] = {}
    for att in email.attachments:
        if att.content_id and att.ref:
            by_cid[att.content_id.lower()] = att
    for att in email.attachments:
        if att.content_id and att.ref:
            by_cid.setdefault(att.content_id.split("@", 1)[0].lower(), att)
    if not by_cid:
        return email
    resolved: dict[str, str] = {}
    for raw_cid in {m for m in _CID_REF_RE.findall(html)}:
        cid = raw_cid.strip().strip("<>").lower()
        att = by_cid.get(cid) or by_cid.get(cid.split("@", 1)[0])
        if att is None or att.ref is None:
            continue
        try:
            fetched = await fetch_backend_attachment(cfg, att.ref, client)
        except (httpx.HTTPError, MailAttachmentError):
            continue
        if not fetched:
            continue
        data, ctype = fetched
        if len(data) > _INLINE_IMAGE_MAX_BYTES:
            continue
        b64 = base64.b64encode(data).decode("ascii")
        resolved[raw_cid] = f"data:{ctype};base64,{b64}"
    if not resolved:
        return email
    for raw_cid, data_uri in resolved.items():
        html = html.replace(f"cid:{raw_cid}", data_uri)
    return email.model_copy(update={"body_html": html})


# ── 본문 원격 이미지 프록시 ────────────────────────────────────────────────
# 메일 본문 <img>가 핫링크 차단(Google mail-sig, claude.ai 로고 등)·만료된
# presigned S3 URL·http 혼합콘텐츠로 깨진다. Gmail의 googleusercontent 프록시처럼
# orthus가 서버측에서 이미지를 받아 되돌려준다.

_REMOTE_IMAGE_MAX_BYTES = 15 * 1024 * 1024
_REMOTE_IMAGE_MAX_REDIRECTS = 3


class MailImageProxyError(RuntimeError):
    """Raised when a remote image URL is not safe/eligible to proxy."""


def nova_attachment_key_from_url(url: str) -> str | None:
    """nova가 본문에 박은 S3 인라인 이미지 URL에서 provider-relative key를 뽑는다.

    path-style(`s3.<region>.amazonaws.com/<bucket>/mail/attachments/...`)과
    virtual-host style(`<bucket>.s3....amazonaws.com/mail/attachments/...`) 둘 다
    지원한다. 이 URL의 서명은 만료돼 직접 fetch가 403이라, key를 뽑아
    `fetch_backend_attachment`(제공자 재서명 경로)로 우회한다.
    """
    try:
        parsed = httpx.URL(url)
    except Exception:
        return None
    host = (parsed.host or "").lower()
    if not host.endswith(".amazonaws.com"):
        return None
    path = parsed.path.lstrip("/")
    idx = path.find(_ATTACHMENT_KEY_PREFIX)
    if idx < 0:
        return None
    key = path[idx:]
    return key if _attachment_key_ok(key) else None


def _host_resolves_public(host: str) -> bool:
    """호스트의 모든 A/AAAA 결과가 공인(global) 대역일 때만 True (SSRF 가드)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def validate_remote_image_url(url: str) -> httpx.URL:
    """프록시 가능한 원격 이미지 URL만 통과시킨다(http/https + 공인 호스트).

    resolve 후 실제 연결 사이의 DNS rebinding 창은 남지만, 이 엔드포인트는
    node operator 세션 전용이라 잔여 위험을 감수한다(붙박이 사설대역 차단이 목적).
    """
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise MailImageProxyError("invalid url") from exc
    if parsed.scheme not in ("http", "https"):
        raise MailImageProxyError("unsupported scheme")
    if not parsed.host:
        raise MailImageProxyError("missing host")
    if not _host_resolves_public(parsed.host):
        raise MailImageProxyError("host not allowed")
    return parsed


def sniff_image_type(data: bytes) -> str | None:
    """content-type이 이미지가 아닐 때 매직 바이트로 흔한 포맷만 판별한다."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def fetch_remote_image(url: str, client: httpx.AsyncClient) -> tuple[bytes, str] | None:
    """원격 이미지를 hop마다 SSRF 검증하는 수동 리다이렉트로 가져온다.

    404는 None, 사이즈 초과는 MailAttachmentTooLargeError, 비이미지 응답은
    MailImageProxyError. referer 없이 브라우저형 UA로 요청해 대부분의 핫링크
    차단을 피한다.
    """
    target = validate_remote_image_url(url)
    headers = {"User-Agent": MAIL_HTTP_USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
    for _ in range(_REMOTE_IMAGE_MAX_REDIRECTS + 1):
        async with client.stream("GET", target, headers=headers, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return None
                target = validate_remote_image_url(str(target.join(location)))
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _REMOTE_IMAGE_MAX_BYTES:
                raise MailAttachmentTooLargeError("image too large")
            buffer = bytearray()
            async for chunk in resp.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > _REMOTE_IMAGE_MAX_BYTES:
                    raise MailAttachmentTooLargeError("image too large")
            data = bytes(buffer)
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                sniffed = sniff_image_type(data)
                if sniffed is None:
                    raise MailImageProxyError("not an image")
                content_type = sniffed
            return data, content_type
    return None


async def mutate_backend_email(
    cfg: MailBackendConfig,
    external_id: str,
    client: httpx.AsyncClient,
    *,
    read: bool | None = None,
    starred: bool | None = None,
    label: str | None = None,
) -> bool:
    """PATCH read/starred/label flags on a company-mail message.

    Sends 0/1 ints which both nova (Pydantic bool coercion) and acme
    (numeric flags) accept. Ownership is enforced backend-side (403/404).
    """
    body: dict[str, object] = {}
    if read is not None:
        body["read"] = 1 if read else 0
    if starred is not None:
        body["starred"] = 1 if starred else 0
    if label is not None:
        body["label"] = label
    if not body:
        return True
    resp = await client.patch(
        f"{cfg.base_url.rstrip('/')}/mail/{external_id}",
        json=body,
        headers=_headers_for_config(cfg),
    )
    if resp.status_code in (403, 404):
        return False
    resp.raise_for_status()
    return True


async def trash_backend_email(
    cfg: MailBackendConfig,
    external_id: str,
    client: httpx.AsyncClient,
    *,
    restore: bool = False,
) -> bool:
    """Move a company-mail message to trash, or restore it. Both backends share
    `POST /mail/trash/{id}[/restore]`."""
    suffix = f"/mail/trash/{external_id}/restore" if restore else f"/mail/trash/{external_id}"
    resp = await client.post(
        f"{cfg.base_url.rstrip('/')}{suffix}",
        headers=_headers_for_config(cfg),
    )
    if resp.status_code in (403, 404):
        return False
    resp.raise_for_status()
    return True


async def fetch_backend_conversation(
    cfg: MailBackendConfig,
    contact: str,
    client: httpx.AsyncClient,
) -> list[CanonicalEmail]:
    """Fetch the message history between this mailbox and `contact`.

    Both backends expose `GET /mail/conversation?owner=&contact=` keyed on the
    address pair (not RFC In-Reply-To threading), returning `{emails: [...]}`.
    """
    resp = await client.get(
        f"{cfg.base_url.rstrip('/')}/mail/conversation",
        params={"owner": cfg.owner, "contact": contact},
        headers=_headers_for_config(cfg),
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("emails", []) if isinstance(payload, Mapping) else []
    if not isinstance(rows, list):
        return []
    return [
        normalize_backend_email(
            cfg.backend,
            row,
            owner_addr=cfg.owner,
            account_id=cfg.account_id,
        )
        for row in rows
        if isinstance(row, Mapping)
    ]


async def _list_gmail_inbox(
    settings: Settings,
    *,
    limit: int,
    offset: int,
) -> _BackendResult:
    if settings.node_kind != "personal":
        return _BackendResult(
            items=[],
            status=MailBackendStatus(backend="gmail", configured=False, ok=True),
        )

    try:
        if not gws_command_available(settings.conn_gws_command):
            return _BackendResult(
                items=[],
                status=MailBackendStatus(backend="gmail", configured=False, ok=True),
            )
    except GwsCliError as exc:
        return _BackendResult(
            items=[],
            status=MailBackendStatus(
                backend="gmail",
                configured=True,
                ok=False,
                error=f"{type(exc).__name__}: {str(exc)[:180]}",
            ),
        )

    try:
        return await asyncio.to_thread(
            _list_gmail_inbox_sync,
            settings,
            limit=limit,
            offset=offset,
        )
    except (GwsCliError, ValueError) as exc:
        return _BackendResult(
            items=[],
            status=MailBackendStatus(
                backend="gmail",
                configured=True,
                ok=False,
                error=f"{type(exc).__name__}: {str(exc)[:180]}",
            ),
        )


def _list_gmail_inbox_sync(
    settings: Settings,
    *,
    limit: int,
    offset: int,
) -> _BackendResult:
    fetch_limit = max(1, min(settings.conn_gws_gmail_max_messages, limit + offset))
    runner = GwsCliRunner(
        command=settings.conn_gws_command,
        timeout_seconds=settings.conn_gws_timeout_seconds,
    )
    connector = GwsGmailConnector(
        runner,
        query=settings.conn_gws_gmail_query,
        max_messages=fetch_limit,
        max_body_chars=settings.conn_gws_gmail_max_body_chars,
    )
    messages = list(connector.iter_messages(None))
    items = [_gmail_message_to_canonical(message) for message in messages]
    window = items[offset : offset + limit]
    return _BackendResult(
        items=window,
        status=MailBackendStatus(
            backend="gmail",
            configured=True,
            ok=True,
            total=len(items),
            unread=sum(1 for item in items if not item.read),
        ),
    )


def _gmail_message_to_canonical(message: GwsGmailMessage) -> CanonicalEmail:
    return normalize_backend_email(
        "gmail",
        {
            "external_id": f"gmail:{message.message_id}",
            "message_id": message.rfc_message_id or message.message_id,
            "direction": message.direction,
            "from_addr": message.from_addr,
            "to_addr": message.to_addr,
            "cc_addr": message.cc_addr,
            "subject": message.subject,
            "body_text": message.body_text,
            "body_html": message.body_html,
            "read": message.read,
            "starred": message.starred,
            "label": message.label,
            "date": message.date,
        },
    )


def normalize_backend_email(
    backend: MailBackendName,
    row: Mapping[str, Any],
    *,
    owner_addr: str | None = None,
    account_id: UUID | None = None,
) -> CanonicalEmail:
    to_addr = _addr_list(row.get("to_addr") or row.get("to") or row.get("to_addrs"))
    cc_addr = _addr_list(row.get("cc_addr") or row.get("cc") or row.get("cc_addrs"))
    from_addr = _str(row.get("from_addr") or row.get("from") or row.get("sender"))
    direction = _str(row.get("direction") or "inbound")
    direction_value: MailDirection = "outbound" if direction == "outbound" else "inbound"
    attachments = _attachments(row.get("attachments"))
    # When the detail carries the attachment array, count only real (non-inline)
    # files so a mail with just inline cid images does not advertise phantom
    # attachments. List rows have no array -> trust the provider's count.
    if attachments:
        attachment_count = sum(1 for a in attachments if not a.inline)
    else:
        attachment_count = _int(row.get("attachment_count"), default=0)
    sent_at = _datetime(row.get("sent_at") or row.get("created_at") or row.get("date"))
    external_id = (
        _optional_str(
            row.get("external_id")
            or row.get("id")
            or row.get("email_id")
            or row.get("message_id")
            or row.get("message-id")
        )
        or f"{from_addr}:{_str(row.get('subject'))}:{sent_at.isoformat() if sent_at else 'undated'}"
    )
    return CanonicalEmail(
        backend=backend,
        account_id=account_id,
        external_id=external_id,
        message_id=_optional_str(row.get("message_id") or row.get("message-id")),
        direction=direction_value,
        # Display scope only. P6.2 ingest destination must use
        # `ingest_scope_for_backend()` so Gmail never writes directly to central
        # just because a thread includes a company-domain recipient.
        scope=_display_scope_for_addresses([from_addr, *to_addr, *cc_addr]),
        owner_addr=owner_addr,
        from_addr=from_addr,
        to_addr=to_addr,
        cc_addr=cc_addr,
        subject=_str(row.get("subject")),
        body_text=_str(row.get("body_text") or row.get("text")),
        body_html=_str(row.get("body_html") or row.get("html")),
        read=_bool(row.get("read")),
        starred=_bool(row.get("starred")),
        replied=_bool(row.get("replied")),
        ai_draft=_str(row.get("ai_draft")),
        label=_str(row.get("label")),
        trashed=_bool(row.get("trashed")),
        sent_at=sent_at if direction_value == "outbound" else None,
        received_at=sent_at if direction_value == "inbound" else None,
        attachment_count=attachment_count,
        attachments=attachments,
    )


class MailIngestScopeError(RuntimeError):
    """Raised when a requested ingest scope cannot be honored (fail-closed).

    Raised when a mail account row asks for owner-scope ingest but
    `ORTHUS_OWNER_SCOPE_ENABLED` is off: there is no owner row-level filter to hide
    the content, so the account must be skipped rather than silently downgraded to
    company-wide scope (`docs/p6-unified-mail.md` §12.2). The backend/kind-based
    scope itself lives in `orthus.mail.ingest.ingest_scope_for_backend` (#321).
    """


def _with_send_state(status: MailBackendStatus, settings: Settings) -> MailBackendStatus:
    """Annotate a read status with whether its send credentials are configured.

    Imported lazily so the read-only inbox path keeps no hard dependency on the
    P6.3 send module.
    """

    if status.backend == "gmail":
        return status
    from orthus.mail.send import send_backend_config

    cfg = send_backend_config(status.backend, settings)
    return status.model_copy(
        update={
            "send_configured": cfg.configured,
            # Keep the per-mailbox owner_addr set during the read fan-out; only
            # fall back to the env send config owner (single-account) if unset.
            "owner_addr": status.owner_addr or (cfg.owner.strip() or None),
        }
    )


def _secret_or_inline(secret_ref: str, inline_value: str) -> str:
    """Resolve configured secret refs first; inline env values remain dev fallback."""

    ref = secret_ref.strip()
    if not ref:
        return inline_value
    try:
        return get_secret(ref) or ""
    except (SecretStoreError, ValueError):
        return ""


def _headers_for_config(cfg: MailBackendConfig) -> dict[str, str]:
    headers = {"User-Agent": MAIL_HTTP_USER_AGENT}
    if cfg.bearer_token:
        headers["Authorization"] = f"Bearer {cfg.bearer_token}"
    elif cfg.session_token:
        headers["x-session"] = cfg.session_token
    return headers


def _email_sort_key(item: CanonicalEmail) -> datetime:
    return item.received_at or item.sent_at or datetime.min


def _matches_search(item: CanonicalEmail, query: str) -> bool:
    haystack = " ".join(
        [
            item.subject,
            item.from_addr,
            " ".join(item.to_addr),
            " ".join(item.cc_addr),
            item.body_text[:1000],
            item.label,
        ]
    ).lower()
    return query in haystack


def _display_scope_for_addresses(values: list[str]) -> MailScope:
    for _, addr in getaddresses(values):
        domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
        if domain in COMPANY_MAIL_DOMAINS:
            return "company"
    return "personal"


def _addr_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = str(value).replace(";", ",").split(",")
    return [item.strip() for item in raw if item.strip()]


def _attachments(value: object) -> list[EmailAttachmentRef]:
    if not isinstance(value, list):
        return []
    refs: list[EmailAttachmentRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        content_id = _optional_str(item.get("content_id") or item.get("cid"))
        refs.append(
            EmailAttachmentRef(
                filename=_str(item.get("filename") or "attachment"),
                content_type=_str(item.get("content_type") or "application/octet-stream"),
                size=_int(item.get("size"), default=0),
                ref=_optional_str(
                    item.get("ref") or item.get("url") or item.get("s3_key") or item.get("r2_key")
                ),
                att_id=_optional_str(
                    item.get("id") or item.get("att_id") or item.get("attachment_id")
                ),
                content_id=content_id,
                inline=bool(content_id) or _bool(item.get("inline")),
            )
        )
    return refs


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _optional_str(value: object) -> str | None:
    text = _str(value).strip()
    return text or None


def _int(value: object, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y"}
