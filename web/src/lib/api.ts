/**
 * Single source of truth for talking to the 대리.ai backend.
 * Base URL + optional demo X-User-Id header live here and nowhere else.
 */

import { deactivateOfflineNoteOwner } from "@/lib/offline-notes";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8820";

export const DEMO_USER_ID =
  process.env.NEXT_PUBLIC_DEMO_USER_ID ?? "00000000-0000-4000-8000-000000000001";

function encodePathSlug(slug: string): string {
  return slug.split("/").map(encodeURIComponent).join("/");
}

function scopeQuery(scope?: "company" | "personal" | null): string {
  return scope ? `?scope=${encodeURIComponent(scope)}` : "";
}

/* ----------------------------- shared types ----------------------------- */

/** Opaque BlockNote block — we round-trip it without inspecting its shape. */
export type Block = Record<string, unknown>;

export interface DocumentSummary {
  doc_id: string;
  title: string;
  source: string;
  updated_at: string;
}

export interface DocumentDetail extends DocumentSummary {
  block_json: Block[];
  markdown: string;
}

export interface DocumentBody {
  title: string;
  block_json: Block[];
  markdown: string;
}

/* ------------------------------- corpus --------------------------------- */

export interface CorpusListItem {
  doc_id: string;
  title: string;
  source: string;
  project: string;
  owner_name: string | null;
  updated_at: string;
  preview: string;
}

export interface CorpusListResponse {
  items: CorpusListItem[];
  total: number;
  counts_by_source: Record<string, number>;
}

export interface CorpusDocumentDetail {
  doc_id: string;
  title: string;
  block_json: Block[];
  markdown: string;
  source: string;
  project: string;
  owner_name: string | null;
  updated_at: string;
}

export interface CorpusListParams {
  source?: string;
  q?: string;
  sort?: "recent" | "title";
  limit?: number;
  offset?: number;
}

/** New wiki source shape (P2.2): grounded against compiled wiki pages/claims. */
export interface WikiSourceRef {
  page_slug: string;
  title: string;
  kind: string; // 'page' | 'claim'
  excerpt: string;
  score: number;
  provenance: string[]; // source slugs / corpus refs
  source_scope: "company" | "personal";
  source_node_id: string | null;
}

export interface WikiLinkRef {
  slug: string;
  title: string;
  scope: "company" | "personal";
}

export type GapReason =
  | "no_data"
  | "weak_retrieval"
  | "insufficient_grounding"
  | "missing_link";

/** Deterministic flag that a wiki answer was poorly grounded (orthus/wiki/gap.py). */
export interface WikiGap {
  detected: boolean;
  reason: GapReason;
  missing_topic: string;
  top_score: number | null;
  message: string;
}

export interface WikiAnswer {
  question: string;
  answer: string;
  sources: WikiSourceRef[];
  wiki_links: WikiLinkRef[];
  warnings: string[];
  gap: WikiGap | null;
}

export interface WikiPage {
  slug: string;
  title: string;
  definition: string;
  overview: string;
  relations: string[];
  evidence: string[];
  competing: string[];
  open_questions: string[];
  sources: string[];
  backlinks: string[];
}

export interface GapSuggestionSection {
  title: string;
  items: string[];
}

/** A persisted data-gap backlog row (GET /gaps, POST /gaps/feedback). */
export interface DataGap {
  gap_id: string;
  scope: "company" | "personal";
  reason: GapReason;
  question: string;
  top_score: number | null;
  suggested_target: string | null;
  suggested_connector: string | null;
  context_wiki_slug: string | null;
  suggested_fields: GapSuggestionSection[];
  suggestion_status: "pending" | "ready" | (string & {});
  hit_count: number;
  status: "open" | "resolved" | "dismissed" | (string & {});
  source: "auto" | "feedback" | (string & {});
  created_at: string;
  updated_at: string;
  last_seen_at: string;
}

export interface DataGapPageResponse {
  count: number;
  items: DataGap[];
}

export type AgentWorkOutcome =
  | "auto_execute"
  | "draft_for_review"
  | "request_more_data"
  | "reject"
  | (string & {});

export type AgentWorkState =
  | "pending"
  | "classified"
  | "auto_execute"
  | "draft_for_review"
  | "request_more_data"
  | "rejected"
  | "resolved"
  | "dismissed"
  | (string & {});

export interface AgentWorkItem {
  work_id: string;
  node_id: string;
  node_kind: "company" | "personal";
  owner_id: string | null;
  source_kind: string;
  source_ref_id: string;
  action_family: string;
  title: string;
  payload: Record<string, unknown>;
  state: AgentWorkState;
  policy_outcome: AgentWorkOutcome | null;
  policy_reason: string | null;
  reason_codes: string[];
  evidence: Array<Record<string, unknown>>;
  correlation_id: string | null;
  last_run_id: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  wiki_slugs: string[];
}

export interface AgentWorkSyncResult {
  source_kind: string;
  created_or_updated: number;
  items: AgentWorkItem[];
}

/** Folders the caller's own collector daemon reported (owner-scoped). Drives the
 *  `@` run-folder picker on the Assistant delegate controls. */
export interface AgentFolderList {
  supported: boolean;
  default: string | null;
  recent: string[];
  reported_at: string | null;
}

/** Structured Assistant delegation request — UI alternative to the `위임:` text
 *  grammar. Mirrors POST /agent-work/delegate. */
export interface AgentDelegateRequest {
  instruction: string;
  assignee?: string | null;
  runner?: "codex" | "claude" | "hermes" | null;
  mode?: "code" | "knowledge" | null;
  cwd?: string | null;
  session_id?: string | null;
  // Pin the run to a specific collector node (device) of the assignee. null =
  // any of the assignee's enrolled daemons. device_id is null for legacy
  // deviceless nodes, which cannot be pinned.
  device_id?: string | null;
}

/** One enrolled collector node (device) a delegation can be dispatched to. */
export interface AgentDelegateTargetNode {
  device_id: string | null; // null = legacy deviceless node, NOT pinnable
  label: string; // e.g. "Mac mini"
  status: "online" | "stale" | "offline";
  realtime: boolean; // WS-connected now
  last_polled_at: string | null;
  default_folder: string | null;
  recent_folders: string[];
}

/** A teammate (or self) a delegation can be assigned to, with their nodes. */
export interface AgentDelegateTarget {
  user_id: string;
  display_name: string;
  email: string | null;
  is_self: boolean;
  online: boolean; // any node online
  nodes: AgentDelegateTargetNode[];
}

/** Delegate-target picker source. Mirrors GET /agent-work/delegate-targets.
 *  supported=false when agent_task delegation is disabled. */
export interface AgentDelegateTargetList {
  supported: boolean;
  self_user_id: string;
  targets: AgentDelegateTarget[]; // self first, then others by name
}

export interface AgentContinueRequest {
  text: string;
}

/** Result of POST /agent-work/chats/{id}/hitl-response. */
export interface AgentChatHitlResponseResult {
  routed: RoutedAnswer;
  response_message: AgentChatMessage;
}

export interface AgentWorkWikiLinkResponse {
  count: number;
  items: AgentWorkItem[];
}

/* --- Agent chat conversation sessions (DB-backed, owner-scoped) --- */

export interface UserInputOption {
  value: string;
  label?: string | null;
}

/** A named fill-in-the-blank field on a text HITL question. When present, the FE
 *  renders a labeled form card instead of the composer hint; submitted values go
 *  to hitlRespond `values` and the server merges them into the answer. */
export interface UserInputField {
  name: string;
  label: string;
  placeholder?: string | null;
  required?: boolean;
}

/** A turn-ending HITL question surfaced by the orchestrator. Rendered inside the
 *  chat as a structured bubble; answered via api.hitlRespond. */
export interface UserInputRequest {
  message_id?: string | null;
  question: string;
  input_type: "text" | "choice" | "approval";
  options: UserInputOption[];
  fields?: UserInputField[];
  work_id?: string | null;
  origin: "policy_gate" | "agentic";
  status: "waiting" | "answered" | "expired";
  resume?: Record<string, unknown>;
}

export interface AgentChatMessage {
  message_id: string;
  role: string; // "user" | "agent"
  text: string;
  work_id: string | null;
  /** HITL (migration 0101). "text" for plain messages; "user_input_request" /
   *  "user_input_response" carry a structured `payload`. Unknown kinds render as
   *  plain text (forward-compatible). */
  kind?: string;
  payload?: Record<string, unknown> | null;
  created_at: string;
}

export interface AgentChatSession {
  session_id: string;
  title: string;
  message_count: number;
  last_message_preview: string | null;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentChatSessionDetail {
  session: AgentChatSession;
  messages: AgentChatMessage[];
}

export interface AgentWorkInboxWikiTaskItem {
  slug: string;
  kind: string;
  title: string;
  created_at: string;
  href: string;
}

export interface AgentWorkInboxPromotionItem {
  stage_id: string;
  source_title: string;
  status: string;
  created_at: string | null;
  href: string;
}

export interface AgentWorkInboxDataGapItem {
  gap_id: string;
  missing_topic: string;
  hit_count: number;
  last_seen_at: string;
  href: string;
}

export interface AgentWorkInboxAgentWorkItem {
  work_id: string;
  title: string;
  source_kind: string;
  action_family: string;
  state: AgentWorkState;
  policy_outcome: AgentWorkOutcome | null;
  updated_at: string;
  wiki_slugs: string[];
  href: string;
}

/** 체크리스트 배지 신호 (`GET /agent-work/notify-state`) — MailNotifyState와 동형.
 *  구서버(endpoint 미배포)는 404 — 호출측은 조용히 무시해야 한다. */
export interface AgentWorkNotifyState {
  total: number;
  latest_at: string | null;
  latest_title: string | null;
}

export interface AgentWorkInboxSummary {
  generated_at: string;
  node_id: string;
  node_kind: "company" | "personal";
  buckets: {
    wiki_tasks: {
      count: number;
      count_capped: boolean;
      items: AgentWorkInboxWikiTaskItem[];
    };
    promote_staging: {
      count: number;
      supported: boolean;
      count_capped: boolean;
      items: AgentWorkInboxPromotionItem[];
    };
    data_gaps: {
      count: number;
      count_capped: boolean;
      items: AgentWorkInboxDataGapItem[];
    };
    agent_work: {
      count: number;
      count_capped: boolean;
      by_outcome: Record<string, number>;
      items: AgentWorkInboxAgentWorkItem[];
    };
  };
}

export type MailBackendName = "nova" | "acme" | "gmail";
export type MailScope = "company" | "personal";
export type MailDirection = "inbound" | "outbound";

export interface EmailAttachmentRef {
  filename: string;
  content_type: string;
  size: number;
  ref: string | null;
  att_id: string | null;
  content_id: string | null;
  inline: boolean;
}

export interface CanonicalEmail {
  backend: MailBackendName;
  account_id: string | null;
  external_id: string;
  message_id: string | null;
  direction: MailDirection;
  scope: MailScope;
  owner_addr: string | null;
  from_addr: string;
  to_addr: string[];
  cc_addr: string[];
  subject: string;
  body_text: string;
  body_html: string;
  read: boolean;
  starred: boolean;
  replied: boolean;
  ai_draft: string;
  label: string;
  trashed: boolean;
  sent_at: string | null;
  received_at: string | null;
  attachment_count: number;
  attachments: EmailAttachmentRef[];
}

export interface MailBackendStatus {
  backend: MailBackendName;
  account_id: string | null;
  configured: boolean;
  ok: boolean;
  total: number;
  unread: number;
  error: string | null;
  send_configured: boolean;
  owner_addr: string | null;
}

export interface MailInboxResponse {
  items: CanonicalEmail[];
  total: number;
  unread: number;
  backends: MailBackendStatus[];
  send_enabled: boolean;
}

/** Cheap local new-mail signal (`GET /mail/notify-state`) for desktop notifications. */
export interface MailNotifyState {
  total: number;
  latest_at: string | null;
  latest_title: string | null;
}

export interface MailSendRequest {
  from_addr: string;
  to: string;
  subject: string;
  text: string;
  html?: string;
  /** 참조 — 콤마로 구분한 주소 목록. */
  cc?: string;
  /** 숨은참조 — 콤마로 구분한 주소 목록. */
  bcc?: string;
  /** 답장(Re) 스레딩 — 원본 메일의 provider message id. */
  reply_to_id?: string;
  /** 열람 추적 픽셀 주입 여부(서버 플래그가 켜진 경우에 한해). 생략=true(기본 추적), 이 발송만 끄려면 false. */
  track?: boolean;
}

export interface MailSendResult {
  backend: "nova" | "acme";
  status: "sent" | "failed";
  provider_message_id: string | null;
  error: string | null;
  /** 열람 추적이 켜져 픽셀이 주입된 발송이면 그 token. `GET /mail/track/status/{token}`로 읽음 폴링. off면 null. */
  tracking_token?: string | null;
}

/** 보낸 메일 한 건의 열람(읽음) 현황. owner 세션이 자기 token만 조회한다. */
export interface MailTrackingStatus {
  token: string;
  opened: boolean;
  open_count: number;
  first_opened_at: string | null;
  last_opened_at: string | null;
}

/** 메일 명함(서명) 외부/소셜 링크 한 개. */
export interface MailSignatureLink {
  label: string;
  url: string;
}

/** 메일 명함(서명) — 작성한 메일 끝에 넣는 본인 비즈니스 카드(owner-scope). */
export interface MailSignature {
  from_addr: string;
  display_name: string;
  title: string;
  company: string;
  email: string;
  phone: string;
  website: string;
  links: MailSignatureLink[];
  enabled: boolean;
}

/** Inline agentic /ask compose output: a NON-sent mail draft the operator
 *  reviews and sends with one click (POST /mail/send). The send click is the
 *  owner approval — the assistant never sends. */
export interface MailComposeDraft {
  from_addr: string;
  to: string;
  subject: string;
  body: string;
  reply_to_contact?: string | null;
}

export interface MailDraftRequest {
  mode?: "compose" | "reply";
  instruction?: string;
  to?: string;
  subject?: string;
  context?: string;
}

/** 초안이 근거로 삼은 compiled wiki page 1건. */
export interface WikiGroundingRef {
  page_slug: string;
  title: string;
  excerpt?: string;
  score?: number;
}

/** 초안이 실제로 반영한 회사 위키 근거. `conflict_warning`이 있으면 지식그래프가
 *  그 근거에 미해결 모순이 걸려 있다고 알려준 것이라, 초안은 값을 단정하지 않는다. */
export interface WikiMailGrounding {
  query: string;
  answer: string;
  sources: WikiGroundingRef[];
  conflict_warning?: string | null;
}

export interface MailDraftResult {
  subject: string;
  body: string;
  wiki_grounding?: WikiMailGrounding | null;
}

/** On-demand local-agent draft dispatch (the "AI 초안" button routed to the
 *  requester's OWN claude/hermes). See docs/mail-draft-local-agent.md. */
export interface MailDraftDispatchRequest {
  mode?: "compose" | "reply";
  instruction?: string;
  to?: string;
  subject?: string;
  context?: string;
  runner?: "claude" | "hermes";
  device_id?: string;
}

export interface MailDraftDispatchResult {
  mode: "local" | "central" | "unavailable";
  work_id: string | null;
  stream: boolean;
  draft: MailDraftResult | null;
  runner: string | null;
  message: string | null;
}

export interface MailPullBackendResult {
  backend: string;
  enabled: boolean;
  listed: number;
  ingested: number;
  skipped: number;
  errors: number;
}

export interface MailPullResponse {
  enabled: boolean;
  ingested: number;
  backends: MailPullBackendResult[];
}

export interface PersonalMailItem {
  doc_id: string;
  title: string;
  subject: string;
  sent_at: string | null;
  external_id: string | null;
  snippet: string;
}

export interface PersonalMailResponse {
  items: PersonalMailItem[];
  total: number;
}

export interface PersonalMailDetail {
  doc_id: string;
  title: string;
  subject: string;
  sent_at: string | null;
  body: string;
}

export interface AgentPolicyWikiSummary {
  page_slug: string;
  title: string;
  scope: "company" | "personal";
  owner_id: string | null;
  project: string;
  bucket_count: number;
  observation_count: number;
  updated_at: string;
}

export type AgentWorkReviewAction =
  | "approve"
  | "dismiss"
  | "request_more_data";

export interface AgentWorkReviewDecisionRequest {
  decision: AgentWorkReviewAction;
  note?: string | null;
  no_edit?: boolean | null;
}

export interface AgentWorkReviewDecision {
  decision_id: string;
  work_id: string;
  node_id: string;
  reviewer_id: string;
  decision: AgentWorkReviewAction;
  note: string | null;
  from_state: AgentWorkState;
  to_state: AgentWorkState;
  correlation_id: string | null;
  node_run_id: string | null;
  decided_at: string;
}

export interface AgentWorkReviewDecisionResult {
  item: AgentWorkItem;
  decision: AgentWorkReviewDecision;
}

export interface WikiAskOptions {
  k?: number;
  since?: string;
  until?: string;
  source?: string;
  /** P8.5: explicit scope override for company-node `/wiki/ask`.
   *  Server clamps to `company` unless ORTHUS_OWNER_SCOPE_ENABLED is true. */
  scope?: AskScope;
}

export interface WikiTaskResolution {
  decision:
    | "keep_existing"
    | "use_incoming"
    | "merge"
    | "answered"
    | "cleanup"
    | "dismissed";
  note?: string | null;
  resolved_by: string;
  resolved_at: string;
  produced_claim_slugs: string[];
}

export interface WikiTask {
  slug: string;
  kind:
    | "open_question"
    | "conflict"
    | "stale_audit"
    | "dedup"
    | "provenance_fix"
    | "entity_conflict";
  description: string;
  related: string[];
  created_at: string;
  resolved: boolean;
  /** conflict (case b): incoming claim text captured at consolidation time. */
  incoming_claim?: string | null;
  /** open_question: short distilled summary of the originating source. */
  source_excerpt?: string | null;
  /** WTR.1/WTR.2 resolve metadata; null on legacy/open tasks. */
  resolution?: WikiTaskResolution | null;
}

export type WikiTaskResolveDecision = WikiTaskResolution["decision"];

export interface WikiTaskResolveBody {
  decision: WikiTaskResolveDecision;
  merged_text?: string;
  answer?: string;
  note?: string;
}

export interface WikiTaskPageResponse {
  count: number;
  items: WikiTask[];
}

/* --- K5 page graph (read-only KG panel) — mirrors orthus/schemas/canonical.py --- */

/**
 * One node in the page-graph response. The backend projects meta only (gate.py
 * `_map_records`); `record_type`/`source`/`owner_id`/etc. exist in Neo4j but are
 * NOT on the wire. `id` is not type-tagged — materialized nodes carry a UUID
 * (page_id/doc_id/row_id), placeholders carry the slug — and since K7.2 a
 * personal + a company placeholder can share the slug-id space, so key nodes by
 * `(label, scope, id)`, never `id` alone.
 *
 * K7.2 owner-scope: `scope` ('company'|'personal') + `is_own_personal`
 * (server-derived owner_id==caller). `owner_id` UUID is NEVER on the wire.
 * Non-owner responses are structurally always `scope==='company'` /
 * `is_own_personal===false` (the gate tripwire drops any violating node). The
 * FE renders the personal-vs-company distinction ONLY on
 * `scope === 'personal' && is_own_personal === true` (K7.3 — AND both flags;
 * never trust `scope` alone).
 */
export interface KgGraphNode {
  id: string;
  label: string;
  slug: string | null;
  title: string | null;
  materialized: boolean;
  scope: "company" | "personal";
  is_own_personal: boolean;
  // K9 — :Entity 노드 전용(다른 라벨은 null). page-only degree(FE "≤3 표시 / 전체 N" 분모,
  // R1 표시 캡) + person 차등 readout(U4)/티저 person 억제(D-TZ B). Entity 노드는 title이
  // display_name(사람이 읽는 이름)으로 매핑된다.
  mention_count: number | null;
  entity_kind: string | null; // "person" | "org" | "project" | "system" | null
}

/**
 * One edge. `src`/`dst` are node `id`s (same space as KgGraphNode.id, mapped
 * server-side from element_id). `properties` is sparse — absent keys are
 * omitted, not null; read with `?.`/`??` (e.g. CONFLICTS_WITH may carry only
 * `status`).
 */
export interface KgGraphEdge {
  src: string;
  dst: string;
  rel: string;
  // K7.2 owner-scope: edge `scope` = the more-restrictive endpoint (personal
  // wins, B3). `owner_id` is never on the wire; `scope` is the only owner signal.
  scope: "company" | "personal";
  properties: Record<string, unknown>;
}

/**
 * K9.2 (C-C/U2/D-TZ B) — pre-expand entity bridge teaser. Present only when entities
 * exist globally AND this page has a bridge. Person names are SUPPRESSED in the auto
 * teaser (entity_names lists only non-person display names, person bridges show only
 * after expand); a person-only page yields entity_names=[] + person_only=true (count only).
 */
export interface EntityTeaser {
  page_count: number;
  entity_names: string[];
  person_only: boolean;
}

export interface WikiPageGraphResponse {
  /** server echoes the clean_wiki_slug-normalized slug — compare nodes to this. */
  slug: string;
  supported: boolean;
  reason: string | null;
  truncated: boolean;
  nodes: KgGraphNode[];
  edges: KgGraphEdge[];
  /**
   * K9.2 — entity bridge teaser. presence-gated, non-null only when this page has a
   * bridge. Carried on the default response (pre-expand); entity nodes/edges join
   * nodes/edges only when fetched with `?include=entity` (U2 expand).
   */
  entity_teaser: EntityTeaser | null;
  /**
   * K7.3 honesty (D2): true when owner-scope is on and personal notes are not yet
   * entity-searchable, so the panel's "내 개인 메모" state-3 copy ("notes mentioning
   * this topic exist but aren't linked; entity matching is coming") is honest, not a
   * silent capability lie. false on flag-off / non-owner.
   */
  personal_entity_search_deferred: boolean;
}

/**
 * K7.3 — `GET /wiki/kg/footprint`. Caller's OWN personal node/edge counts in the
 * shared central graph (owner-only legibility: "what of mine crossed in, and only I
 * can see it"). Never carries owner_id (counts only). Empty when flag-off / owner-scope
 * off / null caller / KG unavailable.
 */
export interface OwnerFootprint {
  node_count: number;
  edge_count: number;
  by_label: Record<string, number>;
  // K7.5 (O1) — genuine 0(측정 성공·실제 0개)만 "ready". 측정 자체를 못 한 fail-open empty는
  // "unavailable", full rebuild 진행 중은 "rebuilding". FE는 ready가 아니면 0을 "메모 없음"으로
  // 렌더하지 않고 '확인 보류 중'을 표시한다(false-zero 차단).
  status: "ready" | "unavailable" | "rebuilding";
}

/**
 * `GET /kg/overview` — company-scope KG 집계(전용 지식그래프 화면 헤더). footprint(내 personal
 * 카운트)와 대비되는 회사 전체 통계로, 다른 owner의 personal 데이터는 절대 섞이지 않는다.
 * `entity_count`는 by_label["Entity"] 편의 노출. status는 footprint 3-state에 flag-off
 * 배너용 "disabled"를 더한 4-state — "ready"가 아니면 count를 확정값으로 렌더하지 않는다.
 */
export interface KgOverview {
  node_count: number;
  edge_count: number;
  by_label: Record<string, number>;
  entity_count: number;
  unresolved_conflicts: number;
  resolved_conflicts: number;
  status: "ready" | "unavailable" | "rebuilding" | "disabled";
}

/**
 * `GET /kg/graph` — 전용 지식그래프 화면 랜딩 그래프(company 개체 co-mention 지도).
 * WikiPageGraphResponse와 동형(supported/nodes/edges)이되 특정 페이지 앵커가 없다.
 */
export interface KgEntityGraphResponse {
  supported: boolean;
  relation: string | null;
  reason: string | null;
  truncated: boolean;
  nodes: KgGraphNode[];
  edges: KgGraphEdge[];
}

/** `GET /wiki/search` 항목 — compiled page/claim 검색 결과(그래프 시드 픽커 겸용). */
export interface WikiSearchItem {
  slug: string;
  title: string;
  kind: string;
  snippet: string;
  score: number;
  // owner-scope: "personal" hit는 caller 본인 것(retrieve가 owner 가시성 강제).
  scope: "company" | "personal";
}

export interface WikiSearchResponse {
  query: string;
  scope: string;
  count: number;
  items: WikiSearchItem[];
}

/**
 * K4b `/ask` graph-mode metadata (mirrors orthus/schemas/canonical.py KgGraphAnswer).
 * The answer BODY is in RoutedAnswer.wiki (compiled-page grounded); this carries the
 * path/relation metadata only. Reuses the same KgGraphNode/KgGraphEdge as the K5 page
 * graph so one renderer serves both surfaces.
 */
export interface KgGraphAnswer {
  template: string; // neighbors | path_between | conflicts_of | provenance_chain
  intent: string; // relation | conflict | provenance
  nodes: KgGraphNode[];
  edges: KgGraphEdge[];
  path_slugs: string[];
  params_redacted: Record<string, unknown>;
  truncated: boolean;
}

export interface CompiledQuery {
  question: string;
  source_id: string | null; // null for the structured (PG JSONB store) backend
  dialect: string;
  sql: string;
}

export interface QueryValidation {
  parsed: boolean;
  statement_kind: string | null;
  schema_ok: boolean;
  read_only_ok: boolean;
  explain_ok: boolean;
  injected_limit: number | null;
  rejected_reason: string | null;
}

export interface Grounding {
  kind: string;
  ref: string;
}

export type AssistantStatus =
  | "executed"
  | "rejected"
  | "failed"
  | (string & {});

export interface AssistantResult {
  query_id: string;
  question: string;
  source_id: string | null; // null for the structured (PG JSONB store) backend
  compiled: CompiledQuery | null;
  validation: QueryValidation;
  status: AssistantStatus;
  columns: string[];
  rows: unknown[][];
  row_count: number | null;
  latency_ms: number | null;
  grounding: Grounding[];
  message: string | null;
}

export interface AssistantResultPart {
  source_scope: "company" | "personal";
  source_node_id: string | null;
  result: AssistantResult;
}

export type AskMode = "structured" | "wiki" | "graph" | (string & {});

/** Unified envelope from the auto-router (P2.2b). For structured/wiki exactly one of
 *  wiki/structured is populated; for K4b mode='graph' BOTH `wiki` (the grounded body)
 *  and `graph` (path metadata) are populated. */
// ── decompose types (docs/company-agent-orchestration.md §10) ────────────────

export interface WikiSourceRef {
  page_slug: string;
  title: string;
  kind: string;
  excerpt: string;
  score: number;
}

export interface SynthesizedBody {
  answer: string;
  sources: WikiSourceRef[];
  warnings: string[];
}

/** mode='agent_work' — a command-shaped request the orchestrator queued as an
 *  Agent Work item (POST /agent-work/chats/{id}/orchestrate). The chat links the
 *  work_id and polls its result. */
/** S4: server cap on the GET /agent-work?ids= batch fetch (mirrors
 *  orthus/api/routes/agent_work.py::_MAX_BATCH_IDS). Over-cap ids are dropped
 *  client-side rather than sent (the server 400s a >cap request). */
export const AGENT_WORK_BATCH_IDS_CAP = 50;

export interface AgentWorkIntakeResult {
  work_id: string;
  source_kind: string;
  source_ref_id: string;
  action_family: string;
  title: string;
  state: string;
  policy_outcome: string | null;
  policy_reason: string | null;
  reason_codes: string[];
  message: string;
  recipient?: string | null; // S4-4a — email_send delivery recipient (P3.4c)
  question_text?: string | null; // S4-4a — redacted command text (payload.question)
}

export interface SubAnswer {
  sub_question_id: string;
  routed: RoutedAnswer | null;
  grounded: boolean;
  agent_work_id: string | null;
  error: string | null; // "timeout" | "leaf_error" | "upstream_unavailable" | null
  context_injected?: boolean; // Phase 2: upstream depends_on context was injected
  text?: string | null; // Phase 2: sub-question text
  depends_on?: number[]; // Phase 2: positional indices into parts this leaf used
  // S4 command-split chip enrichment — back-injected from the queued AgentWork item.
  // All optional; an item-less action leaf leaves these unset and draws no chip.
  // BADGE SoR is `state` (the execution truth — a held item reads request_more_data);
  // `policy_outcome` is reference/tooltip only, NOT the badge source.
  state?: string | null;
  policy_outcome?: string | null;
  reason_codes?: string[];
  title?: string | null;
  recipient?: string | null; // email_send delivery recipient (P3.4c)
}

export interface RoutedAnswer {
  question: string;
  mode: AskMode;
  wiki: WikiAnswer | null;
  structured: AssistantResult | null;
  graph: KgGraphAnswer | null; // K4b — populated alongside `wiki` when mode='graph'
  agent_work?: AgentWorkIntakeResult | null; // mode='agent_work' (orchestrator intake)
  structured_parts: AssistantResultPart[];
  warnings: string[];
  /** Orchestrator (agent-work chat) only — echoes the caller's live-stream id
   *  (GET /agent-work/chats/stream/{id}). */
  stream_id?: string | null;
  /** Inline agentic /ask (bedrock engine) only — a NON-sent mail draft rendered as
   *  an editable card with a 보내기 button (POST /mail/send). */
  mail_draft?: MailComposeDraft | null;
  // decomposed mode only
  synthesized_body?: SynthesizedBody | null;
  parts?: SubAnswer[];
  /** HITL (agent-work chat only): a turn-ending question to the chat owner. When
   *  set, the orchestrator paused and needs an answer (POST .../hitl-response). */
  user_input_request?: UserInputRequest | null;
}

/** Past structured run pulled from the audit log (GET /ask/runs/{query_id}). */
export interface QueryRun {
  query_id: string;
  nl_question: string;
  source_id: string | null;
  compiled_sql: string | null;
  validation: QueryValidation;
  status: AssistantStatus;
  result_meta: Record<string, unknown> | null;
  created_at: string;
}

export type AskScope = "all" | "company" | "personal";

/* ------------------------------- projects ------------------------------- */

/** The valid project enum (mirrors connectors/project_map.PROJECTS). */
export type Project = "atlas" | "nova" | "orbit" | "company";

/** One db_name's current project assignment (GET /projects → groups[]). */
export interface ProjectGroup {
  db_name: string;
  project: string; // a Project value, but the server is the source of truth
  count: number;
  source: "override" | "default" | (string & {});
}

/** GET /projects response (ProjectsOut). */
export interface ProjectsResult {
  projects: string[]; // the valid enum values, in PROJECTS order
  groups: ProjectGroup[];
}

/** PATCH /projects response (ProjectPatchOut): the new assignment + re-tag counts. */
export interface ProjectPatchResult {
  db_name: string;
  project: string;
  source: "override" | "auto"; // whether the result is a user override or auto-classification
  updated: Record<string, number>; // e.g. { notion_rows: 778, wiki: 26 }
}

/* ------------------------------- promote -------------------------------- */

export interface PromotionPackage {
  source_node_id: string;
  source_doc_id: string;
  source_owner_id: string | null;
  source_scope: "personal";
  source_title: string;
  sanitized_title: string;
  sanitized_markdown: string;
  source_meta: Record<string, unknown>;
  target_project: string;
}

export interface PromotionAuditEvent {
  audit_id: number;
  node_run_id: string;
  node: string;
  phase: string;
  status: "ok" | "error";
  occurred_at: string;
  meta: Record<string, unknown>;
  error_class: string | null;
  error_message: string | null;
}

export interface PromotionStage extends PromotionPackage {
  stage_id: string;
  status: "pending" | "approved" | "rejected";
  created_by: string;
  approved_by: string | null;
  promoted_doc_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  decided_at: string | null;
  audit_events: PromotionAuditEvent[];
}

/* --------------------------------- board -------------------------------- */

export interface BoardCard {
  row_id: string;
  title: string;
  status: string; // "Todo" | "In Progress" | "Review" | "Done"
  notion_status: string | null;
  assignee: string | null;
  priority: string | null;
  due_at: string | null;
  is_local_override: boolean;
  writeback_status: "synced" | "disabled" | "failed" | "local_only" | null;
  writeback_error: string | null;
}

export interface BoardResponse {
  columns: string[];
  cards: BoardCard[];
}

/* -------------------------- personal workspace board -------------------------- */

export interface PersonalBoardProject {
  project_id: string;
  name: string;
  color: string | null;
  // 'personal' = 사적 채널(나만 봄), 'company' = 담당 회사 프로젝트 채널(회사 공개).
  kind?: "personal" | "company" | string;
  company_project_id?: string | null;
}

export interface PersonalBoardFolder {
  folder_id: string;
  name: string;
  kind: "system" | "custom" | string;
  order_index: number;
}

export interface PersonalBoardIntegration {
  integration_id: string;
  kind: string;
  label: string;
  enabled: boolean;
  has_notification: boolean;
  order_index: number;
}

export interface PersonalBoardSubtask {
  subtask_id: string;
  task_id: string;
  title: string;
  completed: boolean;
  order_index: number;
}

export interface PersonalBoardTask {
  task_id: string;
  title: string;
  status: "open" | "done" | "archived" | string;
  completed: boolean;
  priority: "urgent" | "priority" | "normal" | "low" | string;
  scheduled_date: string | null;
  scheduled_time: string | null;
  // Optional end time of the scheduled block (start = scheduled_time). Only set
  // when scheduled_time is set and end > start. null = open-ended / single time.
  scheduled_end_time: string | null;
  // '복귀불가' — person is not coming back. Independent of scheduled_end_time but
  // set in the same end-time picker. Defaults to false.
  no_return: boolean;
  due_date: string | null;
  due_time: string | null;
  backlog_bucket_id: string | null;
  order_index: number;
  source_kind: string;
  source_label: string | null;
  // 'personal' = 소유자 전용, 'company' = 회사 프로젝트 채널 업무(회사 구성원 전체 가시).
  scope?: "personal" | "company" | string;
  company_project_id?: string | null;
  // 티켓 상세의 자유 노트(서버 영속). null/미존재 = 없음.
  note?: string | null;
  project: PersonalBoardProject | null;
  subtasks: PersonalBoardSubtask[];
}

// 티켓 상세 팝업 댓글(서버 영속).
export interface PersonalTaskComment {
  comment_id: string;
  task_id: string;
  user_id: string;
  author_name: string | null;
  body: string;
  created_at: string;
}

export interface PersonalBoardFixedEvent {
  event_id: string;
  title: string;
  starts_at: string;
  ends_at: string;
  source_kind: string;
  source_label: string | null;
  project: PersonalBoardProject | null;
}

export interface PersonalBoardNote {
  note_id: string;
  note_date: string;
  kind: "note" | "incident" | "decision" | string;
  title: string;
  body: string;
  order_index: number;
  created_at: string;
}

export interface PersonalBoardBacklogBucket {
  bucket_id: string;
  key: string;
  label: string;
  badge: string;
  color: string;
  order_index: number;
  collapsed: boolean;
  tasks: PersonalBoardTask[];
}

export interface PersonalBoardDay {
  date: string;
  tasks: PersonalBoardTask[];
  fixed_events: PersonalBoardFixedEvent[];
  notes: PersonalBoardNote[];
}

export interface PersonalBoardPreferences {
  selected_date: string;
  right_panel: string;
  active_integration: string | null;
  filter_mode: string;
  sort_mode: string;
}

export interface PersonalBoardBootstrap {
  node_id: string;
  workspace_id: string;
  workspace_name: string;
  timezone: string;
  selected_date: string;
  folders: PersonalBoardFolder[];
  days: PersonalBoardDay[];
  backlog_buckets: PersonalBoardBacklogBucket[];
  integrations: PersonalBoardIntegration[];
  projects: PersonalBoardProject[];
  preferences: PersonalBoardPreferences;
}

// P10 개인 알림 — 위임/외부 생성 보드 태스크의 알림 피드(개인 네비 "알림" 탭).
// 서버 endpoint는 병행 PR로 추가된다: 미배포(404)면 FE는 빈 상태/무배지로
// 조용히 degrade해야 한다.
export interface PersonalBoardNotifyState {
  total: number;
  latest_at: string | null;
  latest_title: string | null;
}

export interface PersonalBoardNotificationItem {
  task_id: string;
  title: string;
  created_at: string;
  source_kind: string;
  source_label: string | null;
  status: string;
  backlog_bucket_id: string | null;
  scheduled_date: string | null;
}

export interface PersonalBoardNotificationList {
  items: PersonalBoardNotificationItem[];
  total: number;
}

export interface PersonalTaskCreateInput {
  title: string;
  scheduled_date?: string;
  scheduled_time?: string | null;
  scheduled_end_time?: string | null;
  due_date?: string | null;
  due_time?: string | null;
  backlog_bucket_id?: string;
  priority?: string;
  project_id?: string | null;
  // Optional client-generated id for offline-first creation (idempotent server upsert).
  task_id?: string;
}

export interface PersonalTaskPatchInput {
  title?: string;
  status?: string;
  priority?: string;
  project_id?: string | null;
  scheduled_date?: string | null;
  scheduled_time?: string | null;
  scheduled_end_time?: string | null;
  due_date?: string | null;
  due_time?: string | null;
  backlog_bucket_id?: string | null;
  order_index?: number | null;
  // 티켓 노트. ""/null = 비우기, 미전송 = 유지.
  note?: string | null;
}

export interface PersonalFolderCreateInput {
  name: string;
}

export interface PersonalSubtaskCreateInput {
  title: string;
  // Optional client-generated id for offline-first creation (idempotent server upsert).
  subtask_id?: string;
}

export interface PersonalSubtaskPatchInput {
  title?: string;
  completed?: boolean;
}

export interface PersonalFixedEventCreateInput {
  title: string;
  starts_at: string;
  ends_at: string;
  project_id?: string | null;
  source_kind?: string;
  source_label?: string | null;
}

export interface PersonalFixedEventPatchInput {
  title?: string;
  starts_at?: string;
  ends_at?: string;
  project_id?: string | null;
}

export interface PersonalNoteCreateInput {
  note_date: string;
  kind?: string;
  title: string;
  body: string;
}

export interface PersonalProjectCreateInput {
  name: string;
  color?: string | null;
}

export interface PersonalProjectPatchInput {
  name?: string;
  color?: string | null;
}

export interface PersonalBacklogBucketPatchInput {
  collapsed?: boolean;
}

export interface PersonalPreferencesPatchInput {
  selected_date?: string;
  right_panel?: string;
  active_integration?: string | null;
  filter_mode?: string;
  sort_mode?: string;
}

export interface DayAllocation {
  weekday: number;
  minutes: number | null;
  // Per-day position so an objective card can interleave with tasks on Home.
  order?: number | null;
  // Per-day note (source of truth for the day-card detail).
  note?: string | null;
}

export interface ObjectiveSubtask {
  subtask_id: string;
  title: string;
  completed: boolean;
  order_index: number;
  weekday: number;
}

export interface ObjectiveComment {
  comment_id: string;
  user_id: string;
  author_name: string | null;
  body: string;
  weekday: number;
  created_at: string;
}

export interface PersonalWeeklyObjective {
  objective_id: string;
  week_start: string;
  title: string;
  project_id: string | null;
  project: PersonalBoardProject | null;
  day_allocations: DayAllocation[];
  completed: boolean;
  order_index: number;
  note: string | null;
  // 'manual' | 'company_plan'. company_plan = 회사 주간계획 담당 항목에서 자동 생성된
  // '회사 부여' 목표. FE는 이걸로 배지 표시 + 제목수정/삭제를 막는다.
  source_kind?: string;
  created_at: string;
  subtasks: ObjectiveSubtask[];
  comments: ObjectiveComment[];
}

export interface PresenceItem {
  user_id: string;
  name: string;
  color: string | null;
  area: string;
  project_id: string | null;
  section: string | null;
}

export interface AssignedCompanyItem {
  item_id: string;
  text: string;
  done: boolean;
  project: string | null;
  project_id: string | null;
  week_start: string;
}

export interface WeeklyPlanResponse {
  week_start: string;
  objectives: PersonalWeeklyObjective[];
  /** 회사 계획·회고에서 본인에게 부여된 항목(읽기전용). */
  assigned_company_items?: AssignedCompanyItem[];
}

export interface PersonalObjectiveCreateInput {
  week_start: string;
  title: string;
  project_id?: string | null;
}

export interface PersonalObjectivePatchInput {
  title?: string;
  project_id?: string | null;
  day_allocations?: DayAllocation[] | null;
  completed?: boolean;
  order_index?: number;
  note?: string | null;
}

export interface ObjectiveSubtaskCreateInput {
  title: string;
  weekday: number;
}

export interface ObjectiveSubtaskPatchInput {
  title?: string;
  completed?: boolean;
  order_index?: number;
}

export interface ObjectiveCommentCreateInput {
  body: string;
  weekday: number;
}

export interface WeeklyReviewDay {
  date: string;
  weekday: number;
  done_count: number;
  open_count: number;
  done_tasks: PersonalBoardTask[];
}

export interface WeeklyReviewProject {
  project_id: string | null;
  project_name: string | null;
  color: string | null;
  done_count: number;
}

export interface WeeklyReviewResponse {
  week_start: string;
  total_done: number;
  total_open: number;
  daily: WeeklyReviewDay[];
  by_project: WeeklyReviewProject[];
}

export interface PersonalDaysResponse {
  days: PersonalBoardDay[];
}

/* ------------------------------ connectors ----------------------------- */

export interface ConnectorManifest {
  slug: string;
  label: string;
  source_kind: string;
  account_kinds: string[];
  auth_modes: string[];
  capabilities: string[];
  default_interval_seconds: number | null;
  default_daily_budget: number | null;
  privacy_class: string;
  redaction_profile: string;
  import_mode: string;
  description: string;
  settings_keys: string[];
  can_ensure_default: boolean;
  default_configured: boolean;
  config_error: string | null;
  config_fields: ConnectorConfigField[];
}

export interface ConnectorConfigField {
  key: string;
  label: string;
  kind: "secret" | "text" | "number";
  required: boolean;
  placeholder: string;
  default_value: string | number | string[] | null;
}

export interface ConnectorAccount {
  account_id: string;
  connector_slug: string;
  account_kind: string;
  node_id: string;
  scope: string;
  owner_id: string | null;
  project: string | null;
  auth_mode: string;
  account_label: string | null;
  status: string;
  settings_redacted: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  last_sync_at: string | null;
  last_error: string | null;
  device_id?: string;
}

export interface ConnectorRun {
  run_id: string;
  account_id: string;
  connector_slug: string;
  reason: string;
  status: string;
  fetched: number;
  created: number;
  updated: number;
  skipped: number;
  errors: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ConnectorDocument {
  doc_id: string;
  account_id: string | null;
  connector_slug: string;
  title: string;
  source: string;
  source_external_id: string | null;
  source_last_edited_at: string | null;
  updated_at: string | null;
}

export interface ConnectorsResponse {
  node_kind: string;
  node_id: string;
  manifests: ConnectorManifest[];
  accounts: ConnectorAccount[];
  runs: ConnectorRun[];
  documents: ConnectorDocument[];
}

export interface ConnectorAccountDeleteResult {
  account_id: string;
  deleted: boolean;
}

export interface ConnectorReport {
  created: number;
  updated: number;
  skipped: number;
  errors: number;
  total: number;
  doc_ids: string[];
}

export interface ConnectorSyncResult {
  account_id: string;
  connector_slug: string;
  run_id: string;
  status: string;
  report: ConnectorReport | null;
  error_message: string | null;
}

export type ConnectorScope = "company" | "personal";

export type CollectorCommandKind = "connector_sync" | "raw_repush";
export type CollectorCommandStatus = "pending" | "claimed" | "done" | "failed";

export interface CollectorCommand {
  command_id: string;
  node_id: string;
  user_id: string;
  kind: CollectorCommandKind;
  payload: Record<string, unknown>;
  status: CollectorCommandStatus;
  result: Record<string, unknown> | null;
  created_at: string;
  claimed_at: string | null;
  completed_at: string | null;
}

export interface CollectorCommandList {
  count: number;
  items: CollectorCommand[];
}

export interface CollectorTokenRecord {
  token_id: string;
  node_id: string;
  name: string;
  scopes: string[];
  created_at: string | null;
  last_used_at: string | null;
  last_polled_at: string | null;
  last_status_at: string | null;
  scheduler_installed: boolean | null;
  scheduler_loaded: boolean | null;
  scheduler_interval_seconds: number | null;
  last_status_error: string | null;
  revoked_at: string | null;
  device_id: string;
}

export interface CollectorTokenList {
  count: number;
  items: CollectorTokenRecord[];
}

export interface CollectorTokenIssueInput {
  name: string;
  scopes?: string[];
  device_label?: string;
}

export interface CollectorTokenIssueResponse {
  token: string;
  item: CollectorTokenRecord;
}

export interface CollectorEvidenceWikiPage {
  slug: string;
  title: string;
  scope: "personal";
}

export interface CollectorEvidenceDocument {
  doc_id: string;
  title: string;
  source: string;
  source_account_id: string | null;
  source_external_id: string | null;
  source_last_edited_at: string | null;
  updated_at: string | null;
  wiki_source_slug: string | null;
  wiki_pages: CollectorEvidenceWikiPage[];
}

export interface CollectorEvidenceCompile {
  indexed: number;
  authored: number;
  skipped: number;
  failed: number;
}

export type CollectorLivenessStatus = "unconfigured" | "live" | "stale" | "offline";

export interface CollectorLiveness {
  status: CollectorLivenessStatus;
  checked_at: string;
  last_seen_at: string | null;
  last_polled_at: string | null;
  last_status_at: string | null;
  active_token_count: number;
  revoked_token_count: number;
  pending_command_count: number;
  claimed_command_count: number;
  oldest_pending_command_at: string | null;
  oldest_claimed_command_at: string | null;
  scheduler_installed: boolean | null;
  scheduler_loaded: boolean | null;
  scheduler_interval_seconds: number | null;
  last_status_error: string | null;
  reason: string;
}

export interface CollectorEvidenceSource {
  source: string;
  document_count: number;
  recent_documents: CollectorEvidenceDocument[];
  latest_command: CollectorCommand | null;
  latest_compile: CollectorEvidenceCompile | null;
}

export interface CollectorEvidenceResponse {
  owner_id: string;
  liveness: CollectorLiveness;
  sources: CollectorEvidenceSource[];
  recent_commands: CollectorCommand[];
}

export interface CollectorCompileResponse {
  indexed: number;
  authored: number;
  skipped: number;
  failed: number;
}

export interface CollectorPersonalDataDeleteResponse {
  owner_id: string;
  documents_deleted: number;
  connector_items_deleted: number;
  structured_rows_deleted: number;
  corpus_chunks_deleted: number;
  corpus_embeddings_deleted: number;
  wiki_items_deleted: number;
}

export interface CollectorCommandCreateInput {
  kind: CollectorCommandKind;
  payload?: Record<string, unknown>;
}

/* ---------------------------------- auth -------------------------------- */

export interface AuthConfig {
  auth_mode: string;
  node_kind: string;
  node_id: string;
  provider: string;
  provider_configured: boolean;
  dev_login_enabled: boolean;
  login_url: string;
  email_domain: string;
  /** Cross-node session bridge role: "hub" | "rp" | "off". */
  sso_role: string;
  /** P8.5: when true, central merges the signed-in user's own personal data into
   *  company-node `/ask`, `/wiki`, and `/board` views. Server stays the source of
   *  truth — FE only uses this to surface the scope selector and board toggle. */
  owner_scope_enabled?: boolean;
}

export interface AuthMe {
  authenticated: boolean;
  auth_mode: string;
  user_id: string;
  display_name: string;
  email: string | null;
  role: string | null;
  node_id: string;
}

export interface AllowlistEntry {
  allowlist_id: string;
  node_id: string;
  email: string;
  role: "owner" | "admin" | "member" | "viewer" | string;
  created_by: string | null;
  created_at: string | null;
  revoked_at: string | null;
}

export interface MagicLinkRequestResult {
  accepted: boolean;
  delivery: string;
  email_domain: string;
}

/* ----------------------------- dashboard -------------------------------- */

export interface TeamMember {
  member_id: string;
  name: string;
  title: string | null;
  department: string | null;
  email: string | null;
  phone: string | null;
  join_date: string | null;
  birthday: string | null;
  address: string | null;
  emergency_contact: string | null;
  bank_account: string | null;
  color: string | null;
  bio: string | null;
  sort_order: number;
  active: boolean;
  // 출력 전용: 로그인 계정 연결 여부(linked) / 로그인 가능하게 초대(allowlist)됐는지(invited).
  user_id?: string | null;
  linked?: boolean;
  invited?: boolean;
}
export type TeamMemberInput = Omit<
  TeamMember,
  "member_id" | "user_id" | "linked" | "invited"
>;

export interface RecruitingCandidate {
  candidate_id: string;
  name: string;
  role: string | null;
  education: string | null;
  phone: string | null;
  email: string | null;
  linkedin: string | null;
  send_status: string;
  note: string | null;
  sort_order: number;
}
export type RecruitingCandidateInput = Omit<RecruitingCandidate, "candidate_id">;

export interface RecruitingComment {
  comment_id: string;
  candidate_id: string;
  author_member_id: string | null;
  author_name: string;
  body: string;
  created_at: string;
}
export interface RecruitingCommentInput {
  author_member_id: string | null;
  author_name: string;
  body: string;
}

// KPI (OKR + North Star)
export type KpiLevel = "north_star" | "objective" | "key_result" | "target";
export type KpiCadence = "annual" | "quarterly" | "monthly";
export type KpiMetricType = "number" | "percent" | "currency" | "count" | "boolean";
export type KpiStatus = "on_track" | "at_risk" | "off_track" | "done" | "archived";

export interface Kpi {
  kpi_id: string;
  parent_id: string | null;
  level: KpiLevel;
  cadence: KpiCadence;
  period_start: string | null;
  fiscal_year: number;
  quarter: number | null;
  month: number | null;
  title: string;
  description: string | null;
  metric_type: KpiMetricType;
  unit: string | null;
  baseline: number | null;
  target: number | null;
  current_value: number | null;
  direction: "up" | "down";
  project_id: string | null;
  owner_member_id: string | null;
  status: KpiStatus;
  sort_order: number;
  progress: number | null;
  /** 주기말 공식 채점 0~10. null=미채점 — 0은 유효(항상 != null 비교). */
  grade: number | null;
  grade_note: string | null;
  graded_at: string | null;
  graded_by_member_id: string | null;
  /** 마감 진실: 채점 있으면 grade/10, 없으면 측정 progress. */
  final_progress: number | null;
}
export interface KpiTreeNode extends Kpi {
  children: KpiTreeNode[];
}
export type KpiInput = Omit<
  Kpi,
  | "kpi_id"
  | "period_start"
  | "progress"
  | "grade"
  | "grade_note"
  | "graded_at"
  | "graded_by_member_id"
  | "final_progress"
>;

export interface KpiCheckin {
  checkin_id: string;
  kpi_id: string;
  value: number | null;
  status: KpiStatus | null;
  note: string | null;
  author_member_id: string | null;
  checkin_date: string;
  created_at: string;
}
export interface KpiCheckinInput {
  value: number | null;
  status: KpiStatus | null;
  note: string | null;
  author_member_id: string | null;
}

export interface KpiSummary {
  fiscal_year: number;
  north_star: Kpi | null;
  objectives: number;
  key_results: number;
  on_track: number;
  at_risk: number;
  off_track: number;
  /** final_progress(채점 우선) 기준 평균. */
  avg_progress: number | null;
  targets_graded: number;
  targets_total: number;
  kr_graded: number;
}

/** KR 주간 신뢰도(1~10) — (kpi, 일요일 주) 당 1값. */
export interface KpiConfidence {
  confidence_id: string;
  kpi_id: string;
  week_start: string;
  confidence: number;
  note: string | null;
  author_member_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface KpiConfidencePoint {
  week_start: string;
  confidence: number;
}
export interface KpiRetroItem {
  kpi: Kpi;
  section: "kr_confidence" | "month_target" | "quarter_kr" | "annual_kr";
  this_week_recorded: boolean;
  confidence_series: KpiConfidencePoint[];
  linked_total: number;
  linked_done: number;
  linked_scored: number;
  linked_score_avg: number | null;
  child_grade_avg: number | null;
  /** 제안 점수(표시 전용) — 0도 유효한 제안(!= null 비교). */
  suggested_grade: number | null;
}
export interface KpiRetroCard {
  mode: "weekly" | "monthly";
  ref: string;
  review_start: string | null;
  review_end: string | null;
  confidence_krs: KpiRetroItem[];
  grade_targets: KpiRetroItem[];
  grade_krs: KpiRetroItem[];
  unrecorded_kr_count: number;
  ungraded_past_count: number;
}

export interface KpiLinked {
  project_id: string | null;
  weekly_plan_count: number;
  weekly_plan_done: number;
  monthly_plan_count: number;
  monthly_plan_done: number;
}

export interface KpiWeeklyItem {
  entry_id: string;
  project_id: string;
  week_start: string;
  item_id: string;
  text: string;
  done: boolean;
}

export interface TeamSyncResult {
  total: number;
  created: number;
  updated: number;
  skipped: number;
}

export interface DashboardProject {
  project_id: string;
  name: string;
  color: string | null;
  description: string | null;
  /** 노션식 자유 본문(BlockNote 블록 JSON 문자열). description은 목록 요약용. */
  body: string | null;
  status: string | null;
  sort_order: number;
  active: boolean;
  /** 하위 프로젝트: 부모 project_id. 루트는 null. 깊이 2단계(루트→하위). */
  parent_project_id: string | null;
  /** SE 단계 (srr|sdr|pdr|cdr|vnv|ops). null=SE 미적용. */
  se_stage: string | null;
  /** 트래킹: 기간(시작/목표일 ISO date), 오너(DRI, team_members.member_id), 건강 신호. */
  start_date: string | null;
  target_date: string | null;
  owner_member_id: string | null;
  health: "on_track" | "at_risk" | "off_track" | null;
  updated_at: string | null;
}
export interface DashboardProjectInput {
  name: string;
  color: string | null;
  description?: string | null;
  body?: string | null;
  status?: string | null;
  sort_order: number;
  active: boolean;
  parent_project_id?: string | null;
  se_stage?: string | null;
  start_date?: string | null;
  target_date?: string | null;
  owner_member_id?: string | null;
  health?: "on_track" | "at_risk" | "off_track" | null;
}
/** 프로젝트 활동 피드 한 줄 (필드 변경·요구조건·배정·보드 행). */
export interface ProjectActivityItem {
  activity_id: string;
  actor_user_id: string | null;
  actor_name: string | null;
  entity_type: "project" | "requirement" | "assignment" | "row";
  entity_id: string | null;
  action: string;
  field: string | null;
  before: string | null;
  after: string | null;
  created_at: string;
}
/** 업로드 진행 콜백 페이로드 (퍼센트·속도·남은시간). */
export interface UploadProgress {
  loaded: number;
  total: number;
  percent: number;
  /** bytes/sec (EMA). 초기엔 0. */
  rateBps: number;
  /** 남은 초. 속도 표본 전엔 null. */
  etaSeconds: number | null;
}
/** 프로젝트 SE 요구조건 (제약조건/목표, SRR 대장). */
export interface ProjectRequirement {
  requirement_id: string;
  project_id: string;
  num: number;
  kind: "constraint" | "goal";
  text: string;
  verify_method: string | null;
  notes: string | null;
  status: "open" | "verifying" | "satisfied" | "failed";
  /** 상위 프로젝트 요구조건에서 flow-down된 경우 원본 requirement_id. */
  parent_requirement_id: string | null;
}
export interface ProjectRequirementInput {
  kind: "constraint" | "goal";
  text: string;
  verify_method?: string | null;
  notes?: string | null;
  status?: "open" | "verifying" | "satisfied" | "failed";
}
/** 프로젝트별 SE 요구조건 충족 현황. */
export interface RequirementSummary {
  project_id: string;
  total: number;
  satisfied: number;
  verifying: number;
  failed: number;
}
/** 프로젝트 진행률 — 소속 데이터베이스 status 속성(complete 그룹) 집계. */
export interface ProjectProgress {
  project_id: string;
  total: number;
  complete: number;
  in_progress: number;
}
/** 팀원이 회사 프로젝트 채널(내 보드)에 올린 회사 공개 업무 — 회사 데이터로 조회. */
export interface ProjectBoardTask {
  task_id: string;
  title: string;
  status: string;
  completed: boolean;
  priority: string;
  scheduled_date: string | null;
  due_date: string | null;
  owner_user_id: string | null;
  owner_name: string | null;
}

export interface DashboardPage {
  page_id: string;
  title: string;
  icon: string | null;
  /** BlockNote 블록 JSON 문자열 본문. */
  body: string | null;
  /** 'memo' = 메모 목록 최상위, 'page' = 본문 안 중첩 하위 페이지. */
  kind: string;
  /** 메모 소유자 user_id. NULL = 레거시 회사 공용. */
  owner_id?: string | null;
}

export interface DashboardPageSummary {
  page_id: string;
  title: string;
  icon: string | null;
  kind: string;
  updated_at: string | null;
  owner_id?: string | null;
  /** Capped plain-text body snippet for note-list rows. */
  preview?: string | null;
}

/* ---- 메모 공유 + 얼라인 확인 + 프로젝트 링크 (docs/personal-notes.md) ---- */
export interface NoteShare {
  share_id: string;
  page_id: string;
  shared_with: string;
  shared_with_name: string;
  shared_with_color: string | null;
  permission: "view" | "edit";
  created_by: string;
  acknowledged_at: string | null;
  created_at: string | null;
}

export interface NoteProjectLink {
  project_id: string;
  name: string;
  color: string | null;
}

export interface SharedNoteSummary {
  page_id: string;
  title: string;
  icon: string | null;
  updated_at: string | null;
  owner_id: string | null;
  owner_name: string | null;
  permission: "view" | "edit";
  acknowledged_at: string | null;
}

export interface NoteLibraryItem {
  page_id: string;
  title: string;
  icon: string | null;
  preview: string | null;
  updated_at: string | null;
  owner_id: string | null;
  owner_name: string | null;
  access: "owner" | "edit" | "view";
  permission: "edit" | "view" | null;
  acknowledged_at: string | null;
  projects: NoteProjectLink[];
  share_count: number;
  acknowledged_count: number;
}

/* ---- 프로젝트 인라인 데이터베이스 (노션식 표/칸반) ---- */
export type DbPropType =
  | "title"
  | "text"
  | "number"
  | "select"
  | "status"
  | "multi_select"
  | "date"
  | "checkbox"
  | "person"
  | "url"
  | "files"
  | "created_time"
  | "last_edited_time";

export interface DbSelectOption {
  id: string;
  name: string;
  color: string;
  /** status 옵션의 컬럼 그룹(to_do|in_progress|complete). 보드 컬럼 정렬용. */
  group?: string | null;
}
export interface DbPropertyDef {
  id: string;
  name: string;
  type: DbPropType;
  options: DbSelectOption[];
}
export interface DbFileValue {
  id: string;
  name: string;
  type: "image" | "video" | "file";
  mime?: string | null;
  size?: number | null;
  url?: string | null;
  dataUrl?: string | null;
  storage?: "database" | "external" | "inline" | null;
}
export interface DbViewFilterDef {
  id: string;
  prop_id: string;
  op: "contains" | "is" | "is_not" | "is_empty" | "is_not_empty" | "checked" | "unchecked";
  value?: unknown;
}
export interface DbViewSortDef {
  id: string;
  prop_id: string;
  direction: "asc" | "desc";
}
export interface DbViewDef {
  id: string;
  name: string;
  type: "table" | "board";
  group_by: string | null;
  cover_property: string | null;
  hidden: string[];
  filters: DbViewFilterDef[];
  sorts: DbViewSortDef[];
  column_widths: Record<string, number>;
}
export interface ProjectDatabase {
  database_id: string;
  project_id: string | null;
  title: string;
  icon: string | null;
  properties: DbPropertyDef[];
  views: DbViewDef[];
}
export interface ProjectDatabaseRow {
  row_id: string;
  database_id: string;
  /** property id → 값 맵. 값 타입은 속성 타입에 따른다(문자열/숫자/불리언/배열). */
  props: Record<string, unknown>;
  sort_order: number;
  /** 노션처럼 행 = 페이지: icon + cover + 본문(BlockNote JSON) + 시스템 시각. */
  icon?: string | null;
  cover?: string | null;
  body?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}
export interface ProjectDatabaseBundle {
  database: ProjectDatabase;
  /** 기본은 body 포함. `{ body: "none" }` opt-in 목록 조회만 body를 생략한다(null)
   *  — 그 경우 단건 행 조회로 지연 로드. */
  rows: ProjectDatabaseRow[];
}
/** 행 select 없는 경량 요약 — 링크 행/브레드크럼용(신규 endpoint, 구 API는 404). */
export interface ProjectDatabaseMeta {
  database_id: string;
  project_id: string | null;
  title: string;
  icon: string | null;
  row_count: number;
}

/** 업로드된 대시보드 미디어 자산(이미지/동영상/파일). url은 즉시 렌더 가능한 절대 경로. */
export interface DashboardMediaAsset {
  url: string;
  name: string;
  content_type: string;
  size: number;
}

export interface ProjectAssignment {
  assignment_id: string;
  project_id: string;
  member_id: string;
  project_name: string | null;
  member_name: string | null;
  role: string | null;
  sort_order: number;
}
export interface AssignmentInput {
  project_id: string;
  member_id: string;
  role: string | null;
  sort_order?: number;
}

/** 노드별 역할 옵션(노션 선택 속성처럼 추가/이름변경/색/삭제). */
export interface ProjectRole {
  role_id: string;
  name: string;
  color: string | null;
  sort_order: number;
}
export interface ProjectRoleInput {
  name: string;
  color?: string | null;
}
export interface ProjectRolePatch {
  name?: string;
  color?: string | null;
  sort_order?: number;
}

export interface PartnerCompany {
  partner_id: string;
  name: string;
  org_type: string | null;
  address: string | null;
  representative: string | null;
  project_ids: string[];
  field_tags: string[];
  status: string | null;
  memo: string | null;
  next_action: string | null;
  last_contact: string | null;
  link: string | null;
  sort_order: number;
  active: boolean;
  contact_count: number;
}
export type PartnerCompanyInput = Omit<
  PartnerCompany,
  "partner_id" | "contact_count"
>;

export interface SupportProgram {
  program_id: string;
  name: string;
  status: string;
  project_id: string | null;
  project_name: string | null;
  company: string | null;
  deadline: string | null;
  presentation_deadline: string | null;
  presentation_date: string | null;
  /** "HH:MM[:SS]" 발표 시간 — 있으면 팀 일정에도 시간 일정으로 등록된다. */
  presentation_time: string | null;
  owner_member_id: string | null;
  owner_name: string | null;
  calendar_event_id: string | null;
  task_number: string | null;
  url: string | null;
  follow_up: string | null;
  body: string | null;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
}
export type SupportProgramInput = Omit<
  SupportProgram,
  | "program_id"
  | "project_name"
  | "owner_name"
  | "calendar_event_id"
  | "created_at"
  | "updated_at"
>;

export interface SupportNote {
  note_id: string;
  kind: "tip" | "site";
  title: string;
  description: string | null;
  url: string | null;
  body: string | null;
  sort_order: number;
}
export type SupportNoteInput = Omit<SupportNote, "note_id">;

export interface PartnerContact {
  contact_id: string;
  partner_id: string;
  name: string;
  role: string | null;
  phone: string | null;
  email: string | null;
  channels: string[];
  link: string | null;
  memo: string | null;
  is_primary: boolean;
  sort_order: number;
}
export type PartnerContactInput = Omit<
  PartnerContact,
  "contact_id" | "partner_id"
>;

export interface PlanItem {
  id: string;
  text: string;
  done: boolean;
  /** Notion-style rich detail (BlockNote blocks JSON string). */
  detail?: string | null;
  /** Per-project sub-column tag (e.g. AX's "대본"/"아트인"); null = single column. */
  group?: string | null;
  /** 담당자(팀원 id). 부여되면 그 사람 개인 보드 주간 플랜에 반영된다. */
  assignee_member_id?: string | null;
  /** 연결된 KPI(dashboard_kpis.kpi_id). 주간 실행을 월간/연간 KPI에 엮는다. */
  kpi_id?: string | null;
  /** 계획 점검 달성 점수 0~10. null/undefined=미채점 — 0은 유효(항상 != null 비교,
   * `score || …` 금지: 0이 사라진다). */
  score?: number | null;
}

/** 담당자가 회사 할당 항목을 개인 주간 목표로 받아 진행한 정도(집계만, read-only).
 * 서브태스크 제목 등 사적 내용은 담지 않는다 — 개인 보드 진행률-only carve-out. */
export interface ItemProgress {
  has_objective: boolean;
  subtasks_total: number;
  subtasks_done: number;
  completed: boolean;
}

export interface WeeklyEntry {
  entry_id: string | null;
  project_id: string;
  week_start: string;
  plan_items: PlanItem[];
  retro_items: PlanItem[];
  /** Loaded row timestamp; echo back as base_updated_at on save (intentional-clear guard). */
  updated_at?: string | null;
  /** plan_item.id -> 담당자 진행률(집계). read-only; 저장하지 않는다. */
  assignee_progress?: Record<string, ItemProgress>;
}

export interface MonthlyEntry {
  entry_id: string | null;
  project_id: string;
  month: string;
  plan_items: PlanItem[];
  retro_items: PlanItem[];
  updated_at?: string | null;
}

/** Pre-overwrite snapshot of a weekly/monthly entry (newest first). */
export interface EntryHistory {
  period: string;
  plan_items: PlanItem[];
  retro_items: PlanItem[];
  prev_updated_at: string | null;
  snapshot_at: string | null;
}

export interface PeriodSummary {
  period: string;
  plan_count: number;
  plan_done: number;
  retro_count: number;
  /** 달성 점수 집계(0~10 스케일). */
  plan_scored: number;
  plan_score_avg: number | null;
}

export type CalendarEventType =
  | "event"
  | "meeting"
  | "vacation"
  | "wfh"
  | "holiday"
  | "deadline"
  | "dev"
  | "update";

export type CalendarRepeatFreq = "daily" | "weekly" | "biweekly" | "monthly";

export interface CalendarEvent {
  event_id: string;
  member_ids: string[];
  title: string;
  description: string | null;
  all_day: boolean;
  event_date: string;
  end_date: string | null;
  start_time: string | null;
  end_time: string | null;
  // "복귀불가" — 이 일정 뒤 담당자가 복귀하지 않음을 팀에 표시(UI는 집 이모지).
  no_return: boolean;
  // "복귀 시간" — 복귀하는 경우의 복귀 시각("HH:MM:SS" 또는 null). no_return과 상호 배타적.
  return_time: string | null;
  // 반복(루틴): null=반복 없음. 서버가 조회 윈도 안의 회차로 펼쳐서 내려준다.
  repeat_freq: CalendarRepeatFreq | null;
  // weekly/biweekly 반복 요일 — 0=월 … 6=일 (Python date.weekday() 규약).
  repeat_weekdays: number[];
  repeat_until: string | null;
  // 추가 날짜: 반복 규칙과 별개로, 같은 일정을 그대로 붙인 임의 시작일(ISO) 목록.
  // 서버가 event_date·반복 회차와 함께 회차로 펼쳐서 내려준다(모든 회차가 같은
  // event_id·series_start를 공유하므로 수정·삭제는 시리즈 전체 대상).
  extra_dates: string[];
  // 반복 회차 행이면 마스터의 시작일(수정/삭제는 시리즈 전체 대상).
  series_start?: string | null;
  starts_at: string | null;
  ends_at: string | null;
  event_type: CalendarEventType;
  color: string | null;
  project_id: string | null;
  location: string | null;
}
export type CalendarEventInput = Omit<CalendarEvent, "event_id" | "series_start">;

export interface Subscription {
  sub_id: string;
  name: string;
  vendor: string | null;
  plan: string | null;
  billing_cycle: "monthly" | "yearly" | "one_time";
  amount: number;
  currency: string;
  next_billing_date: string | null;
  status: "active" | "trial" | "paused" | "canceled";
  category: string | null;
  owner_member_id: string | null;
  masked_account: string | null;
  notes: string | null;
}
export type SubscriptionInput = Omit<Subscription, "sub_id">;

export interface ApiKeyEntry {
  key_id: string;
  service_name: string;
  label: string | null;
  key_last4: string | null;
  environment: "prod" | "dev" | "staging";
  monthly_cost: number;
  currency: string;
  status: "active" | "revoked" | "expired";
  rotated_at: string | null;
  owner_member_id: string | null;
  notes: string | null;
}
export type ApiKeyInput = Omit<ApiKeyEntry, "key_id">;

export interface FinanceAccount {
  account_id: string;
  account_name: string;
  kind: "bank" | "card" | "saas" | "login" | "other";
  masked_identifier: string | null;
  owner_member_id: string | null;
  notes: string | null;
}
export type FinanceAccountInput = Omit<FinanceAccount, "account_id">;

export interface LedgerEntry {
  ledger_id: string;
  entry_date: string;
  entry_type: "revenue" | "expense";
  amount: number;
  currency: string;
  category: string | null;
  description: string | null;
  project_id: string | null;
}
export type LedgerEntryInput = Omit<LedgerEntry, "ledger_id">;

export interface FinanceSummary {
  total_revenue: number;
  total_expense: number;
  balance: number;
  monthly_subscription_cost: number;
  active_subscription_count: number;
  monthly_api_cost: number;
  monthly_burn: number;
  runway_months: number | null;
  currency: string;
}

export interface CompanyCulture {
  content: Record<string, unknown>;
}

export type InfraKind = "gpu" | "storage" | "server" | "api_key" | "dashboard" | "other";
export interface InfraResource {
  resource_id: string;
  kind: InfraKind;
  name: string;
  vendor: string | null;
  model: string | null;
  location: string | null;
  status: "active" | "idle" | "reserved" | "down" | "expired";
  capacity: number | null;
  used: number | null;
  unit: string | null;
  usage_percent: number | null;
  owner_member_id: string | null;
  link: string | null;
  notes: string | null;
  period: string | null;
  parent_id: string | null;
  unit_price: number | null;
  balance: number | null;
  provider_id: string | null;
  price_unit: string | null; // per_call | per_1m | monthly | hourly | other
  color: string | null; // graph line color (hex)
  currency: string | null; // KRW | USD
  sort_order: number;
}
export type InfraResourceInput = Omit<InfraResource, "resource_id">;

export interface InfraResourceLink {
  link_id: string;
  resource_id: string;
  project_id: string;
}

export interface InfraProvider {
  provider_id: string;
  name: string;
  balance: number | null;
  currency: string | null; // KRW | USD
  link: string | null;
  notes: string | null;
  sort_order: number;
}
export type InfraProviderInput = Omit<InfraProvider, "provider_id">;

export interface GpuSummary {
  total: number;
  used: number;
  available: number;
  down: number;
}

export interface GpuSyncResult {
  host: string;
  total: number;
  used: number;
  available: number;
  avg_util: number;
  detail: string;
}

export interface StorageSyncResult {
  nodes: number;
  detail: string;
}

export interface MlRun {
  run_id: string;
  run_name: string;
  experiment_id: string;
  experiment_name: string;
  status: string;
  start_time: number | null;
  end_time: number | null;
  metrics: Record<string, number>;
  url: string;
}

export interface MlPanel {
  configured: boolean;
  mlflow_url: string;
  grafana_url: string;
  grafana_ok: boolean;
  grafana_embed_url: string;
  error: string | null;
  runs: MlRun[];
}

export interface MeetingAttachment {
  attachment_id: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number;
  url: string;
  created_at: string | null;
}
export interface MeetingNote {
  note_id: string;
  title: string;
  project_id: string | null;
  partner_id: string | null;
  meeting_kind: "internal" | "external";
  meeting_date: string;
  attendee_ids: string[];
  body: string | null;
  source: string;
  attachments: MeetingAttachment[];
}
export type MeetingNoteInput = Omit<
  MeetingNote,
  "note_id" | "source" | "attachments"
>;

/* ------------------------------- internals ------------------------------ */

/** HTTP 오류 응답 — 호출측이 status로 분기한다(하단에서 export — 예: 배포 skew
 *  404만 fallback하고 일시 5xx는 확언으로 취급하지 않는다). */
class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// During a public-web deploy the Mac mini briefly restarts the Next/FastAPI
// upstream, so Caddy's handle_errors gate returns a 503
// {"error":"upstream_unavailable","detail":"deploying, retry shortly"} for
// requests that land in that window. The request never reaches the app, so a
// short retry (even for mutations) is safe and turns a transient blip into a
// silent reload instead of a hard "요청 실패".
const TRANSIENT_RETRY_ATTEMPTS = 4;
const TRANSIENT_RETRY_BASE_MS = 500;
const TRANSIENT_RETRY_MAX_MS = 2000;

function isSafeMethod(init?: RequestInit): boolean {
  const method = (init?.method ?? "GET").toUpperCase();
  return method === "GET" || method === "HEAD";
}

function transientBackoffMs(attempt: number): number {
  return Math.min(TRANSIENT_RETRY_BASE_MS * 2 ** (attempt - 1), TRANSIENT_RETRY_MAX_MS);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 동시에 발화된 동일 GET(예: 같은 라우트의 여러 패널/컴포넌트가 같은 endpoint를
// 부르는 경우)을 한 네트워크 호출로 합친다. 응답 캐시가 아니라 in-flight 공유만
// 한다 — settle 즉시 삭제되므로 이후 호출은 다시 네트워크로 간다. body 없는 GET 한정.
const inflightGetRequests = new Map<string, Promise<unknown>>();

// ---- auth 세션 캐시 ---------------------------------------------------------
// auth config는 배포/세션 단위로 안정적이라 resolved promise를 모듈 레벨에 캐시하고,
// /auth/me는 짧은 TTL(30초)로 캐시한다 — 라우트 전환마다 AuthGate/AuthFooter가 다시
// 부르는 왕복을 없앤다. 실패(reject)는 캐시하지 않으므로 401/redirect 시맨틱은
// request() 경로 그대로다. 로그아웃 시 clearApiSessionCaches()로 무효화한다.
let authConfigPromise: Promise<AuthConfig> | null = null;
const AUTH_ME_TTL_MS = 30_000;
let authMeCache: { promise: Promise<AuthMe>; ts: number } | null = null;

export function clearApiSessionCaches(): void {
  authConfigPromise = null;
  authMeCache = null;
}

function cachedGetAuthConfig(): Promise<AuthConfig> {
  if (!authConfigPromise) {
    const promise = request<AuthConfig>("/auth/config").catch((err) => {
      if (authConfigPromise === promise) authConfigPromise = null;
      throw err;
    });
    authConfigPromise = promise;
  }
  return authConfigPromise;
}

function cachedGetAuthMe(): Promise<AuthMe> {
  const now = Date.now();
  if (!authMeCache || now - authMeCache.ts > AUTH_ME_TTL_MS) {
    const promise = request<AuthMe>("/auth/me").catch((err) => {
      if (authMeCache?.promise === promise) authMeCache = null;
      throw err;
    });
    authMeCache = { promise, ts: now };
  }
  return authMeCache.promise;
}

function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  if (method !== "GET" || init?.body != null) {
    return performRequest<T>(path, init);
  }
  const key = `GET ${path}`;
  const pending = inflightGetRequests.get(key);
  if (pending) return pending as Promise<T>;
  const shared = performRequest<T>(path, init).finally(() => {
    inflightGetRequests.delete(key);
  });
  inflightGetRequests.set(key, shared);
  return shared;
}

async function performRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  let lastNetworkError: unknown;

  for (let attempt = 1; attempt <= TRANSIENT_RETRY_ATTEMPTS; attempt += 1) {
    let res: Response;
    try {
      res = await fetch(`${API_BASE}${path}`, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          ...demoAuthHeaders(),
          ...(init?.headers ?? {}),
        },
        cache: "no-store",
        credentials: "include",
      });
    } catch (networkError) {
      // Connection refused/reset (upstream restarting): retry safe requests only,
      // since a mutation could already have been applied before the socket died.
      lastNetworkError = networkError;
      if (isSafeMethod(init) && attempt < TRANSIENT_RETRY_ATTEMPTS) {
        await delay(transientBackoffMs(attempt));
        continue;
      }
      throw networkError;
    }

    if (!res.ok) {
      const bodyText = await res.text();
      let parsed: unknown = null;
      try {
        parsed = bodyText ? JSON.parse(bodyText) : null;
      } catch {
        /* non-JSON body — keep null */
      }
      const errorCode =
        parsed && typeof parsed === "object" ? (parsed as { error?: unknown }).error : undefined;

      // Retry the Caddy deploy gate (guaranteed not processed by the app) for any
      // method; retry other transient 503s for safe methods only.
      const transient503 =
        res.status === 503 && (errorCode === "upstream_unavailable" || isSafeMethod(init));
      if (transient503 && attempt < TRANSIENT_RETRY_ATTEMPTS) {
        await delay(transientBackoffMs(attempt));
        continue;
      }

      redirectToLoginOnUnauthorized(path, res.status);
      const detailField =
        parsed && typeof parsed === "object" ? (parsed as { detail?: unknown }).detail : undefined;
      const detail =
        typeof detailField === "string"
          ? detailField
          : parsed
            ? JSON.stringify(parsed)
            : res.statusText;
      throw new ApiError(res.status, detail);
    }

    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  // Exhausted retries on repeated network errors.
  throw lastNetworkError instanceof Error
    ? lastNetworkError
    : new Error("요청을 완료하지 못했습니다.");
}

export { ApiError };

// 언로드 중 keepalive fetch처럼 request() 헬퍼를 못 쓰는 호출자용으로 export.
export function demoAuthHeaders(): Record<string, string> {
  return DEMO_USER_ID ? { "X-User-Id": DEMO_USER_ID } : {};
}

/** 진행률(onProgress)·취소(signal)를 지원하는 XHR 업로드. fetch는 업로드 진행
 *  이벤트가 없어 XMLHttpRequest를 쓴다. 속도는 EMA, 남은시간은 rate 기반. */
function xhrUpload<T>(
  url: string,
  body: FormData | Blob,
  opts?: {
    method?: string;
    onProgress?: (p: UploadProgress) => void;
    signal?: AbortSignal;
    /** 여러 파트 업로드에서 전체 파일 기준 진행을 내보내기 위한 오프셋/총량. */
    baseLoaded?: number;
    totalOverride?: number;
    rateState?: { rate: number; lastTime: number; lastLoaded: number };
  },
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(opts?.method ?? "POST", url);
    xhr.withCredentials = true;
    for (const [k, v] of Object.entries(demoAuthHeaders())) xhr.setRequestHeader(k, v);
    const rateState = opts?.rateState ?? { rate: 0, lastTime: Date.now(), lastLoaded: 0 };
    xhr.upload.onprogress = (event) => {
      if (!opts?.onProgress || !event.lengthComputable) return;
      const loaded = (opts?.baseLoaded ?? 0) + event.loaded;
      const total = opts?.totalOverride ?? event.total;
      const now = Date.now();
      const dt = (now - rateState.lastTime) / 1000;
      if (dt > 0.2) {
        const instant = (loaded - rateState.lastLoaded) / dt;
        rateState.rate = rateState.rate ? rateState.rate * 0.7 + instant * 0.3 : instant;
        rateState.lastTime = now;
        rateState.lastLoaded = loaded;
      }
      const remaining = total - loaded;
      opts.onProgress({
        loaded,
        total,
        percent: total ? Math.min(100, Math.round((loaded / total) * 100)) : 0,
        rateBps: rateState.rate,
        etaSeconds: rateState.rate > 1 ? Math.max(0, Math.round(remaining / rateState.rate)) : null,
      });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(xhr.responseText ? (JSON.parse(xhr.responseText) as T) : (undefined as T));
        } catch {
          reject(new ApiError(xhr.status, "응답 파싱 실패"));
        }
      } else {
        const detail =
          xhr.status === 413 ? "파일이 너무 큽니다." : `업로드 실패 (${xhr.status})`;
        reject(new ApiError(xhr.status, detail));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "네트워크 오류로 업로드하지 못했습니다."));
    xhr.onabort = () => reject(new DOMException("업로드 취소", "AbortError"));
    if (opts?.signal) {
      if (opts.signal.aborted) {
        xhr.abort();
        return;
      }
      opts.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(body);
  });
}

/** 청크 업로드 파트 크기(8MB) — Cloudflare 요청 바디 상한(~100MB)을 여유 있게 피한다. */
const UPLOAD_PART_BYTES = 8 * 1024 * 1024;

function redirectToLoginOnUnauthorized(path: string, status: number): void {
  if (
    status !== 401 ||
    typeof window === "undefined" ||
    path.startsWith("/auth/") ||
    window.location.pathname.startsWith("/login")
  ) {
    return;
  }
  deactivateOfflineNoteOwner();
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

/* --------------------------------- API ---------------------------------- */

export const api = {
  /* documents */
  listDocuments: () => request<DocumentSummary[]>("/documents"),

  getDocument: (docId: string) =>
    request<DocumentDetail>(`/documents/${docId}`),

  createDocument: (body: DocumentBody) =>
    request<{ doc_id: string }>("/documents", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateDocument: (docId: string, body: DocumentBody) =>
    request<{ doc_id: string }>(`/documents/${docId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  publishDocument: (docId: string, body: DocumentBody) =>
    request<{ doc_id: string }>(`/documents/${docId}/publish`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /* corpus — company-shared inline-editable browser over `documents` */

  listCorpus: (params?: CorpusListParams) => {
    const search = new URLSearchParams();
    if (params?.source) search.set("source", params.source);
    if (params?.q) search.set("q", params.q);
    if (params?.sort) search.set("sort", params.sort);
    if (params?.limit != null) search.set("limit", String(params.limit));
    if (params?.offset != null) search.set("offset", String(params.offset));
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<CorpusListResponse>(`/corpus/documents${suffix}`);
  },

  getCorpusDocument: (docId: string) =>
    request<CorpusDocumentDetail>(`/corpus/documents/${docId}`),

  updateCorpusDocument: (docId: string, body: DocumentBody) =>
    request<{ doc_id: string }>(`/corpus/documents/${docId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /* wiki (RAG) */
  askWiki: (question: string, options?: number | WikiAskOptions) => {
    const body: Record<string, string | number> = { question };
    if (typeof options === "number") {
      body.k = options;
    } else if (options) {
      if (options.k != null) body.k = options.k;
      if (options.since) body.since = options.since;
      if (options.until) body.until = options.until;
      if (options.source) body.source = options.source;
      if (options.scope) body.scope = options.scope;
    }
    return request<WikiAnswer>("/wiki/ask", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  getWikiPage: (slug: string, options?: { scope?: "company" | "personal" | null }) =>
    request<WikiPage>(`/wiki/pages/${encodePathSlug(slug)}${scopeQuery(options?.scope)}`),

  getWikiPageAgentWork: (slug: string, options?: { scope?: "company" | "personal" | null }) =>
    request<AgentWorkWikiLinkResponse>(
      `/wiki/pages/${encodePathSlug(slug)}/agent-work${scopeQuery(options?.scope)}`,
    ),

  getWikiPageDataGaps: (slug: string, options?: { scope?: "company" | "personal" | null }) =>
    request<DataGapPageResponse>(
      `/wiki/pages/${encodePathSlug(slug)}/data-gaps${scopeQuery(options?.scope)}`,
    ),

  getWikiPageWikiTasks: (slug: string, options?: { scope?: "company" | "personal" | null }) =>
    request<WikiTaskPageResponse>(
      `/wiki/pages/${encodePathSlug(slug)}/tasks${scopeQuery(options?.scope)}`,
    ),

  // K9.2 — `includeEntity` 펼침 시 `?include=entity`로 entity 다리 노드/엣지를 합류시킨다
  // (U2: 기본은 티저만, 클릭 시 펼침). 기본 호출은 티저(entity_teaser)만 받는다.
  getWikiPageGraph: (slug: string, depth: 1 | 2 = 1, options?: { includeEntity?: boolean }) =>
    request<WikiPageGraphResponse>(
      `/wiki/pages/${encodePathSlug(slug)}/graph?depth=${depth}` +
        (options?.includeEntity ? `&include=entity` : ``),
    ),

  // K7.3 — caller 본인 personal KG footprint(공유 central 그래프에 올라간 내 노드/엣지 수).
  // owner-only, owner_id 미노출. flag-off/owner-scope off/null caller는 빈 footprint.
  getOwnerKgFootprint: () => request<OwnerFootprint>(`/wiki/kg/footprint`),

  // 전용 지식그래프 화면 — company-scope KG 집계 헤더. flag-off는 status="disabled",
  // rebuild 중은 "rebuilding", Neo4j 미가용은 "unavailable"(모두 200).
  getKgOverview: () => request<KgOverview>(`/kg/overview`),

  // 전용 지식그래프 화면 랜딩 그래프 — company 개체 co-mention 지도(검색 없이 바로 렌더).
  // flag-off/rebuild/미가용은 supported:false(200). 탐색기가 시드로 소비, 더블클릭 확장.
  getKgEntityGraph: () => request<KgEntityGraphResponse>(`/kg/graph`),

  // 전용 지식그래프 화면 시드 픽커 — compiled page/claim 검색(그래프 시작 노드 선택용).
  // owner 가시성은 서버 retrieve가 강제(personal hit는 caller 본인 것만).
  searchWiki: (query: string, options?: { limit?: number; scope?: string }) => {
    const search = new URLSearchParams({ query });
    if (options?.limit != null) search.set("limit", String(options.limit));
    if (options?.scope) search.set("scope", options.scope);
    return request<WikiSearchResponse>(`/wiki/search?${search.toString()}`);
  },

  // E2 그래프 탐색기 — 임의 (label, id) 노드 1-hop 확장(E1b `GET /wiki/graph/expand`).
  // id는 응답 KgGraphNode.id 그대로(Entity의 namespaced entity_node_key 포함 — 서버가
  // 접두를 벗겨 라우팅). 미존재/foreign/미허용 label은 404(ApiError), KG 미가용은 200
  // supported:false(fail-open — 호출부가 확장 실패 안내로 처리).
  expandWikiGraphNode: (label: string, id: string) =>
    request<WikiPageGraphResponse>(
      `/wiki/graph/expand?label=${encodeURIComponent(label)}&id=${encodeURIComponent(id)}`,
    ),

  listWikiTasks: (params?: { resolved?: boolean; kind?: string }) => {
    const search = new URLSearchParams();
    if (params?.resolved != null) search.set("resolved", String(params.resolved));
    if (params?.kind) search.set("kind", params.kind);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<WikiTask[]>(`/wiki/tasks${suffix}`);
  },

  setWikiTaskResolved: (slug: string, resolved: boolean) =>
    request<WikiTask>(`/wiki/tasks/${encodeURIComponent(slug)}`, {
      method: "PATCH",
      body: JSON.stringify({ resolved }),
    }),

  /** WTR.3 kind-aware resolve: routes through POST /wiki/tasks/{slug}/resolve.
   *  Server enforces decision↔kind constraints, required fields (merged_text iff
   *  merge, answer iff answered), and owner/admin for content-mutating decisions
   *  (use_incoming/merge/answered). Returns the updated task with resolution. */
  resolveWikiTask: (slug: string, body: WikiTaskResolveBody) =>
    request<WikiTask>(`/wiki/tasks/${encodeURIComponent(slug)}/resolve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /* assistant — unified auto-router */

  /**
   * POST /ask — pure knowledge search. Auto-routes the NL question to the
   * structured(PG) or wiki/graph backend and returns a RoutedAnswer. It does NOT
   * decompose compound questions, run the agentic loop, or intake commands — that is
   * the agent-work chat orchestrator (see `orchestrate`). A structured run the gate
   * rejects comes back as 422 carrying the SAME RoutedAnswer body (compiled SQL +
   * reason). A rejection is an expected outcome, not a transport error — read the JSON
   * body either way and never throw on 422.
   */
  ask: async (
    question: string,
    scope?: AskScope,
    contextWikiSlug?: string | null,
  ): Promise<RoutedAnswer> => {
    const body: Record<string, string> = { question };
    if (scope) body.scope = scope;
    if (contextWikiSlug) body.context_wiki_slug = contextWikiSlug;
    const res = await fetch(`${API_BASE}/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...demoAuthHeaders(),
      },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "include",
    });

    if (res.status === 200 || res.status === 422) {
      return (await res.json()) as RoutedAnswer;
    }

    let detail = res.statusText;
    try {
      detail = JSON.stringify(await res.json());
    } catch {
      /* keep statusText */
    }
    redirectToLoginOnUnauthorized("/ask", res.status);
    throw new ApiError(res.status, detail);
  },

  /**
   * POST /agent-work/chats/{id}/orchestrate — the agent-work chat orchestrator.
   * This is where compound-question decompose + single-command intake + read→act
   * live (moved off /ask). Synchronous + live SSE: open
   * GET /agent-work/chats/stream/{streamId} BEFORE awaiting this so decompose wave
   * frames render live in the chat bubble. Returns a RoutedAnswer (mode agent_work |
   * decomposed | wiki | structured | graph); a gate-rejected structured query comes
   * back as 422 with the same body — never throw on 422.
   */
  orchestrate: async (
    sessionId: string,
    opts: {
      text: string;
      scope?: AskScope;
      contextWikiSlug?: string | null;
      contextMailId?: string | null;
      streamId?: string | null;
      // 중단 버튼용 클라이언트 분리 신호. abort는 fetch만 끊는다 — 서버측
      // 오케스트레이션 취소는 시도하지 않는다(서버는 계속 돌 수 있고 결과는
      // 클라이언트에서 폐기된다). optional이라 다른 호출부는 무영향.
      signal?: AbortSignal | null;
    },
  ): Promise<RoutedAnswer> => {
    const body: Record<string, string> = { text: opts.text };
    if (opts.scope) body.scope = opts.scope;
    if (opts.contextWikiSlug) body.context_wiki_slug = opts.contextWikiSlug;
    if (opts.contextMailId) body.context_mail_id = opts.contextMailId;
    if (opts.streamId) body.stream_id = opts.streamId;
    const res = await fetch(`${API_BASE}/agent-work/chats/${sessionId}/orchestrate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...demoAuthHeaders(),
      },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "include",
      signal: opts.signal ?? undefined,
    });

    if (res.status === 200 || res.status === 422) {
      return (await res.json()) as RoutedAnswer;
    }

    let detail = res.statusText;
    try {
      detail = JSON.stringify(await res.json());
    } catch {
      /* keep statusText */
    }
    redirectToLoginOnUnauthorized("/agent-work", res.status);
    throw new ApiError(res.status, detail);
  },

  /** Inspect a past structured run from the audit log. */
  getRun: (queryId: string) =>
    request<QueryRun>(`/ask/runs/${queryId}`),

  /* data gaps — answer-insufficiency backlog */

  /** '이 답변 부족해요': record the question as a gap and get an LLM suggestion of
   *  what to add where (one LLM call, so this is intentionally on demand). */
  gapFeedback: (question: string) =>
    request<DataGap>("/gaps/feedback", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  /** Backlog rows in the caller's node scope (data owner review). */
  listGaps: (status?: DataGap["status"]) => {
    const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<DataGap[]>(`/gaps${suffix}`);
  },

  /** (Re)generate the LLM suggestion for an existing backlog row. */
  suggestGap: (gapId: string) =>
    request<DataGap>(`/gaps/${gapId}/suggest`, { method: "POST" }),

  /** Resolve / dismiss / reopen a backlog row. */
  patchGap: (gapId: string, status: DataGap["status"]) =>
    request<DataGap>(`/gaps/${gapId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  /* agent work — P3 deterministic review queue */

  listAgentWork: (params?: {
    state?: AgentWorkState;
    source_kind?: string;
    outcome?: AgentWorkOutcome;
    scope?: "personal" | "company";
    wiki_slug?: string;
  }) => {
    const search = new URLSearchParams();
    if (params?.state) search.set("state", params.state);
    if (params?.source_kind) search.set("source_kind", params.source_kind);
    if (params?.outcome) search.set("outcome", params.outcome);
    if (params?.scope) search.set("scope", params.scope);
    if (params?.wiki_slug) search.set("wiki_slug", params.wiki_slug);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<AgentWorkItem[]>(`/agent-work${suffix}`);
  },

  getAgentWorkItem: (workId: string) =>
    request<AgentWorkItem>(`/agent-work/${workId}`),

  /** S4-4a batch fetch: poll several queued fragment work_ids in ONE owner-scoped
   *  roundtrip (GET /agent-work?ids=<csv uuid>). Drives the command-split chip live
   *  badges. Respects the server's 50 cap (extra ids are dropped, never 400'd). */
  getAgentWorkItemsByIds: (workIds: string[]) => {
    const ids = Array.from(new Set(workIds.filter(Boolean))).slice(0, AGENT_WORK_BATCH_IDS_CAP);
    if (ids.length === 0) return Promise.resolve<AgentWorkItem[]>([]);
    const search = new URLSearchParams({ ids: ids.join(",") });
    return request<AgentWorkItem[]>(`/agent-work?${search.toString()}`);
  },

  /** Structured delegation from the Assistant UI controls (toggle + selects +
   *  `@` folder), instead of the `위임:` text grammar. Reuses the same
   *  deterministic policy gate; returns the created AgentWorkItem. */
  delegateAgentTask: (body: AgentDelegateRequest) =>
    request<AgentWorkItem>(`/agent-work/delegate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Owner-scoped run folders reported by the caller's collector daemon, for the
   *  `@` folder picker. supported=false when agent_task is disabled. */
  getAgentFolders: () => request<AgentFolderList>(`/agent-work/agent-folders`),

  /** Enrolled teammates (self first) + their collector nodes, for the delegation
   *  user/node pickers. supported=false when agent_task delegation is disabled. */
  getDelegateTargets: () =>
    request<AgentDelegateTargetList>(`/agent-work/delegate-targets`),

  getAgentWorkInboxSummary: () =>
    request<AgentWorkInboxSummary>("/agent-work/inbox-summary"),

  // 체크리스트 배지 신호 (mailNotifyState와 동형 — count/max 로컬 폴링용).
  // 데스크탑 review 알림(orthus://notify/review)이 폴링한다.
  agentWorkNotifyState: () =>
    request<AgentWorkNotifyState>("/agent-work/notify-state"),

  /* agent chat — DB-backed conversation sessions (restart-safe, owner-scoped) */

  getAgentChats: () => request<AgentChatSession[]>("/agent-work/chats"),

  createAgentChat: (title?: string) =>
    request<AgentChatSession>("/agent-work/chats", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  getAgentChat: (id: string) =>
    request<AgentChatSessionDetail>(`/agent-work/chats/${id}`),

  /** Rename an owner-scoped chat session. Returns the updated session summary. */
  renameAgentChat: (id: string, title: string) =>
    request<AgentChatSession>(`/agent-work/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  /** Delete an owner-scoped chat session and its messages (204 on success). */
  deleteAgentChat: (id: string) =>
    request<void>(`/agent-work/chats/${id}`, { method: "DELETE" }),

  appendAgentChatMessage: (
    id: string,
    body: { role: "user" | "agent"; text: string; work_id?: string | null },
  ) =>
    request<AgentChatMessage>(`/agent-work/chats/${id}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Slice C: poll for a delegated work item's terminal result and append it to
   *  the session. Returns the appended (idempotent, once per (session, work)) agent
   *  message when the item is terminal, or null/204 while it is still in progress.
   *  Safe to call repeatedly. */
  postChatWorkResult: (sessionId: string, workId: string) =>
    request<AgentChatMessage | null>(
      `/agent-work/chats/${sessionId}/work-result`,
      {
        method: "POST",
        body: JSON.stringify({ work_id: workId }),
      },
    ).then((res) => res ?? null),

  continueAgentTask: (sessionId: string, body: AgentContinueRequest) =>
    request<AgentWorkItem>(`/agent-work/chats/${sessionId}/continue-agent-task`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Answer a waiting HITL question and get the resume turn's RoutedAnswer plus
   *  the server-appended user answer row. Treats 200 and 422 (rejected structured
   *  resume) as valid JSON, mirroring orchestrate. */
  hitlRespond: async (
    sessionId: string,
    body: {
      message_id: string;
      answer?: string | null;
      decision?: "approve" | "reject" | null;
      values?: Record<string, string> | null;
      stream_id?: string | null;
    },
  ): Promise<AgentChatHitlResponseResult> => {
    const res = await fetch(`${API_BASE}/agent-work/chats/${sessionId}/hitl-response`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...demoAuthHeaders(),
      },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "include",
    });
    if (res.status === 200 || res.status === 422) {
      return (await res.json()) as AgentChatHitlResponseResult;
    }
    let detail = res.statusText;
    try {
      detail = JSON.stringify(await res.json());
    } catch {
      /* keep statusText */
    }
    redirectToLoginOnUnauthorized("/agent-work", res.status);
    throw new ApiError(res.status, detail);
  },

  /* mail — P6 unified inbox */

  listMailInbox: (params?: { limit?: number; offset?: number; search?: string }) => {
    const search = new URLSearchParams();
    if (params?.limit != null) search.set("limit", String(params.limit));
    if (params?.offset != null) search.set("offset", String(params.offset));
    if (params?.search) search.set("search", params.search);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<MailInboxResponse>(`/mail/inbox${suffix}`);
  },

  sendMail: (body: MailSendRequest) =>
    request<MailSendResult>("/mail/send", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** 현재 owner의 메일 명함(서명) 조회. 없으면 빈 기본값. */
  getMailSignature: (fromAddr?: string | null) => {
    const search = new URLSearchParams();
    if (fromAddr) search.set("from_addr", fromAddr);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<MailSignature>(`/mail/signature${suffix}`);
  },

  /** 메일 명함 저장/갱신 (owner-scope). */
  saveMailSignature: (body: MailSignature, fromAddr?: string | null) => {
    const search = new URLSearchParams();
    if (fromAddr) search.set("from_addr", fromAddr);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<MailSignature>(`/mail/signature${suffix}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },

  /** 보낸 메일 열람(읽음) 현황 조회. 추적 off이거나 내 token이 아니면 404. */
  getMailTrackingStatus: (token: string) =>
    request<MailTrackingStatus>(`/mail/track/status/${encodeURIComponent(token)}`),

  // On-demand company-mail pull: fetch + ingest new mail (and dispatch reply
  // drafts) now, instead of waiting for the periodic mail-sync.
  pullMail: () => request<MailPullResponse>("/mail/pull", { method: "POST" }),

  // Cheap local new-mail signal (count/max over ingested mail docs). No provider
  // fan-out, so the desktop shell can poll it to raise native notifications.
  mailNotifyState: () => request<MailNotifyState>("/mail/notify-state"),

  listPersonalMail: (params?: { limit?: number; offset?: number; search?: string }) => {
    const search = new URLSearchParams();
    if (params?.limit != null) search.set("limit", String(params.limit));
    if (params?.offset != null) search.set("offset", String(params.offset));
    if (params?.search) search.set("search", params.search);
    const suffix = search.toString() ? `?${search.toString()}` : "";
    return request<PersonalMailResponse>(`/mail/personal${suffix}`);
  },

  getPersonalMail: (docId: string) =>
    request<PersonalMailDetail>(`/mail/personal/${docId}`),

  // Company-mail single detail (auto-marks read) + triage mutations. gmail is
  // read-only and 404s here; the FE keeps gmail on getPersonalMail.
  getMailMessage: (backend: MailBackendName, id: string, accountId?: string | null) => {
    const search = new URLSearchParams({ backend, id });
    if (accountId) search.set("account_id", accountId);
    return request<CanonicalEmail>(`/mail/message?${search.toString()}`);
  },

  patchMailFlags: (
    backend: MailBackendName,
    id: string,
    flags: { read?: boolean; starred?: boolean; label?: string },
    accountId?: string | null,
  ) => {
    const search = new URLSearchParams({ backend, id });
    if (accountId) search.set("account_id", accountId);
    return request<{ ok: boolean }>(`/mail/message?${search.toString()}`, {
      method: "PATCH",
      body: JSON.stringify(flags),
    });
  },

  trashMailMessage: (
    backend: MailBackendName,
    id: string,
    restore = false,
    accountId?: string | null,
  ) => {
    const search = new URLSearchParams({ backend, id, restore: String(restore) });
    if (accountId) search.set("account_id", accountId);
    return request<{ ok: boolean }>(`/mail/message/trash?${search.toString()}`, {
      method: "POST",
    });
  },

  // Message history between the mailbox owner and a contact address.
  getMailConversation: (backend: MailBackendName, contact: string, accountId?: string | null) => {
    const search = new URLSearchParams({ backend, contact });
    if (accountId) search.set("account_id", accountId);
    return request<CanonicalEmail[]>(`/mail/conversation?${search.toString()}`);
  },

  // AI compose/reply draft (text only — never sends).
  draftMail: (body: MailDraftRequest) =>
    request<MailDraftResult>("/mail/draft", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Route the AI draft to the requester's OWN local claude/hermes via the
  // collector/agent-task loop. Throws (404) when the feature flag is off — the
  // caller then falls back to the synchronous draftMail above. When mode==="local"
  // poll getAgentWorkItem(work_id) until payload.mail_draft is present.
  dispatchMailDraft: (body: MailDraftDispatchRequest) =>
    request<MailDraftDispatchResult>("/mail/draft/dispatch", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Download one company-mail attachment. The provider attachment store is
  // credential-gated and keys are provider-relative, so the bytes are proxied
  // through orthus (with the same auth as request()) and saved client-side. We
  // fetch a blob rather than navigating an <a href> so the demo X-User-Id header
  // is sent in local/dev mode too.
  downloadMailAttachment: async (
    backend: MailBackendName,
    ref: string,
    filename: string,
    accountId?: string | null,
  ): Promise<void> => {
    const qs = new URLSearchParams({ backend, ref });
    if (filename) qs.set("filename", filename);
    if (accountId) qs.set("account_id", accountId);
    const res = await fetch(`${API_BASE}/mail/attachment?${qs.toString()}`, {
      headers: { ...demoAuthHeaders() },
      credentials: "include",
      cache: "no-store",
    });
    if (!res.ok) {
      throw new ApiError(res.status, "첨부파일을 불러오지 못했습니다.");
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "attachment";
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke only after the browser has started reading the blob — revoking
    // synchronously can abort the download for larger files.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  },

  syncAgentWorkDataGaps: () =>
    request<AgentWorkSyncResult>("/agent-work/sync/data-gaps", {
      method: "POST",
    }),

  syncAgentWorkWikiTasks: () =>
    request<AgentWorkSyncResult>("/agent-work/sync/wiki-tasks", {
      method: "POST",
    }),

  syncAgentWorkPromoteStaging: () =>
    request<AgentWorkSyncResult>("/agent-work/sync/promote-staging", {
      method: "POST",
    }),

  syncAgentWorkConnectorRuns: () =>
    request<AgentWorkSyncResult>("/agent-work/sync/connector-runs", {
      method: "POST",
    }),

  writeAgentPolicyWikiSummary: () =>
    request<AgentPolicyWikiSummary>("/agent-work/policy-memory/wiki-summary", {
      method: "POST",
    }),

  decideAgentWork: (workId: string, body: AgentWorkReviewDecisionRequest) =>
    request<AgentWorkReviewDecisionResult>(
      `/agent-work/${encodeURIComponent(workId)}/decision`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  /** POST /agent-work/{id}/draft: reviewer edits a draft_for_review email_send
   *  draft body before approving. The edited body is what approve→send sends. */
  editAgentWorkDraft: (workId: string, bodyTemplate: string) =>
    request<AgentWorkItem>(`/agent-work/${encodeURIComponent(workId)}/draft`, {
      method: "POST",
      body: JSON.stringify({ body_template: bodyTemplate }),
    }),

  /* projects — auto-tagged Notion DB → project assignment + manual override */

  /** GET /projects: each db_name with its current project, row count, and whether
   *  the project is a user override or the ingest default, plus the enum values. */
  listProjects: () => request<ProjectsResult>("/projects"),

  /** PATCH /projects: set a db_name's project override and re-tag its content.
   *  Returns the new assignment + per-store re-tag counts. 422 on invalid project. */
  setProjectOverride: (db_name: string, project: Project) =>
    request<ProjectPatchResult>("/projects", {
      method: "PATCH",
      body: JSON.stringify({ db_name, project }),
    }),

  /** PATCH /projects with project:null — clear the override and revert to
   *  auto-classification. Returns the resolved project + re-tag counts with
   *  source="auto". */
  clearProjectOverride: (db_name: string) =>
    request<ProjectPatchResult>("/projects", {
      method: "PATCH",
      body: JSON.stringify({ db_name, project: null }),
    }),

  /* promote — explicit personal→central staging gate */

  listPromotionStages: (status?: PromotionStage["status"]) =>
    request<PromotionStage[]>(`/promote/stage${status ? `?status=${status}` : ""}`),

  stagePromotionPackage: (body: PromotionPackage) =>
    request<PromotionStage>("/promote/stage", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  approvePromotionStage: (stageId: string) =>
    request<PromotionStage>(`/promote/stage/${stageId}/approve`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  rejectPromotionStage: (stageId: string) =>
    request<PromotionStage>(`/promote/stage/${stageId}/reject`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  /* board — kanban overlay over Nova 개발 로드맵 */

  /** GET /board: flat card list + the 4 fixed column names. */
  listBoard: () => request<BoardResponse>("/board"),

  /** PATCH /board/{row_id}: update local status overlay. 422 on invalid status. */
  setBoardStatus: (row_id: string, status: string) =>
    request<BoardCard>(`/board/${row_id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  /* personal workspace board */

  getPersonalBoard: (date?: string) =>
    request<PersonalBoardBootstrap>(
      `/personal-board/bootstrap${date ? `?date=${date}` : ""}`,
    ),

  // 개인 알림 피드 신호 (mailNotifyState와 동형 — count/max 로컬 폴링용).
  boardNotifyState: () =>
    request<PersonalBoardNotifyState>("/personal-board/notify-state"),

  // 개인 알림 목록 — 위임 등으로 내 보드에 생성된 태스크의 최근 알림.
  boardNotifications: (limit = 20) =>
    request<PersonalBoardNotificationList>(
      `/personal-board/notifications?limit=${limit}`,
    ),

  createPersonalTask: (body: PersonalTaskCreateInput) =>
    request<PersonalBoardTask>("/personal-board/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listPersonalTaskComments: (taskId: string) =>
    request<PersonalTaskComment[]>(`/personal-board/tasks/${taskId}/comments`),

  addPersonalTaskComment: (taskId: string, body: string) =>
    request<PersonalTaskComment>(`/personal-board/tasks/${taskId}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    }),

  updatePersonalTask: (taskId: string, body: PersonalTaskPatchInput) =>
    request<PersonalBoardTask>(`/personal-board/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  createPersonalProject: (body: PersonalProjectCreateInput) =>
    request<PersonalBoardProject>("/personal-board/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePersonalProject: (projectId: string, body: PersonalProjectPatchInput) =>
    request<PersonalBoardProject>(`/personal-board/projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deletePersonalProject: (projectId: string) =>
    request<void>(`/personal-board/projects/${projectId}`, { method: "DELETE" }),

  createPersonalFolder: (body: PersonalFolderCreateInput) =>
    request<PersonalBoardFolder>("/personal-board/folders", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePersonalBacklogBucket: (bucketId: string, body: PersonalBacklogBucketPatchInput) =>
    request<PersonalBoardBacklogBucket>(`/personal-board/backlog-buckets/${bucketId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  updatePersonalPreferences: (body: PersonalPreferencesPatchInput) =>
    request<PersonalBoardPreferences>("/personal-board/preferences", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  createPersonalSubtask: (taskId: string, body: PersonalSubtaskCreateInput) =>
    request<PersonalBoardSubtask>(`/personal-board/tasks/${taskId}/subtasks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePersonalSubtask: (subtaskId: string, body: PersonalSubtaskPatchInput) =>
    request<PersonalBoardSubtask>(`/personal-board/subtasks/${subtaskId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deletePersonalSubtask: (subtaskId: string) =>
    request<void>(`/personal-board/subtasks/${subtaskId}`, {
      method: "DELETE",
    }),

  createPersonalFixedEvent: (body: PersonalFixedEventCreateInput) =>
    request<PersonalBoardFixedEvent>("/personal-board/fixed-events", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePersonalFixedEvent: (eventId: string, body: PersonalFixedEventPatchInput) =>
    request<PersonalBoardFixedEvent>(`/personal-board/fixed-events/${eventId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  createPersonalNote: (body: PersonalNoteCreateInput) =>
    request<PersonalBoardNote>("/personal-board/notes", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getPersonalWeeklyPlan: (weekStart?: string) =>
    request<WeeklyPlanResponse>(
      `/personal-board/weekly-plan${weekStart ? `?week_start=${weekStart}` : ""}`,
    ),

  createPersonalObjective: (body: PersonalObjectiveCreateInput) =>
    request<PersonalWeeklyObjective>("/personal-board/weekly-plan/objectives", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePersonalObjective: (objectiveId: string, patch: PersonalObjectivePatchInput) =>
    request<PersonalWeeklyObjective>(
      `/personal-board/weekly-plan/objectives/${objectiveId}`,
      {
        method: "PATCH",
        body: JSON.stringify(patch),
      },
    ),

  deletePersonalObjective: (objectiveId: string) =>
    request<void>(`/personal-board/weekly-plan/objectives/${objectiveId}`, {
      method: "DELETE",
    }),

  moveObjectiveDay: (
    objectiveId: string,
    body: { from_weekday: number; to_weekday: number },
  ) =>
    request<PersonalWeeklyObjective>(
      `/personal-board/weekly-plan/objectives/${objectiveId}/move-day`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  createObjectiveSubtask: (objectiveId: string, body: ObjectiveSubtaskCreateInput) =>
    request<ObjectiveSubtask>(
      `/personal-board/weekly-plan/objectives/${objectiveId}/subtasks`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  updateObjectiveSubtask: (subtaskId: string, patch: ObjectiveSubtaskPatchInput) =>
    request<ObjectiveSubtask>(
      `/personal-board/weekly-plan/objective-subtasks/${subtaskId}`,
      {
        method: "PATCH",
        body: JSON.stringify(patch),
      },
    ),

  createObjectiveComment: (objectiveId: string, body: ObjectiveCommentCreateInput) =>
    request<ObjectiveComment>(
      `/personal-board/weekly-plan/objectives/${objectiveId}/comments`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  getPersonalWeeklyReview: (weekStart?: string) =>
    request<WeeklyReviewResponse>(
      `/personal-board/weekly-review${weekStart ? `?week_start=${weekStart}` : ""}`,
    ),

  getPersonalDays: (start: string, days?: number) =>
    request<PersonalDaysResponse>(
      `/personal-board/days?start=${start}${days != null ? `&days=${days}` : ""}`,
    ),

  duplicatePersonalTask: (taskId: string) =>
    request<PersonalBoardTask>(`/personal-board/tasks/${taskId}/duplicate`, {
      method: "POST",
    }),

  deletePersonalTask: (taskId: string) =>
    request<void>(`/personal-board/tasks/${taskId}`, {
      method: "DELETE",
    }),

  /* connectors — generic FE attach/sync surface */

  listConnectors: (scope?: ConnectorScope) =>
    request<ConnectorsResponse>(`/connectors${scope ? `?scope=${scope}` : ""}`),

  ensureConnector: (connectorSlug: string) =>
    request<ConnectorAccount>(`/connectors/${connectorSlug}/ensure`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  syncConnector: (
    connectorSlug: string,
    concurrency = 8,
    background = true,
    scope?: ConnectorScope,
    accountId?: string,
  ) => {
    const params = new URLSearchParams();
    if (scope) params.set("scope", scope);
    if (accountId) params.set("account_id", accountId);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return request<ConnectorSyncResult>(`/connectors/${connectorSlug}/sync${suffix}`, {
      method: "POST",
      body: JSON.stringify({ concurrency, background }),
    });
  },

  // P6.7.5: add/update one mailbox row (owner_addr keyed; several per domain).
  // The company shared key authenticates the mailbox, so the web form sends only
  // non-secret settings (owner_addr/ingest_scope); per-mailbox secrets stay CLI.
  configureConnector: (
    connectorSlug: string,
    body: {
      settings?: Record<string, unknown>;
      secrets?: Record<string, string>;
      account_label?: string;
    },
    scope?: ConnectorScope,
  ) =>
    request<ConnectorAccount>(
      `/connectors/${connectorSlug}/config${scope ? `?scope=${scope}` : ""}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  // P6.7.5: delete one mailbox row (owner-checked server-side; 404 if not yours).
  deleteConnectorAccount: (
    connectorSlug: string,
    accountId: string,
    scope?: ConnectorScope,
  ) =>
    request<ConnectorAccountDeleteResult>(
      `/connectors/${connectorSlug}/accounts/${accountId}${scope ? `?scope=${scope}` : ""}`,
      {
        method: "DELETE",
      },
    ),

  createCollectorCommand: (input: CollectorCommandCreateInput) =>
    request<CollectorCommand>("/collector/commands", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  listCollectorCommands: (status?: CollectorCommandStatus) =>
    request<CollectorCommandList>(
      `/collector/commands${status ? `?status=${status}` : ""}`,
    ),

  getCollectorEvidence: () => request<CollectorEvidenceResponse>("/collector/evidence"),

  retryCollectorCompile: () =>
    request<CollectorCompileResponse>("/collector/compile/retry", {
      method: "POST",
      body: JSON.stringify({}),
    }),

  deleteCollectorDocument: (docId: string) =>
    request<CollectorPersonalDataDeleteResponse>(`/collector/documents/${docId}`, {
      method: "DELETE",
    }),

  pruneCollectorPersonalData: (input: { older_than_days: number; source?: string }) =>
    request<CollectorPersonalDataDeleteResponse>("/collector/personal-data/prune", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  listCollectorTokens: () => request<CollectorTokenList>("/collector/tokens"),

  issueCollectorToken: (input: CollectorTokenIssueInput) =>
    request<CollectorTokenIssueResponse>("/collector/tokens", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  revokeCollectorToken: (tokenId: string) =>
    request<CollectorTokenRecord>(`/collector/tokens/${tokenId}/revoke`, {
      method: "POST",
    }),

  /* auth */
  getAuthConfig: () => cachedGetAuthConfig(),

  getAuthMe: () => cachedGetAuthMe(),

  logout: () =>
    request<void>("/auth/logout", {
      method: "POST",
      body: JSON.stringify({}),
    }).finally(() => {
      clearApiSessionCaches();
    }),

  devLogin: (email: string) =>
    request<AuthMe>("/auth/dev-login", {
      method: "POST",
      body: JSON.stringify({ email }),
    }).then((me) => {
      // 로그인 주체가 바뀌었으므로 이전 세션의 /auth/me 캐시를 버린다.
      authMeCache = null;
      return me;
    }),

  requestMagicLink: (email: string, next = "/editor") =>
    request<MagicLinkRequestResult>("/auth/magic-link/request", {
      method: "POST",
      body: JSON.stringify({ email, next }),
    }),

  googleLoginUrl: (next = "/editor") =>
    `${API_BASE}/auth/oauth/google/start?next=${encodeURIComponent(next)}`,

  /** RP-side: start the cross-node SSO handoff (bounce to the hub). */
  ssoStartUrl: (next = "/editor", silent = false) =>
    `${API_BASE}/auth/sso/start?next=${encodeURIComponent(next)}${silent ? "&silent=1" : ""}`,

  /** Hub-side: the continue page forwards here after a hub login resumes SSO. */
  ssoIssueUrl: (returnTo: string, next = "/editor") =>
    `${API_BASE}/auth/sso/issue?return_to=${encodeURIComponent(returnTo)}&next=${encodeURIComponent(next)}`,

  listAllowlist: () => request<AllowlistEntry[]>("/auth/allowlist"),

  addAllowlistEntry: (email: string, role: string) =>
    request<AllowlistEntry>("/auth/allowlist", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),

  revokeAllowlistEntry: (email: string) =>
    request<void>(`/auth/allowlist/${encodeURIComponent(email)}`, {
      method: "DELETE",
    }),

  /* ----------------------------- dashboard ----------------------------- */

  // team members
  listTeamMembers: () => request<TeamMember[]>("/dashboard/team-members"),
  createTeamMember: (body: TeamMemberInput) =>
    request<TeamMember>("/dashboard/team-members", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateTeamMember: (memberId: string, body: TeamMemberInput) =>
    request<TeamMember>(`/dashboard/team-members/${memberId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteTeamMember: (memberId: string) =>
    request<void>(`/dashboard/team-members/${memberId}`, { method: "DELETE" }),
  syncTeamMembersFromNotion: () =>
    request<TeamSyncResult>("/dashboard/team-members/sync-notion", {
      method: "POST",
    }),

  // recruiting candidates (인재영입)
  listRecruitingCandidates: () =>
    request<RecruitingCandidate[]>("/dashboard/recruiting-candidates"),
  createRecruitingCandidate: (body: RecruitingCandidateInput) =>
    request<RecruitingCandidate>("/dashboard/recruiting-candidates", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateRecruitingCandidate: (candidateId: string, body: RecruitingCandidateInput) =>
    request<RecruitingCandidate>(`/dashboard/recruiting-candidates/${candidateId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteRecruitingCandidate: (candidateId: string) =>
    request<void>(`/dashboard/recruiting-candidates/${candidateId}`, {
      method: "DELETE",
    }),
  listRecruitingComments: (candidateId: string) =>
    request<RecruitingComment[]>(
      `/dashboard/recruiting-candidates/${candidateId}/comments`,
    ),
  createRecruitingComment: (candidateId: string, body: RecruitingCommentInput) =>
    request<RecruitingComment>(
      `/dashboard/recruiting-candidates/${candidateId}/comments`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  deleteRecruitingComment: (candidateId: string, commentId: string) =>
    request<void>(
      `/dashboard/recruiting-candidates/${candidateId}/comments/${commentId}`,
      { method: "DELETE" },
    ),

  // KPI (OKR + North Star)
  getKpiTree: (fiscalYear: number, projectId?: string) =>
    request<KpiTreeNode[]>(
      `/dashboard/kpis/tree?fiscal_year=${fiscalYear}${projectId ? `&project_id=${projectId}` : ""}`,
    ),
  getKpiSummary: (fiscalYear: number, projectId?: string) =>
    request<KpiSummary>(
      `/dashboard/kpis/summary?fiscal_year=${fiscalYear}${projectId ? `&project_id=${projectId}` : ""}`,
    ),
  createKpi: (body: KpiInput) =>
    request<Kpi>("/dashboard/kpis", { method: "POST", body: JSON.stringify(body) }),
  updateKpi: (kpiId: string, body: KpiInput) =>
    request<Kpi>(`/dashboard/kpis/${kpiId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteKpi: (kpiId: string) =>
    request<void>(`/dashboard/kpis/${kpiId}`, { method: "DELETE" }),
  listKpiCheckins: (kpiId: string) =>
    request<KpiCheckin[]>(`/dashboard/kpis/${kpiId}/checkins`),
  createKpiCheckin: (kpiId: string, body: KpiCheckinInput) =>
    request<KpiCheckin>(`/dashboard/kpis/${kpiId}/checkins`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getKpiLinked: (kpiId: string) =>
    request<KpiLinked>(`/dashboard/kpis/${kpiId}/linked`),
  listKpis: (fiscalYear: number) =>
    request<Kpi[]>(`/dashboard/kpis?fiscal_year=${fiscalYear}`),
  getKpiWeeklyItems: (kpiId: string) =>
    request<KpiWeeklyItem[]>(`/dashboard/kpis/${kpiId}/weekly-items`),
  // OKR 채점 루프: 신뢰도(멤버 전원) · 채점(멤버 전원) · 채점 말소(operator) · 회고 카드
  setKpiConfidence: (
    kpiId: string,
    body: { confidence: number; week_start?: string; note?: string | null },
  ) =>
    request<KpiConfidence>(`/dashboard/kpis/${kpiId}/confidence`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listKpiConfidence: (fiscalYear: number) =>
    request<KpiConfidence[]>(`/dashboard/kpis/confidence?fiscal_year=${fiscalYear}`),
  gradeKpi: (kpiId: string, body: { grade: number; note?: string | null }) =>
    request<Kpi>(`/dashboard/kpis/${kpiId}/grade`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  clearKpiGrade: (kpiId: string) =>
    request<Kpi>(`/dashboard/kpis/${kpiId}/grade`, { method: "DELETE" }),
  getKpiRetroCard: (mode: "weekly" | "monthly", ref: string, projectId?: string) =>
    request<KpiRetroCard>(
      `/dashboard/kpis/retro-card?mode=${mode}&ref=${ref}${projectId ? `&project_id=${projectId}` : ""}`,
    ),

  // dashboard projects
  listDashboardProjects: () =>
    request<DashboardProject[]>("/dashboard/projects"),
  listProjectActivity: (projectId: string, limit = 50) =>
    request<ProjectActivityItem[]>(
      `/dashboard/projects/${projectId}/activity?limit=${limit}`,
    ),
  listProjectProgress: () =>
    request<ProjectProgress[]>("/dashboard/projects/progress"),
  listRequirementsSummary: () =>
    request<RequirementSummary[]>("/dashboard/projects/requirements-summary"),
  listProjectRequirements: (projectId: string) =>
    request<ProjectRequirement[]>(`/dashboard/projects/${projectId}/requirements`),
  createProjectRequirement: (projectId: string, body: ProjectRequirementInput) =>
    request<ProjectRequirement>(`/dashboard/projects/${projectId}/requirements`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProjectRequirement: (
    projectId: string,
    requirementId: string,
    body: ProjectRequirementInput,
  ) =>
    request<ProjectRequirement>(
      `/dashboard/projects/${projectId}/requirements/${requirementId}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  deleteProjectRequirement: (projectId: string, requirementId: string) =>
    request<void>(`/dashboard/projects/${projectId}/requirements/${requirementId}`, {
      method: "DELETE",
    }),
  flowDownRequirements: (projectId: string, requirementIds: string[]) =>
    request<ProjectRequirement[]>(
      `/dashboard/projects/${projectId}/requirements/flow-down`,
      { method: "POST", body: JSON.stringify({ requirement_ids: requirementIds }) },
    ),
  createDashboardProject: (body: DashboardProjectInput) =>
    request<DashboardProject>("/dashboard/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDashboardProject: (projectId: string, body: DashboardProjectInput) =>
    request<DashboardProject>(`/dashboard/projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteDashboardProject: (projectId: string) =>
    request<void>(`/dashboard/projects/${projectId}`, { method: "DELETE" }),

  /** GET /dashboard/projects/{id}: 단일 프로젝트(상세 페이지). */
  getDashboardProject: (projectId: string) =>
    request<DashboardProject>(`/dashboard/projects/${projectId}`),

  /** GET /dashboard/projects/{id}/board-tasks: 팀원이 회사 채널에 올린 회사 공개 업무. */
  listProjectBoardTasks: (projectId: string) =>
    request<ProjectBoardTask[]>(`/dashboard/projects/${projectId}/board-tasks`),

  /* ---- 프로젝트 인라인 데이터베이스 (노션식 표/칸반) ---- */
  listProjectDatabases: (projectId: string) =>
    request<ProjectDatabase[]>(`/dashboard/projects/${projectId}/databases`),
  /** POST: 프로젝트 기본 칸반 보드 보장(멱등) — 보드 뷰가 있으면 그 DB를 반환. */
  ensureProjectBoard: (projectId: string) =>
    request<ProjectDatabase>(`/dashboard/projects/${projectId}/board/ensure`, {
      method: "POST",
    }),
  createProjectDatabase: (body: {
    project_id?: string | null;
    title?: string;
    icon?: string | null;
    default_view?: "table" | "board";
    template?: "basic" | "tasks";
  }) =>
    request<ProjectDatabaseBundle>("/dashboard/databases", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** 기본은 행 body 포함(구 API/구 FE 배포 skew 호환). `{ body: "none" }`은 목록
   *  렌더용 opt-in으로 행 body를 생략한다(구 API는 파라미터를 무시하고 body를 준다). */
  getProjectDatabase: (databaseId: string, opts?: { body?: "none" }) =>
    request<ProjectDatabaseBundle>(
      `/dashboard/databases/${databaseId}${opts?.body === "none" ? "?body=none" : ""}`,
    ),
  /** 신규 endpoint — 배포 skew 동안 구 API에서 404가 나므로 호출측은
   *  기존 전체 번들 조회로 fallback해야 한다. */
  getProjectDatabaseMeta: (databaseId: string) =>
    request<ProjectDatabaseMeta>(`/dashboard/databases/${databaseId}/meta`),
  /** 신규 endpoint — 번들이 생략한 body 포함 단건 행. 구 API 404 시 번들 fallback. */
  getProjectDatabaseRow: (databaseId: string, rowId: string) =>
    request<ProjectDatabaseRow>(`/dashboard/databases/${databaseId}/rows/${rowId}`),
  updateProjectDatabase: (
    databaseId: string,
    body: {
      title?: string;
      icon?: string | null;
      properties?: DbPropertyDef[];
      views?: DbViewDef[];
    },
  ) =>
    request<ProjectDatabase>(`/dashboard/databases/${databaseId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteProjectDatabase: (databaseId: string) =>
    request<void>(`/dashboard/databases/${databaseId}`, { method: "DELETE" }),
  createDatabaseRow: (
    databaseId: string,
    body: {
      props?: Record<string, unknown>;
      sort_order?: number;
      icon?: string | null;
      cover?: string | null;
      body?: string | null;
    },
  ) =>
    request<ProjectDatabaseRow>(`/dashboard/databases/${databaseId}/rows`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDatabaseRow: (
    databaseId: string,
    rowId: string,
    body: {
      props?: Record<string, unknown>;
      sort_order?: number;
      icon?: string | null;
      cover?: string | null;
      body?: string | null;
    },
  ) =>
    request<ProjectDatabaseRow>(`/dashboard/databases/${databaseId}/rows/${rowId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteDatabaseRow: (databaseId: string, rowId: string) =>
    request<void>(`/dashboard/databases/${databaseId}/rows/${rowId}`, {
      method: "DELETE",
    }),
  uploadProjectDatabaseFile: async (
    databaseId: string,
    rowId: string,
    file: File,
    opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal },
  ): Promise<DbFileValue> => {
    const body = new FormData();
    body.append("file", file);
    const uploaded = await xhrUpload<DbFileValue>(
      `${API_BASE}/dashboard/databases/${databaseId}/rows/${rowId}/files`,
      body,
      { onProgress: opts?.onProgress, signal: opts?.signal },
    );
    return { ...uploaded, storage: "database" };
  },
  deleteProjectDatabaseFile: (databaseId: string, fileId: string) =>
    request<void>(`/dashboard/databases/${databaseId}/files/${fileId}`, {
      method: "DELETE",
    }),

  /** 대시보드 미디어 업로드(이미지/동영상/파일). 멀티파트라 request() 대신 직접 fetch.
   *  반환 url은 즉시 <img>/<video>에 쓸 수 있는 절대 경로다. */
  uploadDashboardMedia: async (
    file: File,
    opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal },
  ): Promise<DashboardMediaAsset> => {
    const fd = new FormData();
    fd.append("file", file);
    const data = await xhrUpload<DashboardMediaAsset & { url: string }>(
      `${API_BASE}/dashboard/media`,
      fd,
      { onProgress: opts?.onProgress, signal: opts?.signal },
    );
    return {
      ...data,
      url: data.url.startsWith("http") ? data.url : `${API_BASE}${data.url}`,
    };
  },

  /** 대용량(동영상) 청크 업로드: 8MB 파트 순차 PUT → complete 조립. 단일 요청의
   *  Cloudflare 바디 상한(~100MB)·RAM 적재를 우회하고 정확한 진행률·취소를 준다. */
  uploadDashboardMediaChunked: async (
    file: File,
    opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal },
  ): Promise<DashboardMediaAsset> => {
    const init = await request<{ upload_id: string }>("/dashboard/media/uploads", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, content_type: file.type || null }),
    });
    const uploadId = init.upload_id;
    const abortCleanup = () =>
      void request<void>(`/dashboard/media/uploads/${uploadId}`, { method: "DELETE" }).catch(
        () => {},
      );
    const rateState = { rate: 0, lastTime: Date.now(), lastLoaded: 0 };
    try {
      const partCount = Math.max(1, Math.ceil(file.size / UPLOAD_PART_BYTES));
      for (let i = 0; i < partCount; i += 1) {
        const blob = file.slice(i * UPLOAD_PART_BYTES, (i + 1) * UPLOAD_PART_BYTES);
        await xhrUpload<{ received: number }>(
          `${API_BASE}/dashboard/media/uploads/${uploadId}/parts/${i}`,
          blob,
          {
            method: "PUT",
            onProgress: opts?.onProgress,
            signal: opts?.signal,
            baseLoaded: i * UPLOAD_PART_BYTES,
            totalOverride: file.size,
            rateState,
          },
        );
      }
      const data = await request<DashboardMediaAsset & { url: string }>(
        `/dashboard/media/uploads/${uploadId}/complete`,
        { method: "POST" },
      );
      return {
        ...data,
        url: data.url.startsWith("http") ? data.url : `${API_BASE}${data.url}`,
      };
    } catch (e) {
      abortCleanup();
      throw e;
    }
  },

  // dashboard pages (노션식 페이지 / 자유 메모)
  listDashboardPages: (
    kind: string = "memo",
    scope: "all" | "mine" = "all",
    includePreview: boolean = false,
  ) =>
    request<DashboardPageSummary[]>(
      `/dashboard/pages?kind=${encodeURIComponent(kind)}&scope=${scope}${includePreview ? "&include_preview=true" : ""}`,
    ),
  createDashboardPage: (body?: {
    /** Offline-first clients allocate the stable UUID before the POST. */
    page_id?: string;
    title?: string;
    icon?: string | null;
    body?: string | null;
    kind?: string;
  }, options?: { signal?: AbortSignal }) =>
    request<DashboardPage>("/dashboard/pages", {
      method: "POST",
      body: JSON.stringify(body ?? {}),
      signal: options?.signal,
    }),
  getDashboardPage: (pageId: string) =>
    request<DashboardPage>(`/dashboard/pages/${pageId}`),
  updateDashboardPage: (
    pageId: string,
    body: { title?: string | null; icon?: string | null; body?: string | null },
  ) =>
    request<DashboardPage>(`/dashboard/pages/${pageId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteDashboardPage: (pageId: string) =>
    request<void>(`/dashboard/pages/${pageId}`, { method: "DELETE" }),

  // notes: 공유 + 얼라인 확인 + 프로젝트 링크 (docs/personal-notes.md)
  listNoteLibrary: (scope: "mine" | "shared") =>
    request<NoteLibraryItem[]>(`/dashboard/notes/library?scope=${scope}`),
  listSharedNotes: () =>
    request<SharedNoteSummary[]>("/dashboard/notes/shared"),
  listNoteShares: (pageId: string) =>
    request<NoteShare[]>(`/dashboard/notes/${pageId}/shares`),
  addNoteShares: (pageId: string, body: { shared_with: string[]; permission: "view" | "edit" }) =>
    request<NoteShare[]>(`/dashboard/notes/${pageId}/shares`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteNoteShare: (pageId: string, shareId: string) =>
    request<void>(`/dashboard/notes/${pageId}/shares/${shareId}`, { method: "DELETE" }),
  acknowledgeNote: (pageId: string) =>
    request<void>(`/dashboard/notes/${pageId}/acknowledge`, { method: "POST" }),
  listNoteProjects: (pageId: string) =>
    request<NoteProjectLink[]>(`/dashboard/notes/${pageId}/projects`),
  setNoteProjects: (pageId: string, projectIds: string[]) =>
    request<NoteProjectLink[]>(`/dashboard/notes/${pageId}/projects`, {
      method: "PUT",
      body: JSON.stringify({ project_ids: projectIds }),
    }),
  listProjectNotes: (projectId: string) =>
    request<DashboardPageSummary[]>(`/dashboard/projects/${projectId}/notes`),

  // project assignments (member ↔ project ↔ role)
  listAssignments: (params?: { projectId?: string; memberId?: string }) => {
    const q = new URLSearchParams();
    if (params?.projectId) q.set("project_id", params.projectId);
    if (params?.memberId) q.set("member_id", params.memberId);
    const qs = q.toString();
    return request<ProjectAssignment[]>(
      `/dashboard/assignments${qs ? `?${qs}` : ""}`,
    );
  },
  createAssignment: (body: AssignmentInput) =>
    request<ProjectAssignment>("/dashboard/assignments", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateAssignmentRole: (assignmentId: string, role: string | null) =>
    request<ProjectAssignment>(`/dashboard/assignments/${assignmentId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  deleteAssignment: (assignmentId: string) =>
    request<void>(`/dashboard/assignments/${assignmentId}`, { method: "DELETE" }),

  // project role options (노션식 선택 속성: 추가/이름변경/색/삭제)
  listProjectRoles: () => request<ProjectRole[]>("/dashboard/project-roles"),
  createProjectRole: (body: ProjectRoleInput) =>
    request<ProjectRole>("/dashboard/project-roles", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProjectRole: (roleId: string, body: ProjectRolePatch) =>
    request<ProjectRole>(`/dashboard/project-roles/${roleId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteProjectRole: (roleId: string) =>
    request<void>(`/dashboard/project-roles/${roleId}`, { method: "DELETE" }),

  // 실시간 협업 프레즌스(누가 어디 작성 중)
  presenceHeartbeat: (body: {
    area: string;
    project_id?: string | null;
    section?: string | null;
  }) =>
    request<PresenceItem[]>("/dashboard/presence/heartbeat", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // weekly plans & retros
  getWeekly: (projectId: string, weekStart: string) =>
    request<WeeklyEntry>(
      `/dashboard/weekly?project_id=${projectId}&week_start=${weekStart}`,
    ),
  saveWeekly: (body: {
    project_id: string;
    week_start: string;
    plan_items: PlanItem[];
    retro_items: PlanItem[];
    base_updated_at?: string | null;
  }) =>
    request<WeeklyEntry>("/dashboard/weekly", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  // Path A live-collab: relay in-progress plan/retro to other viewers via SSE
  // without a DB write (the debounced saveWeekly/saveMonthly persists). Best-effort.
  liveEditPlan: (body: {
    kind: "weekly" | "monthly";
    project_id: string;
    period_key: string;
    plan: PlanItem[];
    retro: PlanItem[];
    editor_id: string;
  }) =>
    request<void>("/dashboard/live-edit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getWeeklyHistory: (projectId: string, weekStart: string) =>
    request<EntryHistory[]>(
      `/dashboard/weekly/history?project_id=${projectId}&week_start=${weekStart}`,
    ),

  listWeeklyPeriods: (projectId: string) =>
    request<PeriodSummary[]>(`/dashboard/weekly/list?project_id=${projectId}`),
  listMonthlyPeriods: (projectId: string) =>
    request<PeriodSummary[]>(`/dashboard/monthly/list?project_id=${projectId}`),

  // monthly plans & retros
  getMonthly: (projectId: string, month: string) =>
    request<MonthlyEntry>(
      `/dashboard/monthly?project_id=${projectId}&month=${month}`,
    ),
  saveMonthly: (body: {
    project_id: string;
    month: string;
    plan_items: PlanItem[];
    retro_items: PlanItem[];
    base_updated_at?: string | null;
  }) =>
    request<MonthlyEntry>("/dashboard/monthly", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getMonthlyHistory: (projectId: string, month: string) =>
    request<EntryHistory[]>(
      `/dashboard/monthly/history?project_id=${projectId}&month=${month}`,
    ),

  // team calendar
  listCalendar: (from: string, to: string) =>
    request<CalendarEvent[]>(`/dashboard/calendar?from=${from}&to=${to}`),
  listCalendarLocations: () =>
    request<string[]>("/dashboard/calendar/locations"),
  // 자동 저장: 편집 중 잦은 저장은 reflect=false로 위키 반영을 건너뛰고, 편집을
  // 마칠 때(닫기) 한 번만 reflect=true로 반영한다(기본 true = 기존 동작).
  createCalendarEvent: (
    body: CalendarEventInput,
    opts?: { reflect?: boolean; clientEventId?: string },
  ) => {
    const qs = new URLSearchParams();
    if (opts?.reflect === false) qs.set("reflect", "false");
    if (opts?.clientEventId) qs.set("client_event_id", opts.clientEventId);
    const q = qs.toString();
    return request<CalendarEvent>(`/dashboard/calendar/events${q ? `?${q}` : ""}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  updateCalendarEvent: (eventId: string, body: CalendarEventInput, opts?: { reflect?: boolean }) =>
    request<CalendarEvent>(
      `/dashboard/calendar/events/${eventId}${opts?.reflect === false ? "?reflect=false" : ""}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),
  deleteCalendarEvent: (eventId: string) =>
    request<void>(`/dashboard/calendar/events/${eventId}`, {
      method: "DELETE",
    }),

  // finance
  getFinanceSummary: () =>
    request<FinanceSummary>("/dashboard/finance/summary"),

  listSubscriptions: () =>
    request<Subscription[]>("/dashboard/finance/subscriptions"),
  createSubscription: (body: SubscriptionInput) =>
    request<Subscription>("/dashboard/finance/subscriptions", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSubscription: (subId: string, body: SubscriptionInput) =>
    request<Subscription>(`/dashboard/finance/subscriptions/${subId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteSubscription: (subId: string) =>
    request<void>(`/dashboard/finance/subscriptions/${subId}`, {
      method: "DELETE",
    }),

  listApiKeys: () => request<ApiKeyEntry[]>("/dashboard/finance/api-keys"),
  createApiKey: (body: ApiKeyInput) =>
    request<ApiKeyEntry>("/dashboard/finance/api-keys", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateApiKey: (keyId: string, body: ApiKeyInput) =>
    request<ApiKeyEntry>(`/dashboard/finance/api-keys/${keyId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteApiKey: (keyId: string) =>
    request<void>(`/dashboard/finance/api-keys/${keyId}`, { method: "DELETE" }),

  listFinanceAccounts: () =>
    request<FinanceAccount[]>("/dashboard/finance/accounts"),
  createFinanceAccount: (body: FinanceAccountInput) =>
    request<FinanceAccount>("/dashboard/finance/accounts", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateFinanceAccount: (accountId: string, body: FinanceAccountInput) =>
    request<FinanceAccount>(`/dashboard/finance/accounts/${accountId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteFinanceAccount: (accountId: string) =>
    request<void>(`/dashboard/finance/accounts/${accountId}`, {
      method: "DELETE",
    }),

  listLedger: () => request<LedgerEntry[]>("/dashboard/finance/ledger"),
  createLedger: (body: LedgerEntryInput) =>
    request<LedgerEntry>("/dashboard/finance/ledger", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateLedger: (ledgerId: string, body: LedgerEntryInput) =>
    request<LedgerEntry>(`/dashboard/finance/ledger/${ledgerId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteLedger: (ledgerId: string) =>
    request<void>(`/dashboard/finance/ledger/${ledgerId}`, { method: "DELETE" }),

  // infra (GPU / 스토리지 / API 키)
  listInfra: () => request<InfraResource[]>("/dashboard/infra"),
  getGpuSummary: () => request<GpuSummary>("/dashboard/infra/gpu-summary"),
  getMlPanel: () => request<MlPanel>("/dashboard/infra/ml-panel"),
  syncGpu: () =>
    request<GpuSyncResult>("/dashboard/infra/sync-gpu", { method: "POST" }),
  syncStorage: () =>
    request<StorageSyncResult>("/dashboard/infra/sync-storage", { method: "POST" }),
  createInfra: (body: InfraResourceInput) =>
    request<InfraResource>("/dashboard/infra", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateInfra: (resourceId: string, body: InfraResourceInput) =>
    request<InfraResource>(`/dashboard/infra/${resourceId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteInfra: (resourceId: string) =>
    request<void>(`/dashboard/infra/${resourceId}`, { method: "DELETE" }),
  listInfraLinks: () =>
    request<InfraResourceLink[]>("/dashboard/infra/links"),
  addInfraLink: (body: { resource_id: string; project_id: string }) =>
    request<InfraResourceLink>("/dashboard/infra/links", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteInfraLink: (linkId: string) =>
    request<void>(`/dashboard/infra/links/${linkId}`, { method: "DELETE" }),
  listInfraProviders: () =>
    request<InfraProvider[]>("/dashboard/infra/providers"),
  createInfraProvider: (body: InfraProviderInput) =>
    request<InfraProvider>("/dashboard/infra/providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateInfraProvider: (providerId: string, body: InfraProviderInput) =>
    request<InfraProvider>(`/dashboard/infra/providers/${providerId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteInfraProvider: (providerId: string) =>
    request<void>(`/dashboard/infra/providers/${providerId}`, { method: "DELETE" }),

  // meeting notes (회의록)
  listMeetingNotes: (params?: { meetingKind?: string; projectId?: string }) => {
    const q = new URLSearchParams();
    if (params?.meetingKind) q.set("meeting_kind", params.meetingKind);
    if (params?.projectId) q.set("project_id", params.projectId);
    const qs = q.toString();
    return request<MeetingNote[]>(
      `/dashboard/meeting-notes${qs ? `?${qs}` : ""}`,
    );
  },
  createMeetingNote: (body: MeetingNoteInput) =>
    request<MeetingNote>("/dashboard/meeting-notes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateMeetingNote: (noteId: string, body: MeetingNoteInput) =>
    request<MeetingNote>(`/dashboard/meeting-notes/${noteId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteMeetingNote: (noteId: string) =>
    request<void>(`/dashboard/meeting-notes/${noteId}`, { method: "DELETE" }),

  // 회의록 첨부(PDF 등 미팅자료) — media_store 실파일 저장
  uploadMeetingAttachment: (
    noteId: string,
    file: File,
    opts?: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal },
  ): Promise<MeetingAttachment> => {
    const form = new FormData();
    form.append("file", file);
    return xhrUpload<MeetingAttachment>(
      `${API_BASE}/dashboard/meeting-notes/${noteId}/attachments`,
      form,
      opts,
    );
  },
  deleteMeetingAttachment: (noteId: string, attachmentId: string) =>
    request<void>(
      `/dashboard/meeting-notes/${noteId}/attachments/${attachmentId}`,
      { method: "DELETE" },
    ),

  // partner companies (파트너사)
  listPartners: (projectId?: string) =>
    request<PartnerCompany[]>(
      `/dashboard/partners${projectId ? `?project_id=${projectId}` : ""}`,
    ),
  createPartner: (body: PartnerCompanyInput) =>
    request<PartnerCompany>("/dashboard/partners", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updatePartner: (partnerId: string, body: PartnerCompanyInput) =>
    request<PartnerCompany>(`/dashboard/partners/${partnerId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deletePartner: (partnerId: string) =>
    request<void>(`/dashboard/partners/${partnerId}`, { method: "DELETE" }),
  listPartnerContacts: (partnerId: string) =>
    request<PartnerContact[]>(`/dashboard/partners/${partnerId}/contacts`),
  createPartnerContact: (partnerId: string, body: PartnerContactInput) =>
    request<PartnerContact>(`/dashboard/partners/${partnerId}/contacts`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updatePartnerContact: (contactId: string, body: PartnerContactInput) =>
    request<PartnerContact>(`/dashboard/partners/contacts/${contactId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deletePartnerContact: (contactId: string) =>
    request<void>(`/dashboard/partners/contacts/${contactId}`, {
      method: "DELETE",
    }),

  // support programs (지원사업)
  listSupportPrograms: (projectId?: string) =>
    request<SupportProgram[]>(
      `/dashboard/support-programs${projectId ? `?project_id=${projectId}` : ""}`,
    ),
  createSupportProgram: (body: SupportProgramInput) =>
    request<SupportProgram>("/dashboard/support-programs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSupportProgram: (programId: string, body: SupportProgramInput) =>
    request<SupportProgram>(`/dashboard/support-programs/${programId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteSupportProgram: (programId: string) =>
    request<void>(`/dashboard/support-programs/${programId}`, {
      method: "DELETE",
    }),
  /** 한 상태 컬럼의 카드 순서(+컬럼 이동)를 통째로 재배치한다. */
  reorderSupportPrograms: (status: string, orderedIds: string[]) =>
    request<SupportProgram[]>("/dashboard/support-programs/reorder", {
      method: "POST",
      body: JSON.stringify({ status, ordered_ids: orderedIds }),
    }),
  listSupportNotes: (kind?: string) =>
    request<SupportNote[]>(
      `/dashboard/support-notes${kind ? `?kind=${kind}` : ""}`,
    ),
  createSupportNote: (body: SupportNoteInput) =>
    request<SupportNote>("/dashboard/support-notes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateSupportNote: (noteId: string, body: SupportNoteInput) =>
    request<SupportNote>(`/dashboard/support-notes/${noteId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteSupportNote: (noteId: string) =>
    request<void>(`/dashboard/support-notes/${noteId}`, { method: "DELETE" }),

  // culture
  getCulture: () => request<CompanyCulture>("/dashboard/culture"),
  saveCulture: (content: Record<string, unknown>) =>
    request<CompanyCulture>("/dashboard/culture", {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
};
