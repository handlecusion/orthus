"""P6.2 mail push ingest path."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from datetime import UTC
from typing import Any
from uuid import UUID

from sqlalchemy import select

from orthus.audit import audit
from orthus.auth import normalize_email
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.mail.backends import MailIngestScopeError, account_ingest_scope
from orthus.schemas.canonical import (
    InternalDocument,
    MailBackendName,
    MailIngestRequest,
    MailIngestResult,
    MailScope,
)
from orthus.secrets import SecretStoreError, get_secret
from orthus.settings import Settings
from orthus.tables import auth_identities, documents

ALLOWED_PUSH_BACKENDS = {"nova", "acme"}


class MailIngestAuthError(RuntimeError):
    """Raised when push ingest credentials are absent or invalid."""


class MailIngestDisabledError(RuntimeError):
    """Raised when this node should not expose push ingest."""


class MailIngestValidationError(ValueError):
    """Raised when an authenticated payload is not accepted by this phase."""


class MailIngestConfigError(RuntimeError):
    """Raised when an individual mailbox cannot be bound to a real owner.

    Fail-closed: an "individual" (single-employee) work mailbox must resolve to a
    real auth_identity owner. If `resolve_mail_owner_user_id` would fall back to the
    service user, we refuse to ingest rather than store one employee's mail under a
    shared service account where everyone could read it.
    """


def assert_mail_ingest_enabled(settings: Settings) -> None:
    if not settings.mail_ingest_enabled or settings.node_kind != "company":
        raise MailIngestDisabledError("mail ingest disabled")


def verify_mail_ingest_auth(
    *,
    authorization: str | None,
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    settings: Settings,
    now: float | None = None,
) -> None:
    """Validate Bearer + D15 HMAC(timestamp.body) envelope."""

    expected_bearer = _secret_or_inline(
        settings.mail_ingest_secret_ref, settings.mail_ingest_secret
    )
    if not expected_bearer:
        raise MailIngestAuthError("mail ingest bearer secret missing")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected_bearer):
        raise MailIngestAuthError("mail ingest bearer invalid")

    hmac_secret = _secret_or_inline(
        settings.mail_ingest_hmac_secret_ref,
        settings.mail_ingest_hmac_secret,
    )
    if not hmac_secret:
        raise MailIngestAuthError("mail ingest hmac secret missing")
    _verify_hmac(
        raw_body=raw_body,
        timestamp=timestamp,
        signature=signature,
        secret=hmac_secret,
        replay_window_seconds=settings.mail_ingest_replay_window_seconds,
        now=now,
    )


def resolve_ingest_route(
    payload: MailIngestRequest,
    settings: Settings,
    *,
    account: Mapping[str, Any] | None = None,
) -> tuple[MailScope, UUID]:
    """Resolve the (scope, owner_id) write destination for one mail message.

    `docs/p6-unified-mail.md` §12.2/§12.11. With no account row (push/env
    single-account path) this delegates to `ingest_scope_and_owner` (#321): the
    per-backend `mail_kind` (individual -> personal+owner, shared -> company) decides.

    With a mail account row (multi-account), the row's `ingest_scope` decides:
      - `"owner"`   -> scope=personal, owner_id = the registrant (account.owner_id)
        so `wiki_pages.owner_id` is the registrant and the P8 owner filter hides it.
        Requires `ORTHUS_OWNER_SCOPE_ENABLED`; otherwise `MailIngestScopeError`
        (fail-closed, the caller skips that account).
      - `"company"` -> scope=company, owner_id resolved from `owner_addr` for audit
        (the P8 owner filter treats a company-scope row as company-wide).
    """

    if account is None:
        # Env / push single-account path: #321 kind-based scope + owner.
        return ingest_scope_and_owner(payload.backend, payload.owner_addr, settings)

    # Multi-account (P6.7.3): the account row's ingest_scope decides.
    raw = account_ingest_scope(account)
    if raw == "company":
        return "company", resolve_mail_owner_user_id(payload.owner_addr, settings)
    # owner-scope (default): personal, bound to the registrant. Requires the
    # owner-scope flag so the P8 wiki owner filter can hide it; else fail-closed.
    if not settings.owner_scope_enabled:
        raise MailIngestScopeError("owner-scope mail ingest requires ORTHUS_OWNER_SCOPE_ENABLED")
    owner_id = account.get("owner_id")
    if isinstance(owner_id, UUID):
        return "personal", owner_id
    if owner_id is not None:
        return "personal", UUID(str(owner_id))
    return "personal", resolve_mail_owner_user_id(payload.owner_addr, settings)


def ingest_mail(
    payload: MailIngestRequest,
    settings: Settings,
    *,
    account: Mapping[str, Any] | None = None,
) -> MailIngestResult:
    """Ingest one mail message into the corpus/wiki pipeline.

    With no `account`, this is the unchanged push/env single-account path. With a
    mail `account` row (P6.7.3 multi-account pull), the row's `ingest_scope` routes
    (scope, owner_id) via `resolve_ingest_route` so owner-scope mailboxes land in
    the registrant's owner-only wiki and company-scope mailboxes stay company-wide.
    """

    if payload.backend not in ALLOWED_PUSH_BACKENDS:
        raise MailIngestValidationError("backend not enabled for push ingest")
    if not payload.message_id and not payload.external_id:
        raise MailIngestValidationError("message_id or external_id required")

    source_canonical_id = mail_source_canonical_id(payload)
    scope, owner_user_id = resolve_ingest_route(payload, settings, account=account)
    project = project_for_mail_backend(payload.backend)
    source = f"mail_{payload.backend}"

    with audit("mail.ingest") as span:
        span.add_meta(
            backend=payload.backend,
            message_id=payload.message_id,
            external_id=payload.external_id,
            source_canonical_id=source_canonical_id,
            scope=scope,
            mailbox_kind=settings.mail_kind_for(payload.backend),
            project=project,
        )
        existing = _existing_mail_doc_id(source, source_canonical_id, scope)
        if existing is not None:
            span.add_meta(idempotent_skip=True, doc_id=str(existing))
            return MailIngestResult(
                doc_id=existing,
                ingested=False,
                scope=scope,
                source_canonical_id=source_canonical_id,
            )

        doc = InternalDocument(
            title=_mail_title(payload),
            block_json=[],
            markdown=_mail_markdown(payload),
            source=source,
            source_external_id=payload.message_id or payload.external_id,
            source_canonical_id=source_canonical_id,
            source_last_edited_at=payload.sent_at,
            project=project,
        )
        doc_id, changed = upsert_source_document(owner_user_id, doc, scope=scope)
        span.add_meta(idempotent_skip=not changed, doc_id=str(doc_id))
        # Owner-scope mail with the local-agent flag drafts the reply on the
        # mailbox owner's own collector daemon; otherwise the central P7.1 path
        # runs. The two are mutually exclusive so a mail is never double-drafted.
        if settings.mail_reply_draft_agent_enabled and scope == "personal":
            reply_work_id = _maybe_create_reply_draft_agent_task(
                payload, owner_user_id, settings, changed=changed, span=span
            )
        else:
            reply_work_id = _maybe_create_reply_candidate(
                payload, owner_user_id, settings, changed=changed, scope=scope, span=span
            )
        _maybe_create_delegation_candidate(
            payload, owner_user_id, settings, changed=changed, span=span
        )
        # Phase 3-B MA.8a — enqueue an offline decompose orchestration for inbound
        # company mail with a compound/action signal. Best-effort, fail-closed
        # (ORTHUS_ASK_EVENT_ORCH_ENABLED), idempotent; never breaks ingest. The P7.1
        # reply-draft path above is unchanged (sink = knowledge brief only).
        _maybe_enqueue_event_orchestration(
            payload,
            owner_user_id,
            settings,
            changed=changed,
            scope=scope,
            source_canonical_id=source_canonical_id,
            project=project,
            span=span,
        )
        return MailIngestResult(
            work_id=reply_work_id,
            doc_id=doc_id,
            ingested=changed,
            scope=scope,
            source_canonical_id=source_canonical_id,
        )


def _maybe_enqueue_event_orchestration(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
    *,
    changed: bool,
    scope: MailScope,
    source_canonical_id: str,
    project: str | None,
    span,
) -> UUID | None:
    """Best-effort MA.8a enqueue. Local import keeps mail free of a router import
    at module load and avoids any import cycle. Never raises (the callee already
    swallows, but guard the import too)."""
    try:
        from orthus.router.event_orchestration import enqueue_mail_event_orchestration

        return enqueue_mail_event_orchestration(
            payload,
            owner_user_id,
            settings,
            scope=scope,
            source_canonical_id=source_canonical_id,
            project=project,
            changed=changed,
            span=span,
        )
    except Exception as exc:  # noqa: BLE001 — orchestration enqueue must not break ingest
        span.add_meta(event_orch_enqueue_error=type(exc).__name__)
        return None


def _maybe_create_reply_candidate(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
    *,
    changed: bool,
    scope: MailScope,
    span,
) -> UUID | None:
    """Best-effort P7.1 reply-draft candidate creation. Never breaks ingest.

    This is the only place mail imports agentwork; agentwork stays free of mail
    imports. The import is local to avoid an import-time cycle.

    For an individual mailbox (scope=personal) the reply-draft Agent Work item must
    be owner-bound so only that employee sees it. On a company node a reply draft is
    otherwise stored with owner_id=NULL (company-shared, visible to all users); a
    shared mailbox (scope=company) keeps that company-shared visibility.
    """
    if not changed or not settings.mail_reply_draft_enabled:
        return None
    try:
        from orthus.agentwork.service import persist_reply_candidate
        from orthus.mail.reply import build_reply_candidate

        candidate = build_reply_candidate(payload, owner_user_id, settings)
        if candidate is None:
            return None
        item = persist_reply_candidate(
            owner_user_id,
            candidate,
            settings=settings,
            owner_scoped=scope == "personal",
        )
        if item is None:
            return None
        span.add_meta(reply_work_id=str(item.work_id))
        return item.work_id
    except Exception as exc:  # noqa: BLE001 — reply candidate failure must not break ingest
        span.add_meta(reply_candidate_error=type(exc).__name__)
        return None


def _maybe_create_delegation_candidate(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
    *,
    changed: bool,
    span,
) -> UUID | None:
    """Best-effort delegation-candidate creation (slice 4). Never breaks ingest.

    Like the reply hook, this is one of the few places mail imports agentwork;
    agentwork stays free of mail imports. The LLM only extracts the delegation
    intent — the deterministic policy gate decides the outcome. The flag + company
    node are re-checked inside build_delegation_candidate.
    """
    if not changed or not settings.mail_agent_task_delegation_enabled:
        return None
    try:
        from orthus.mail.delegation import build_delegation_candidate

        item = build_delegation_candidate(payload, owner_user_id, settings)
        if item is None:
            return None
        span.add_meta(delegation_work_id=str(item.work_id), delegation_state=item.state)
        return item.work_id
    except Exception as exc:  # noqa: BLE001 — delegation failure must not break ingest
        span.add_meta(delegation_candidate_error=type(exc).__name__)
        return None


def _maybe_create_reply_draft_agent_task(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
    *,
    changed: bool,
    span,
) -> UUID | None:
    """Best-effort local-agent reply draft (owner-scope). Never breaks ingest.

    Self-assigns an agent_task to the mailbox owner so their own collector daemon
    drafts the reply, instead of the central LLM (P7.1). Called only for
    owner-scope mail with the flag on; the flag + company node are re-checked
    inside build_reply_draft_agent_task.
    """
    if not changed or not settings.mail_reply_draft_agent_enabled:
        return None
    try:
        from orthus.mail.reply_delegation import build_reply_draft_agent_task

        item = build_reply_draft_agent_task(payload, owner_user_id, settings)
        if item is None:
            return None
        span.add_meta(reply_agent_work_id=str(item.work_id), reply_agent_state=item.state)
        return item.work_id
    except Exception as exc:  # noqa: BLE001 — reply-draft delegation must not break ingest
        span.add_meta(reply_agent_candidate_error=type(exc).__name__)
        return None


def mail_source_canonical_id(payload: MailIngestRequest) -> str:
    stable_id = payload.message_id or payload.external_id
    return f"mail:{payload.backend}:{stable_id}"


def project_for_mail_backend(backend: str) -> str:
    if backend == "nova":
        return "nova"
    return "company"


def ingest_scope_and_owner(
    backend: str,
    owner_addr: str,
    settings: Settings,
) -> tuple[MailScope, UUID]:
    """Decide the ingest scope AND owner for a mail backend (privacy boundary).

    docs/p6-mail-individual-scope.md:
    - gmail -> personal, owner resolved from the address (unchanged).
    - non-gmail + kind="shared" -> company (whole-company readable). owner is still
      resolved for audit/owner_addr but does not gate visibility.
    - non-gmail + kind="individual" -> personal, bound to the resolved owner. The
      owner is REQUIRED: if it falls back to the service user (owner_addr did not map
      to a real auth_identity), raise MailIngestConfigError (fail-closed) so a single
      employee's mailbox is never ingested under the service user.
    """
    owner_user_id = resolve_mail_owner_user_id(owner_addr, settings)
    if backend == "gmail":
        return "personal", owner_user_id

    kind = settings.mail_kind_for(backend)
    if kind == "shared":
        return "company", owner_user_id

    # individual mailbox: owner must be a real, resolvable user.
    service_user_id = UUID(settings.mail_ingest_service_user_id)
    if owner_user_id == service_user_id:
        raise MailIngestConfigError(
            f"individual mailbox owner unresolved for backend={backend} "
            f"owner_addr={owner_addr!r}; configure ORTHUS_MAIL_*_OWNER to a real "
            f"account or set ORTHUS_MAIL_*_KIND=shared for a shared mailbox"
        )
    return "personal", owner_user_id


def ingest_scope_for_backend(backend: MailBackendName, settings: Settings) -> MailScope:
    """Backend+config scope (owner-agnostic) for cheap idempotency lookups.

    The authoritative scope+owner decision is `ingest_scope_and_owner`; this helper
    returns only the scope so the pull path can compute the same idempotency key
    without resolving the owner twice.
    """
    if backend == "gmail":
        return "personal"
    return "company" if settings.mail_kind_for(backend) == "shared" else "personal"


def resolve_mail_owner_user_id(owner_addr: str, settings: Settings) -> UUID:
    email = normalize_email(owner_addr)
    if email:
        with session() as s:
            row = s.execute(
                select(auth_identities.c.user_id)
                .where(auth_identities.c.email == email)
                .order_by(auth_identities.c.created_at.desc())
                .limit(1)
            ).first()
        if row:
            return row.user_id
    return UUID(settings.mail_ingest_service_user_id)


def _existing_mail_doc_id(source: str, source_canonical_id: str, scope: MailScope) -> UUID | None:
    with session() as s:
        row = s.execute(
            select(documents.c.doc_id).where(
                documents.c.source == source,
                documents.c.scope == scope,
                documents.c.source_canonical_id == source_canonical_id,
            )
        ).first()
    return row.doc_id if row else None


def _mail_title(payload: MailIngestRequest) -> str:
    subject = payload.subject.strip() or "(no subject)"
    return f"Mail: {subject}"


def _mail_markdown(payload: MailIngestRequest) -> str:
    sent_at = payload.sent_at.astimezone(UTC).isoformat() if payload.sent_at else ""
    attachments = "\n".join(
        f"- {item.filename} ({item.content_type}, {item.size} bytes)"
        for item in payload.attachments
    )
    sections = [
        "# Mail",
        f"Backend: {payload.backend}",
        f"Direction: {payload.direction}",
        f"Owner: {payload.owner_addr}",
        f"Message-ID: {payload.message_id or ''}",
        f"External-ID: {payload.external_id}",
        f"From: {payload.from_addr}",
        f"To: {', '.join(payload.to_addr)}",
        f"Cc: {', '.join(payload.cc_addr)}",
        f"Subject: {payload.subject}",
        f"Sent at: {sent_at}",
        "",
        "## Body",
        payload.body_text.strip(),
    ]
    if payload.body_html.strip():
        sections.extend(["", "## HTML", payload.body_html.strip()])
    if attachments:
        sections.extend(["", "## Attachments", attachments])
    return "\n".join(sections).strip()


def _verify_hmac(
    *,
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    replay_window_seconds: int,
    now: float | None,
) -> None:
    if not timestamp or not signature:
        raise MailIngestAuthError("mail ingest hmac missing")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise MailIngestAuthError("mail ingest timestamp invalid") from exc
    current = int(now if now is not None else time.time())
    if abs(current - ts) > max(1, replay_window_seconds):
        raise MailIngestAuthError("mail ingest timestamp outside replay window")

    signed = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=").strip()
    if not hmac.compare_digest(provided, expected):
        raise MailIngestAuthError("mail ingest hmac invalid")


def _secret_or_inline(secret_ref: str, inline_value: str) -> str:
    ref = secret_ref.strip()
    if not ref:
        return inline_value
    try:
        return get_secret(ref) or ""
    except (SecretStoreError, ValueError):
        return ""
