"""P6 unified mail read + P6.3 manual send endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import ValidationError
from starlette.background import BackgroundTask

from orthus.api.deps import get_current_user
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.audit import audit
from orthus.auth import AuthenticatedUser, require_node_operator
from orthus.email.log import email_hash, email_send_rate_limited, insert_email_send_log
from orthus.mail import (
    fetch_backend_conversation,
    fetch_backend_email_detail,
    list_unified_inbox,
    mutate_backend_email,
    resolve_backend_config,
    trash_backend_email,
)
from orthus.mail.backends import (
    MailAttachmentError,
    MailAttachmentTooLargeError,
    MailImageProxyError,
    fetch_backend_attachment,
    fetch_remote_image,
    nova_attachment_key_from_url,
    sniff_image_type,
)
from orthus.mail.compose import draft_email
from orthus.mail.ingest import (
    MailIngestAuthError,
    MailIngestConfigError,
    MailIngestDisabledError,
    MailIngestValidationError,
    assert_mail_ingest_enabled,
    ingest_mail,
    verify_mail_ingest_auth,
)
from orthus.mail.notify import mail_notify_state
from orthus.mail.personal import get_personal_gmail, list_personal_gmail
from orthus.mail.pull import pull_ingest_all
from orthus.mail.send import (
    MailSendOwnershipError,
    MailSendRoutingError,
    MailSendUnconfiguredError,
    resolve_send_backend_config,
    send_mail,
)
from orthus.mail.signature import get_signature, upsert_signature
from orthus.mail.tracking import (
    TRANSPARENT_GIF,
    create_tracking,
    get_status,
    inject_tracking_pixel,
    is_plausible_token,
    make_tracking_token,
    record_open,
)
from orthus.schemas.canonical import (
    CanonicalEmail,
    MailBackendName,
    MailDraftDispatchRequest,
    MailDraftDispatchResult,
    MailDraftRequest,
    MailDraftResult,
    MailFlagUpdate,
    MailIngestRequest,
    MailIngestResult,
    MailInboxResponse,
    MailMutationResult,
    MailNotifyState,
    MailPullBackendResult,
    MailPullResponse,
    MailSendRequest,
    MailSendResult,
    MailSignature,
    MailSignatureInput,
    MailTrackingStatus,
    PersonalMailDetail,
    PersonalMailResponse,
)
from orthus.settings import get_settings

router = APIRouter(prefix="/mail", tags=["mail"])


@router.get("/inbox", response_model=MailInboxResponse)
async def get_mail_inbox(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, min_length=1, max_length=200),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> MailInboxResponse:
    """Return canonical inbox rows from configured approved mail backends.

    Dual-auth (P8.7b pattern): the owner's orthus-mcp knowledge token reads its
    OWN inbox (P10 gateway `mail_list` tool). The token resolves to its owning
    user_id, so the same owner isolation below applies; `require_node_operator`
    only gates session members, exactly like the personal-board reads. Send and
    mutation routes stay session-only (`get_current_user`).
    """

    require_node_operator(current)
    if current.auth_mode == "collector_token":
        # 에이전트(토큰) read를 브라우저 read와 구분해 감사 추적 (operator review).
        with audit("mail.inbox.token_read") as span:
            span.add_meta(owner=str(current.user_id), limit=limit)
            return await list_unified_inbox(
                get_settings(),
                owner_id=current.user_id,
                limit=limit,
                offset=offset,
                search=search,
            )
    # Read isolation (§12.4-2): with multi-account on, only the requesting
    # owner's mail account rows are enumerated. Flag off / no rows is unchanged.
    return await list_unified_inbox(
        get_settings(),
        owner_id=current.user_id,
        limit=limit,
        offset=offset,
        search=search,
    )


@router.get("/signature", response_model=MailSignature)
async def get_mail_signature(
    from_addr: str | None = Query(default=None, max_length=320),
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailSignature:
    """현재 owner의 메일 명함(서명)을 돌려준다. 없으면 빈 기본값."""

    require_node_operator(current)
    return get_signature(
        node_id=get_settings().node_id,
        owner_id=current.user_id,
        from_addr=from_addr,
    )


@router.put("/signature", response_model=MailSignature)
async def put_mail_signature(
    payload: MailSignatureInput,
    from_addr: str | None = Query(default=None, max_length=320),
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailSignature:
    """현재 owner의 메일 명함을 저장/갱신한다(owner-scope, (node, owner)당 1개).

    구조화 필드만 저장하고 HTML은 보관하지 않는다(저장 XSS 차단).
    """

    require_node_operator(current)
    with audit("mail.signature.save"):
        return upsert_signature(
            node_id=get_settings().node_id,
            owner_id=current.user_id,
            payload=payload,
            from_addr=from_addr,
        )


def _company_backend_config(
    current: AuthenticatedUser,
    backend: MailBackendName,
    account_id: UUID | None = None,
):
    """Resolve an owner-isolated config for a company mail backend or raise 404.

    gmail is read-only (P6.1b) and never resolves here; mutation/detail proxies
    stay on nova/acme only.
    """
    cfg = resolve_backend_config(
        get_settings(),
        backend,
        owner_id=current.user_id,
        account_id=account_id,
    )
    if cfg is None:
        raise HTTPException(status_code=404, detail="mail backend not configured")
    return cfg


@router.get("/message", response_model=CanonicalEmail)
async def get_mail_message(
    backend: MailBackendName = Query(...),
    id: str = Query(..., min_length=1, max_length=512),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> CanonicalEmail:
    """Fetch one company-mail message body+attachments (auto-marks it read).

    Read-only proxy to the approved backend's GET /mail/{id}; orthus injects the
    backend credential server-side and never exposes it. Slice 1 of the Gmail-style
    mail UX (`docs/p6-unified-mail.md`). Dual-auth read like `/mail/inbox`: the
    owner's knowledge token fetches one message from its OWN mailbox (the
    owner-isolated `_company_backend_config` resolve is unchanged); flag/patch/
    trash/send stay session-only.
    """
    require_node_operator(current)
    if current.auth_mode == "collector_token":
        # 에이전트(토큰) read 감사 마커 — 브라우저 read와 구분 (operator review).
        # 이 read는 backend 측 읽음 처리 side-effect가 있다(위 docstring 계약).
        with audit("mail.message.token_read") as span:
            span.add_meta(owner=str(current.user_id), backend=str(backend))
    cfg = _company_backend_config(current, backend, account_id)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.mail_timeout_seconds) as client:
            email = await fetch_backend_email_detail(cfg, id, client)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"mail backend error: {type(exc).__name__}"
        ) from exc
    if email is None:
        raise HTTPException(status_code=404, detail="mail message not found")
    return email


def _safe_attachment_filename(name: str) -> str:
    """Strip path + control characters from a client-supplied download name."""
    cleaned = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in '"\r\n')
    return cleaned.strip()[:255]


@router.get("/attachment")
async def get_mail_attachment(
    backend: MailBackendName = Query(...),
    ref: str = Query(..., min_length=1, max_length=1024),
    filename: str = Query(default="", max_length=255),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Stream one company-mail attachment's bytes through orthus.

    The provider attachment store is credential-gated and the keys are
    provider-relative (S3/R2), so the browser cannot fetch them directly. orthus
    injects the backend credential server-side and streams the bytes back. `ref`
    is validated by the backend adapter as a `mail/attachments/` key. Gmail is
    not supported on this company-mail proxy path.
    """
    require_node_operator(current)
    cfg = _company_backend_config(current, backend, account_id)
    settings = get_settings()
    timeout = max(settings.mail_timeout_seconds, 30.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            fetched = await fetch_backend_attachment(cfg, ref, client)
    except MailAttachmentError:
        raise HTTPException(status_code=400, detail="invalid attachment reference") from None
    except MailAttachmentTooLargeError:
        raise HTTPException(status_code=413, detail="attachment too large") from None
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"mail backend error: {type(exc).__name__}"
        ) from exc
    if fetched is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    data, content_type = fetched
    safe_name = _safe_attachment_filename(filename) or "attachment"
    disposition = f"attachment; filename*=UTF-8''{quote(safe_name)}"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/image")
async def get_mail_image(
    url: str = Query(..., min_length=8, max_length=2048),
    backend: MailBackendName | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """본문 <img>용 원격 이미지 프록시 (Gmail의 googleusercontent 프록시 상당).

    핫링크 차단·만료 presigned S3 URL·http 혼합콘텐츠로 깨지는 본문 이미지를
    서버가 대신 받아 스트리밍한다. nova S3 인라인 이미지 URL은 provider
    재서명 경로(`fetch_backend_attachment`)로 우회하고, 그 외에는 공인 호스트만
    허용하는 SSRF 가드를 지나 직접 가져온다. 이미지 타입만 통과.
    """
    require_node_operator(current)
    settings = get_settings()
    timeout = max(settings.mail_timeout_seconds, 20.0)
    fetched: tuple[bytes, str] | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            key = nova_attachment_key_from_url(url) if backend == "nova" else None
            if key:
                cfg = resolve_backend_config(
                    settings, "nova", owner_id=current.user_id, account_id=account_id
                )
                if cfg is not None:
                    try:
                        fetched = await fetch_backend_attachment(cfg, key, client)
                    except (MailAttachmentError, httpx.HTTPError):
                        fetched = None
            if fetched is None:
                fetched = await fetch_remote_image(url, client)
    except MailImageProxyError:
        raise HTTPException(status_code=400, detail="invalid image url") from None
    except MailAttachmentTooLargeError:
        raise HTTPException(status_code=413, detail="image too large") from None
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"image fetch error: {type(exc).__name__}"
        ) from exc
    if fetched is None:
        raise HTTPException(status_code=404, detail="image not found")
    data, content_type = fetched
    if not content_type.startswith("image/"):
        sniffed = sniff_image_type(data)
        if sniffed is None:
            raise HTTPException(status_code=415, detail="not an image")
        content_type = sniffed
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # 재렌더마다 재요청하지 않게 브라우저 캐시(세션 사용자 전용).
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/message", response_model=MailMutationResult)
async def patch_mail_message(
    update: MailFlagUpdate,
    backend: MailBackendName = Query(...),
    id: str = Query(..., min_length=1, max_length=512),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailMutationResult:
    """Mutate read/starred/label flags on one company-mail message (slice 2)."""
    require_node_operator(current)
    cfg = _company_backend_config(current, backend, account_id)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.mail_timeout_seconds) as client:
            ok = await mutate_backend_email(
                cfg,
                id,
                client,
                read=update.read,
                starred=update.starred,
                label=update.label,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"mail backend error: {type(exc).__name__}"
        ) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="mail message not found")
    return MailMutationResult(ok=True)


@router.get("/conversation", response_model=list[CanonicalEmail])
async def get_mail_conversation(
    backend: MailBackendName = Query(...),
    contact: str = Query(..., min_length=3, max_length=320),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[CanonicalEmail]:
    """Message history between the mailbox owner and `contact` (slice 7).

    Address-pair conversation (not RFC threading) proxied from the approved
    backend's GET /mail/conversation. Read-only; gmail is unsupported here.
    """
    require_node_operator(current)
    cfg = _company_backend_config(current, backend, account_id)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.mail_timeout_seconds) as client:
            return await fetch_backend_conversation(cfg, contact, client)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"mail backend error: {type(exc).__name__}"
        ) from exc


@router.post("/message/trash", response_model=MailMutationResult)
async def trash_mail_message(
    backend: MailBackendName = Query(...),
    id: str = Query(..., min_length=1, max_length=512),
    restore: bool = Query(default=False),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailMutationResult:
    """Move one company-mail message to trash, or restore it (slice 2)."""
    require_node_operator(current)
    cfg = _company_backend_config(current, backend, account_id)
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.mail_timeout_seconds) as client:
            ok = await trash_backend_email(cfg, id, client, restore=restore)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"mail backend error: {type(exc).__name__}"
        ) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="mail message not found")
    return MailMutationResult(ok=True)


@router.post("/ingest", response_model=MailIngestResult)
async def post_mail_ingest(
    request: Request,
    authorization: str | None = Header(default=None),
    x_orthus_timestamp: str | None = Header(default=None),
    x_orthus_signature: str | None = Header(default=None),
) -> MailIngestResult:
    settings = get_settings()
    try:
        assert_mail_ingest_enabled(settings)
    except MailIngestDisabledError:
        raise HTTPException(status_code=404, detail="mail ingest not found") from None

    raw_body = await request.body()
    try:
        verify_mail_ingest_auth(
            authorization=authorization,
            raw_body=raw_body,
            timestamp=x_orthus_timestamp,
            signature=x_orthus_signature,
            settings=settings,
        )
    except MailIngestAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None

    try:
        payload = MailIngestRequest.model_validate_json(raw_body)
        return ingest_mail(payload, settings)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from None
    except MailIngestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MailIngestConfigError as exc:
        # Individual mailbox with an unresolvable owner is a config error, not a
        # server fault: surface a clean 422 instead of an uncaught 500.
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/pull", response_model=MailPullResponse)
def post_mail_pull(
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailPullResponse:
    """On-demand company-mail pull-ingest — the '새로고침' trigger.

    Runs the same pull-ingest as the periodic mail-sync, but inside the API
    process, so any reply-draft agent_task it enqueues fires a WS notify to the
    connected collector daemon and is drafted within seconds instead of waiting
    for the next 15-minute cycle. Company-node owner/admin only; a no-op (zero
    counts) when pull-ingest is off.
    """

    settings = get_settings()
    if settings.node_kind != "company":
        raise HTTPException(status_code=404, detail="mail pull not found")
    require_node_operator(current)
    with audit("mail.pull_ingest_manual") as span:
        results = pull_ingest_all(settings)
        backends = [
            MailPullBackendResult(
                backend=r.backend,
                enabled=r.enabled,
                listed=r.listed,
                ingested=r.ingested,
                skipped=r.skipped,
                errors=r.errors,
            )
            for r in results
        ]
        total_ingested = sum(b.ingested for b in backends)
        span.add_meta(
            backends=len(backends),
            ingested=total_ingested,
            errors=sum(b.errors for b in backends),
        )
    return MailPullResponse(
        enabled=any(b.enabled for b in backends),
        ingested=total_ingested,
        backends=backends,
    )


@router.get("/notify-state", response_model=MailNotifyState)
def get_mail_notify_state(
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailNotifyState:
    """Cheap new-mail signal for desktop notifications.

    Local-only: a count/max over already-ingested mail documents visible to the
    caller (company scope + own personal). No external provider fan-out, so the
    desktop shell can poll it frequently to decide whether to raise a native
    notification. Returns total, latest ingest time, and latest subject.
    """

    return mail_notify_state(current.user_id)


@router.post("/draft", response_model=MailDraftResult)
def post_mail_draft(
    payload: MailDraftRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailDraftResult:
    """AI-generate a compose/reply draft (text only, never sends)."""

    require_node_operator(current)
    return draft_email(payload, user_id=current.user_id)


@router.post("/draft/dispatch", response_model=MailDraftDispatchResult)
def post_mail_draft_dispatch(
    payload: MailDraftDispatchRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailDraftDispatchResult:
    """Route the "AI 초안" draft to the requester's own local claude/hermes."""

    settings = get_settings()
    # Fail-closed: disabled flag hides the surface so the FE falls back to the
    # legacy synchronous `/mail/draft` path.
    if not settings.mail_agent_draft_enabled:
        raise HTTPException(status_code=404, detail="mail draft dispatch not found")
    require_node_operator(current)
    if settings.node_kind != "company":
        raise HTTPException(status_code=404, detail="mail draft dispatch not found")

    runner = payload.runner or "claude"

    # Import locally to keep orthus.agentwork free of a mail import at module load.
    from orthus.mail.draft_delegation import build_compose_draft_agent_task

    item = build_compose_draft_agent_task(
        payload,
        current.user_id,
        actor_role=current.role,
        settings=settings,
    )
    if item is not None and item.state == "auto_execute":
        return MailDraftDispatchResult(
            mode="local",
            work_id=item.work_id,
            stream=settings.agent_task_streaming_enabled,
            runner=runner,
        )

    if settings.mail_draft_central_fallback:
        draft = draft_email(payload.to_draft_request(), user_id=current.user_id)
        return MailDraftDispatchResult(mode="central", draft=draft)
    return MailDraftDispatchResult(
        mode="unavailable",
        message=(
            "이 메일 초안은 회원님 PC의 로컬 에이전트로 생성됩니다. "
            "orthus collector daemon을 등록(opt-in)하고 ORTHUS_AGENT_TASK_ENABLED를 켜 주세요."
        ),
    )


@router.post("/send", response_model=MailSendResult)
async def post_mail_send(
    payload: MailSendRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailSendResult:
    """Manual compose/send. The operator clicking send IS the owner approval."""

    settings = get_settings()
    # Kill switch fail-closed: a disabled switch hides the surface entirely.
    if not settings.mail_send_enabled:
        raise HTTPException(status_code=404, detail="mail send not found")

    # Mail send is company-domain mail: central/company node only.
    if settings.node_kind != "company":
        raise HTTPException(status_code=404, detail="mail send not found")

    # Owner/admin approval per §11-4. require_node_operator gates session members;
    # demo operators are additionally rejected for real external sends.
    require_node_operator(current)
    if current.role == "demo":
        raise HTTPException(status_code=403, detail="node operator required")

    # Send isolation (§12.4-3): resolve the from_addr to a mailbox row the
    # session user owns when multi-account is on; a from_addr they do not own
    # rejects (403) and never falls through to env/domain routing.
    try:
        send_cfg = resolve_send_backend_config(payload.from_addr, settings, current.user_id)
    except MailSendRoutingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MailSendOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    backend = send_cfg.backend
    if not send_cfg.configured:
        raise HTTPException(status_code=422, detail=f"{backend} send not configured")

    recipient_hash = email_hash(payload.to)
    subject_hash = email_hash(payload.subject)
    body_hash = email_hash(payload.text)
    now = datetime.now(UTC)
    if email_send_rate_limited(
        node_id=settings.node_id,
        owner_id=current.user_id,
        recipient_hash=recipient_hash,
        now=now,
    ):
        raise HTTPException(status_code=429, detail="recipient rate limited")

    # 열람 추적(읽음): fail-closed — 플래그 + 공개 픽셀 base URL + HTML 본문이
    # 모두 있을 때만 자체 픽셀을 본문에 심어 보낸다(provider 협조 불필요). 토큰은
    # 발송 성공 시에만 추적 행으로 남긴다. 미사용 발송 바디는 불변.
    send_payload = payload
    tracking_token: str | None = None
    if (
        settings.mail_open_tracking_enabled
        and settings.mail_tracking_pixel_base_url.strip()
        and payload.html
        and payload.track
    ):
        tracking_token = make_tracking_token()
        send_payload = payload.model_copy(
            update={
                "html": inject_tracking_pixel(
                    payload.html, settings.mail_tracking_pixel_base_url, tracking_token
                )
            }
        )

    with audit("mail.send") as span:
        span.add_meta(
            backend=backend,
            origin="manual",
            node_id=settings.node_id,
            owner_id=str(current.user_id),
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
        )
        try:
            result = await send_mail(send_payload, settings, owner_id=current.user_id)
        except MailSendUnconfiguredError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        provider_id_hash = (
            email_hash(result.provider_message_id) if result.provider_message_id else None
        )
        insert_email_send_log(
            node_id=settings.node_id,
            owner_id=current.user_id,
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
            body_hash=body_hash,
            sender_kind=backend,
            status=result.status,
            sent_at=now,
            origin="manual",
            work_id=None,
            correlation_id=span.correlation_id,
            error_message=result.error,
        )
        # 추적 행은 실제 발송 성공 건만 남긴다(실패 발송은 열릴 일이 없다). 기록 실패가
        # 이미 나간 발송을 500으로 만들지 않게 best-effort로 감싼다(메일은 이미 전송됨).
        if tracking_token is not None and result.status == "sent":
            try:
                create_tracking(
                    node_id=settings.node_id,
                    owner_id=current.user_id,
                    token=tracking_token,
                    recipient_hash=recipient_hash,
                    subject_hash=subject_hash,
                    sender_kind=backend,
                )
            except Exception:
                tracking_token = None
        else:
            tracking_token = None
        span.add_meta(tracking=tracking_token is not None)
        span.set_output({"backend": backend, "status": result.status})

    return MailSendResult(
        backend=result.backend,
        status=result.status,
        provider_message_id=provider_id_hash,
        error=result.error,
        tracking_token=tracking_token,
    )


@router.get("/track/open/{token}.gif")
async def get_mail_track_open(token: str) -> Response:
    """공개 무인증 열람 픽셀. 수신자 메일 클라이언트가 이 이미지를 로드하면 호출돼
    해당 token의 open을 1건 기록한다.

    fail-closed: 플래그 off면 라우트는 존재하지 않는 것처럼 404다(status 엔드포인트와
    동일, pre-PR 동작 동등). 플래그 on이면 항상 1x1 투명 GIF를 no-store로 반환한다
    (token 유효성/정보 비노출). open 기록은 동기 DB 쓰기라 이벤트 루프를 막지 않게
    BackgroundTask(threadpool)로 미루고, 형태가 명백히 토큰이 아닌 요청은 DB를 건드리지
    않는다(스캐너 write-amplification 완화)."""

    settings = get_settings()
    if not settings.mail_open_tracking_enabled:
        raise HTTPException(status_code=404, detail="not found")
    task = (
        BackgroundTask(record_open, token, datetime.now(UTC)) if is_plausible_token(token) else None
    )
    return Response(
        content=TRANSPARENT_GIF,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, private"},
        background=task,
    )


@router.get("/track/status/{token}", response_model=MailTrackingStatus)
async def get_mail_track_status(
    token: str,
    current: AuthenticatedUser = Depends(get_current_user),
) -> MailTrackingStatus:
    """owner 세션이 자기 보낸 메일의 읽음 현황을 조회. 타 owner/없는 token은 404."""

    settings = get_settings()
    if not settings.mail_open_tracking_enabled:
        raise HTTPException(status_code=404, detail="mail tracking not found")
    require_node_operator(current)
    status = get_status(token, node_id=settings.node_id, owner_id=current.user_id)
    if status is None:
        raise HTTPException(status_code=404, detail="tracking not found")
    return MailTrackingStatus(
        token=status.token,
        opened=status.opened,
        open_count=status.open_count,
        first_opened_at=status.first_opened_at.isoformat() if status.first_opened_at else None,
        last_opened_at=status.last_opened_at.isoformat() if status.last_opened_at else None,
    )


def _personal_mail_node_gate(settings) -> None:
    """Mirror of personal_board._node_id: serve on personal node, or company
    node only when owner_scope_enabled is true; otherwise 404."""
    if settings.node_kind == "personal":
        return
    if settings.node_kind == "company" and settings.owner_scope_enabled:
        return
    raise HTTPException(status_code=404, detail="personal mail is only available on personal nodes")


@router.get("/personal", response_model=PersonalMailResponse)
def get_personal_mail_inbox(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, min_length=1, max_length=200),
    current: AuthenticatedUser = Depends(get_current_user),
) -> PersonalMailResponse:
    """Owner-only gws_gmail inbox projection. Personal node or company node with
    owner_scope_enabled=true; never calls gws or any external provider."""
    settings = get_settings()
    _personal_mail_node_gate(settings)
    require_node_operator(current)
    with audit("mail.personal.list") as span:
        span.add_meta(
            node_id=settings.node_id,
            owner_id=str(current.user_id),
            limit=limit,
            offset=offset,
        )
        result = list_personal_gmail(
            current.user_id,
            limit=limit,
            offset=offset,
            search=search,
        )
        span.set_output({"total": result.total, "returned": len(result.items)})
    return result


@router.get("/personal/{doc_id}", response_model=PersonalMailDetail)
def get_personal_mail_detail(
    doc_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> PersonalMailDetail:
    """Full body for one owned gws_gmail document. 404 if not owned or not found."""
    settings = get_settings()
    _personal_mail_node_gate(settings)
    require_node_operator(current)
    with audit("mail.personal.detail") as span:
        span.add_meta(node_id=settings.node_id, owner_id=str(current.user_id), doc_id=str(doc_id))
        result = get_personal_gmail(current.user_id, doc_id)
    if result is None:
        raise HTTPException(status_code=404, detail="not found")
    return result
