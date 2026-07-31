"""P6.2-pull orthus-side mail pull-ingest.

A periodic orthus-controlled job that reads company-domain mail (nova +
acme) and feeds each message through the existing push-ingest pipeline
(`ingest_mail`) so mail becomes wiki knowledge — without depending on
provider-side push (blocked on external deploy access).

Boundaries (same as the push path):
- Company-node only; refuses on personal node kind.
- Fail-closed behind `ORTHUS_MAIL_PULL_INGEST_ENABLED` (default false).
- nova/acme backends only; gmail stays out of company ingest.
- redaction is skipped on the mail ingest path per `docs/p6-unified-mail.md`
  §5-B, matching `ingest_mail`.

Scope routing (`docs/p6-unified-mail.md` §12 / §12.11) — all via `resolve_ingest_route`:
- env single-account path (no account row): `ingest_scope_and_owner` (#321) by
  per-backend `mail_kind` — individual -> personal+owner, shared -> company.
- P6.7.3 multi-account: when `ORTHUS_MAIL_MULTI_ACCOUNT_ENABLED` is on the loop
  iterates every active `mail_nova`/`mail_acme` account row across owners,
  and each row's `ingest_scope` routes (scope, owner_id) (owner -> personal+owner_id,
  company -> company). owner-scope requires `ORTHUS_OWNER_SCOPE_ENABLED`; with that off
  the account is skipped fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import and_, select

from orthus.audit import audit
from orthus.db import session
from orthus.mail.backends import (
    _MAIL_ACCOUNT_BACKENDS,
    MailBackendConfig,
    MailIngestScopeError,
    _account_to_config,
    _datetime,
    _headers_for_config,
    backend_configs_from_settings,
)
from orthus.mail.ingest import (
    ALLOWED_PUSH_BACKENDS,
    _existing_mail_doc_id,
    ingest_mail,
    mail_source_canonical_id,
    resolve_ingest_route,
)
from orthus.schemas.canonical import (
    EmailAttachmentRef,
    MailBackendName,
    MailIngestRequest,
)
from orthus.settings import Settings
from orthus.tables import connector_accounts


@dataclass(frozen=True)
class PullIngestResult:
    backend: MailBackendName
    enabled: bool = False
    listed: int = 0
    ingested: int = 0
    skipped: int = 0
    errors: int = 0


def pull_ingest_all(settings: Settings) -> list[PullIngestResult]:
    """Pull-ingest every enabled, configured company-mail mailbox.

    Multi-account on: iterate ALL active `mail_nova`/`mail_acme` account
    rows across owners (this is a system/background job, not per-session), each
    routed by its own config and `ingest_scope`. Zero rows -> nothing ingested
    (no env single-account fallback). Multi-account off: the legacy env
    single-account backend list (scope=company), unchanged.
    """

    if settings.mail_multi_account_enabled:
        return [
            pull_ingest_account(account, settings) for account in _all_mail_account_rows(settings)
        ]

    results: list[PullIngestResult] = []
    for cfg in backend_configs_from_settings(settings):
        if cfg.backend not in ALLOWED_PUSH_BACKENDS:
            continue
        results.append(pull_ingest_backend(cfg.backend, settings))
    return results


def pull_ingest_backend(
    backend: MailBackendName,
    settings: Settings,
    *,
    limit: int = 50,
    client: httpx.Client | None = None,
) -> PullIngestResult:
    """List a backend inbox (env single-account), then ingest unseen messages.

    Fail-closed: returns `enabled=False` with zero counts when the pull flag is
    off, the node is not company-kind, the backend is not a company push
    backend, or the backend is not configured. One bad message is logged and
    counted as an error without aborting the batch.
    """

    if not settings.mail_pull_ingest_enabled or settings.node_kind != "company":
        return PullIngestResult(backend=backend, enabled=False)
    if backend not in ALLOWED_PUSH_BACKENDS:
        return PullIngestResult(backend=backend, enabled=False)

    cfg = _config_for_backend(backend, settings)
    if cfg is None or not cfg.configured:
        return PullIngestResult(backend=backend, enabled=False)

    return _pull_ingest_config(backend, cfg, settings, account=None, limit=limit, client=client)


def pull_ingest_account(
    account: Mapping[str, Any],
    settings: Settings,
    *,
    limit: int = 50,
    client: httpx.Client | None = None,
) -> PullIngestResult:
    """Pull-ingest one mail account row, routing scope via its `ingest_scope`.

    Each message is ingested with `account=` so `resolve_ingest_route` derives
    (scope, owner_id) from the row. An owner-scope row whose node has
    `ORTHUS_OWNER_SCOPE_ENABLED` off is skipped fail-closed (never company).
    """

    spec = _MAIL_ACCOUNT_BACKENDS.get(str(account.get("connector_slug")))
    backend: MailBackendName = spec[0] if spec else "nova"

    if not settings.mail_pull_ingest_enabled or settings.node_kind != "company":
        return PullIngestResult(backend=backend, enabled=False)
    if spec is None or backend not in ALLOWED_PUSH_BACKENDS:
        return PullIngestResult(backend=backend, enabled=False)

    cfg = _account_to_config(account, settings)
    if cfg is None or not cfg.configured:
        return PullIngestResult(backend=backend, enabled=False)

    return _pull_ingest_config(backend, cfg, settings, account=account, limit=limit, client=client)


def _pull_ingest_config(
    backend: MailBackendName,
    cfg: MailBackendConfig,
    settings: Settings,
    *,
    account: Mapping[str, Any] | None,
    limit: int,
    client: httpx.Client | None,
) -> PullIngestResult:
    fetch_limit = max(1, min(limit, 200))
    close_client = client is None
    http_client = client or httpx.Client(timeout=settings.mail_timeout_seconds)
    listed = ingested = skipped = errors = unread_restored = 0
    with audit("mail.pull_ingest") as span:
        span.add_meta(backend=backend, limit=fetch_limit, owner=cfg.owner)
        try:
            rows = _list_inbox(cfg, http_client, limit=fetch_limit)
            listed = len(rows)
            source = f"mail_{backend}"
            for row in rows:
                try:
                    stable_id = _stable_id(row)
                    if not stable_id:
                        errors += 1
                        continue
                    # Build the summary payload first so the cheap idempotency
                    # check uses the exact canonical id `ingest_mail` would
                    # compute, then only spend a body fetch on unseen messages.
                    summary = _to_ingest_request(backend, cfg, row)
                    canonical_id = mail_source_canonical_id(summary)
                    # Resolve the per-account (scope, owner_id) once so the
                    # idempotency probe matches the row `ingest_mail` would write.
                    # An owner-scope account with ORTHUS_OWNER_SCOPE_ENABLED off
                    # raises here and skips the whole account fail-closed.
                    scope, _owner = resolve_ingest_route(summary, settings, account=account)
                    if _existing_mail_doc_id(source, canonical_id, scope) is not None:
                        skipped += 1
                        continue
                    full = _fetch_message(cfg, http_client, stable_id)
                    # The detail GET above auto-marks the message read on the
                    # provider, but an ingest body fetch is not a user open —
                    # restore the unread flag so the user's inbox state is
                    # untouched. Best-effort: on failure the message stays read
                    # (the pre-fix behavior) and the batch continues.
                    if _listed_unread(row):
                        try:
                            _restore_unread(cfg, http_client, stable_id)
                            unread_restored += 1
                        except Exception as exc:  # noqa: BLE001
                            span.add_meta(
                                unread_restore_error=f"{type(exc).__name__}: {str(exc)[:120]}"
                            )
                    payload = _to_ingest_request(backend, cfg, full) if full else summary
                    result = ingest_mail(payload, settings, account=account)
                    if result.ingested:
                        ingested += 1
                    else:
                        skipped += 1
                except MailIngestScopeError as exc:
                    # owner-scope requested but owner-scope flag off -> skip this
                    # account entirely; do NOT downgrade to company-scope.
                    span.add_meta(skipped_owner_scope=f"{type(exc).__name__}: {str(exc)[:120]}")
                    return PullIngestResult(backend=backend, enabled=True)
                except Exception as exc:  # noqa: BLE001 - one bad message must not abort the batch
                    errors += 1
                    span.add_meta(last_error=f"{type(exc).__name__}: {str(exc)[:180]}")
        finally:
            if close_client:
                http_client.close()
        span.add_meta(
            listed=listed,
            ingested=ingested,
            skipped=skipped,
            errors=errors,
            unread_restored=unread_restored,
        )

    return PullIngestResult(
        backend=backend,
        enabled=True,
        listed=listed,
        ingested=ingested,
        skipped=skipped,
        errors=errors,
    )


def _config_for_backend(backend: MailBackendName, settings: Settings) -> MailBackendConfig | None:
    for cfg in backend_configs_from_settings(settings):
        if cfg.backend == backend:
            return cfg
    return None


def _all_mail_account_rows(settings: Settings) -> list[dict[str, Any]]:
    """Active `mail_nova`/`mail_acme` rows on this node, all owners.

    Background job scope: unlike `backends._mail_account_rows` (per-owner read
    isolation), this enumerates every owner's mailbox so the system pull cycle
    ingests all registered company mailboxes. Owner isolation is preserved at
    write time by the per-account (scope, owner_id) routing, not at list time.
    """

    conditions = [
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(tuple(_MAIL_ACCOUNT_BACKENDS)),
        connector_accounts.c.account_kind == "personal",
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


# Safety cap on a single pull cycle so a large mailbox cannot drive an unbounded
# fetch. Idempotency means already-ingested messages are skipped on later cycles.
_PULL_MAX_TOTAL = 500


def _list_inbox(
    cfg: MailBackendConfig,
    client: httpx.Client,
    *,
    limit: int,
) -> list[Mapping[str, object]]:
    """Page the backend inbox until exhausted or `_PULL_MAX_TOTAL`.

    The previous single `offset=0` read silently dropped every message past the
    first page (e.g. >50). We page by `limit` and stop when a page is short or the
    cap is reached. Idempotency is unchanged: callers de-dup on canonical id.
    """

    page_size = max(1, limit)
    rows: list[Mapping[str, object]] = []
    offset = 0
    while len(rows) < _PULL_MAX_TOTAL:
        params: dict[str, str | int] = {
            "owner": cfg.owner,
            "limit": page_size,
            "offset": offset,
        }
        response = client.get(
            f"{cfg.base_url.rstrip('/')}/mail/inbox",
            params=params,
            headers=_headers_for_config(cfg),
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("emails", []) if isinstance(payload, Mapping) else []
        if not isinstance(page, list):
            raise ValueError("mail backend response missing emails[]")
        page_rows = [row for row in page if isinstance(row, Mapping)]
        rows.extend(page_rows)
        if len(page) < page_size:
            break
        offset += page_size
    return rows[:_PULL_MAX_TOTAL]


def _fetch_message(
    cfg: MailBackendConfig,
    client: httpx.Client,
    message_id: str,
) -> Mapping[str, object] | None:
    response = client.get(
        f"{cfg.base_url.rstrip('/')}/mail/{message_id}",
        headers=_headers_for_config(cfg),
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, Mapping):
        body = payload.get("email") or payload.get("message")
        if isinstance(body, Mapping):
            return body
        return payload
    return None


def _listed_unread(row: Mapping[str, object]) -> bool:
    """True only when the inbox list row explicitly says unread (`read` falsy).

    A missing `read` field returns False — without list-level evidence we must
    not flip an already-read message back to unread.
    """
    value = row.get("read")
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false")
    return not bool(value)


def _restore_unread(
    cfg: MailBackendConfig,
    client: httpx.Client,
    message_id: str,
) -> None:
    """Undo the provider-side auto-mark-read triggered by `_fetch_message`.

    Mirrors `backends.mutate_backend_email` (0/1 int flags both backends accept);
    ownership is enforced backend-side via the injected credentials.
    """
    resp = client.patch(
        f"{cfg.base_url.rstrip('/')}/mail/{message_id}",
        json={"read": 0},
        headers=_headers_for_config(cfg),
    )
    resp.raise_for_status()


def _to_ingest_request(
    backend: MailBackendName,
    cfg: MailBackendConfig,
    row: Mapping[str, object],
) -> MailIngestRequest:
    stable_id = _stable_id(row) or ""
    message_id = _optional_str(row.get("message_id") or row.get("message-id"))
    direction = "outbound" if _str(row.get("direction")) == "outbound" else "inbound"
    return MailIngestRequest(
        backend=backend,
        owner_addr=cfg.owner,
        external_id=stable_id,
        message_id=message_id,
        direction=direction,
        from_addr=_str(row.get("from_addr") or row.get("from") or row.get("sender")),
        to_addr=_addr_list(row.get("to_addr") or row.get("to") or row.get("to_addrs")),
        cc_addr=_addr_list(row.get("cc_addr") or row.get("cc") or row.get("cc_addrs")),
        subject=_str(row.get("subject")),
        body_text=_str(row.get("body_text") or row.get("text")),
        body_html=_str(row.get("body_html") or row.get("html")),
        attachments=_attachments(row.get("attachments")),
        sent_at=_sent_at(row),
    )


def _stable_id(row: Mapping[str, object]) -> str | None:
    return _optional_str(
        row.get("id")
        or row.get("external_id")
        or row.get("email_id")
        or row.get("message_id")
        or row.get("message-id")
    )


def _sent_at(row: Mapping[str, object]) -> datetime | None:
    return _datetime(
        row.get("sent_at")
        or row.get("created_at")
        or row.get("created")
        or row.get("received_at")
        or row.get("date")
    )


def _attachments(value: object) -> list[EmailAttachmentRef]:
    if not isinstance(value, list):
        return []
    refs: list[EmailAttachmentRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        refs.append(
            EmailAttachmentRef(
                filename=_str(item.get("filename") or "attachment"),
                content_type=_str(item.get("content_type") or "application/octet-stream"),
                size=_int(item.get("size")),
                ref=_optional_str(
                    item.get("ref") or item.get("url") or item.get("s3_key") or item.get("r2_key")
                ),
            )
        )
    return refs


def _addr_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = str(value).replace(";", ",").split(",")
    return [item.strip() for item in raw if item.strip()]


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _optional_str(value: object) -> str | None:
    text = _str(value).strip()
    return text or None


def _int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
