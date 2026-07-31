"""Environment-driven config. Runtime secrets prefer local secret store."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (orthus/settings.py).
_REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORTHUS_", env_file=".env", extra="ignore", populate_by_name=True
    )

    node_kind: str = "company"  # company | personal
    node_id: str = "company"
    node_root: Path | None = None

    # P8.1 central owner-scope. When true on the company (central) node, /ask may
    # resolve a requested scope of "all"/"personal" so a logged-in user sees their
    # own personal rows merged with company knowledge. Fail-closed default false
    # keeps the company node company-only until an operator opts in. The own-owner
    # row filters on Agent Work / data gaps are NOT gated on this flag — they are
    # the permanent fail-closed privacy boundary (docs/p8-central-consolidation.md
    # §3/§5-A).
    owner_scope_enabled: bool = False

    # --- KG (K-series, docs/kg-model.md §1 / docs/kg-implementation-spec.md §2.2) ---
    # central-only rebuildable Neo4j 관계 인덱스. fail-closed: flag off가 default이고
    # personal node는 Neo4j를 갖지 않는다.
    kg_enabled: bool = False  # ORTHUS_KG_ENABLED
    kg_uri: str = "bolt://127.0.0.1:7687"  # ORTHUS_KG_URI
    kg_user: str = "neo4j"  # ORTHUS_KG_USER
    # ORTHUS_KG_PASSWORD — keychain secret(orthus/kg/password) 우선, env는 dev fallback.
    kg_password: str = ""
    # ORTHUS_KG_OWNER_SCOPE_ENABLED — K1에서 선언만 하고 코드 참조는 K7에서 시작
    # (그 전까지 어떤 동작도 바꾸지 않음).
    kg_owner_scope_enabled: bool = False
    kg_query_timeout_ms: int = 2000  # K4 게이트 transaction timeout
    kg_query_limit: int = 50  # K4 결과 LIMIT 주입 기본·상한 (완화는 별도 결정)
    # ORTHUS_KG_ENTITY_HUB_THRESHOLD — K9 entity_neighbors degree ceiling. mention_count(page-only
    # 집계)가 이 값 **이상**인 흔한 엔티티는 다리 매개에서 완전 제외한다(C-E/U1 — 캡이 아니라
    # 화면 미노출). read 게이트가 bound `$hub_threshold` $param으로 주입하므로 full-rebuild 없이
    # 조정 가능하다(R9 — mention_count만 rebuild 의존, threshold는 read 값). 40은 첫 시작 기본값
    # 이며 실데이터 1회 튜닝 패스(done-criterion, kg-k9-plan §1 C-E)가 확정한다.
    kg_entity_hub_threshold: int = 40
    # ORTHUS_KG_EXPAND_LIMIT — E1b(D4) 그래프 탐색기 per-expand 1-hop 상한. 게이트가 아니라
    # endpoint가 run_kg_template(..., limit=kg_expand_limit)로 넘긴다 — 기존 limit 파라미터 경로를
    # 재사용해 effective_limit=min(requested, kg_query_limit) cap + truncated(len==limit) 판정이
    # 무손질로 동작한다. setting_params($limit 재바인딩)는 gate.py bind["limit"]와 충돌하므로 쓰지
    # 않는다. 누적 총상한 200은 FE(E2)가 강제. 신규 flag 아님(K9 kg_entity_hub_threshold 동형 튜닝 상수).
    kg_expand_limit: int = 50  # ORTHUS_KG_EXPAND_LIMIT
    # ORTHUS_KG_OVERVIEW_LIMIT — 전용 지식그래프 화면의 랜딩 개체 지도(entity_overview) 상한.
    # `$limit`(kg_query_limit=50 cap)과 충돌하지 않도록 별도 이름 `$overview_limit`로 주입한다
    # (setting_params). FE 탐색기 누적 캡 200 이내라 렌더 안전. 신규 flag 아님(튜닝 상수).
    kg_overview_limit: int = 150  # ORTHUS_KG_OVERVIEW_LIMIT
    kg_outbox_poll_seconds: int = 5  # K3 worker poll 주기
    # ORTHUS_KG_REBUILD_LOCK_MINUTES — K7.5 **headroom 임계값 전용**(release clock 아님).
    # rebuild HOLD는 :KgMeta.rebuild_in_progress boolean이 좌우하고(시간 만료 release 없음),
    # 이 값은 post-rebuild headroom WARN(measured rebuild_seconds > 0.5×lock → 적체 위험)과
    # monitor의 stuck-rebuild(rebuild_started_at 경과 > lock) could-not-verify 판정에만 쓴다.
    kg_rebuild_lock_minutes: int = 30

    pg_dsn: str = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus"
    pg_dsn_readonly: str = "postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus"

    # Connection pool. Default 15 (size 5 + overflow 10) drained under dashboard
    # burst load (parallel plan loads + per-save wiki-reflect background tasks),
    # timing out /auth/me at 30s. Bigger pool + fail-fast timeout absorbs bursts;
    # overflow conns close on return so idle footprint stays at pg_pool_size.
    pg_pool_size: int = 10
    pg_max_overflow: int = 30
    pg_pool_timeout: float = 10.0
    pg_pool_recycle: int = 1800

    # LLM wiki markdown SoR root. ORTHUS_WIKI_STORE is the public env name;
    # ORTHUS_WIKI_STORE_PATH remains accepted for compatibility with pydantic's
    # generated env name for this field.
    wiki_store_path: Path = Field(
        default=_REPO_ROOT / "wiki-store",
        validation_alias=AliasChoices("ORTHUS_WIKI_STORE", "ORTHUS_WIKI_STORE_PATH"),
    )
    # 대시보드 미디어(이미지/동영상/파일) 저장 루트. 노션식 본문/카드 미디어를
    # base64가 아닌 실파일로 보관한다. capability URL(unguessable uuid 파일명)로 서빙.
    media_store_path: Path = Field(
        default=_REPO_ROOT / "media-store",
        validation_alias=AliasChoices("ORTHUS_MEDIA_STORE", "ORTHUS_MEDIA_STORE_PATH"),
    )
    # 업로드 1건 최대 바이트(기본 2GB — 실촬영 동영상까지; 디스크 저장이라 RAM 무관.
    # 대용량은 청크 업로드 경로가 파트 단위로 받는다). 초과는 413.
    media_max_bytes: int = 2 * 1024 * 1024 * 1024
    cors_origins: str = "http://localhost:3820,http://127.0.0.1:3820"

    # retrieval index slot: mock | solar
    # Measured 2026-07-16 (experiments/fugu-ko/embedding/README.md §8.6): Solar
    # `embedding-passage` beats text-embedding-3-small on retrieval MRR (+0.080 veconly /
    # +0.112 hybrid, p<0.001) and query latency (p50 241ms -> 69ms). Use the PASSAGE model
    # for queries too — the query/passage split Upstage documents measured significantly
    # WORSE here (4/4 conditions), because `embedding-query` embeds far from passage space.
    embedding: str = "mock"
    # distill/compile slot: mock | solar. `mock` is the deterministic offline slot
    # (tests/CI); `solar` is the real backend and reads the vendor spec below.
    llm: str = "mock"
    embedding_dimensions: int = 1024
    # `ORTHUS_EMBEDDING=solar`. Key falls back to the chat slot's Upstage key
    # (`llm_solar_api_key`) — same vendor, same account, one credential.
    # `embedding-passage` is the default on purpose: it is the SYMMETRIC configuration
    # (index and query both go through it), which measured significantly better than the
    # documented query/passage split. Do not "fix" this to `embedding-query`.
    embedding_solar_api_key: str = ""
    embedding_solar_base_url: str = "https://api.upstage.ai/v1"
    embedding_solar_model: str = "embedding-passage"
    # Task-aware model assignment (docs/model-orchestration.md). Off = every task
    # keeps the default chat slot. On, measured tasks route through the per-task
    # assignment table (all Solar in this build); unconfigured workers fall back
    # silently-but-audited. Keys stay in env only (no plaintext in repo).
    model_orchestration_enabled: bool = False
    llm_solar_api_key: str = ""
    llm_solar_base_url: str = "https://api.upstage.ai/v1"
    llm_solar_model: str = "solar-pro"
    # Inline agentic /ask (docs/inline-agentic-ask.md). When true, /ask runs an
    # in-process Solar function-calling loop over the deterministic tool backends
    # instead of the fixed wiki/structured/graph ladder. Fail-closed default —
    # behavior is unchanged until the operator opts in (env ORTHUS_AGENTIC_ASK_ENABLED).
    agentic_ask_enabled: bool = False
    # Bounds concurrent agentic /ask loops so an unbounded arrival rate cannot
    # exhaust the node. Excess /ask gets a busy answer.
    agentic_ask_max_concurrency: int = 3
    # Human-in-the-Loop for the agent-work chat orchestrator (docs plan
    # cryptic-meandering-gadget). When true, the orchestrator can end a turn with
    # a structured question (kind="user_input_request") — either projected from a
    # policy-gate request_more_data outcome (deterministic) or raised by the
    # agentic loop's ask_user tool — and the FE resumes via
    # POST /agent-work/chats/{id}/hitl-response. Fail-closed default: off ⇒ no
    # question messages, hitl-response 404s, orchestrate byte-identical. The
    # deterministic policy gate is never modified; questions only project its
    # output and resume re-enters the same gate (설계 원칙 1).
    agent_hitl_enabled: bool = False  # ORTHUS_AGENT_HITL_ENABLED
    # Agent-chat follow-up reference rewrite (orthus/router/rewrite.py): when the
    # current message carries a Korean anaphor ("그거", "해당 내용", …) and the chat
    # session has history, one LLM call rewrites it into a self-contained question
    # BEFORE retrieval/routing (retrieval is otherwise history-blind — see
    # agentwork/chat.py::load_chat_history). Deterministic prefilter + fail-open to
    # the original text. Fail-closed default: off ⇒ orchestrate byte-identical.
    chat_followup_rewrite_enabled: bool = False  # ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED

    # SVC — structured 검증 캐스케이드 (experiments/fugu-ko/analysis/
    # structured-verification-cascade.md). 1차 structured 실행 결과가 결정론 신호
    # (게이트 실패 / 미실행 / 0행 / 결과 정수집합 == {0})를 띄우면 2차 모델로 한 번
    # 더 묻는다. 모델 confidence는 보지 않는다(절대룰: confidence routing 금지).
    #
    # `llm_fallback_model`이 마스터 스위치다 — 빈 값이면 신호 계산조차 하지 않고
    # 기존 동작이 완전히 불변이다(fail-closed).
    llm_fallback_model: str = ""
    # 2차 모델의 provider 슬롯. 빈 값이면 1차와 같은 provider(`llm`)를 쓴다.
    # Solar 단일 빌드에서는 같은 벤더의 **다른 모델**(예: solar-mini)이 SVC 2차다.
    llm_fallback_provider: str = ""  # "" | solar | mock
    # 보조 노브. off | shadow | on. "shadow"는 트리거만 audit에 기록하고 재질의는 하지
    # 않는다(SVC.0 관측 모드 — 발동률/오발동을 먼저 측정하기 위한 것). "on"이어도
    # 마스터 스위치인 `llm_fallback_model`이 비어 있으면 아무 동작도 하지 않는다.
    # 알 수 없는 값과 빈 문자열은 fail-closed로 "off"로 읽는다(`_fallback_mode`) —
    # `.env`에 `ORTHUS_STRUCTURED_FALLBACK_MODE=`(빈 값)를 두면 캐스케이드가 꺼진다.
    structured_fallback_mode: str = "on"
    # read→act 게이트: 결정론 신호가 뜬 structured 상위 결과(게이트 실패/0행/결과가 0)를
    # **명령형** 의존 리프의 근거로 인정하지 않는다(`decompose._deps_unavailable`).
    #
    # **신규 기능 플래그가 아니라 kill switch다**(default True). `_deps_unavailable`은 이미
    # "read→act must not run the action on a failed read"를 계약으로 선언해 놓고도
    # zero-answer(게이트 통과 + status=executed → grounded=True)를 그냥 통과시키고 있었다.
    # 그 선언된 계약을 복원하는 **버그 수정**이다. 명령 판정은 결정론 키워드 감지기
    # (`detect_assistant_command_action`, LLM 0회)라 읽기 의존 리프는 막지 않고, 막힌 명령
    # 리프는 기존 `upstream_unavailable`(가시적 skip)로 떨어진다. 운영 문제 시 false로 끈다.
    structured_zero_blocks_dependents: bool = True

    conn_notion_token: str = ""
    # Per-project Notion integration tokens scoped to a subtree of the same
    # workspace (P2 multitenant). _1 = atlas subtree, _2 = nova subtree.
    # When both are set, ingest derives project by id-set membership instead of
    # the db_name heuristic (notion_import.main).
    conn_notion_token_1: str = ""
    conn_notion_token_2: str = ""
    conn_local_files_roots: str = ""
    conn_local_files_extensions: str = ".md,.mdx,.txt,.json,.jsonl,.csv,.tsv"
    conn_local_files_max_bytes: int = 1024 * 1024
    conn_codex_sessions_roots: str = ""
    conn_claude_sessions_roots: str = ""
    conn_ai_sessions_max_bytes: int = 5 * 1024 * 1024
    conn_ai_sessions_max_files: int = 200
    conn_ai_sessions_max_messages: int = 200
    conn_ai_sessions_max_message_chars: int = 4000
    conn_chat_exports_roots: str = ""
    conn_chat_exports_max_bytes: int = 50 * 1024 * 1024
    conn_chat_exports_max_messages: int = 300
    conn_chat_exports_max_message_chars: int = 4000
    conn_email_exports_roots: str = ""
    conn_email_exports_max_bytes: int = 50 * 1024 * 1024
    conn_email_exports_max_body_chars: int = 12000
    conn_github_token: str = ""
    conn_github_repos: str = ""
    conn_github_max_items: int = 200
    conn_slack_bot_token: str = ""
    conn_slack_channels: str = ""
    conn_slack_max_messages: int = 300
    # Optional channel defaults: "C123=atlas,C999=nova". Explicit content
    # keywords in Slack messages still override this fallback.
    conn_slack_channel_projects: str = ""
    conn_gws_command: str = "gws"
    conn_gws_timeout_seconds: int = 60
    conn_gws_gmail_query: str = "in:anywhere"
    conn_gws_gmail_max_messages: int = 50
    conn_gws_gmail_max_body_chars: int = 12000
    conn_gws_drive_query: str = "trashed=false"
    conn_gws_drive_max_files: int = 50
    conn_gws_drive_max_bytes: int = 1024 * 1024
    email_sender: str = "none"  # none | fake
    mail_nova_base_url: str = Field(
        default="https://api.nova.example",
        validation_alias=AliasChoices("ORTHUS_MAIL_NOVA_BASE_URL", "NOVA_API_BASE_URL"),
    )
    mail_nova_api_key_secret_ref: str = ""
    mail_nova_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ORTHUS_MAIL_NOVA_API_KEY", "NOVA_APP_API_KEY"),
    )
    mail_nova_owner: str = ""
    mail_acme_base_url: str = "https://mail-api.acme.example"
    mail_acme_api_token_secret_ref: str = ""
    mail_acme_api_token: str = ""
    mail_acme_session_secret_ref: str = ""
    mail_acme_session: str = ""
    mail_acme_owner: str = ""
    # Mailbox boundary (docs/p6-mail-individual-scope.md). An "individual" work
    # mailbox (e.g. owner01@nova.example) is one employee's mail and must ingest as
    # scope=personal bound to that owner (owner-only readable). Only an explicitly
    # configured "shared" mailbox (e.g. hello@nova.example) ingests as scope=company.
    # Default individual is fail-safe; shared is an operator opt-in.
    mail_nova_kind: str = "individual"  # individual | shared
    mail_acme_kind: str = "individual"  # individual | shared
    # P6.3 manual compose/send. Global kill switch is fail-closed off. The
    # acme send token (D2) is a scoped M2M token distinct from the read
    # session token; an unset send token leaves acme send-unconfigured (no
    # reuse of the read session credential). Nova send reuses the existing read
    # APP_API_KEY (D1).
    mail_send_enabled: bool = False
    mail_acme_send_token_secret_ref: str = ""
    mail_acme_send_token: str = ""
    # P7: 보낸 HTML 메일에 orthus 자체 열람 추적 픽셀을 심는다(provider 협조 불필요).
    # fail-closed: 플래그 off거나 pixel base URL이 비면 픽셀을 심지 않는다. base URL은
    # 수신자 메일 클라이언트가 도달 가능한 공개 API origin이어야 한다(Caddy가 /api로
    # 프록시; 예: https://orthus-central.example.com/api). 공개 무인증 엔드포인트라
    # owner/operator 검토 후 활성화한다.
    mail_open_tracking_enabled: bool = False
    mail_tracking_pixel_base_url: str = ""
    mail_timeout_seconds: float = 10.0
    mail_inbox_default_limit: int = 50
    mail_ingest_enabled: bool = False
    mail_ingest_secret_ref: str = ""
    mail_ingest_secret: str = ""
    mail_ingest_hmac_secret_ref: str = ""
    mail_ingest_hmac_secret: str = ""
    mail_ingest_replay_window_seconds: int = 300
    mail_ingest_service_user_id: str = "00000000-0000-4000-8000-000000000001"
    # P6.2-pull: orthus-side periodic pull-ingest of company-domain mail
    # (nova/acme) as an alternative to provider push. Fail-closed default;
    # company-node only. Reuses the same scope=company ingest boundaries.
    mail_pull_ingest_enabled: bool = False
    # P6.7: per-owner self-service mail accounts via connector_accounts rows.
    # Fail-closed; when off (or when an owner has zero mail account rows) mail
    # backends resolve from env settings exactly as today. See
    # `docs/p6-unified-mail.md` §12 and
    # `mail/backends.backend_configs_from_accounts`.
    mail_multi_account_enabled: bool = False
    # P7.1: create an email_send reply-draft Agent Work candidate when a new
    # inbound company-domain mail is ingested. Fail-closed; company-node only.
    mail_reply_draft_enabled: bool = False
    # Local-agent reply draft: for owner-scope (personal) inbound mail, self-assign
    # an agent_task to the mailbox owner so THEIR own collector daemon drafts the
    # reply (instead of the central LLM P7.1 path). Mutually exclusive with the
    # central reply draft for owner-scope mail. Fail-closed; company-node only.
    mail_reply_draft_agent_enabled: bool = False
    # Delegation slice 4: when a new inbound company-domain mail is ingested,
    # extract a delegation intent (assignee/mode/runner/instruction) via the LLM
    # and queue an `agent_task` Agent Work candidate. The LLM only extracts; the
    # deterministic policy gate decides auto_execute vs request_more_data, and the
    # dispatch itself still requires `agent_task_enabled`. Fail-closed; company-node
    # only. See `docs/agent-task-delegation.md` §8.
    mail_agent_task_delegation_enabled: bool = False
    # On-demand local-agent mail draft: route the "AI 초안" compose/reply button
    # through the REQUESTING user's OWN collector daemon (their local claude/hermes)
    # instead of the central ORTHUS_LLM, so no central provider key is needed and the
    # draft is authored on the user's machine. Self-assigned agent_task
    # (assignee = requester), mode=knowledge, mail_origin kind="compose_draft"; the
    # completion branch parses {subject,body} into payload.mail_draft. Composes with
    # `agent_task_enabled` (both processes). Fail-closed; company-node only. See
    # `docs/mail-draft-local-agent.md`.
    mail_agent_draft_enabled: bool = False
    # Explicit opt-in: when the requester has NO enrolled local daemon, fall back to
    # a synchronous central `draft_email()` (central ORTHUS_LLM) instead of returning
    # mode="unavailable". Default off to honour "no central provider key required" —
    # when off, a missing daemon yields a clear actionable error, never a silent
    # central draft. See `docs/mail-draft-local-agent.md`.
    mail_draft_central_fallback: bool = False
    # 메일 초안이 회사 위키(compiled page)를 근거로 삼게 한다. off이면 초안은 지금처럼
    # 받은 메일 본문만 보고 쓰이고, 사내에 이미 답이 있어도 자리표시자로 비운다.
    # on이면 `/ask`와 동일한 retrieve → answer_from_hits 경로로 근거를 뽑아 프롬프트에
    # 얹는다(학습·갭기록 없음, 조회 실패는 fail-open). 상세 `orthus/mail/grounding.py`.
    mail_draft_wiki_grounding_enabled: bool = False
    # P7.5: global kill switch for bounded reply auto-send (D16). Fail-closed.
    # This slice ships no auto-send code path; the flag only gates candidacy
    # evidence that always records used_for_outcome=False.
    mail_auto_send_enabled: bool = False
    # Nova ML platform (k3s on the Tailscale tailnet) — live infra panel on the
    # company dashboard. NOT related to mail_nova_* above (that is the Nova mail
    # API). Empty vm_url disables the VM pull and GPU sync falls back to the legacy
    # SSH path (ORTHUS_GPU_SSH_HOST). The orthus host must be on the tailnet.
    nova_ml_vm_url: str = ""  # e.g. http://100.64.0.1:30428 (VictoriaMetrics PromQL)
    nova_ml_mlflow_url: str = ""  # e.g. http://100.64.0.1:30500 (MLflow REST + UI)
    nova_ml_grafana_url: str = ""  # e.g. http://100.64.0.1:30300 (Grafana UI deep links)
    # Grafana dashboard embedded as an <iframe> on the infra page (kiosk mode).
    # Requires anonymous Viewer + allow_embedding on Grafana (tailnet-only).
    nova_ml_grafana_dashboard_uid: str = "nova-gpu-kpi"
    nova_ml_timeout_seconds: float = 8.0
    secret_backend: str = "auto"  # auto | keychain | memory
    auth_mode: str = "demo"  # demo | jwt | session
    auth_jwt_secret: str = ""
    auth_session_secret: str = ""
    auth_public_base_url: str = "http://localhost:8820"
    auth_web_base_url: str = "http://localhost:3820"
    auth_cookie_name: str = ""
    auth_cookie_secure: bool = True
    # Session longevity: "log in once and stay logged in" on mobile. The cookie
    # is rolled on every authenticated request (see get_current_user), so the
    # sliding window is the inactivity tolerance and the absolute window is a far
    # ceiling. Both stay env-overridable for stricter deployments.
    auth_session_sliding_days: int = 90
    auth_session_absolute_days: int = 3650
    auth_dev_login_enabled: bool = False
    auth_magic_link_domain: str = "acme.example"
    auth_magic_link_ttl_minutes: int = 15
    auth_magic_link_delivery: str = "none"  # none | console | resend
    auth_magic_link_from_email: str = "Orthus <login@auth.acme.example>"
    resend_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ORTHUS_RESEND_API_KEY", "ORTHUS_AUTH_RESEND_API_KEY"),
    )
    resend_base_url: str = "https://api.resend.com"
    resend_timeout_seconds: int = 10
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    central_admin_emails: str = ""
    personal_owner_email: str = ""

    # Personal node -> central company read-only federation. Empty token/base keeps
    # federation disabled and personal answers local-only.
    central_read_base_url: str = ""
    central_read_token: str = ""
    central_read_timeout_ms: int = 6000
    federation_service_token: str = ""
    federation_service_user_id: str = "00000000-0000-4000-8000-000000000001"

    # Cross-node SSO session bridge. The hub (central) issues short-lived,
    # single-use, audience-bound tickets; relying-party nodes (personal) consume
    # them to mint their own local session. Both sides share sso_shared_secret.
    # Hub role:     sso_shared_secret + sso_trusted_return_origins set.
    # RP role:      sso_shared_secret + sso_hub_base_url set.
    # Desktop role: sso_shared_secret + sso_desktop_return_uris set. Same-node
    #   variant where the hub both mints and consumes a ticket so a Tauri webview
    #   hands off the browser session via a custom-scheme return URI.
    sso_shared_secret: str = ""
    sso_hub_base_url: str = (
        ""  # RP: hub API base, e.g. https://orthus-central.example.com/api
    )
    sso_trusted_return_origins: str = ""  # hub: csv of allowed RP consume origins
    sso_desktop_return_uris: str = (
        ""  # desktop: csv of exact custom-scheme URIs, e.g. orthus://sso/consume
    )
    sso_ticket_ttl_seconds: int = 45

    # P8.2 central ingestion API + command queue for the thin personal collector
    # daemon. Fail-closed: the collector surface 404s on every node unless this is
    # explicitly enabled and the node is company-kind.
    collector_api_enabled: bool = False
    # Slice 8: push a "command_available" notify to a connected collector daemon
    # over a 1-way WebSocket (central->daemon) the moment a command is enqueued,
    # so the daemon picks it up immediately instead of waiting for its poll
    # interval. Fail-closed: when off (default) the /collector/ws endpoint is
    # refused and the enqueue notify is a no-op, so the daemon falls back to
    # polling and production behavior is unchanged.
    collector_ws_enabled: bool = False
    # 위임 루프(agent_task): central enqueue + collector daemon 실행(headless
    # claude/codex/hermes). 2026-07-17 owner 결정으로 default ON — 위임 루프는
    # 별도 스위치 없이 항상 가능해야 한다. env(ORTHUS_AGENT_TASK_ENABLED=false)는
    # 긴급 kill switch로 유지된다(central/daemon 각 프로세스에서 개별 차단 가능).
    # 실제 안전 경계는 이 flag가 아니라: 결정론 policy gate(`agent_task` family)
    # + runner allowlist + company node/owner-admin-scheduler actor 체크 +
    # owner-scope/audit이며, 이들은 전부 그대로 유지된다.
    agent_task_enabled: bool = True
    # P10.7: agent_task execution runtime. "spawn" (default) = the
    # subprocess-per-task path. ("resident" required the removed P10 resident
    # gateway daemon and is no longer available in this stripped repo.)
    agent_task_runtime: str = "spawn"
    # P10.4b: 개인 에이전트 게이트웨이 데몬의 commit-action 브리지. true(company
    # node)이면 collector-token 라우터 `/collector/gateway/actions`가 owner 데몬의
    # typed action 후보를 P3 policy gate에 태우고 owner 결정을 릴레이한다.
    # 2026-07-17 owner 결정으로 default ON — 위임 루프는 항상 가능해야 한다.
    # env(ORTHUS_AGENT_GATEWAY_ACTIONS_ENABLED=false)는 긴급 kill switch로
    # 유지된다(off → 라우터 404). 실제 안전 경계는 flag가 아니라: `agent_task`
    # token scope + token.user_id의 명시적 owner/admin 역할 해석 + strict
    # owner-scope predicate(`_agent_work_owner_predicate`) + P3 결정론 policy
    # gate + audit이며, 전부 그대로 유지된다.
    # See docs/p10-personal-agent-gateway.md §5-A.
    agent_gateway_actions_enabled: bool = True
    # Daemon-side default working directory for code-mode agent_task runs. Central
    # does not know a team member's local repo path, so a code-mode command may
    # omit `cwd` and the daemon resolves it from this setting instead. None keeps
    # code mode fail-closed (it still fails when neither payload.cwd nor this is
    # set), so behavior is unchanged until an operator sets it on the daemon.
    agent_task_workspace: str | None = None
    # Delegation slice 7: attach the company knowledge MCP (orthus-mcp) to each
    # dispatched agent so a headless claude/codex run can query company knowledge.
    # Fail-closed: when off (default) the runner builds the exact same argv/env as
    # before. The central URL + knowledge-scoped token reuse the documented MCP env
    # names from `orthus/mcp/central.py` (ORTHUS_MCP_CENTRAL_URL / ORTHUS_MCP_TOKEN).
    # Both must be set or the runner skips attach and records the reason; it never
    # substitutes a default URL/token. See `docs/agent-task-delegation.md`.
    # TODO: this uses the long-lived configured knowledge token; minting a
    # short-lived per-dispatch token is out of scope for this slice.
    agent_task_mcp_enabled: bool = False
    agent_task_mcp_central_url: str | None = Field(
        default=None, validation_alias=AliasChoices("ORTHUS_MCP_CENTRAL_URL")
    )
    agent_task_mcp_token: str | None = Field(
        default=None, validation_alias=AliasChoices("ORTHUS_MCP_TOKEN")
    )
    # Delegation streaming slice: live-stream a dispatched agent_task's stdout/stderr
    # lines back to the owner's browser while it runs (instead of only revealing the
    # batch-captured result at completion). The daemon side, when on, pushes
    # `agent_output` frames onto the already-open outbound /collector/ws socket; the
    # central side, when on, accepts those frames and serves them over the per-work
    # SSE endpoint. Fail-closed on BOTH sides: off (default) means the daemon produces
    # no frames and the central SSE endpoint 404s, so the SoR HTTP complete_command
    # path is unchanged and production behavior is identical to before this slice.
    agent_task_streaming_enabled: bool = False
    # P10.7 Part B: route an explicitly personal-scope app-chat turn to the
    # caller's own enrolled resident daemon (agent_task self-delegation) instead
    # of the inline company orchestrator. Fail-closed: off (default) keeps the
    # orchestrate chat path byte-identical; company-scope search and the merged
    # `scope=all` company orchestrator are never routed even when on (spec §5).
    app_chat_gateway_routing_enabled: bool = False
    collector_ingest_owner_window_seconds: int = 900
    collector_ingest_owner_docs_limit: int = 1000
    collector_ingest_owner_bytes_limit: int = 50 * 1024 * 1024
    collector_compile_owner_window_seconds: int = 300

    # /ask decompose: compound question split + parallel fan-out (docs/company-agent-orchestration.md)
    # Fail-closed defaults — behavior is unchanged until an operator opts in.
    # MA.2b (FE renderer) must be complete before enabling in production.
    ask_decompose_enabled: bool = False  # ORTHUS_ASK_DECOMPOSE_ENABLED
    ask_prompt_cache_enabled: bool = False  # ORTHUS_ASK_PROMPT_CACHE_ENABLED
    ask_semantic_cache_enabled: bool = False  # ORTHUS_ASK_SEMANTIC_CACHE_ENABLED (Phase 3)
    ask_decompose_max_parts: int = 5  # ORTHUS_ASK_DECOMPOSE_MAX_PARTS
    ask_decompose_max_concurrency: int = 4  # ORTHUS_ASK_DECOMPOSE_MAX_CONCURRENCY
    ask_decompose_leaf_timeout_sec: int = 30  # ORTHUS_ASK_DECOMPOSE_LEAF_TIMEOUT_SEC
    # Phase 2 (depends_on 위상 실행). off → Phase 1 flat parallel과 byte-identical.
    # 운영 적용은 MA.5 측정 게이트 (docs/company-agent-orchestration.md §P2).
    ask_decompose_depends_enabled: bool = False  # ORTHUS_ASK_DECOMPOSE_DEPENDS_ENABLED
    ask_decompose_max_depth: int = 2  # ORTHUS_ASK_DECOMPOSE_MAX_DEPTH (wave depth 상한)
    # Phase 3-B MA.8b — full DAG (depth > 2). off → depth > 2 무시 (flat 강등) → Phase 2
    # byte-identical (불변식 32). on이어야 ask_decompose_max_depth > 2가 실효한다.
    # docs/company-agent-orchestration.md §P3B.3.
    ask_decompose_full_dag_enabled: bool = False  # ORTHUS_ASK_DECOMPOSE_FULL_DAG_ENABLED
    # depth×width 누적 leaf 상한(비용 폭발 차단, R18). full DAG on일 때만 적용되며, split이
    # 이보다 많은 sub-question을 내면 decompose를 포기하고 단일 answer()로 강등한다. full DAG
    # off면 이 가드가 비활성이라 Phase 1/2가 어떤 K 설정에서도 byte-identical하다(불변식 32).
    ask_decompose_max_total_leaves: int = 12  # ORTHUS_ASK_DECOMPOSE_MAX_TOTAL_LEAVES
    # Phase 3-A semantic answer cache (MA.7) — docs/company-agent-orchestration.md §P3A.
    # `ask_semantic_cache_enabled` (above) is the master switch; off → /ask byte-identical
    # (불변식 27). Cache stores company-scope grounded answers only (불변식 22).
    ask_cache_ttl_sec: int = 86400  # ORTHUS_ASK_CACHE_TTL_SEC (watermark 1차, TTL 2차 방어)
    ask_cache_semantic_match_enabled: bool = False  # ORTHUS_ASK_CACHE_SEMANTIC_MATCH_ENABLED (MA.7b)
    ask_cache_similarity_threshold: float = (
        0.95  # ORTHUS_ASK_CACHE_SIMILARITY_THRESHOLD (MA.7b 고정 τ)
    )
    # 넓은 질문 검색 1-hop 확장 (wiki/qa.ask 훅). off → ask/retrieve byte-identical.
    # ON이면 1차 hits 상위 seed 페이지의 entity_neighbors(KG, off/미가용 시 조용히 skip)
    # + provenance BFS로 **후보 페이지 집합만** 넓혀 2차 retrieve(page_slugs=...) 후
    # 결정론 병합(페이지 dedupe + 확장 유래 점수 할인)한다. 근거는 여전히 compiled wiki
    # page 전용(raw-chunk RAG 아님), 추가 LLM 콜 0, 확장 블록 전체 fail-open.
    wiki_retrieve_expand_enabled: bool = False  # ORTHUS_WIKI_RETRIEVE_EXPAND_ENABLED
    # Phase 3-B MA.8a — event-triggered decompose orchestration. off → mail ingest
    # byte-identical (불변식 32). Inbound company mail with a compound/action signal
    # enqueues a knowledge-brief job that a lifespan worker runs offline; the result
    # is a draft_for_review AgentWork item. docs/company-agent-orchestration.md §P3B.
    ask_event_orch_enabled: bool = False  # ORTHUS_ASK_EVENT_ORCH_ENABLED
    # Storm guard — skip new enqueues once this many jobs are already pending (bounds
    # the queue so one pull cycle can't flood the worker). §P3B.2.3 / R16.
    ask_event_orch_max_per_cycle: int = 5  # ORTHUS_ASK_EVENT_ORCH_MAX_PER_CYCLE
    # Command/Question 상위 분할 (docs/company-agent-orchestration.md — command-split).
    # 혼합/다중 입력의 명령 fragment를 fan-out으로 fragment별 라우팅한다. decompose 엔진을
    # 재사용하므로 ask_decompose_enabled에 AND 종속(command_split_active). off → orchestrate
    # 535 whole-text 경로 byte-identical. flag-on 게이트: S2+S4+S5 머지 전 on 금지.
    ask_command_split_enabled: bool = False  # ORTHUS_ASK_COMMAND_SPLIT_ENABLED
    # 명령 보존 가드(BLOCKER F): 원문에 strong-command 신호(connector_sync/recipient literal
    # email_send)가 있으면 LLM split이 명령을 소실하지 않도록 decompose 진입 전 535 single-queue로
    # 강등한다. default true — command_split이 켜져도 명령 소실을 결정론으로 차단한다.
    ask_decompose_command_guard: bool = True  # ORTHUS_ASK_DECOMPOSE_COMMAND_GUARD
    # O4/E3 — should_decompose 프리필터 신호 확장 티어 (docs/decompose-prefilter-ext.md).
    # 0 = off (default, fail-closed): 프리필터/`command_split_signal`/
    # `mail_has_orchestration_signal` 전 경로가 기존과 byte-identical.
    # 1 = T1(저위험: 각각/비교/차이/vs/둘 다), 2 = T1+T2(중위험: 이랑/랑/~는 뭐고),
    # 3 = T1+T2+T3(고위험: 병렬 조사 "와 "/"과 "),
    # 4 = T1+T2+T3+T4(고위험: "-고" 접속어미 co-occurrence 규칙 — 절경계 "-고" + 독립
    #     의문/순차 지시절). 누적 적용이며 확장은 `should_decompose` 경로에만 전달된다 —
    # command-split/event-orch 모집단은 불변.
    decompose_prefilter_ext_tier: int = 0  # ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER

    # 라우팅 사각지대 처리 (routing holdout C2, docs/routing-holdout-plan.md §13).
    # True(default) = 결정론 규칙이 미결정인 사각지대에서 LLM의 structured/wiki 판정을
    #   믿지 않고 wiki로 강제한다. 홀드아웃 290문항에서 LLM 라우터(60%)는 "무조건 wiki"
    #   상수(77%)보다 낮았고, 규칙확장+wiki기본값은 90.7%(p=0.000)였다. wiki는 틀려도
    #   "정보 없음"으로 눈에 보이게 실패하므로(structured 오라우팅=조용한 빈 답과 달리)
    #   안전 방향이다. **단 LLM `graph` 판정은 살린다** — 홀드아웃은 structured vs wiki만
    #   쟀고 graph 평면을 미측정했으므로 그 결론이 K8 conflict/관계 질문의 graph 라우팅을
    #   죽이면 안 된다(규칙 앵커 없는 NL conflict는 이 LLM enum으로만 graph에 도달).
    # False = 사각지대에서 기존처럼 `TASK_ROUTING` 모델 판정을 그대로 따른다(revert switch).
    routing_wiki_default: bool = True  # ORTHUS_ROUTING_WIKI_DEFAULT

    # 교차평면 회수 (routing holdout C3-a, docs/routing-holdout-plan.md §13).
    # True(default) = structured가 **게이트 거부**되면(SQL이 무효/환각 컬럼 형태로 컴파일)
    #   그 질문을 wiki 평면으로 재질의한다. 게이트 거부는 평면 선택 자체가 틀렸을 신호이고,
    #   거부된 SQL은 "0건입니다"라는 참인 답을 애초에 만들 수 없어 TN 환각 위험이 없다
    #   (홀드아웃: 회수 10/12, TN 발화 0). **0행 트리거는 넣지 않는다** — 진짜 0건을 wiki로
    #   재질의하면 정답 '없음'을 지어낸다(C3-c는 TN 환각 1/9로 기각).
    # False = 게이트 거부 결과를 그대로 반환한다(revert switch).
    routing_gate_reject_wiki_recover: bool = True  # ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER
    # F2 — C3-a의 대칭: wiki로 라우팅됐는데 근거 0 공답("근거 없음")이면 structured를
    # 시도하고, sqlglot 검증 게이트 전부 통과일 때만 structured로 서빙한다. 지식형
    # 질문이 잘못 넘어오면 환각 컬럼/테이블로 게이트가 리젝하므로 공답이 유지된다
    # (TN-환각 방어는 C3-a와 동일 논리). fail-closed default — 켜기 전 현행 불변.
    routing_wiki_empty_structured_recover: bool = (
        False  # ORTHUS_ROUTING_WIKI_EMPTY_STRUCTURED_RECOVER
    )

    assistant_query_timeout_ms: int = 8000
    assistant_default_limit: int = 1000

    def mail_kind_for(self, backend: str) -> str:
        """Per-backend mailbox kind ("individual" | "shared").

        Unknown values fall back to the fail-safe "individual" so a misconfigured
        backend never widens an employee mailbox to company scope.
        """
        raw = {
            "nova": self.mail_nova_kind,
            "acme": self.mail_acme_kind,
        }.get(backend, "individual")
        kind = (raw or "").strip().lower()
        return kind if kind in {"individual", "shared"} else "individual"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def mail_nova_v0_base_url(self) -> str:
        base = (self.mail_nova_base_url or "https://api.nova.example").strip().rstrip("/")
        return base if base.endswith("/v0") else f"{base}/v0"

    def sso_trusted_origin_list(self) -> list[str]:
        return [
            o.strip().rstrip("/") for o in self.sso_trusted_return_origins.split(",") if o.strip()
        ]

    def sso_desktop_return_uri_list(self) -> list[str]:
        # Exact custom-scheme URIs: strip whitespace, drop empties, but do NOT
        # rstrip slashes — the audience match is against the full literal URI.
        return [u.strip() for u in self.sso_desktop_return_uris.split(",") if u.strip()]

    def resolved_node_root(self) -> Path:
        return self.node_root or Path.home() / ".orthus" / "nodes" / self.node_id


_LOCAL_AUTH_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# 위 집합과 달리 빈 host를 포함하지 않는다. auth origin 판정에서는 "host를 못 읽었다"가
# 곧 "공개 origin이 아니다"라 빈 값이 안전한 쪽이지만, 여기서는 반대다 — scheme 없는
# 오타(`api.nova.example`)의 host가 빈 값으로 파싱되므로, 빈 값을 허용하면 잘못된 설정이
# 기동 시점에 안 걸리고 요청 시점까지 미뤄진다.
_MAIL_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_runtime_settings(settings: Settings) -> None:
    """Fail closed for public browser exposure mistakes."""
    if settings.mail_nova_api_key:
        # 가드의 목적은 **실제 API 키가 평문으로 네트워크에 나가는 것**을 막는 것이다.
        # loopback(127.0.0.1/localhost/::1)행 http는 그 노출이 성립하지 않으므로 — 패킷이
        # 기계 밖으로 나가지 않는다 — 로컬 스텁 백엔드로 메일 화면을 띄우는 개발/시연
        # 경로까지 막을 이유가 없다. 그 외에는 전부 기동 거부(fail-closed)다.
        parts = urlsplit(settings.mail_nova_base_url)
        loopback_http = parts.scheme == "http" and (parts.hostname or "") in _MAIL_LOOPBACK_HOSTS
        if parts.scheme != "https" and not loopback_http:
            raise RuntimeError("NOVA_API_BASE_URL must use https when NOVA_APP_API_KEY is set")
    if not _uses_public_auth_origin(settings):
        return

    if settings.auth_mode == "demo":
        raise RuntimeError("public auth base cannot run with ORTHUS_AUTH_MODE=demo")
    if settings.auth_mode != "session":
        return

    errors: list[str] = []
    if not (settings.auth_session_secret or settings.auth_jwt_secret):
        errors.append("ORTHUS_AUTH_SESSION_SECRET")
    if not settings.auth_cookie_secure:
        errors.append("ORTHUS_AUTH_COOKIE_SECURE=true")
    if settings.auth_dev_login_enabled:
        errors.append("ORTHUS_AUTH_DEV_LOGIN_ENABLED=false")
    delivery = settings.auth_magic_link_delivery.strip().lower()
    if delivery in {"", "none"}:
        errors.append("ORTHUS_AUTH_MAGIC_LINK_DELIVERY")
    elif delivery == "resend":
        if not settings.resend_api_key:
            errors.append("ORTHUS_RESEND_API_KEY")
        if not settings.auth_magic_link_from_email:
            errors.append("ORTHUS_AUTH_MAGIC_LINK_FROM_EMAIL")
    elif delivery != "console":
        errors.append("ORTHUS_AUTH_MAGIC_LINK_DELIVERY=console|resend")
    if (
        settings.sso_hub_base_url
        or settings.sso_trusted_return_origins
        or settings.sso_desktop_return_uris
    ) and not settings.sso_shared_secret:
        errors.append("ORTHUS_SSO_SHARED_SECRET")
    if settings.mail_ingest_enabled:
        if settings.node_kind != "company":
            errors.append("ORTHUS_NODE_KIND=company for mail ingest")
        if not (settings.mail_ingest_secret or settings.mail_ingest_secret_ref):
            errors.append("ORTHUS_MAIL_INGEST_SECRET or ORTHUS_MAIL_INGEST_SECRET_REF")
        if not (settings.mail_ingest_hmac_secret or settings.mail_ingest_hmac_secret_ref):
            errors.append("ORTHUS_MAIL_INGEST_HMAC_SECRET or ORTHUS_MAIL_INGEST_HMAC_SECRET_REF")
    if errors:
        raise RuntimeError("public session auth misconfigured: " + ", ".join(errors))


def command_split_active(settings: Settings) -> bool:
    """command_split은 decompose 엔진(split/fan_out/synthesize/SSE)을 재사용하므로
    ask_decompose_enabled에 AND 종속한다(BLOCKER 정합). 상위 flag가 off면 command_split도
    강등(silent False) — 설정 로드는 성공한다(fail-hard 금지). 오설정(command_split=True +
    decompose=False)은 warning으로만 남긴다."""
    if settings.ask_command_split_enabled and not settings.ask_decompose_enabled:
        import logging

        logging.getLogger("orthus.settings").warning(
            "ORTHUS_ASK_COMMAND_SPLIT_ENABLED=true but ORTHUS_ASK_DECOMPOSE_ENABLED=false; "
            "command-split degraded to off (AND dependency)."
        )
    return settings.ask_command_split_enabled and settings.ask_decompose_enabled


def _uses_public_auth_origin(settings: Settings) -> bool:
    return _is_public_url(settings.auth_public_base_url) or _is_public_url(
        settings.auth_web_base_url
    )


def _is_public_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "") not in _LOCAL_AUTH_HOSTS


@lru_cache
def get_settings() -> Settings:
    return Settings()
