"""Canonical Pydantic v2 models. Cross-layer communication uses these only —
no single giant integration schema (prompt §4.2, architecture §2)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

SCHEMA_VERSION = 1


# --- 비서 (secretary) core types (prompt §6) ------------------------------------


class CompiledQuery(BaseModel):
    question: str
    source_id: UUID | None = None  # None for the structured (PG JSONB store) backend
    dialect: str
    sql: str


class ValidationResult(BaseModel):
    parsed: bool = False
    statement_kind: str = "other"  # 'select' | 'other'
    schema_ok: bool = False
    read_only_ok: bool = False
    explain_ok: bool = False
    injected_limit: int | None = None
    rejected_reason: str | None = None  # None => passed

    @property
    def passed(self) -> bool:
        return self.rejected_reason is None


# --- connector normalization target (source-agnostic) ---------------------------


class InternalDocument(BaseModel):
    """Common internal doc model every connector normalizes to (prompt §4.1)."""

    title: str
    block_json: list[dict] = Field(default_factory=list)  # BlockNote block model
    markdown: str
    # Connector slug/source name, e.g. 'notion', 'editor', 'gmail', 'slack',
    # 'github', 'local_file'. C0 removed the old notion/editor-only DB check.
    source: str
    source_external_id: str | None = None
    # Stable source identity used when multiple connector accounts can discover
    # the same upstream object. Example: notion:page:<normalized_page_uuid>.
    source_canonical_id: str | None = None
    # The originating Notion DB name for structured DB rows; None for standalone
    # pages and editor docs. Persisted on `documents.source_db_name` so wiki/corpus
    # content stays traceable to its db_name for project re-tagging (P2).
    source_db_name: str | None = None
    source_last_edited_at: datetime | None = None
    # Project the content belongs to within the company (P2): one of
    # 'atlas' | 'nova' | 'orbit' | 'company'. Resolved by the connector via
    # `orthus.connectors.project_map.resolve_project`; defaults to the workspace
    # project (atlas).
    project: str = "atlas"
    # When the doc is a structured DB row, this carries the typed row for the
    # structured(PG) backend: {"db_id","db_name","row_id","properties"} where
    # `properties` is a flat JSON-able {key: readable scalar/list} map (P2.2a).
    structured: dict | None = None
    # Generic structured facts/events extracted from non-Notion sources such as
    # Slack. Each row is stored in `structured_rows` and may carry source-specific
    # `record_type`, `record_key`, `properties`, `evidence`, and `confidence`.
    structured_rows: list[dict] = Field(default_factory=list)
    # Runtime-only ingestion hint. False keeps the raw document/corpus/structured
    # rows but prevents low-value source snippets from becoming compiled wiki pages.
    wiki_authoring: bool = True
    wiki_skip_reason: str | None = None


# --- corpus / RAG ---------------------------------------------------------------


class ChunkHit(BaseModel):
    chunk_id: UUID
    doc_id: UUID
    ordinal: int
    content: str
    score: float


class WikiSourceRef(BaseModel):
    page_slug: str
    title: str
    kind: str  # 'page' | 'claim'
    excerpt: str
    score: float
    provenance: list[str] = Field(default_factory=list)  # source slugs / corpus refs
    source_scope: Literal["company", "personal"] = "company"
    source_node_id: str | None = None


class WikiLinkRef(BaseModel):
    slug: str
    title: str
    scope: Literal["company", "personal"]


def derive_wiki_links(sources: list[WikiSourceRef]) -> list[WikiLinkRef]:
    """API citation projection; unrelated to the Postgres `wiki_links` table."""

    links: dict[str, WikiLinkRef] = {}
    for source in sorted(sources, key=lambda s: (-s.score, s.page_slug)):
        if source.page_slug in links:
            continue
        links[source.page_slug] = WikiLinkRef(
            slug=source.page_slug,
            title=source.title,
            scope=source.source_scope,
        )
    return list(links.values())


# "missing_link" (K6 PR3): the relevant company pages exist but are disconnected in
# the KG — a deterministic graph signal layered on insufficient_grounding (no LLM).
GapReason = Literal["no_data", "weak_retrieval", "insufficient_grounding", "missing_link"]


class WikiGap(BaseModel):
    """Deterministic signal that a wiki answer was poorly grounded (orthus/wiki/gap.py).

    Attached to every weak `WikiAnswer` so the FE can show a '데이터 부족' banner and
    a deterministic 'add data here' message — without any extra LLM call. The richer
    LLM field suggestion is produced on demand by the /gaps/feedback path."""

    detected: bool
    reason: GapReason
    missing_topic: str  # the question, echoed back to the asker
    top_score: float | None = None
    message: str  # deterministic guidance ('회사 Notion에 … 추가 후 sync')


class WikiAnswer(BaseModel):
    question: str
    answer: str
    sources: list[WikiSourceRef]
    wiki_links: list[WikiLinkRef] = Field(
        default_factory=list,
        description="API citation links derived from sources; not the DB wiki_links table.",
    )
    warnings: list[str] = Field(default_factory=list)
    gap: WikiGap | None = None


# --- 비서 end-to-end result -----------------------------------------------------


class GroundingRef(BaseModel):
    kind: str  # 'wiki_chunk' | 'catalog_table' | 'catalog_column'
    ref: str


class AssistantResult(BaseModel):
    query_id: UUID
    question: str
    source_id: UUID | None = None  # None for the structured (PG JSONB store) backend
    compiled: CompiledQuery | None
    validation: ValidationResult
    status: str  # compiled | validated | rejected | executed | failed
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int | None = None
    latency_ms: int | None = None
    grounding: list[GroundingRef] = Field(default_factory=list)
    message: str | None = None  # human guidance, esp. on reject/fail


class AssistantResultPart(BaseModel):
    source_scope: Literal["company", "personal"]
    source_node_id: str | None = None
    result: AssistantResult


# --- P6 unified mail ----------------------------------------------------------


MailBackendName = Literal["nova", "acme", "gmail"]
MailScope = Literal["company", "personal"]
MailDirection = Literal["inbound", "outbound"]


class EmailAttachmentRef(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    # Provider-relative path or key used to fetch the bytes (never an absolute
    # external URL the browser hits directly — orthus proxies it with creds).
    ref: str | None = None
    # Provider attachment id (the download proxy's stable handle).
    att_id: str | None = None
    # Set for inline parts (cid: images embedded in the HTML body). The unified
    # FE hides these from the attachment list so a mail with 1 real file does not
    # show "5 attachments" full of UUID-named tracking pixels.
    content_id: str | None = None
    inline: bool = False


class CanonicalEmail(BaseModel):
    backend: MailBackendName
    account_id: UUID | None = None
    external_id: str
    message_id: str | None = None
    direction: MailDirection = "inbound"
    scope: MailScope
    owner_addr: str | None = None
    from_addr: str
    to_addr: list[str] = Field(default_factory=list)
    cc_addr: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    read: bool = False
    starred: bool = False
    # inbound only: whether we've already sent a reply (acme native; nova
    # has no list-level replied flag so it stays False there).
    replied: bool = False
    # Saved AI reply-draft text on an inbound row (acme ai-draft feature).
    # Empty = no draft. Surfaces the "AI 초안" badge/folder in the FE.
    ai_draft: str = ""
    label: str = ""
    trashed: bool = False
    sent_at: datetime | None = None
    received_at: datetime | None = None
    attachment_count: int = 0
    attachments: list[EmailAttachmentRef] = Field(default_factory=list)


class MailBackendStatus(BaseModel):
    backend: MailBackendName
    account_id: UUID | None = None
    configured: bool
    ok: bool
    total: int = 0
    unread: int = 0
    error: str | None = None
    # P6.3: whether this backend has the send credentials configured so the FE
    # can gate the compose-from selector, plus the configured from-owner address
    # (already-exposed identity, same as CanonicalEmail.owner_addr).
    send_configured: bool = False
    owner_addr: str | None = None


class MailInboxResponse(BaseModel):
    items: list[CanonicalEmail]
    total: int
    unread: int
    backends: list[MailBackendStatus]
    # P6.3 manual compose/send kill switch. False keeps the compose UI hidden.
    send_enabled: bool = False


class MailPullBackendResult(BaseModel):
    backend: str
    enabled: bool = False
    listed: int = 0
    ingested: int = 0
    skipped: int = 0
    errors: int = 0


class MailPullResponse(BaseModel):
    """Result of an on-demand company-mail pull-ingest ('새로고침' trigger)."""

    enabled: bool = False
    ingested: int = 0
    backends: list[MailPullBackendResult] = Field(default_factory=list)


class MailNotifyState(BaseModel):
    """Cheap local signal for desktop new-mail notifications.

    Backed by a count/max over already-ingested mail documents visible to the
    caller — no external provider fan-out — so the desktop app can poll it
    frequently. The client remembers the last-seen `latest_at`/`total` and fires
    a native notification when `latest_at` advances.
    """

    total: int = 0
    latest_at: datetime | None = None
    latest_title: str | None = None


class MailIngestRequest(BaseModel):
    backend: MailBackendName
    owner_addr: str
    external_id: str
    message_id: str | None = None
    direction: MailDirection = "inbound"
    from_addr: str
    to_addr: list[str] = Field(default_factory=list)
    cc_addr: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    reply_to_external_id: str | None = None
    attachments: list[EmailAttachmentRef] = Field(default_factory=list)
    sent_at: datetime | None = None


class MailIngestResult(BaseModel):
    work_id: UUID | None = None
    doc_id: UUID | None = None
    ingested: bool
    scope: MailScope
    source_canonical_id: str


# Send is routed from the from_addr domain to a company mail backend; gmail send
# stays out of scope (P6.4+).
MailSendBackend = Literal["nova", "acme"]


def _validate_email(candidate: str) -> str:
    if "@" not in candidate or candidate.startswith("@") or candidate.endswith("@"):
        raise ValueError("invalid email address")
    local, _, domain = candidate.partition("@")
    if not local or "." not in domain or " " in candidate:
        raise ValueError("invalid email address")
    return candidate


class MailSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_addr: str
    to: str
    subject: str
    text: str
    html: str | None = None
    # 참조/숨은참조 — 콤마로 구분한 주소 목록(빈 값/None = 없음). reply_to_id는
    # 답장(Re) 스레딩용 원본 메일의 provider message id다. nova 컨트랙트는 cc/bcc/
    # reply_to_id를 지원하고, 값이 있을 때만 전송 바디에 실린다(기존 발송 무변경).
    cc: str | None = None
    bcc: str | None = None
    reply_to_id: str | None = None
    # 열람 추적 픽셀 주입 여부(서버 플래그가 켜진 경우에 한해). 기본 True라 추적이
    # 켜진 노드에서는 기본 동작이고, 특정 발송만 끄려면 False로 보낸다(per-message opt-out).
    track: bool = True

    @field_validator("from_addr")
    @classmethod
    def _basic_email(cls, value: str) -> str:
        return _validate_email(value.strip())

    @field_validator("to")
    @classmethod
    def _to_email_list(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("invalid email address")
        for part in parts:
            _validate_email(part)
        return ", ".join(parts)

    @field_validator("cc", "bcc")
    @classmethod
    def _email_list(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        parts = [part.strip() for part in value.split(",") if part.strip()]
        for part in parts:
            _validate_email(part)
        return ", ".join(parts)


class MailSendResult(BaseModel):
    backend: MailSendBackend
    status: Literal["sent", "failed"]
    provider_message_id: str | None = None
    error: str | None = None
    # 열람 추적이 켜져 픽셀이 주입된 발송이면 그 token을 돌려준다(클라이언트가
    # `GET /mail/track/status/{token}`로 읽음 현황을 폴링). 추적 off면 None.
    tracking_token: str | None = None


class MailTrackingStatus(BaseModel):
    """보낸 메일 한 건의 열람(읽음) 현황. owner 세션이 자기 token만 조회한다."""

    model_config = ConfigDict(extra="forbid")

    token: str
    opened: bool = False
    open_count: int = 0
    first_opened_at: str | None = None
    last_opened_at: str | None = None


class MailComposeDraft(BaseModel):
    """Inline agentic /ask compose output: a NON-sent mail draft the operator
    reviews and sends with one click (POST /mail/send). The agentic loop never
    sends — the operator's send click IS the owner approval (absolute rule: no
    LLM-only execution). `from_addr` is resolved server-side to a mailbox the
    owner owns (the model never picks the sender identity); to/subject/body come
    from the model. Populated only on the in-process Solar engine."""

    from_addr: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    reply_to_contact: str | None = None


class MailSignatureLink(BaseModel):
    """메일 명함 외부/소셜 링크 한 개 (예: LinkedIn)."""

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    url: str = ""

    @field_validator("label", "url")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()[:200]

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("링크 주소는 http:// 또는 https:// 로 시작해야 합니다")
        return value


class MailSignatureInput(BaseModel):
    """메일 명함 저장 입력 (PUT /mail/signature). 구조화 필드만 받고 HTML 렌더는
    FE가 한다 — 서버에 임의 HTML을 저장하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    # 발신 메일함별 명함. 빈 문자열은 legacy/default 명함을 뜻한다.
    from_addr: str = ""
    display_name: str = ""
    title: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    links: list[MailSignatureLink] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("from_addr", "display_name", "title", "company", "email", "phone", "website")
    @classmethod
    def _trim_fields(cls, value: str) -> str:
        return value.strip()[:200]

    @field_validator("website")
    @classmethod
    def _website_http(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("웹사이트 주소는 http:// 또는 https:// 로 시작해야 합니다")
        return value

    @field_validator("links")
    @classmethod
    def _cap_links(cls, value: list[MailSignatureLink]) -> list[MailSignatureLink]:
        # 빈 링크(라벨·URL 모두 공백) 제거 후 최대 8개로 제한.
        cleaned = [link for link in value if link.label or link.url]
        if len(cleaned) > 8:
            raise ValueError("링크는 최대 8개까지 추가할 수 있습니다")
        return cleaned


class MailSignature(MailSignatureInput):
    """메일 명함 조회 응답 (GET /mail/signature). 저장된 값이 없으면 빈 기본값을
    돌려준다 — 모든 필드가 비어 있으면 아직 명함이 설정되지 않은 상태."""


class MailDraftRequest(BaseModel):
    """AI compose/reply draft generation input. The LLM writes the draft text
    only; it never sends. The human still clicks send through the unchanged P6.3
    `/mail/send` manual gate."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["compose", "reply"] = "compose"
    # Natural-language instruction ("정중하게 거절", "미팅 일정 제안" ...).
    instruction: str = ""
    to: str = ""
    # Existing subject (reply: the original subject) the draft should respect.
    subject: str = ""
    # Thread/original-mail excerpt for grounding a reply.
    context: str = ""


class WikiGroundingRef(BaseModel):
    """메일 초안이 근거로 삼은 compiled wiki page 1건 (`orthus.mail.grounding`)."""

    model_config = ConfigDict(extra="forbid")

    page_slug: str
    title: str
    excerpt: str = ""
    score: float = 0.0


class WikiMailGrounding(BaseModel):
    """받은 메일 내용으로 회사 위키를 조회해 얻은 근거 묶음.

    `/ask`와 동일한 retrieve → answer_from_hits 경로의 산출물이라, 같은 질문이면
    Assistant 화면과 메일 초안이 같은 근거를 댄다.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    answer: str
    sources: list[WikiGroundingRef] = Field(default_factory=list)
    # 지식그래프가 이 근거에 미해결 모순(CONFLICTS_WITH)이 걸려 있다고 알려준 경우의
    # 경고. 채워져 있으면 초안은 그 값을 확정 표현으로 쓰지 않는다. 모순은 두 주장 사이의
    # 관계라 위키 top-k 검색만으로는 절대 드러나지 않는다.
    conflict_warning: str | None = None


class MailDraftResult(BaseModel):
    subject: str
    body: str
    # 초안이 실제로 반영한 위키 근거. 비어 있으면 ungrounded 초안이다(flag off이거나
    # 관련 위키 페이지를 못 찾은 경우) — 화면이 근거 칩을 숨기는 판단에 쓴다.
    wiki_grounding: WikiMailGrounding | None = None


class MailDraftDispatchRequest(BaseModel):
    """On-demand local-agent draft request: route the "AI 초안" button through the
    requesting user's OWN collector daemon (their local claude/hermes) instead of
    the central LLM. Wraps the same fields as MailDraftRequest plus an optional
    per-dispatch runner override and an optional device pin. See
    `docs/mail-draft-local-agent.md`."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["compose", "reply"] = "compose"
    instruction: str = ""
    to: str = ""
    subject: str = ""
    context: str = ""
    # Per-dispatch runner override; falls back to the configured default (claude).
    # codex is deliberately excluded for mail drafting (plain-text, code-oriented).
    runner: Literal["claude", "hermes"] | None = None
    # Optional device pin to target one of the requester's enrolled daemons.
    device_id: str | None = None

    def to_draft_request(self) -> "MailDraftRequest":
        return MailDraftRequest(
            mode=self.mode,
            instruction=self.instruction,
            to=self.to,
            subject=self.subject,
            context=self.context,
        )


class MailDraftDispatchResult(BaseModel):
    """Result of POST /mail/draft/dispatch.

    - mode="local": dispatched to the requester's local daemon; poll the work item
      (`work_id`) and/or open the SSE stream for live output. `draft` is None until
      the daemon completes (FE reads payload.mail_draft via the work item).
    - mode="central": no enrolled daemon but ORTHUS_MAIL_DRAFT_CENTRAL_FALLBACK is on,
      so a synchronous central draft is returned inline in `draft`.
    - mode="unavailable": no enrolled daemon and no central fallback; `message`
      explains how to enrol a local daemon."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["local", "central", "unavailable"]
    work_id: UUID | None = None
    stream: bool = False
    draft: MailDraftResult | None = None
    runner: str | None = None
    message: str | None = None


class MailFlagUpdate(BaseModel):
    """Read/star/label mutation for one company-mail message (slice 2 triage)."""

    model_config = ConfigDict(extra="forbid")

    read: bool | None = None
    starred: bool | None = None
    label: str | None = None


class MailMutationResult(BaseModel):
    ok: bool


# --- personal mail inbox (P6.1b read-only Gmail projection) ---------------------


class PersonalMailItem(BaseModel):
    doc_id: UUID
    title: str
    subject: str
    sent_at: datetime | None
    external_id: str | None
    snippet: str


class PersonalMailResponse(BaseModel):
    items: list[PersonalMailItem]
    total: int


class PersonalMailDetail(BaseModel):
    doc_id: UUID
    title: str
    subject: str
    sent_at: datetime | None
    body: str


class AgentWorkIntakeResult(BaseModel):
    work_id: UUID
    source_kind: str
    source_ref_id: str
    action_family: str
    title: str
    state: str
    policy_outcome: str | None = None
    policy_reason: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    message: str
    # S4 — homogenize the case2 (single-command) intake result with the case4 decomposed
    # SubAnswer chip so the FE renders one ChipRenderer for both. Optional/back-compat.
    recipient: str | None = None  # email_send delivery recipient (already stored plain, P3.4c)
    question_text: str | None = None  # redacted command text (payload.question)


# --- router (auto-route NL question to a backend) -------------------------------


# --- decompose: compound question split + parallel fan-out (docs/company-agent-orchestration.md) ---


class SubQuestion(BaseModel):
    """One independent sub-question produced by split_question()."""

    id: UUID
    text: str
    scope: str  # inherited from parent effective scope
    depends_on: list[UUID] = Field(default_factory=list)  # reserved — Phase 2 위상 정렬 예정


class SynthesizedBody(BaseModel):
    """LLM-synthesized answer over grounded sub-answers only."""

    answer: str
    sources: list[WikiSourceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SubAnswer(BaseModel):
    """Result of one sub-question leaf execution."""

    sub_question_id: UUID
    routed: "RoutedAnswer | None" = None  # None on leaf failure
    grounded: bool = False
    gap: WikiGap | None = None
    agent_work_id: UUID | None = None
    error: str | None = None  # "timeout" | "leaf_error" | "upstream_unavailable" | None
    # Phase 2: True when upstream depends_on context was injected into this leaf (§P2.4).
    context_injected: bool = False
    # Phase 2 envelope enrichment (MA.6c) — the sub-question text and the positional
    # indices (into `parts`) this leaf depends on, so the FE can render read→act
    # relationships without re-deriving them. Populated at envelope-build time.
    text: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    # S4 command-split chip enrichment — back-injected from the queued AgentWork item so the
    # FE renders an action chip without a second fetch. All optional: an item-less action leaf
    # (queue suppressed / returned None) leaves these unset and draws no chip.
    # BADGE SoR is `state` (the execution truth: a command_split-held item is
    # request_more_data). `policy_outcome` is kept for reference/tooltip only — it may read
    # `auto_execute` on a held item (the gate's verdict), which is NOT the display state until
    # the hold-representation follow-up (docs/command-split-hold-followup.md #1).
    state: str | None = None
    policy_outcome: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    title: str | None = None
    recipient: str | None = None  # email_send delivery recipient (already stored plain, P3.4c)


class CoordinateResult(BaseModel):
    """Phase 1: always done=True (no-op). Phase 2 (ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED)
    drives the wave loop: done=False with next_wave = sub-question ids whose deps are
    all satisfied (§P2.5). flag off keeps done=True (불변식 12/21 byte-identical)."""

    done: bool = True
    next_wave: list[UUID] = Field(default_factory=list)  # Phase 2 — 다음 wave 후보


class RoutedAnswer(BaseModel):
    """Unified envelope returned by the auto-router (P2.2b). For 'structured'/'wiki'/
    'agent_work' exactly one of `structured`/`wiki`/`agent_work` is populated. For the
    K4b 'graph' mode BOTH `wiki` (the compiled-page grounded body) and `graph` (path/
    relation metadata) are populated — the answer body always stays compiled-wiki-page
    grounded (불변식 5). For 'decomposed' mode `synthesized_body` and `parts` carry
    the multi-part result (docs/company-agent-orchestration.md §10)."""

    question: str
    mode: str  # 'structured' | 'wiki' | 'agent_work' | 'graph' | 'decomposed' | 'user_input'
    wiki: WikiAnswer | None = None
    structured: AssistantResult | None = None
    agent_work: AgentWorkIntakeResult | None = None
    graph: "KgGraphAnswer | None" = None  # K4b — populated alongside `wiki` when mode=='graph'
    # K7.4 — owner-scope two-framing(직접 연결 vs 내 메모 경유). graph-success owner path
    # 답변에만 세팅된다(demote는 wiki 반환이므로 None). 공유 KgGraphAnswer에 중첩하지 않고
    # 별도 top-level로 둬, byte-shared K4/K5 neighbors-only shape가 /ask 전용 개념을 타입
    # 차원에서 못 싣게 한다(L2). owner-scope OFF/1-slug neighbors/non-owner는 None.
    path_framings: "KgPathFramings | None" = None
    structured_parts: list[AssistantResultPart] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Orchestrator (agent-work chat) only: the caller-supplied id for the live SSE
    # feedback stream (GET /agent-work/chats/stream/{stream_id}). Echoed back so the FE
    # can correlate; None for the search-only /ask path and non-streamed answers.
    stream_id: str | None = None
    # Inline agentic /ask only (in-process Solar engine): a NON-sent mail draft produced by
    # the mail_compose tool. The FE renders it as an editable card with a 보내기
    # button that POSTs to /mail/send (the operator click = owner approval). None
    # for every other path.
    mail_draft: MailComposeDraft | None = None
    # Decomposed mode only (mode == 'decomposed').
    synthesized_body: SynthesizedBody | None = None
    parts: list[SubAnswer] = Field(default_factory=list)
    # HITL (agent-work chat only): a turn-ending question to the chat owner. When
    # set, the orchestrator paused and needs an answer (POST .../hitl-response).
    # For an agentic question-only turn mode == 'user_input'; a gate-origin
    # question rides along on the existing mode ('agent_work'/'decomposed').
    # None on every non-HITL path and when ORTHUS_AGENT_HITL_ENABLED is off.
    user_input_request: "UserInputRequest | None" = None


# --- data-gap backlog (ask answer insufficiency) --------------------------------


class GapSuggestionSection(BaseModel):
    """One block of fields the data owner should add (LLM-generated, on demand)."""

    title: str
    items: list[str] = Field(default_factory=list)


class DataGap(BaseModel):
    """A persisted data-gap backlog row: a question the wiki could not ground, with
    an optional LLM suggestion of what to add where. Deduped per normalized question."""

    gap_id: UUID
    scope: Literal["company", "personal"]
    reason: GapReason
    question: str
    top_score: float | None = None
    suggested_target: str | None = None  # e.g. "회사 Notion '회사 개요' 페이지"
    suggested_connector: str | None = None  # e.g. "notion"
    context_wiki_slug: str | None = None
    suggested_fields: list[GapSuggestionSection] = Field(default_factory=list)
    suggestion_status: str  # 'pending' | 'ready'
    hit_count: int
    status: str  # 'open' | 'resolved' | 'dismissed'
    source: str  # 'auto' | 'feedback'
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime


class DataGapPageResponse(BaseModel):
    count: int
    items: list[DataGap] = Field(default_factory=list)


# --- agent work loop (P3.1a substrate) ------------------------------------------


PolicyOutcome = Literal[
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "reject",
]

AgentWorkState = Literal[
    "pending",
    "classified",
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "rejected",
    "resolved",
    "dismissed",
]

AgentWorkSourceKind = Literal[
    "data_gap",
    "wiki_task",
    "promote_staging",
    "connector_run",
    "assistant_command",
    "schedule_tick",
    "event_orchestration",
]

AgentWorkActionFamily = Literal[
    "connector_sync",
    "document_draft",
    "email_send",
    "personal_board_cleanup",
    "central_wiki_task_cleanup",
    "data_request",
    "promote_review",
    "event_orchestration",
]


class AgentPolicyMemoryContext(BaseModel):
    bucket_key: str
    total: int = 0
    approvals: int = 0
    dismissals: int = 0
    request_more_data: int = 0
    note_present_count: int = 0
    no_edit_approvals: int = 0
    recent_window_days: int = 60
    recent_total: int = 0
    recent_no_edit_approvals: int = 0
    recent_no_edit_approval_rate: float = 0.0
    email_auto_send_observation_threshold_met: bool = False
    last_observed_at: datetime | None = None
    used_for_outcome: bool = False


class AgentWorkCandidate(BaseModel):
    """Untrusted work candidate before deterministic policy classification.

    LLMs may help draft payload/evidence in later phases, but policy outcome is
    derived only by deterministic code over these slots."""

    source_kind: AgentWorkSourceKind | str
    source_ref_id: str
    action_family: AgentWorkActionFamily | str
    title: str
    payload: dict = Field(default_factory=dict)
    evidence: list[dict] = Field(default_factory=list)
    policy_memory: AgentPolicyMemoryContext | None = None


class AgentWorkDecision(BaseModel):
    outcome: PolicyOutcome
    state: AgentWorkState
    policy_reason: str
    reason_codes: list[str] = Field(default_factory=list)
    required_review_role: str | None = None
    required_data: list[str] = Field(default_factory=list)
    policy_memory: AgentPolicyMemoryContext | None = None


class EmailDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_hint: str
    subject_hint: str
    body_template: str
    intent: Literal["draft_for_review"] = "draft_for_review"
    source: Literal["assistant_command_template"] = "assistant_command_template"
    status: Literal["draft"] = "draft"
    llm_drafted: bool = False
    used_for_outcome: Literal[False] = False


# --- P7.1 mail reply draft + P7.5 auto-send envelope (fail-closed) ---------------


class MailReplyContext(BaseModel):
    """Routing + grounding context for an inbound mail that may get a reply draft.
    `from_addr` is the company address that received the mail (the reply From);
    `to_addr` is the original sender (the reply recipient)."""

    model_config = ConfigDict(extra="forbid")

    backend: MailSendBackend
    from_addr: str
    to_addr: str
    in_reply_to_message_id: str | None = None
    in_reply_to_external_id: str | None = None
    subject: str
    thread_excerpt: str = ""


class ReplyDraftPayload(BaseModel):
    """`payload.email_draft` for a reply-draft Agent Work item. Reuses the same
    payload slot the gate already inspects so the email_send family classifies to
    draft_for_review when a recipient is present."""

    model_config = ConfigDict(extra="forbid")

    recipient_hint: str
    subject_hint: str
    body_template: str
    reply_context: MailReplyContext
    intent: Literal["draft_for_review"] = "draft_for_review"
    source: Literal["agent_reply_draft"] = "agent_reply_draft"
    status: Literal["draft"] = "draft"
    llm_drafted: bool = False
    used_for_outcome: Literal[False] = False


class MailAutoSendBucket(BaseModel):
    """P7.5 D17 typed auto-send policy bucket. v1 is strict per-recipient scope;
    a domain-wide scope would be a separate opt-in."""

    model_config = ConfigDict(extra="forbid")

    from_account_hash: str
    backend: MailSendBackend
    template_key: str
    recipient_scope: Literal["recipient_hash"] = "recipient_hash"
    thread_kind: str


class MailAutoSendCandidacy(BaseModel):
    """Deterministic P7.5 auto-send candidacy evidence. Fail-closed: this slice has
    no opt-in store and no code path that actually auto-sends, so `eligible` is
    always False and `used_for_outcome` is always False."""

    eligible: bool = False
    kill_switch_enabled: bool = False
    bucket_opted_in: bool = False
    threshold_met: bool = False
    guards_passed: bool = False
    reasons: list[str] = Field(default_factory=list)
    used_for_outcome: Literal[False] = False


class AgentWorkItem(BaseModel):
    work_id: UUID
    node_id: str
    node_kind: Literal["company", "personal"]
    owner_id: UUID | None = None
    source_kind: str
    source_ref_id: str
    action_family: str
    title: str
    payload: dict = Field(default_factory=dict)
    state: AgentWorkState
    policy_outcome: PolicyOutcome | None = None
    policy_reason: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    correlation_id: UUID | None = None
    last_run_id: UUID | None = None
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime
    wiki_slugs: list[str] = Field(default_factory=list)


class ReplyDraftResult(BaseModel):
    """MA.3 outcome of the create_reply_draft decompose tool.

    - outcome="draft_for_review": a reply candidate was built + persisted; `item`
      is the queued AgentWork draft (state draft_for_review). No send happens here.
    - outcome="request_more_data": reply_context was missing or could not produce a
      routable reply; `item` is None. `reason` carries the precise diagnostic, and
      `required_data` lists only data the CALLER can supply to proceed (so a flag-off
      or non-routable cause yields an empty list — nothing the caller provides fixes
      it). The action leaf maps this to a SubAnswer (no auto-send — 불변식 9).
    """

    outcome: Literal["draft_for_review", "request_more_data"]
    item: AgentWorkItem | None = None
    required_data: list[str] = Field(default_factory=list)
    reason: str | None = None


class AgentWorkSyncResult(BaseModel):
    source_kind: str
    created_or_updated: int
    items: list[AgentWorkItem] = Field(default_factory=list)


class AgentWorkWikiLinkResponse(BaseModel):
    count: int
    items: list[AgentWorkItem] = Field(default_factory=list)


class AgentWorkInboxWikiTaskItem(BaseModel):
    slug: str
    kind: str
    title: str
    created_at: datetime
    href: str


class AgentWorkInboxPromotionItem(BaseModel):
    stage_id: UUID
    source_title: str
    status: str
    created_at: datetime | None = None
    href: str


class AgentWorkInboxDataGapItem(BaseModel):
    gap_id: UUID
    missing_topic: str
    hit_count: int
    last_seen_at: datetime
    href: str


class AgentWorkInboxAgentWorkItem(BaseModel):
    work_id: UUID
    title: str
    source_kind: str
    action_family: str
    state: str
    policy_outcome: PolicyOutcome | None = None
    updated_at: datetime
    wiki_slugs: list[str] = Field(default_factory=list)
    href: str


class AgentWorkInboxWikiTaskBucket(BaseModel):
    count: int
    count_capped: bool = False
    items: list[AgentWorkInboxWikiTaskItem] = Field(default_factory=list)


class AgentWorkInboxPromotionBucket(BaseModel):
    count: int
    supported: bool = True
    count_capped: bool = False
    items: list[AgentWorkInboxPromotionItem] = Field(default_factory=list)


class AgentWorkInboxDataGapBucket(BaseModel):
    count: int
    count_capped: bool = False
    items: list[AgentWorkInboxDataGapItem] = Field(default_factory=list)


class AgentWorkInboxAgentWorkBucket(BaseModel):
    count: int
    count_capped: bool = False
    by_outcome: dict[str, int] = Field(default_factory=dict)
    items: list[AgentWorkInboxAgentWorkItem] = Field(default_factory=list)


class AgentWorkInboxBuckets(BaseModel):
    wiki_tasks: AgentWorkInboxWikiTaskBucket
    promote_staging: AgentWorkInboxPromotionBucket
    data_gaps: AgentWorkInboxDataGapBucket
    agent_work: AgentWorkInboxAgentWorkBucket


class AgentWorkInboxSummary(BaseModel):
    generated_at: datetime
    node_id: str
    node_kind: Literal["company", "personal"]
    buckets: AgentWorkInboxBuckets


# --- Owner-scope notification read surface (PR-N1) ---------------------------
# Read-only poll signals mirroring the MailNotifyState precedent: a cheap
# count/max the web/desktop client polls to decide whether to raise a native
# notification. No new tables, no writes, no policy outcome changes.


class PersonalBoardNotifyState(BaseModel):
    """Cheap local signal for board-ticket notifications.

    Count/max over the caller's own personal-board tasks — calendar-materialized
    auto-rows (`source_kind='calendar'`) excluded so recurring schedule mirrors
    never fire notifications. Zero-state when the caller has no workspace yet.
    Same field shape as `MailNotifyState`.
    """

    total: int = 0
    latest_at: datetime | None = None
    latest_title: str | None = None


class PersonalBoardNotificationItem(BaseModel):
    """One recent board ticket for the notification list (read-only projection)."""

    task_id: UUID
    title: str
    created_at: datetime | None = None
    source_kind: str
    source_label: str | None = None
    status: str
    backlog_bucket_id: UUID | None = None
    scheduled_date: date | None = None


class PersonalBoardNotificationList(BaseModel):
    """Recent non-calendar board tickets, newest first.

    `total` matches `PersonalBoardNotifyState.total` (the full matching count,
    not `len(items)`).
    """

    items: list[PersonalBoardNotificationItem] = Field(default_factory=list)
    total: int = 0


class AgentWorkNotifyState(BaseModel):
    """Server-side mirror of the FE checklist badge (AppShell isChecklistBadgeItem).

    Counts open agent-work items excluding `wiki_task` source rows and
    `agent_task` (chat delegation) family rows — exactly what the 체크리스트
    badge shows. Owner-scoped fail-closed. Same field shape as `MailNotifyState`.
    """

    total: int = 0
    latest_at: datetime | None = None
    latest_title: str | None = None


AgentWorkReviewAction = Literal[
    "approve",
    "dismiss",
    "request_more_data",
]


class AgentWorkReviewDecisionRequest(BaseModel):
    decision: AgentWorkReviewAction
    note: str | None = None
    no_edit: bool | None = None


class AgentWorkDraftEditRequest(BaseModel):
    """Reviewer edit of a draft_for_review email_send draft before approval."""

    body_template: str
    subject_hint: str | None = None


class AgentWorkReviewDecision(BaseModel):
    decision_id: UUID
    work_id: UUID
    node_id: str
    reviewer_id: UUID
    decision: AgentWorkReviewAction
    note: str | None = None
    from_state: AgentWorkState
    to_state: AgentWorkState
    correlation_id: UUID | None = None
    node_run_id: UUID | None = None
    decided_at: datetime


class AgentWorkReviewDecisionResult(BaseModel):
    item: AgentWorkItem
    decision: AgentWorkReviewDecision


# --- Agent-chat conversation sessions (Slice A, storage only) -------------------
# DB-backed, restart-safe, strictly owner-scoped Agent-chat threads. Pure
# storage: no policy gate, no LLM calls, no central write paths.


class UserInputOption(BaseModel):
    """One selectable answer for a choice/approval HITL question. `value` is what
    the resume turn receives; `label` is the display text (falls back to value)."""

    value: str
    label: str | None = None


class UserInputField(BaseModel):
    """One labeled blank in a fill-in-the-blank HITL form (input_type="text").
    `name` keys the submitted value; `label` is the display text."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    placeholder: str | None = None
    required: bool = True


class UserInputRequest(BaseModel):
    """A structured turn-ending question to the chat owner (Human-in-the-Loop).

    Persisted as an agent_chat_messages row (kind="user_input_request",
    payload=this model) and echoed on RoutedAnswer so the FE renders the question
    without a refetch. The user answers via POST /agent-work/chats/{id}/hitl-response,
    which starts a fresh resume turn carrying `resume`.

    origin="policy_gate": deterministically built from a gate request_more_data
    outcome (required_data) — no LLM. origin="agentic": raised by the Solar
    agentic loop's ask_user tool. Either way execution stays gated (설계 원칙 1)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str | None = None  # the chat message row carrying this question
    question: str
    input_type: Literal["text", "choice", "approval"] = "text"
    options: list[UserInputOption] = Field(default_factory=list)
    # Fill-in-the-blank form fields (후속 B). Empty ⇒ legacy free-text behavior
    # (the FE falls back to the composer hint). Only deterministic gate
    # projections populate this (email_send recipient in slice 1).
    fields: list[UserInputField] = Field(default_factory=list)
    work_id: str | None = None  # policy_gate origin: the blocked AgentWork item
    origin: Literal["policy_gate", "agentic"]
    status: Literal["waiting", "answered", "expired"] = "waiting"
    # Resume context persisted verbatim: {original_question, action_family?, scope?,
    # project?, context_wiki_slug?}. Consumed by the resume turn only.
    resume: dict = Field(default_factory=dict)


class AgentChatMessage(BaseModel):
    message_id: str
    role: str  # "user" | "agent"
    text: str
    work_id: str | None = None
    # HITL (migration 0101). "text" for every legacy/plain message;
    # "user_input_request" carries a UserInputRequest in `payload`;
    # "user_input_response" carries the user's answer. The FE renders any unknown
    # kind as plain text (forward-compatible fallback).
    kind: str = "text"
    payload: dict | None = None
    created_at: datetime


class AgentChatSession(BaseModel):
    session_id: str
    title: str
    message_count: int
    last_message_preview: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentChatSessionDetail(BaseModel):
    session: AgentChatSession
    messages: list[AgentChatMessage] = Field(default_factory=list)


class AgentChatSessionCreate(BaseModel):
    title: str | None = None


class AgentChatSessionUpdate(BaseModel):
    title: str


class AgentChatMessageCreate(BaseModel):
    role: str
    text: str
    work_id: str | None = None


class AgentChatWorkResultRequest(BaseModel):
    work_id: str


class AgentChatContinueRequest(BaseModel):
    text: str


class AgentChatHitlResponseRequest(BaseModel):
    """Body for POST /agent-work/chats/{id}/hitl-response — the chat owner's
    answer to a waiting user_input_request. `answer` is free text or a chosen
    option value; `decision` is set instead for input_type=="approval"."""

    message_id: str  # the waiting user_input_request message being answered
    answer: str | None = None
    decision: Literal["approve", "reject"] | None = None
    # Fill-in-the-blank submission: field name → value. Mutually exclusive with
    # `answer`; the server deterministically merges values into one answer text.
    values: dict[str, str] | None = None
    stream_id: str | None = None


class AgentChatHitlResponseResult(BaseModel):
    """Result of a HITL response: the resume turn's RoutedAnswer plus the
    server-appended user answer row (the FE appends messages itself elsewhere, so
    it needs the persisted row back)."""

    routed: RoutedAnswer
    response_message: AgentChatMessage


class AgentChatOrchestrateRequest(BaseModel):
    """Body for the agent-work chat orchestrator (POST /agent-work/chats/{id}/orchestrate).

    The orchestrator is where compound-question decompose + single-command intake live
    (moved off /ask, which is search-only). `stream_id` is a caller-generated uuid4 for
    the live SSE feedback stream (the FE opens GET /agent-work/chats/stream/{stream_id}
    BEFORE awaiting this POST). None disables streaming (no-op)."""

    text: str
    scope: str | None = None
    project: str | None = None
    context_wiki_slug: str | None = None
    context_mail_id: str | None = None
    stream_id: str | None = None


class ChatTurn(BaseModel):
    """One prior turn of a chat thread, reconstructed server-side from the
    owner-scoped session. Used ONLY as non-authoritative phrasing/reference
    context for the wiki branch — never as a grounding source (AGENTS.md §7)."""

    role: str
    text: str


class AgentPolicyBucketSummary(BaseModel):
    bucket_key: str
    node_id: str
    action_family: str
    source_kind: str
    total: int
    approvals: int
    dismissals: int
    request_more_data: int
    note_present_count: int
    no_edit_approvals: int
    recent_window_days: int = 60
    recent_total: int
    recent_no_edit_approvals: int
    recent_no_edit_approval_rate: float
    email_auto_send_observation_threshold_met: bool
    last_observed_at: datetime


class AgentPolicyWikiSummary(BaseModel):
    page_slug: str
    title: str
    scope: Literal["company", "personal"]
    owner_id: UUID | None = None
    project: str
    bucket_count: int
    observation_count: int
    updated_at: datetime


class AgentGatewayJobMirror(BaseModel):
    """P10 C-7 — 게이트웨이 잡 정의의 central 미러 row (조회 전용).

    데몬 로컬 SQLite가 실행 SoR이고 이 row는 owner-scoped 조회용 사본이다.
    seen-set 항목(민감 URL 가능)과 chat_id는 미러 대상이 아니다."""

    job_id: str
    name: str
    instruction: str
    schedule: dict
    schedule_human: str | None = None
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None
    mirrored_at: datetime


# --- promote gate ---------------------------------------------------------------


class PromotionPackage(BaseModel):
    """Explicit personal→central transfer artifact.

    Only sanitized fields leave the personal node; central still re-sanitizes
    before staging because package input is not trusted.
    """

    source_node_id: str
    source_doc_id: UUID
    source_owner_id: UUID | None = None
    source_scope: Literal["personal"] = "personal"
    source_title: str
    sanitized_title: str
    sanitized_markdown: str
    source_meta: dict = Field(default_factory=dict)
    target_project: str = "company"


class PromotionAuditEvent(BaseModel):
    audit_id: int
    node_run_id: UUID
    node: str
    phase: str
    status: Literal["ok", "error"]
    occurred_at: datetime
    meta: dict = Field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None


class PromotionStage(BaseModel):
    stage_id: UUID
    source_node_id: str
    source_doc_id: UUID
    source_owner_id: UUID | None = None
    source_scope: Literal["personal"]
    source_title: str
    sanitized_title: str
    sanitized_markdown: str
    source_meta: dict
    target_project: str
    status: Literal["pending", "approved", "rejected"]
    created_by: UUID
    approved_by: UUID | None = None
    promoted_doc_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    decided_at: datetime | None = None
    audit_events: list[PromotionAuditEvent] = Field(default_factory=list)


# --- LLM wiki (self-authoring grounding layer) ----------------------------------


class WikiSource(BaseModel):
    slug: str
    title: str
    source_type: Literal["corpus_doc", "qa_session", "conversation"]
    source_ref: str  # doc_id / query_id 등
    ingested_at: datetime
    summary: str
    key_concepts: list[str] = Field(default_factory=list)
    key_claims: list[str] = Field(default_factory=list)  # claim slug
    terminology: list[str] = Field(default_factory=list)
    related_pages: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence_notes: str | None = None


class WikiClaim(BaseModel):
    slug: str
    claim: str
    supporting: list[str] = Field(default_factory=list)  # source slug / corpus chunk ref
    conflicting: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    last_reviewed: date
    related_pages: list[str] = Field(default_factory=list)
    evidence: str
    notes: str | None = None
    # KG 그래프 노드 라벨용 사람이 읽는 짧은 헤드라인(≤~120자, 한 줄). LLM이 wiki 저작/
    # 백필 계층에서 생성해 실어 넘긴다(store/투영은 LLM 미호출). None이면 slug 폴백.
    display_title: str | None = None


class WikiPage(BaseModel):
    slug: str
    title: str
    definition: str
    overview: str
    relations: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    competing: list[str] = Field(default_factory=list)  # 경쟁 해석
    open_questions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    backlinks: list[str] = Field(default_factory=list)


class WikiTaskResolution(BaseModel):
    """conflict/open_question task resolve 결과 — frontmatter 영속화(WTR.1)."""

    decision: Literal["keep_existing", "use_incoming", "merge", "answered", "cleanup", "dismissed"]
    note: str | None = None
    resolved_by: UUID
    resolved_at: datetime
    # open_question answered 경로에서 author_from_qa가 생성한 claim slug 목록.
    produced_claim_slugs: list[str] = Field(default_factory=list)


class WikiTask(BaseModel):
    slug: str
    kind: Literal[
        "open_question", "conflict", "stale_audit", "dedup", "provenance_fix", "entity_conflict"
    ]
    description: str
    related: list[str] = Field(default_factory=list)
    created_at: datetime
    resolved: bool = False
    # conflict case (b): consolidate 시점에 skip된 incoming claim 텍스트.
    # use_incoming write-back을 위해 저장(description 파싱 금지).
    incoming_claim: str | None = None
    # Distilled source summary shown in the review UI so a reviewer has context
    # to answer the open_question; None for legacy/non-open_question tasks.
    source_excerpt: str | None = None
    # resolve 결과 메타. 기존 task 파일과의 하위호환을 위해 기본값 None.
    resolution: WikiTaskResolution | None = None


class ExtractedEntity(BaseModel):
    """distill이 추출한 entity 후보 (K6 — LLM 출력, 추출만/원칙 1).

    distill JSON `{"entities":[{"kind","name"}]}`의 항목. 정규화·dedup·충돌
    판정·관계 유도는 전부 결정론 코드(`orthus/kg/entities.py`)가 한다 — LLM은
    이름만 뽑는다. `name`은 원문 표기이며 persist 시점에 redact_pii + 정규화된다.
    """

    kind: Literal["person", "org", "project", "system"]
    name: str


class WikiTaskPageResponse(BaseModel):
    count: int
    items: list[WikiTask]


# --- K4 KG read path (docs/kg-model.md §4) ---------------------------------------


class KgGraphNode(BaseModel):
    """K4 graph 응답 노드 — 본문 없는 메타만(불변식 5: 본문은 page GET으로).

    `id`는 라벨 무관 고정 우선순위 `page_id → doc_id → row_id → entity_key →
    name → slug`로 추출한다(구현 명세 §6.1 결과 매핑 규약). placeholder 노드는
    `materialized=false` + `id=slug`로 포함한다 — dangling 링크 가시화는 wiki
    품질 신호다.

    **FE 키 규칙(K7.2 갱신):** `id`는 type-tagged가 아니다 — 실체 노드는 UUID(page_id 등),
    placeholder는 slug 문자열이라 한 namespace에 UUID-공간과 slug-공간이 섞인다. K7.2부터
    개인+회사 placeholder가 slug-id 공간을 공유할 수 있으므로(같은 slug, 다른 scope) 소비자는
    `(label, scope, id)`로 노드를 키해야 안전하다. company 노드는 종전과 동일하게 동작하므로
    이 key 변경은 additive·하위호환이다. v1 토폴로지에서 `:WikiPage`는 항상 page_id를 가져
    충돌이 없었다.

    **K7.2 owner-scope 필드:** `scope`('company'|'personal')와 `is_own_personal`(서버 파생
    = owner_id==caller)을 노출한다. `owner_id` UUID는 **절대 wire에 싣지 않는다**. 비-owner
    응답은 구조상 항상 `scope=='company'`, `is_own_personal==False`다(gate tripwire가 위반
    노드를 drop). FE가 개인/회사를 구분 렌더하는 단일 출처다(K7.3)."""

    id: str
    label: str  # "WikiPage" | "WikiClaim" | "WikiSource" | "Document" | "Entity" | ...
    slug: str | None = None
    title: str | None = None
    materialized: bool = True
    scope: Literal["company", "personal"] = "company"
    is_own_personal: bool = False
    # K9 — :Entity 노드의 page-only degree(projection mention_count). FE "≤3 표시 / 전체 N"
    # 분모(R1 표시 캡) + entity readout 신호용. 비-Entity 노드는 None(고정 화이트리스트 필드라
    # 임의 prop은 wire로 안 나간다 — `_map_records`가 Entity 노드에서만 매핑). additive·하위호환.
    mention_count: int | None = None
    # K9 — :Entity 노드의 종류(person|org|project|system). 티저 person 억제(D-TZ B)와 FE readout
    # 차등 렌더(U4)의 단일 출처. Entity 노드의 display_name은 `title`로 매핑된다(_map_records).
    entity_kind: str | None = None


class KgGraphEdge(BaseModel):
    """K4 graph 응답 엣지. `properties`는 **sparse**다 — Neo4j는 null 속성을
    저장하지 않으므로(projection이 None으로 쓴 키는 그래프에 존재하지 않는다),
    값이 없는 키는 응답에서 **생략**된다(null로 나오지 않는다). 예: 매칭 task가
    없는 `CONFLICTS_WITH`는 `{"status": "UNRESOLVED"}`만 담고 `detected_at`/
    `conflict_reason`/`resolved_favoring_id`는 키 자체가 없다. 소비자는 반드시
    `properties.get(k)`로 읽어야 한다(kg-model §2의 "항상 null"은 논리적 의미이며
    wire format에서는 키 부재로 표현된다)."""

    src: str
    dst: str
    rel: str
    # K7.2: 엣지 scope = 양 끝점 중 더 제한적인 쪽(personal 우선, B3). owner_id는 wire에
    # 싣지 않는다(서버 측 tripwire/파생 전용). properties에서 scope/owner_id는 제거되고
    # scope는 이 typed 필드로만 노출된다.
    scope: Literal["company", "personal"] = "company"
    properties: dict[str, Any] = Field(default_factory=dict)


class EntityTeaser(BaseModel):
    """K9.2 (C-C/U2/D-TZ B) — 패널 펼침 전 entity-티저 데이터. presence-gated(엔티티 전역 존재
    + 이 페이지에 다리가 있을 때만 non-None). person 이름은 **자동 티저에서 억제**한다
    (person-forward 기본 뷰 회피, §0 비목표) — 비-person 최고신호 엔티티 display name만 노출하고,
    person 다리는 펼친 뒤에만 보인다. 비-person 다리가 없으면(person만) `entity_names=[]` +
    `person_only=True`로 page 수만 표기한다(옵션 A fallback, 안전쪽 실패)."""

    page_count: int  # 이 페이지와 공유 개체로 엮인 다른 페이지 수
    entity_names: list[str] = Field(
        default_factory=list
    )  # 비-person 최고신호 엔티티명(person 억제)
    person_only: bool = False  # 비-person 다리 없음 → entity_names 비고 page_count만(옵션 A)


class WikiPageGraphResponse(BaseModel):
    """`GET /wiki/pages/{slug:path}/graph` 응답 (kg-model §4 계약).

    KG off/미가용은 200 + `supported=false`다(P4 unsupported-state 패턴 —
    fail-open). `reason`은 계약상 2종뿐이며 timeout/error도 `kg_unavailable`로
    수렴한다(세부 사유는 `kg_query_runs.status`)."""

    slug: str
    supported: bool
    reason: str | None = None  # "kg_disabled" | "kg_unavailable"
    truncated: bool = False
    nodes: list[KgGraphNode] = Field(default_factory=list)
    edges: list[KgGraphEdge] = Field(default_factory=list)
    # K9.2 (C-C/U2) — entity 다리 티저. presence-gated이며 이 페이지에 다리가 있을 때만 non-None.
    # `include=entity` 펼침 전 기본 응답에도 실린다(FE가 티저 한 줄 자동 노출 → 클릭 시 펼침).
    # entity 노드/엣지는 `include=entity`일 때만 nodes/edges에 합류한다(기본 패널 무변, U2).
    entity_teaser: EntityTeaser | None = None
    # K7.3 honesty 신호(D2 — personal note의 entity 기반 연결은 미구현). owner-scope가
    # 켜진 caller 응답에서 True이며, FE가 state 3("이 주제 언급 개인 메모는 있으나 미연결,
    # 엔티티 연결 준비 중") 카피를 정직하게 렌더하는 단일 출처다. personal-entity 슬라이스가
    # 들어오면 서버가 False로 뒤집는다(미래 가법). flag-off/비-owner는 False.
    personal_entity_search_deferred: bool = False


class KgRelationResponse(BaseModel):
    """`POST /wiki/kg/query` 응답 — orthus-mcp `kg_relations`/`entity_relations` 툴이
    소비한다. `WikiPageGraphResponse`와 동형(supported/reason/truncated/nodes/edges)이되
    단일 페이지 그래프 패널이 아니라 일반 관계 질의용이다(relation enum + name 기반 포함).

    `supported`는 KG 가용성이다 — OK거나 입력/slug reject면 True(KG는 살아 있고 결과만
    비거나 입력이 나쁨), Neo4j down/timeout/driver 오류면 False(fail-open). `reason`은
    게이트 reject_reason을 그대로 싣는다(LLM 가독 — slug_not_found:<slug> 등)."""

    supported: bool = True
    relation: str | None = None
    reason: str | None = None
    truncated: bool = False
    nodes: list[KgGraphNode] = Field(default_factory=list)
    edges: list[KgGraphEdge] = Field(default_factory=list)


class OwnerFootprint(BaseModel):
    """`GET /wiki/kg/footprint` 응답 — caller **본인**의 personal 노드/엣지가 공유 central
    그래프에 얼마나 올라가 있는지(K7.3, kg-k7.3-plan §4.1). owner에게 "내 것 중 무엇이
    공유 그래프로 넘어갔고 누가 볼 수 있나(나만)"의 legible 답을 준다(monitor가 operator에게
    주는 것과 같은 가시성).

    술어는 read 술어(`company OR owner=$caller`)가 아니라 **`scope='personal' AND
    owner_id=$caller`**다 — footprint는 "내 personal footprint"라 company는 무관하다.
    `owner_id` UUID는 응답에 **절대 싣지 않는다**(카운트만). null caller(B-null-caller)는
    빈 footprint(company에는 owner footprint가 없다)."""

    node_count: int = 0
    edge_count: int = 0
    # 라벨별 노드 수(WikiPage/WikiClaim/WikiSource/Document/StructuredFact). personal-entity
    # 슬라이스의 per-node inventory view 앵커(§8). 빈 dict면 footprint 0.
    by_label: dict[str, int] = Field(default_factory=dict)
    # K7.5 (O1) — `(status=ready, count=0)` 거짓성공 방지. genuine 0(쿼리 성공·실제 0개)만
    # `ready`다. flag-off/Neo4j-down/null-caller 등 측정 자체를 못 한 fail-open empty는
    # `unavailable`(default — 안전값), rebuild 진행 중은 `rebuilding`(half-projected 오노출
    # 차단). FE는 `unavailable`/`rebuilding`을 "0/0"로 렌더하지 않고 보류 상태로 표시한다.
    status: Literal["ready", "unavailable", "rebuilding"] = "unavailable"


class KgOverview(BaseModel):
    """`GET /kg/overview` 응답 — company-scope KG의 집계 통계(전용 지식그래프 화면 헤더).

    footprint(caller 본인 personal 카운트)와 대비되는 **company 집계**다: 술어는
    `scope='company'`뿐이고 `$caller`를 바인딩하지 않으므로 타 owner의 personal 노드/엣지는
    절대 집계에 섞이지 않는다(본인 personal 카운트는 별도로 `OwnerFootprint`가 준다). count만
    싣고 slug/이름/owner_id 같은 식별자는 담지 않는다.

    `entity_count`는 `by_label["Entity"]` 파생값의 편의 노출이다(:Entity는 항상 company
    scope로 투영 — project.py). status는 OwnerFootprint의 3-state에 `disabled`(flag-off)를
    더한 4-state다 — FE가 KG 비활성 배너를 별도로 렌더할 수 있게 한다. `unavailable`(default,
    안전값)은 Neo4j 미가용/측정 실패, `rebuilding`은 full rebuild 중 half-projected 오노출
    보류, `ready`는 genuine 측정 성공(count==0이어도 정직)."""

    node_count: int = 0
    edge_count: int = 0
    by_label: dict[str, int] = Field(default_factory=dict)
    entity_count: int = 0
    unresolved_conflicts: int = 0
    resolved_conflicts: int = 0
    status: Literal["ready", "unavailable", "rebuilding", "disabled"] = "unavailable"


class OwnerErasureReport(BaseModel):
    """K7.5 (W1) — owner-scope KG erasure 결과의 **2-state 정직 리포트**.

    'forget_complete' 단일 헤드라인을 폐기한다(Q2 — 실제 PG owner-row 삭제는 P8 위임이라
    K7.5 erasure는 **그래프 표시(graph view)만** 제거한다). 그래서 두 사실을 분리해 보고한다:
    `removed_from_graph`(그래프에서 지웠나)와 `pg_copies_pending`(저장된 PG 사본이 남아 있나 —
    K7.5에선 구조적으로 항상 True). '완전 forget'은 `pg_copies_pending=False`(P8 PG delete 후)일
    때만 성립한다.

    **content-blind**: 카운트만 담는다 — slug/title/entity_key 같은 target 식별 내용은 절대
    싣지 않는다(T3). audit도 동일하게 counts-only다. `footprint_before/after`는 status-gated
    `OwnerFootprint`(O-r1) — status!=ready면 FE/CLI가 0을 성공으로 렌더하지 않고 '확인 보류'로
    표시한다."""

    target_owner_id: UUID
    actor_user_id: UUID | None = None
    erased_at: datetime
    footprint_before: OwnerFootprint = Field(default_factory=OwnerFootprint)
    footprint_after: OwnerFootprint = Field(default_factory=OwnerFootprint)
    nodes_detached: int = 0
    edges_detached: int = 0
    removed_from_graph: bool = False  # 그래프 표시 제거 수행 여부(Neo4j 가용 + DETACH 실행)
    pg_copies_pending: bool = True  # Q2 — PG owner-row 삭제는 P8 위임 → K7.5에선 항상 True
    neo4j_available: bool = False
    # HR/직원 대상 정규 문구(M-r1) — 확정 일자 없음(SLA 날짜 약속 아님). content-blind.
    what_remains: str = ""


class KgGraphAnswer(BaseModel):
    """K4b `/ask` graph 분기 메타 (docs/kg-model.md §4, kg-implementation-spec §7.3).

    답변 본문은 `RoutedAnswer.wiki`(compiled wiki page grounding)에 담기고, 이 모델은
    경로/관계 메타만 담는다(불변식 5 — 본문은 graph 메타에서 생성하지 않는다).
    `nodes`/`edges`는 K4 `GET /wiki/pages/{slug:path}/graph`와 **동일한 typed
    KgGraphNode/KgGraphEdge**를 재사용한다 — FE가 단일 그래프 shape로 K4/K4b를
    함께 렌더하기 위함이다(K5 path 렌더가 backend 추가 없이 가능). `edges`의
    `properties`는 sparse하다(KgGraphEdge docstring 참조)."""

    # §4 registry: neighbors | path_between | conflicts_of | provenance_chain | page_conflicts
    # | entity_mentions(K9.3b entity star). intent: relation | conflict | provenance | entity.
    # K9.3b — entity intent는 `entity_mentions` 결과(엔티티 중심 star + MENTIONED_IN 다리)를
    # 그대로 실어, 답 본문(grounding) 위에 cross-page 연결 시각 레이어로 얹는다(불변식 5 보존).
    template: str
    intent: str
    nodes: list[KgGraphNode] = Field(default_factory=list)
    edges: list[KgGraphEdge] = Field(default_factory=list)
    # 경로/이웃 노드 slug 순서 — RAW node ordering, non-groundable/placeholder slug
    # 포함. grounded compiled-page 집합이 아니다(materialized WikiPage/WikiClaim 한정).
    # K5 consumer는 이를 클릭 가능한 wiki page target으로 가정하면 안 된다.
    path_slugs: list[str] = Field(default_factory=list)
    params_redacted: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


class KgPathFraming(BaseModel):
    """K7.4 — owner-scope path 답변의 한 framing(둘 중 하나).

    framing A = "직접 연결(전부 회사)" = `path_between_company`(company-only predicate),
    framing B = "내 메모 경유" = `path_between` owner-variant(visibility_predicate). 둘 다
    `KgGraphNode`/`KgGraphEdge`를 재사용하므로 `owner_id`는 wire에 없고 `scope`만 노출된다.

    `personal_dependent`는 **서버 파생**이다 — framing B의 반환 node/edge 중 하나라도
    `scope=='personal'`이면 True(`_map_records` tripwire가 foreign-personal을 이미 drop하므로
    'personal'은 오직 caller 본인 hop). 템플릿 정체성/끝점 scope가 아니라 **반환 scope**에서
    파생해야 A·B가 합법적으로 같은 all-company 경로를 반환하는 케이스의 오라벨을 막는다.
    `same_as_direct`는 그 역(B가 A와 동일 = personal_dependent False) — A==B coincident일 때
    personal 캡션을 구조적으로 withhold한다(렌더 정책은 FE 슬라이스, L4)."""

    label: str  # framing 식별 키(예: "direct_company" | "via_personal") — scope-enum 금지(L7/§9)
    nodes: list[KgGraphNode] = Field(default_factory=list)
    edges: list[KgGraphEdge] = Field(default_factory=list)
    path_slugs: list[str] = Field(default_factory=list)
    personal_dependent: bool = False  # 서버 파생 — B의 반환 scope에 personal hop 존재
    truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def same_as_direct(self) -> bool:
        """B가 A와 동일(personal_dependent False) → personal 캡션 withheld. `personal_dependent`의
        엄밀한 역이라 별도 저장 필드가 아니라 파생값으로 둔다(두 bool이 역으로 어긋나는 모순 상태
        구조적 차단 — 미래 3번째 framing이 한쪽만 set하는 사고 방지)."""
        return not self.personal_dependent


class KgPathFramings(BaseModel):
    """K7.4 — ORDERED framing 리스트(A 먼저, 그다음 B). 고정 A/B 쌍이 아니라 list로 둬
    미래 sharing의 3번째 'shared-collaborator' framing을 마이그레이션 0으로 append할 수 있다.
    `RoutedAnswer.path_framings`에만 mount한다(공유 KgGraphAnswer 중첩 금지, L2)."""

    framings: list[KgPathFraming] = Field(default_factory=list)


class KgFramingsSpanMeta(BaseModel):
    """K7.4 — `router.graph.framings` orchestrator span meta의 **typed allowlist**(§5 item 7).

    이 슬라이스가 새로 여는 유일한 텔레메트리 표면이라, slug/title/owner_id가 가장 새기 쉬운
    곳이다. `extra='forbid'`로 scalar/boolean만 허용해 어떤 code path에서도 slug/title/owner_id를
    **물리적으로** 붙일 수 없게 한다(enumerated absence 테스트보다 강한 구조적 가드 — P3.4d
    `EmailDraftPayload` 선례). emit-site가 이 모델로 meta를 빌드하므로 unknown 키는 검증에서
    거부된다."""

    model_config = ConfigDict(extra="forbid")

    framing_count: int = 0
    personal_dependent: bool = False
    same_as_direct: bool = False
    short_circuit: bool = False  # all-company-no-personal 단축경로(framing B 합성, 1 row)
    # demote(framing_count=0) 시 cause를 구분하는 bounded 토큰 — gate 오류(transient: timeout/
    # driver_error/mapping_error)와 genuine empty path를 분리해 owner-scope graph 실패를 진단
    # 가능하게 한다(§1 observability). reject_reason의 **coarse 카테고리**(`:` 앞)만 실어 slug/detail
    # 누출 위험 0. 성공 시 None.
    demote_cause: str | None = None


# RoutedAnswer.graph/path_framings are forward refs to KgGraphAnswer/KgPathFramings, both
# declared after RoutedAnswer; rebuild now that they exist. (총 1회 호출 — 참조되는 모든
# forward-ref 클래스 선언 뒤에 위치해야 import-time NameError/stale-rebuild를 피한다.)
RoutedAnswer.model_rebuild()


# --- P8.7b MCP knowledge surface ------------------------------------------------


class WikiSearchItem(BaseModel):
    """One compiled-page hit for `GET /wiki/search`.

    Carries page metadata + a single snippet excerpt from the matching compiled
    page chunk. No raw corpus chunk bodies are exposed beyond that snippet — the
    grounding contract (answers ground only on compiled wiki pages) is intact."""

    slug: str
    title: str
    kind: str  # 'page' | 'claim'
    snippet: str
    score: float
    scope: Literal["company", "personal"]


class WikiSearchResponse(BaseModel):
    query: str
    scope: str
    count: int
    items: list[WikiSearchItem]


class WikiTaskCreateRequest(BaseModel):
    """Owner-scoped wiki update candidate (P8.7b).

    Creates an `open_question` WikiTask in the CALLER's personal scope referencing
    a target wiki slug + a free-text note. Never writes wiki page content and
    never touches company scope."""

    slug: str
    note: str
    evidence_urls: list[str] = Field(default_factory=list)

    @field_validator("note")
    @classmethod
    def _note_nonempty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("note required")
        return cleaned

    @field_validator("evidence_urls")
    @classmethod
    def _clean_evidence_urls(cls, value: list[str]) -> list[str]:
        return [u.strip() for u in value if u and u.strip()]


class WikiTaskCreateResult(BaseModel):
    slug: str
    target_slug: str
    kind: Literal["open_question"]
    # company on a company node (owner+admin review queue), personal on a personal node.
    scope: Literal["personal", "company"]
    created: bool


class CollectorWhoamiOut(BaseModel):
    """Identity + node-local authority for the calling collector/knowledge token.

    `role` is the node allowlist role (owner/admin/member) or null; agents use it
    to decide whether they may submit wiki update candidates that owner/admin
    review."""

    user_id: UUID
    email: str | None
    role: str | None
    node_id: str
    scopes: list[str] = Field(default_factory=list)


# --- board (kanban overlay over notion_rows) ------------------------------------


class BoardCard(BaseModel):
    row_id: UUID
    title: str
    status: str  # resolved column: Todo | In Progress | Review | Done
    notion_status: str | None = None  # raw properties->>'상태' for transparency
    assignee: str | None = None
    priority: str | None = None
    due_at: datetime | None = None
    is_local_override: bool = False  # True when a task_states row exists
    writeback_status: str | None = None  # synced | disabled | failed | local_only
    writeback_error: str | None = None


# --- P8.2 collector ingestion + command queue -----------------------------------

# Max documents accepted in one ingestion batch. Beyond this the endpoint rejects
# the whole request rather than silently truncating.
COLLECTOR_INGEST_MAX_BATCH = 200

CollectorCommandKind = Literal["connector_sync", "raw_repush", "agent_task"]
CollectorCommandStatus = Literal["pending", "claimed", "done", "failed"]


class CollectorIngestDocument(BaseModel):
    """One owner-scoped document pushed by the personal collector daemon."""

    source: str
    source_external_id: str
    source_account_id: UUID | None = None
    source_canonical_id: str | None = None
    source_last_edited_at: datetime | None = None
    title: str
    content_md: str
    meta: dict = Field(default_factory=dict)
    project: str | None = None


class CollectorIngestRequest(BaseModel):
    documents: list[CollectorIngestDocument] = Field(
        ..., min_length=1, max_length=COLLECTOR_INGEST_MAX_BATCH
    )


class CollectorIngestDocumentResult(BaseModel):
    doc_id: UUID
    source_external_id: str
    status: Literal["created", "updated", "unchanged"]


class CollectorIngestResponse(BaseModel):
    results: list[CollectorIngestDocumentResult] = Field(default_factory=list)


class CollectorConfigAccount(BaseModel):
    """Non-secret owner personal connector settings for a collector daemon."""

    connector_slug: str
    settings: dict[str, str | int | list[str]] = Field(default_factory=dict)


class CollectorConfigResponse(BaseModel):
    """Remote collector runtime config.

    Secret refs and configured flags stay server-side; daemon credentials remain
    owner-local env/Keychain/gws state.
    """

    owner_id: UUID
    accounts: list[CollectorConfigAccount] = Field(default_factory=list)


class CollectorCommandCreate(BaseModel):
    kind: CollectorCommandKind
    payload: dict = Field(default_factory=dict)
    # Target one device (migration 0070). None = broadcast to every device
    # of the owner.
    device_id: str | None = None


class CollectorCommand(BaseModel):
    command_id: UUID
    node_id: str
    user_id: UUID
    kind: CollectorCommandKind
    payload: dict = Field(default_factory=dict)
    device_id: str | None = None
    status: CollectorCommandStatus
    result: dict | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None


class CollectorCommandList(BaseModel):
    count: int
    items: list[CollectorCommand] = Field(default_factory=list)


class CollectorTokenRecord(BaseModel):
    token_id: UUID
    node_id: str
    name: str
    scopes: list[str] = Field(default_factory=list)
    device_id: str = ""
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_status_at: datetime | None = None
    scheduler_installed: bool | None = None
    scheduler_loaded: bool | None = None
    scheduler_interval_seconds: int | None = None
    last_status_error: str | None = None
    revoked_at: datetime | None = None


class CollectorTokenList(BaseModel):
    count: int
    items: list[CollectorTokenRecord] = Field(default_factory=list)


class CollectorTokenIssueRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["ingest"], min_length=1)
    # Human label for the machine (e.g. "Mac mini"); the server derives a
    # collision-free device_id slug from it (migration 0070).
    device_label: str | None = None


class CollectorTokenIssueResponse(BaseModel):
    token: str
    item: CollectorTokenRecord


class CollectorEvidenceWikiPage(BaseModel):
    slug: str
    title: str
    scope: Literal["personal"] = "personal"


class CollectorEvidenceDocument(BaseModel):
    doc_id: UUID
    title: str
    source: str
    source_account_id: UUID | None = None
    source_external_id: str | None = None
    source_last_edited_at: datetime | None = None
    updated_at: datetime | None = None
    wiki_source_slug: str | None = None
    wiki_pages: list[CollectorEvidenceWikiPage] = Field(default_factory=list)


class CollectorEvidenceCompile(BaseModel):
    indexed: int = 0
    authored: int = 0
    skipped: int = 0
    failed: int = 0


class CollectorLiveness(BaseModel):
    status: Literal["unconfigured", "live", "stale", "offline"]
    checked_at: datetime
    last_seen_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_status_at: datetime | None = None
    active_token_count: int = 0
    revoked_token_count: int = 0
    pending_command_count: int = 0
    claimed_command_count: int = 0
    oldest_pending_command_at: datetime | None = None
    oldest_claimed_command_at: datetime | None = None
    scheduler_installed: bool | None = None
    scheduler_loaded: bool | None = None
    scheduler_interval_seconds: int | None = None
    last_status_error: str | None = None
    reason: str


class CollectorWorkspaces(BaseModel):
    """Daemon-reported agent_task run folders for the owner's delegation UI.

    `default` is the daemon's configured workspace (ORTHUS_AGENT_TASK_WORKSPACE)
    or None; `recent` are the resolved cwds the daemon actually used, newest
    first.
    """

    default: str | None = None
    recent: list[str] = Field(default_factory=list)


class CollectorStatusReport(BaseModel):
    scheduler_installed: bool | None = None
    scheduler_loaded: bool | None = None
    scheduler_interval_seconds: int | None = Field(default=None, ge=1)
    status_error: str | None = Field(default=None, max_length=500)
    workspaces: CollectorWorkspaces | None = None


class AgentFolderList(BaseModel):
    """Owner-scoped union of the daemon-reported agent_task run folders.

    `supported` is False when agent_task delegation is disabled on the node
    (fail-closed). `default` is the configured default from the most-recently
    reported token; `recent` is the deduped union of recent cwds (newest
    first); `reported_at` is the latest collector status report time across the
    owner's tokens.
    """

    supported: bool
    default: str | None = None
    recent: list[str] = Field(default_factory=list)
    reported_at: datetime | None = None


class AgentDelegateTargetNode(BaseModel):
    """One enrolled collector node a teammate's daemon can run agent_task on.

    `device_id` is null for a legacy deviceless token (not pinnable). `status`
    folds WS-realtime + poll recency into online/stale/offline; `realtime` is
    True only while a daemon for this device holds a live notify socket.
    """

    device_id: str | None = None
    label: str
    status: str
    realtime: bool = False
    last_polled_at: datetime | None = None
    default_folder: str | None = None
    recent_folders: list[str] = Field(default_factory=list)


class AgentDelegateTarget(BaseModel):
    """A delegatable user plus their enrolled collector nodes."""

    user_id: UUID
    display_name: str
    email: str | None = None
    is_self: bool = False
    online: bool = False
    nodes: list[AgentDelegateTargetNode] = Field(default_factory=list)


class AgentDelegateTargetList(BaseModel):
    """delegate-targets response: who/what the caller may delegate to."""

    supported: bool
    self_user_id: UUID
    targets: list[AgentDelegateTarget] = Field(default_factory=list)


class AgentWorkDelegateRequest(BaseModel):
    """Structured agent_task delegation intake from the Agent-chat UI.

    Mirrors the fields the /ask natural-language delegation path parses, but
    supplied directly. `assignee` defaults to the caller; `runner`/`mode`
    default to codex/knowledge in the endpoint. `session_id` links the work to
    an owner-scoped Agent-chat thread so the collector can resume the runner's
    native conversation on later turns.
    """

    instruction: str
    assignee: str | None = None
    runner: str | None = None
    mode: str | None = None
    cwd: str | None = None
    session_id: str | None = None
    # Pin the dispatch to a specific enrolled device (collector_tokens.device_id);
    # None = any of the assignee's daemons.
    device_id: str | None = None


class CollectorEvidenceSource(BaseModel):
    source: str
    document_count: int
    recent_documents: list[CollectorEvidenceDocument] = Field(default_factory=list)
    latest_command: CollectorCommand | None = None
    latest_compile: CollectorEvidenceCompile | None = None


class CollectorEvidenceResponse(BaseModel):
    owner_id: UUID
    liveness: CollectorLiveness
    sources: list[CollectorEvidenceSource] = Field(default_factory=list)
    recent_commands: list[CollectorCommand] = Field(default_factory=list)


class CollectorCommandComplete(BaseModel):
    status: Literal["done", "failed"]
    result: dict = Field(default_factory=dict)


class CollectorCompileRequest(BaseModel):
    """Optional bound on how many of the owner's personal documents to compile."""

    limit: int | None = Field(default=None, ge=1)


class CollectorCompileResponse(BaseModel):
    """P8.4 central compile counts for one owner's personal documents."""

    indexed: int = 0
    authored: int = 0
    skipped: int = 0
    failed: int = 0


class CollectorPersonalDataPruneRequest(BaseModel):
    """Delete owner-scoped personal collector documents older than a cutoff."""

    older_than_days: int = Field(..., ge=1)
    source: str | None = None


class CollectorPersonalDataDeleteResponse(BaseModel):
    """Central personal collector deletion counts. No document content is returned."""

    owner_id: UUID
    documents_deleted: int = 0
    connector_items_deleted: int = 0
    structured_rows_deleted: int = 0
    corpus_chunks_deleted: int = 0
    corpus_embeddings_deleted: int = 0
    wiki_items_deleted: int = 0
