# Orthus — 데이터 모델

> status: current data model contract
> updated: 2026-06-06
> authority: current Alembic migrations `0001_base` through `0020_agent_policy_observations`.
> 제품 방향은 내부 문서(비공개), 운영 정책은 `docs/operations.md`, node 경계는
> 내부 문서(비공개)를 따른다.

이 문서는 현행 사내 SaaS 라인(central company 아카식 + personal local nodes)의
DB 계약이다. 현재 저장소는 **Postgres + pgvector + node-local wiki-store**다.
`raw_events`, `decisions`, `execution_attempts`, `feedback_events`,
`persona_snapshots`, `erasure_log`, `connector_errors`는 과거 P0/P1 비전의
legacy schema이며 현재 Alembic head에 없다.
Neo4j/KG는 K-series rebuildable index 인프라로 재정의됐다(`docs/kg-model.md`
§5.1). `kg_outbox`는 K3에서 동명 신규 스키마로 재정의 완료(migration
`0047_kg_outbox`, §4b)이고 `kg_change_log`는 재정의 없이 legacy로 남는다.
둘 다 K0 spec-lock 이전 레거시 명칭과 동일하나 스키마·역할이 다르다.
P8(내부 문서(비공개))은 P8.1에서 `owner_user_id` 기반
owner-only personal scope를 central schema에 추가할 예정이다. 해당 migration
PR에서 본 문서를 함께 개정한다. 그 전까지 본 문서가 현행 schema 계약이다.

Migrations are source of truth for DDL. `orthus/tables.py` mirrors current tables
for application queries.

일반 규칙:

- 모든 식별자는 UUID v4다.
- 모든 시각 컬럼은 `TIMESTAMPTZ`, UTC 저장이다.
- `scope` 컬럼은 central hardening/legacy compatibility 장치다. personal privacy
  boundary가 아니다.
- personal privacy boundary는 node별 DB/corpus/vector/wiki-store/agent/FE/runtime
  분리로 보장한다.
- personal→central 이동은 `promote_staging` publish/promote gate만 허용한다.
- secret value는 DB에 저장하지 않는다. `settings_redacted.secret_refs`만 저장한다.

---

## 1. 저장소 경계

| 저장소 | 역할 | current status |
|---|---|---|
| Postgres | users, documents, corpus, wiki index, structured rows, connector state, promote staging, auth/session, audit | active |
| pgvector | `embeddings.vec vector(1024)` | active Postgres extension |
| wiki-store markdown | LLM wiki markdown source-of-record | node-local active |
| Neo4j | KG rebuildable index (K-series) | not active; `ORTHUS_KG_ENABLED=false` fail-closed; K1 이후 **central 단일** 컨테이너 신규 추가, v1 projection은 company scope만(P8 정합) — `docs/kg-model.md` |

Company와 personal node는 같은 schema를 사용할 수 있지만 같은 database를 공유하지 않는다.
예: `orthus_company`, `orthus_personal_ys`.

**KG projection 원칙 (K-series):** Postgres + node-local wiki-store가 유일
SoR이다. Neo4j는 `wiki_pages`/`wiki_links`/`structured_rows`/`documents`를
읽기 전용으로 투영하는 파생 인덱스이며, 불일치 시 `make kg-rebuild`가 언제나
SoR 기준으로 복원한다. v1(K2–K6) projection은 `scope='company'` row만
대상으로 하며 personal owner-scope row(`scope='personal'`, `owner_id`
보유)는 포함하지 않는다. K7에서 전 템플릿 owner 술어 + 경로 가시성 규칙을
조건으로 owner-scope row까지 확장한다(P8 정합·K7 계약 —
`docs/kg-model.md`).

---

## 2. Core Tables

### `users`

기본 user row. Demo, auth identity, documents, connector owner, allowlist actor가 참조한다.

| Column | Type | Notes |
|---|---|---|
| `user_id` | UUID PK | user id |
| `external_id` | TEXT UNIQUE | demo/dev/external identity key |
| `display_name` | TEXT NOT NULL | UI display |
| `preferred_timezone` | TEXT NOT NULL DEFAULT `UTC` | IANA timezone |
| `created_at` | TIMESTAMPTZ DEFAULT now() | created time |

### `audit_log`

호출 단위 audit span. `audit()` context manager가 primary writer다.

| Column | Type | Notes |
|---|---|---|
| `audit_id` | BIGSERIAL PK | audit row id |
| `correlation_id` | UUID NOT NULL | request/run correlation |
| `node_run_id` | UUID NOT NULL | one invocation/span id |
| `node` | TEXT NOT NULL | e.g. `connector.command`, `ask.router` |
| `phase` | TEXT NOT NULL | `enter`, `exit`, `error` |
| `output` | JSONB | redacted |
| `meta` | JSONB DEFAULT `{}` | redacted metadata |
| `error_class` | TEXT | error class |
| `error_message` | TEXT | redacted message |
| `occurred_at` | TIMESTAMPTZ DEFAULT now() | event time |

Indexes:

- `idx_audit_correlation(correlation_id, occurred_at)`
- `idx_audit_run(node_run_id)`
- `uq_audit_run_phase(node_run_id, phase)`

---

## 3. Corpus / Documents / Vector

### `documents`

Editor document + connector-ingested document source table. Corpus/wiki build starts here.

| Column | Type | Notes |
|---|---|---|
| `doc_id` | UUID PK | document id |
| `user_id` | UUID FK users | owning/importing user |
| `title` | TEXT NOT NULL | document title |
| `block_json` | JSONB NOT NULL | BlockNote/editor blocks or normalized source blocks |
| `markdown` | TEXT NOT NULL | canonical text for chunk/wiki |
| `source` | TEXT NOT NULL | `editor`, `agent_draft`, `notion`, connector slug, `promoted_personal`, etc. |
| `source_account_id` | UUID FK connector_accounts NULL | connector account |
| `source_external_id` | TEXT NULL | source id |
| `source_canonical_id` | TEXT NULL | cross-account canonical source id, e.g. `notion:page:<uuid>` |
| `source_db_name` | TEXT NULL | originating Notion DB name |
| `source_last_edited_at` | TIMESTAMPTZ NULL | source updated time |
| `schema_version` | INT NOT NULL | document schema |
| `scope` | TEXT NOT NULL DEFAULT `company` | `company` or `personal` |
| `project` | TEXT NOT NULL DEFAULT `atlas` | `atlas`, `nova`, `orbit`, `company` |
| `created_at` / `updated_at` | TIMESTAMPTZ | timestamps |

Important indexes/constraints:

- `uq_documents_source_account_external(source, source_account_id, source_external_id) NULLS NOT DISTINCT WHERE source_external_id IS NOT NULL`
- `uq_documents_company_canonical_source(source, scope, source_canonical_id) WHERE source_canonical_id IS NOT NULL AND scope='company'`
- `uq_documents_personal_canonical_source(source, scope, user_id, source_canonical_id) WHERE source_canonical_id IS NOT NULL AND scope='personal'`
- `idx_documents_source_account(source, source_account_id)`
- `idx_documents_source_canonical(source, source_canonical_id) WHERE source_canonical_id IS NOT NULL`
- `idx_documents_source_db_name(source_db_name)`
- `idx_documents_scope_updated(scope, updated_at DESC)` — corpus/문서 목록 최근 정렬 (0097)
- `idx_documents_user_updated(user_id, updated_at DESC)` — owner-scope 문서 목록 최근 정렬 (0097)

`agent_draft` rows are editor-visible P3 draft artifacts. Creation intentionally
does not trigger corpus indexing or LLM wiki authoring. Reviewer save keeps the
row as `agent_draft` and remains draft-only; explicit publish flips `source` to
`editor` and then triggers corpus indexing plus LLM wiki authoring.

Default idempotency is account-scoped. Connectors with upstream-stable identity
can set `source_canonical_id`; Notion uses this so the same page discovered by
multiple company Notion accounts maps to one company document while personal
owners remain isolated. Migration `0012` backfills Notion canonical ids and
rewires existing duplicate `connector_items` to the kept document before adding
the unique indexes.

### `corpus_chunks`

Raw-layer retrieval/indexing chunk table. Not an answer-grounding surface by itself.

| Column | Type | Notes |
|---|---|---|
| `chunk_id` | UUID PK | chunk id |
| `doc_id` | UUID FK documents ON DELETE CASCADE | source document |
| `ordinal` | INT NOT NULL | order in doc |
| `content` | TEXT NOT NULL | chunk text |
| `embedding_id` | UUID FK embeddings NULL | vector row |
| `meta` | JSONB DEFAULT `{}` | chunk metadata |
| `scope` | TEXT DEFAULT `company` | company/personal marker |
| `project` | TEXT DEFAULT `atlas` | project marker |
| `created_at` | TIMESTAMPTZ | timestamp |

Index:

- `idx_corpus_chunks_doc(doc_id, ordinal)`

### `embeddings`

pgvector table. Dimension is 1024.

| Column | Type | Notes |
|---|---|---|
| `embedding_id` | UUID PK | vector row |
| `user_id` | UUID FK users | owner/importing user |
| `kind` | TEXT NOT NULL | `corpus_chunk` or `wiki_chunk` |
| `ref_id` | UUID NOT NULL | referenced chunk/page unit |
| `vec` | `vector(1024)` NOT NULL | embedding |
| `meta` | JSONB DEFAULT `{}` | metadata |
| `schema_version` | INT NOT NULL | schema version |
| `model_version` | TEXT NOT NULL | embedding model id |
| `scope` | TEXT DEFAULT `company` | company/personal marker |
| `project` | TEXT DEFAULT `atlas` | project marker |
| `created_at` | TIMESTAMPTZ | timestamp |

Indexes:

- `idx_embeddings_user_kind(user_id, kind)`
- `idx_embeddings_user_scope_kind(user_id, scope, kind, project)`
- `idx_embeddings_vec USING ivfflat (vec vector_cosine_ops)`

---

## 4. LLM Wiki Index

Markdown files under node-local `wiki-store/` remain wiki source-of-record.
Postgres wiki tables index them for lookup/retrieval.

### `wiki_pages`

| Column | Type | Notes |
|---|---|---|
| `page_id` | UUID PK | page row |
| `slug` | TEXT NOT NULL | wiki slug |
| `kind` | TEXT NOT NULL | `source`, `claim`, `page`, `task` |
| `path` | TEXT NOT NULL | markdown path |
| `title` | TEXT NOT NULL | display title |
| `display_title` | TEXT NULL | claim 노드 그래프 라벨용 사람이 읽는 헤드라인(migration 0089) |
| `confidence` | TEXT NULL | `high`, `medium`, `low` |
| `content_hash` | TEXT NOT NULL | content hash |
| `schema_version` | INT NOT NULL | wiki schema |
| `scope` | TEXT DEFAULT `company` | company/personal marker |
| `project` | TEXT DEFAULT `atlas` | project marker |
| `owner_id` | UUID NULL | personal owner; company rows use NULL |
| `created_at` / `updated_at` | TIMESTAMPTZ | timestamps |

Constraints/indexes:

- `uq_wiki_pages_slug_scope_owner(slug, scope, owner_id) NULLS NOT DISTINCT`
- `idx_wiki_pages_scope_owner(scope, owner_id)`
- `idx_wiki_pages_project_scope(project, scope)`

### `wiki_chunks`

| Column | Type | Notes |
|---|---|---|
| `chunk_id` | UUID PK | chunk id |
| `page_id` | UUID FK wiki_pages ON DELETE CASCADE | source page |
| `ordinal` | INT NOT NULL | order |
| `content` | TEXT NOT NULL | chunk text |
| `embedding_id` | UUID FK embeddings NULL | vector row |
| `created_at` | TIMESTAMPTZ | timestamp |

Index:

- `idx_wiki_chunks_page(page_id, ordinal)`

### `wiki_links`

| Column | Type | Notes |
|---|---|---|
| `src_page_id` | UUID FK wiki_pages ON DELETE CASCADE | source page |
| `dst_slug` | TEXT NOT NULL | target slug |
| `rel` | TEXT NOT NULL | `backlink`, `supports`, `conflicts`, `derived_from` |

Primary key:

- `(src_page_id, dst_slug, rel)`

---

## 4b. KG Sync (K3)

### `kg_outbox`

K3 transactional outbox (`docs/kg-model.md` §3, migration `0047_kg_outbox`).
wiki 쓰기 공통 commit 지점(`wiki/store.py::_persist`), document
publish(`documents.py::publish_agent_draft_document`), connector/promote 문서
upsert(`documents.py::upsert_source_document`)의 PG commit과 **같은
트랜잭션**에서 enqueue되고, central API lifespan의 `KGOutboxWorker`가 Neo4j에
멱등 적용한다(`:OutboxApplied` 마커 — 단일 Cypher 트랜잭션). K7 이전에는
`scope='company'` 변경만 적재하며, `kg_enabled=false` 기간의 변경은 적재하지
않는다(flag 활성화 시 rebuild 1회가 운영 절차 — `docs/operations.md` §2.1).
legacy persona-era 동명 테이블과 무관한 신규 정의다(§12).

| Column | Type | Notes |
|---|---|---|
| `outbox_id` | UUID PK | 이벤트 id |
| `entity_kind` | TEXT NOT NULL | `wiki_page` \| `document` \| `structured_row` (CHECK) |
| `entity_id` | UUID NOT NULL | 해당 PG row PK |
| `op` | TEXT NOT NULL | `upsert` \| `delete` (CHECK) |
| `status` | TEXT NOT NULL DEFAULT `pending` | `pending` \| `applied` \| `dead` (CHECK) |
| `attempts` | INT NOT NULL DEFAULT 0 | 이벤트 실패 횟수 — 5회에서 `dead`. Neo4j 미가용은 미증가 |
| `lease_until` | TIMESTAMPTZ NULL | worker claim lease (`FOR UPDATE SKIP LOCKED` + 60s) |
| `last_error` | TEXT NULL | 마지막 실패 사유 |
| `correlation_id` | UUID NULL | audit 전파 (`kg.apply` span) |
| `enqueued_at` | TIMESTAMPTZ DEFAULT now() | enqueue 시각 |

Index:

- `idx_kg_outbox_status_enqueued(status, enqueued_at)`

`applied` row는 30일 보존 후 worker가 같은 기준의 Neo4j `:OutboxApplied`
마커와 함께 정리한다(`python -m orthus.kg.outbox trim`도 동일 경로). `dead`는
운영자 처리 전까지 보존한다. 크로스-DB 원자성은 없다 — Postgres 권위, Neo4j
eventual convergence, 불일치는 `kg-rebuild`가 수렴.

### `ask_event_jobs`

Phase 3-B MA.8a 이벤트 트리거 오케스트레이션 큐(`docs/company-agent-orchestration.md`
§P3B, migration `0079_ask_event_jobs`). 복합/실행 신호가 있는 inbound company mail이
`mail/ingest.py` post-commit 훅에서 한 행 enqueue하고, central API lifespan의
`EventOrchestrationWorker`(`kg_outbox` 패턴 — `FOR UPDATE SKIP LOCKED` + lease +
5회 dead-letter)가 claim해 `answer_or_decompose`(scope=company, knowledge-only)를
오프라인 실행한 뒤 결과를 `event_orchestration` AgentWork 브리프(`draft_for_review`)로
적재한다. sink은 트리거 소스가 아니라 큐는 구조적으로 비순환이다(불변식 29). 전부
`ORTHUS_ASK_EVENT_ORCH_ENABLED`(default false) fail-closed — off면 미적재·worker 미기동.

| Column | Type | Notes |
|---|---|---|
| `job_id` | UUID PK | job id |
| `source_kind` | TEXT NOT NULL | 트리거 소스 — 현재 `mail`만 |
| `source_ref` | TEXT NOT NULL | idempotency 키 (`mail:<canonical_id>`) |
| `seed_question` | TEXT NOT NULL | redaction 통과한 지식 framing seed |
| `scope` | TEXT NOT NULL DEFAULT `company` | MA.8a는 company 전용 |
| `project` | TEXT NULL | 부모 mail backend project |
| `created_by` | UUID NOT NULL | 오케스트레이션 acting user(resolve된 mail owner) |
| `owner_id` | UUID NULL | company-shared → NULL |
| `meta` | JSONB NOT NULL DEFAULT `{}` | 브리프용 mail ref(redacted subject 등) |
| `status` | TEXT NOT NULL DEFAULT `pending` | `pending` \| `done` \| `dead` |
| `attempts` | INT NOT NULL DEFAULT 0 | 실패 횟수 — 5회에서 `dead`. DB infra-down은 미증가(release) |
| `lease_until` | TIMESTAMPTZ NULL | worker claim lease |
| `last_error` | TEXT NULL | 마지막 실패 사유 |
| `result_work_id` | UUID NULL | 생성된 AgentWork 브리프 work_id |
| `correlation_id` | UUID NULL | audit 전파 (`router.event_orchestration` span) |
| `enqueued_at` | TIMESTAMPTZ DEFAULT now() | enqueue 시각 |

Index / 제약:

- `idx_ask_event_jobs_status_enqueued(status, enqueued_at)`
- `uq_ask_event_jobs_source(source_kind, source_ref)` — 같은 메일 재pull 시 재enqueue 차단

운영 CLI: `python -m orthus.router.event_orchestration drain`(flag+company node fail-closed).

---

## 5. Structured Query Store

### `notion_rows`

Notion DB row JSONB store. Structured `/ask` compiles read-only SQL against this table,
then validation gate decides whether execution is allowed.

| Column | Type | Notes |
|---|---|---|
| `row_id` | UUID PK | deterministic Notion row page id |
| `db_id` | TEXT NOT NULL | Notion database id |
| `db_name` | TEXT NOT NULL | Notion database name |
| `properties` | JSONB DEFAULT `{}` | row properties |
| `scope` | TEXT DEFAULT `company` | company/personal marker |
| `project` | TEXT DEFAULT `atlas` | project marker |
| `owner_id` | UUID NULL | personal owner when relevant |
| `user_id` | UUID NOT NULL | importing user |
| `updated_at` / `created_at` | TIMESTAMPTZ | timestamps |

Indexes:

- `idx_notion_rows_properties USING gin(properties)`
- `idx_notion_rows_dbname_scope(db_name, scope)`
- `idx_notion_rows_user_scope(user_id, scope)`
- `idx_notion_rows_project_scope(project, scope)`

### `structured_rows`

Generic structured facts extracted from non-Notion sources such as Slack. `/ask`
structured can query this table together with `notion_rows`; source-specific
fact extraction must keep provenance back to the originating document.

| Column | Type | Notes |
|---|---|---|
| `row_id` | UUID PK | deterministic source_doc/type/key id |
| `source` | TEXT NOT NULL | connector/source slug, e.g. `slack` |
| `record_type` | TEXT NOT NULL | `contact`, `action_item`, `event`, `decision`, `link`, etc. |
| `source_doc_id` | UUID FK documents ON DELETE CASCADE | originating normalized document |
| `source_external_id` | TEXT NULL | upstream thread/message id when available |
| `source_account_id` | UUID NULL | connector account |
| `record_key` | TEXT NOT NULL | stable key within source doc |
| `properties` | JSONB DEFAULT `{}` | extracted fields |
| `evidence` | TEXT NULL | source excerpt |
| `confidence` | TEXT DEFAULT `medium` | extraction confidence |
| `scope` / `project` / `owner_id` / `user_id` | mixed | same node/project boundary as other content tables |
| `updated_at` / `created_at` | TIMESTAMPTZ | timestamps |

Indexes:

- `uq_structured_rows_source_doc_type_key(source_doc_id, record_type, record_key)`
- `idx_structured_rows_source_type_scope(source, record_type, scope)`
- `idx_structured_rows_properties USING gin(properties)`
- `idx_structured_rows_project_scope(project, scope)`

### `query_runs`

Audit/run record for `/ask` structured branch.

| Column | Type | Notes |
|---|---|---|
| `query_id` | UUID PK | query id |
| `user_id` | UUID FK users | requester |
| `source_id` | UUID NULL | legacy audit column; external `data_sources` was removed |
| `nl_question` | TEXT NOT NULL | redacted before write |
| `compiled_sql` | TEXT NULL | redacted before write |
| `validation` | JSONB NOT NULL | gate result |
| `status` | TEXT NOT NULL | `compiled`, `validated`, `rejected`, `executed`, `failed` |
| `result_meta` | JSONB NULL | result metadata |
| `schema_version` | INT NOT NULL | schema version |
| `created_at` | TIMESTAMPTZ DEFAULT now() | run time |

Index:

- `idx_query_runs_user(user_id, created_at DESC)`

`data_sources` was dropped in migration `0005_structured_store`. Do not reintroduce
external DSN registry without a new spec/ADR.

### `kg_query_runs` (K4)

KG 읽기 게이트(`docs/kg-model.md` §4)의 run record — 모든 템플릿 실행이
reject 포함 한 행씩 남는다. 기존 `query_runs`는 확장하지 않는다(별도 테이블
결정, kg-model §4). Migration `0048_kg_query_runs`.

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID PK | 실행 id |
| `template_name` | TEXT NOT NULL | 템플릿 registry 이름 (`neighbors` 등) |
| `params_redacted` | JSONB NOT NULL DEFAULT `{}` | `redact_pii` 통과한 파라미터 |
| `status` | TEXT NOT NULL CHECK | `ok` \| `rejected` \| `timeout` \| `error` |
| `reject_reason` | TEXT NULL | reject 사유 — `redact_pii_text` 통과 후 저장 |
| `duration_ms` | INT NULL | 실행 시간 |
| `result_count` | INT NULL | 반환 노드 수 (ok일 때만) |
| `user_id` | UUID NULL | 호출자 |
| `correlation_id` | UUID NULL | `audit("kg.retrieve")` span 조인 키 |
| `created_at` | TIMESTAMPTZ DEFAULT now() | 기록 시각 |

Index:

- `idx_kg_query_runs_created(created_at)`
- `idx_kg_query_runs_template(template_name)`

### `kg_entities` / `kg_entity_mentions` (K6)

Entity 레이어의 SoR(`docs/kg-model.md` §2, 구현 명세 §9.1). LLM은 distill에서
entity 이름을 추출만 하고, 결정론 코드가 이 PG 테이블에 적재한 뒤 KG가
`:Entity`/`MENTIONED_IN`/`RELATES_TO`로 결정론 투영한다(rebuild 시 LLM 재호출
없음 — rebuildable 계약). Migration `0049_kg_entities`(down_revision
`fadb806ba302`). **batch-only**: K3 outbox `entity_kind`를 확장하지 않으며 주기
`kg-sync`/`kg-rebuild`로만 그래프에 반영된다.

`kg_entities`:

| Column | Type | Notes |
|---|---|---|
| `entity_id` | UUID PK | |
| `entity_key` | TEXT NOT NULL UNIQUE | `{entity_kind}:{name_norm}` — `:Entity` MERGE 키 |
| `entity_kind` | TEXT NOT NULL CHECK | `person`/`org`/`project`/`system` |
| `name_norm` | TEXT NOT NULL | NFKC→casefold→공백collapse→직함/조사 strip; idx 보유(교차-kind 충돌 검사) |
| `display_name` | TEXT NOT NULL | **person은 `redact_pii` 통과 후 저장** — Direct PII carve-out(operations §8, `docs/kg-model.md` §1) |
| `scope` | TEXT NOT NULL DEFAULT `company` | K6 projection은 company만 |
| `owner_id` | UUID NULL | K7 대비 — K6에서는 항상 NULL |
| `first_seen` / `last_seen` | TIMESTAMPTZ NOT NULL | |
| `schema_version` | INT NOT NULL | |
| timestamps | TIMESTAMPTZ | `updated_at`은 내용 불변 시 no-op(watermark sync quiesce) |

`kg_entity_mentions`:

| Column | Type | Notes |
|---|---|---|
| `mention_id` | UUID PK | |
| `entity_id` | UUID NOT NULL FK→`kg_entities` | |
| `page_id` | UUID NOT NULL | `wiki_pages(kind in page\|claim)`, **company scope page만** |
| `evidence_slug` | TEXT NOT NULL | RELATES_TO evidence — company page slug만 |
| `schema_version` | INT NOT NULL | |
| `created_at` | TIMESTAMPTZ | |

Index/제약: `UNIQUE(entity_id, page_id)`, `idx_kg_entities_name_norm(name_norm)`.
Erasure 시 두 테이블 모두 §8.4 절차 대상(operations.md).

---

## 6. Project / Board

### `project_overrides`

User-editable `db_name -> project` mapping.

| Column | Type | Notes |
|---|---|---|
| `db_name` | TEXT PK | Notion DB name |
| `project` | TEXT NOT NULL | `atlas`, `nova`, `orbit`, `company` |
| `updated_at` | TIMESTAMPTZ DEFAULT now() | update time |

### `task_states`

`/board` local status overlay for Notion rows.

| Column | Type | Notes |
|---|---|---|
| `row_id` | UUID PK FK notion_rows ON DELETE CASCADE | row |
| `status` | TEXT NOT NULL | board status |
| `updated_by` | UUID FK users | actor |
| `updated_at` | TIMESTAMPTZ DEFAULT now() | update time |
| `schema_version` | INT DEFAULT 1 | schema version |

Board read path uses overlay first, then `notion_rows.properties->>'상태'`.
Notion write-back success updates `notion_rows` and clears overlay.

### `personal_board_*`

Personal node `/board` workspace tables. These tables are node-local and are not a
central privacy boundary by themselves; the boundary is still the personal node
database/wiki-store/runtime. Company node `/board` continues to use `task_states`
with Notion rows.

| Table | Role |
|---|---|
| `personal_board_workspaces` | user + node workspace, timezone, display name |
| `personal_board_projects` | optional local project labels/colors |
| `personal_board_folders` | Ojosama sidebar folders: system Backlog + custom user folders |
| `personal_board_backlog_buckets` | Ojosama backlog lanes: next week/month/quarter/year, someday, never |
| `personal_board_tasks` | dated or backlog tasks, status/priority/order, free-form `note` (0093) |
| `personal_board_subtasks` | ordered checklist items under tasks |
| `personal_board_task_comments` | ticket-detail comments per task (0093, owner-scoped append log) |
| `personal_board_fixed_events` | manual calendar blocks with start/end timestamps |
| `personal_board_notes` | daily special notes/incidents/decisions |
| `personal_board_integrations` | Ojosama integration rail slots, enabled state, notification dots |
| `personal_board_preferences` | selected date, right panel, active integration, filter/sort preferences |

Migration `0014_personal_board_fks` binds these rows to users/workspaces/tasks with
foreign keys so UI/API bugs cannot leave orphan personal board records behind.
Migration `0015_personal_board_folders_integrations` adds the Ojosama sidebar
folder contract and integration rail state.

Daily sync writes a `documents` row with `source='personal_board'`,
`scope='personal'`, and a canonical id shaped like
`personal_board:daily:{node_id}:{user_id}:{yyyy-mm-dd}`. Inline LLM wiki
authoring is attempted; if local LLM/CLI authoring fails, the source document
upsert remains and later wiki rebuild/authoring can consume it.

Current API contract maps directly to these tables:

- `GET /personal-board/bootstrap` loads one workspace, default folders/backlog
  buckets/integrations, selected-date preferences, 3 visible days, and backlog
  tasks.
- `POST/PATCH /personal-board/tasks` writes `personal_board_tasks`; a task must
  belong to exactly one placement (`scheduled_date` or `backlog_bucket_id`).
  Moving without explicit `order_index` appends to the destination. `project_id`
  points at a local `personal_board_projects` channel label.
- `POST/PATCH /personal-board/projects` writes local channel labels in
  `personal_board_projects`.
- `POST /personal-board/folders` writes custom Ojosama sidebar folders in
  `personal_board_folders`. System `Backlog` is seeded deterministically, and
  `Backlog` is reserved case-insensitively so custom folder creation cannot
  mutate the system folder.
- `PATCH /personal-board/backlog-buckets/{bucket_id}` writes bucket UI state in
  `personal_board_backlog_buckets.collapsed`.
- `PATCH /personal-board/preferences` writes `selected_date`, `right_panel`,
  `active_integration`, `filter_mode`, and `sort_mode` in
  `personal_board_preferences`.
- `POST/PATCH /personal-board/tasks/{task_id}/subtasks` and
  `/personal-board/subtasks/{subtask_id}` write `personal_board_subtasks`.
- `GET/POST /personal-board/tasks/{task_id}/comments` reads/appends
  `personal_board_task_comments` (owner-scoped). Ticket notes are saved through
  the regular task PATCH (`note` field, empty string clears). Both existed only
  as FE memory state before migration 0093 and were lost on reload.
- `POST/PATCH /personal-board/fixed-events` writes
  `personal_board_fixed_events`.
  Times are stored as `TIMESTAMPTZ`; API grouping and FE display use the
  workspace timezone, not raw ISO string slicing. Updating an event re-syncs the
  union of old and new local days so moved cross-midnight blocks stay reflected
  in personal wiki daily documents.
- `POST /personal-board/notes` writes `personal_board_notes`.

Any dated task/subtask/event/note write calls daily sync for the affected date,
preserving the personal wiki update behavior used by the FE.

---

## 7. Connector Substrate

### `connector_accounts`

Connection/account policy row.

| Column | Type | Notes |
|---|---|---|
| `account_id` | UUID PK | account id |
| `connector_slug` | TEXT NOT NULL | source slug |
| `account_kind` | TEXT NOT NULL | `company` or `personal` |
| `node_id` | TEXT NOT NULL | node id |
| `scope` | TEXT NOT NULL | `company` or `personal` |
| `owner_id` | UUID FK users NULL | personal account owner |
| `project` | TEXT NULL | optional project |
| `auth_mode` | TEXT NOT NULL | `token`, `local_path`, `local_cli`, etc. |
| `account_label` | TEXT NULL | display label |
| `status` | TEXT DEFAULT `active` | `active`, `paused`, `error`, `disabled` |
| `settings_redacted` | JSONB DEFAULT `{}` | non-secret config + `secret_refs` only |
| `created_at` / `updated_at` | TIMESTAMPTZ | timestamps |

Policy check:

```sql
(account_kind = 'company' AND scope = 'company' AND owner_id IS NULL)
OR
(account_kind = 'personal' AND scope = 'personal' AND owner_id IS NOT NULL)
```

Indexes:

- `idx_connector_accounts_kind_slug(account_kind, connector_slug, status)`
- `idx_connector_accounts_owner(owner_id, connector_slug) WHERE owner_id IS NOT NULL`

### `connector_sync_state`

| Column | Type | Notes |
|---|---|---|
| `account_id` | UUID PK FK connector_accounts ON DELETE CASCADE | account |
| `cursor_json` | JSONB DEFAULT `{}` | source cursor |
| `seen_json` | JSONB DEFAULT `{}` | seen ids/hash budget |
| `daily_budget_json` | JSONB DEFAULT `{}` | budget state |
| `last_seen_id` | TEXT NULL | latest external id |
| `last_sync_at` | TIMESTAMPTZ NULL | latest sync |
| `last_error` | TEXT NULL | latest error |
| `updated_at` | TIMESTAMPTZ DEFAULT now() | update time |

### `connector_runs`

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID PK | run id |
| `account_id` | UUID FK connector_accounts ON DELETE SET NULL | account |
| `connector_slug` | TEXT NOT NULL | connector slug |
| `reason` | TEXT NOT NULL | manual, due, import, etc. |
| `status` | TEXT NOT NULL | `running`, `succeeded`, `failed` |
| `fetched` / `created` / `updated` / `skipped` / `errors` | INT DEFAULT 0 | counters |
| `error_message` | TEXT NULL | redacted error |
| `started_at` / `finished_at` | TIMESTAMPTZ | timestamps |

Index:

- `idx_connector_runs_account(account_id, started_at DESC)`

### `connector_items`

| Column | Type | Notes |
|---|---|---|
| `account_id` | UUID FK connector_accounts ON DELETE CASCADE | account |
| `external_id` | TEXT NOT NULL | source id |
| `external_version` | TEXT NULL | source revision/version |
| `content_hash` | TEXT NULL | normalized content hash |
| `doc_id` | UUID FK documents ON DELETE SET NULL | imported doc |
| `ingested_at` | TIMESTAMPTZ DEFAULT now() | ingest time |

Primary key:

- `(account_id, external_id)`

Index:

- `idx_connector_items_doc(doc_id)`

Connector boundary:

| Connector | Node policy |
|---|---|
| Notion | company or personal, account policy decides |
| Slack | company only |
| local_files | personal only |
| codex_sessions / claude_sessions | personal only |
| chat_exports / email_exports | personal only |
| GitHub | personal only |
| `gws_gmail` / `gws_drive` | personal only, node-local `gws` CLI auth |

---

## 7b. Collector (P8.2)

Thin personal collector daemon surface. Both tables are created by migration
`0044_collector_tokens_commands`. The collector API itself is fail-closed: it is
exposed only on a company-kind node with `ORTHUS_COLLECTOR_API_ENABLED=true`.

### `collector_tokens`

Owner-scoped bearer credentials for the personal collector daemon. The DB stores
only the sha256 hash of the `dct_<random>` token; the plaintext lives in the
operator's local Keychain. These tokens authenticate the ingestion/queue
endpoints only and never grant a browser session.

| Column | Type | Notes |
|---|---|---|
| `token_id` | UUID PK | token id |
| `user_id` | UUID FK users ON DELETE CASCADE | owner |
| `node_id` | TEXT NOT NULL | node the token is valid on |
| `name` | TEXT NOT NULL DEFAULT `''` | operator label |
| `token_hash` | TEXT UNIQUE NOT NULL | sha256 of `dct_` token |
| `scopes` | TEXT[] NOT NULL DEFAULT `{ingest}` | P8.7a app-enforced scope set |
| `created_at` | TIMESTAMPTZ DEFAULT now() | issue time |
| `last_used_at` | TIMESTAMPTZ NULL | stamped on each successful auth |
| `last_polled_at` | TIMESTAMPTZ NULL | command poll timestamp from `/collector/commands/pending` |
| `last_status_at` | TIMESTAMPTZ NULL | local collector heartbeat timestamp |
| `scheduler_installed` | BOOLEAN NULL | last reported launchd install state |
| `scheduler_loaded` | BOOLEAN NULL | last reported launchd loaded state |
| `scheduler_interval_seconds` | INTEGER NULL | last reported launchd interval |
| `last_status_error` | TEXT NULL | last non-secret collector status error |
| `revoked_at` | TIMESTAMPTZ NULL | revoked time |

`scopes` (added in `0045_collector_tokens_scopes`) carries the app-enforced
vocabulary `{ingest, commands, knowledge, knowledge:write}`; unknown DB values
are ignored, not fatal. Ingest requires `ingest` and the command
poll/claim/complete endpoints require `commands`, but `ingest` implies
`commands` so pre-P8.7a tokens (default `{ingest}`) keep polling. `knowledge` /
`knowledge:write` are reserved for the later MCP knowledge surface.
`last_polled_at` and heartbeat fields (added in `0047_collector_liveness`) let
central `/connectors/personal` distinguish queued work waiting for the next poll
from stale/offline Desktop Collector state. Scheduler fields are best-effort
local status only; no local paths or secrets are stored.

Index:

- `idx_collector_tokens_owner(node_id, user_id)`

### `collector_commands`

central→personal command queue. central appends rows (session operator path);
the collector daemon polls its OWN owner's pending commands and reports results
(collector-token path, outbound-only). Every read/write is scoped to a single
`user_id` so one owner can never list, claim, or complete another owner's
commands.

| Column | Type | Notes |
|---|---|---|
| `command_id` | UUID PK | command id |
| `node_id` | TEXT NOT NULL | node |
| `user_id` | UUID FK users ON DELETE CASCADE | owner |
| `kind` | TEXT CHECK | DB allows historical `file_fetch`, but public API creates only `connector_sync` and `raw_repush` |
| `payload` | JSONB DEFAULT `{}` | command input |
| `status` | TEXT DEFAULT `pending` | `pending`, `claimed`, `done`, `failed` |
| `result` | JSONB NULL | daemon-reported result |
| `created_by` | UUID NULL | session actor that created it |
| `created_at` | TIMESTAMPTZ DEFAULT now() | created time |
| `claimed_at` | TIMESTAMPTZ NULL | set on pending→claimed |
| `completed_at` | TIMESTAMPTZ NULL | set on claimed→done/failed |

Index:

- `idx_collector_commands_poll(node_id, user_id, status, created_at)`

Claim/complete are atomic owner-scoped updates
(`UPDATE ... WHERE status=... AND user_id=...`). Document ingest from the
collector writes `documents` rows with `scope='personal'` and the token owner's
`user_id`, honoring the `(source, source_account_id, source_external_id)`
idempotency contract. P8.2 does no corpus chunking/embedding or wiki authoring;
P8.4 wires the central compile step.

---

## 8. Publish / Promote

### `promote_staging`

Personal→central explicit staging gate. Pending rows are not imported into central corpus/wiki.

| Column | Type | Notes |
|---|---|---|
| `stage_id` | UUID PK | stage id |
| `source_node_id` | TEXT NOT NULL | personal source node |
| `source_doc_id` | UUID NOT NULL | personal document id |
| `source_owner_id` | UUID NULL | personal owner |
| `source_scope` | TEXT CHECK `personal` | must be personal |
| `source_title` | TEXT NOT NULL | original title |
| `sanitized_title` | TEXT NOT NULL | redacted/sanitized title |
| `sanitized_markdown` | TEXT NOT NULL | redacted/sanitized content |
| `source_meta` | JSONB DEFAULT `{}` | redacted metadata |
| `status` | TEXT DEFAULT `pending` | `pending`, `approved`, `rejected` |
| `created_by` | UUID FK users ON DELETE RESTRICT | staging actor |
| `approved_by` | UUID FK users NULL | reviewer |
| `promoted_doc_id` | UUID FK documents NULL | central doc after approval |
| `created_at` / `updated_at` / `decided_at` | TIMESTAMPTZ | timestamps |

Indexes:

- `idx_promote_staging_status_created(status, created_at DESC)`
- `idx_promote_staging_source(source_node_id, source_doc_id)`

Approval imports a central document with `source='promoted_personal'`,
`scope='company'`, and `source_external_id='promote:{source_node_id}:{source_doc_id}'`.

`source_meta` may include promote review-only sanitize summaries:

- `export_sanitization`: personal-node raw→export stats. Contains counts and
  changed flags only, never raw personal content.
- `central_sanitization`: central-node package recheck stats. Used by `/promote`
  reviewers to see whether central sanitization changed the submitted package.

---

## 9. Data Gap / Agent Work

### `data_gaps`

Answer-insufficiency backlog. `/ask` records poorly grounded wiki answers here so
data owners can add source material.

| Column | Type | Notes |
|---|---|---|
| `gap_id` | UUID PK | backlog id |
| `scope` | TEXT NOT NULL | `company` or `personal` |
| `owner_id` | UUID NULL | personal owner, NULL for company |
| `node_id` | TEXT NOT NULL | node that owns the row |
| `question_norm` | TEXT NOT NULL | dedupe key |
| `question` | TEXT NOT NULL | redacted question |
| `reason` | TEXT NOT NULL | `no_data`, `weak_retrieval`, `insufficient_grounding`, `missing_link`(K6 — KG 비연결 감지, CHECK 없는 TEXT라 migration 불필요; `GapReason` Literal만 확장) |
| `top_score` | DOUBLE PRECISION NULL | retrieval score when present |
| `suggested_target` / `suggested_connector` | TEXT NULL | optional owner guidance |
| `context_wiki_slug` | TEXT NULL | P4.5a wiki page context that produced the gap; no historic backfill |
| `suggested_fields` | JSONB DEFAULT `[]` | suggested missing sections/items |
| `suggestion_status` | TEXT DEFAULT `pending` | `pending` or `ready` |
| `hit_count` | INTEGER DEFAULT 1 | deduped occurrence count |
| `status` | TEXT DEFAULT `open` | `open`, `resolved`, `dismissed`; Agent Work `data_gap` approve/dismiss writes closure back here |
| `source` | TEXT DEFAULT `auto` | `auto` or `feedback` |
| timestamps | TIMESTAMPTZ | create/update/last seen |

Indexes:

- `uq_data_gaps_scope_owner_question(scope, COALESCE(owner_id, sentinel), question_norm)`
- `idx_data_gaps_scope_status(scope, status, hit_count DESC)`
- `idx_data_gaps_scope_owner_context_slug(scope, COALESCE(owner_id, sentinel), context_wiki_slug) WHERE context_wiki_slug IS NOT NULL`

### `agent_work_items`

P3 node-local Agent Work queue. The table is present in each node DB; it is not a
shared central/personal store. P3.1a adapts `data_gaps`; P3.1b adds reviewer
decisions; P3.1c adapts unresolved `WikiTask` rows; P3.1d adapts pending
`promote_staging` rows and failed `connector_runs`; P3.2b records reviewer
decisions as policy observations. P3.3a auto-executes cleanup-only WikiTask rows
by storing `payload.auto_execution.kind="wiki_task_cleanup"` and moving the work
item to `state='resolved'`; this writes no `agent_work_decisions` row because no
reviewer decision occurred. P3.3b auto-executes configured Assistant connector
sync commands by storing `payload.auto_execution.kind="connector_sync"` with
`connector_slug`, `account_id`, `run_id`, status/report metadata, and moving the
item to `state='resolved'` on success or `state='request_more_data'` on sync
failure. This also writes no `agent_work_decisions` row because no reviewer
decision occurred. Failed `connector_runs` source items are still triage only and
are not automatically retried; P3.3e records this as `payload.retry_guard` with
`auto_retry_allowed=false`, `requires_operator_review=true`, and
`used_for_outcome=false`. Reviewer decisions on these items do not create new
`connector_runs` rows. P3.3c auto-executes personal-board cleanup by
storing `payload.auto_execution.kind="personal_board_cleanup"` with
`cleanup_kind="archive_done_tasks"` and archived task ids/count; it moves the
work item to `state='resolved'` and writes no reviewer decision row. P3.3d writes
`source_kind='data_gap'` reviewer closure back to `data_gaps.status`: approve
means `resolved`, dismiss means `dismissed`, and request-more-data keeps the
source row `open`. The work item records this as `payload.source_writeback`.
P3.4c stores reviewable email draft template content in `payload.email_draft`
when an Assistant email command has a redacted recipient hint; missing recipient
commands stay `request_more_data` and record `payload.required_data`. P3.4d
generates `payload.email_draft` through canonical `EmailDraftPayload` with
`extra="forbid"` so SMTP/send/provider delivery fields cannot be stored by this
draft helper. P3.6a can auto-resolve eligible email drafts only through the
`fake` sender boundary and records `payload.auto_execution.kind="email_send"`;
real provider email send remains out of scope.
These slices do not run promote imports, real provider email send, destructive
board changes, external calendar/email writes, central wiki content writes, or
unbounded policy gate escalation.

| Column | Type | Notes |
|---|---|---|
| `work_id` | UUID PK | work item id |
| `node_id` / `node_kind` | TEXT | owner node; `company` or `personal` |
| `owner_id` | UUID NULL | personal owner, NULL for company |
| `source_kind` | TEXT NOT NULL | `data_gap`, `wiki_task`, `promote_staging`, `connector_run`, etc. |
| `source_ref_id` | TEXT NOT NULL | source row id/string |
| `action_family` | TEXT NOT NULL | P3 typed action family |
| `title` | TEXT NOT NULL | review title |
| `payload` | JSONB DEFAULT `{}` | redacted candidate payload; may include `policy_memory`, `auto_execution`, `draft_document`, `email_draft`, `required_data`, `retry_guard`, or `source_writeback` evidence |
| `state` | TEXT DEFAULT `pending` | `pending`, outcome states, `resolved`, `dismissed` |
| `policy_outcome` | TEXT NULL | `auto_execute`, `draft_for_review`, `request_more_data`, `reject` |
| `policy_reason` | TEXT NULL | reviewer-visible policy reason |
| `reason_codes` | JSONB DEFAULT `[]` | deterministic reason codes |
| `evidence` | JSONB DEFAULT `[]` | source refs shown in `/agent-work` |
| `correlation_id` / `last_run_id` | UUID NULL | audit correlation/run ids |
| `created_by` | UUID NULL | actor that materialized item |
| timestamps | TIMESTAMPTZ | create/update time |

Indexes:

- `uq_agent_work_node_source(node_id, source_kind, source_ref_id)`
- `idx_agent_work_node_state(node_id, state, updated_at DESC)`
- `idx_agent_work_node_outcome(node_id, policy_outcome, updated_at DESC)`

### `agent_work_decisions`

Append-only reviewer decision log for Agent Work. This is separated from
`agent_work_items` so later node-local policy memory can consume reviewer
observations without rewriting the queue row history.

| Column | Type | Notes |
|---|---|---|
| `decision_id` | UUID PK | reviewer decision id |
| `work_id` | UUID FK | references `agent_work_items(work_id)` |
| `node_id` | TEXT NOT NULL | owner node at decision time |
| `reviewer_id` | UUID NOT NULL | owner/admin reviewer user id |
| `decision` | TEXT NOT NULL | `approve`, `dismiss`, `request_more_data` |
| `note` | TEXT NULL | reviewer note, redacted before persist |
| `from_state` / `to_state` | TEXT NOT NULL | canonical Agent Work state vocabulary |
| `correlation_id` / `node_run_id` | UUID NULL | `agent_work.decision` audit correlation/run ids |
| `decided_at` | TIMESTAMPTZ | decision time |

Indexes:

- `UNIQUE(work_id)` rejects double decisions in P3.1b.
- `idx_agent_work_decisions_node_decided(node_id, decided_at DESC)`

### `agent_policy_observations`

Append-only node-local policy memory source for Agent Work reviewer decisions.
Rows are written in the same transaction as `agent_work_decisions`. They copy
deterministic policy/source slots and reviewer action counts; reviewer note
bodies are not duplicated into this table.

| Column | Type | Notes |
|---|---|---|
| `observation_id` | UUID PK | policy observation id |
| `node_id` / `node_kind` | TEXT | owner node; `company` or `personal` |
| `owner_id` | UUID NULL | personal owner scope, NULL for company |
| `work_id` | UUID FK | references `agent_work_items(work_id)` |
| `decision_id` | UUID FK UNIQUE | references `agent_work_decisions(decision_id)` |
| `reviewer_id` | UUID NOT NULL | owner/admin reviewer user id |
| `source_kind` / `source_ref_id` | TEXT | original source row identity |
| `action_family` | TEXT NOT NULL | P3 typed action family |
| `policy_outcome` | TEXT NULL | policy gate outcome at decision time |
| `reason_codes` | JSONB DEFAULT `[]` | deterministic reason codes |
| `reviewer_decision` | TEXT NOT NULL | `approve`, `dismiss`, `request_more_data` |
| `from_state` / `to_state` | TEXT NOT NULL | state transition observed |
| `note_present` | BOOLEAN DEFAULT false | true when reviewer supplied a non-empty note |
| `bucket_key` | TEXT NOT NULL | deterministic action/source/outcome/reason bucket |
| `meta` | JSONB DEFAULT `{}` | non-PII policy metadata, no note body; includes explicit `no_edit` / `no_edit_approval` telemetry when supplied |
| `observed_at` | TIMESTAMPTZ | observation time |

`POST /agent-work/policy-memory/wiki-summary` reads these rows and writes an
owner-scoped `agent-policy-memory` wiki page. That page is generated
deterministically from bucket counts and remains operator context only:
`used_for_outcome=false`; no policy outcome escalation is derived from it in
P3.5a. P3.5b extends the same summaries and `policy_memory` payload context with
explicit no-edit approval metrics for email auto-send readiness:
`no_edit_approvals`, `recent_window_days=60`, `recent_total`,
`recent_no_edit_approvals`, `recent_no_edit_approval_rate`, and
`email_auto_send_observation_threshold_met`. The threshold is evidence only and
does not alter `email_send` outcome or send mail. Server-side telemetry guard
normalizes direct API `no_edit` values to `null` unless the observation is an
`approve` decision on a `draft_for_review` item in the exact email auto-send
policy bucket. P3.5c also attaches `payload.email_auto_send_gate` to
draft-review email command work items after the bucket re-query; email commands
stopped as `request_more_data` do not carry this gate payload. That payload
records exact-bucket, personal-node, owner/admin, recipient/domain/template/rate-limit,
sensitive-content, and attachment preflight checks with `used_for_outcome=false`.

Indexes:

- `UNIQUE(decision_id)` prevents duplicate observation for one reviewer decision.
- `idx_agent_policy_obs_node_bucket(node_id, bucket_key, observed_at DESC)`
- `idx_agent_policy_obs_node_observed(node_id, observed_at DESC)`

---

### `email_send_log`

P3.6a node-local email send evidence. This is not a provider delivery table and
does not store raw recipient, subject, or body content.

| Column | Type | Notes |
|---|---|---|
| `send_id` | UUID PK | send attempt id |
| `work_id` | UUID FK | references `agent_work_items(work_id)` |
| `node_id` | TEXT NOT NULL | node that attempted the send |
| `owner_id` | UUID NOT NULL | personal owner |
| `recipient_hash` | TEXT NOT NULL | SHA-256 hash only |
| `subject_hash` | TEXT NOT NULL | SHA-256 hash only |
| `body_hash` | TEXT NOT NULL | SHA-256 hash only |
| `sender_kind` | TEXT NOT NULL | currently `fake` only |
| `status` | TEXT NOT NULL | `sent`, `failed`, or `rate_limited` |
| `sent_at` | TIMESTAMPTZ | attempt time |
| `correlation_id` | UUID NULL | audit correlation |
| `error_message` | TEXT NULL | redacted failure note |

Indexes:

- `idx_email_send_log_node_recipient(node_id, owner_id, recipient_hash, sent_at)`
- `idx_email_send_log_work(work_id, status)`

---

## 9b. Dashboard Plan Recovery

### `dashboard_entry_history`

Pre-overwrite snapshot of weekly/monthly plan content (migration 0035).
`PUT /dashboard/weekly|monthly` snapshots the PRIOR `weekly_entries` /
`monthly_entries` row here before any overwrite that actually changes content,
so an accidental wipe is recoverable. No FK/cascade: history must survive
project/entry deletion. Read-only via
`GET /dashboard/weekly/history` and `GET /dashboard/monthly/history`.

| Column | Type | Notes |
|---|---|---|
| `history_id` | UUID PK | snapshot row id |
| `node_id` | TEXT NOT NULL | company node |
| `project_id` | UUID NOT NULL | dashboard project (no FK) |
| `period_kind` | TEXT NOT NULL | `weekly` or `monthly` |
| `period` | DATE NOT NULL | week_start (Monday) or month-first |
| `plan_items` | JSONB DEFAULT `[]` | PRIOR plan content |
| `retro_items` | JSONB DEFAULT `[]` | PRIOR retro content |
| `prev_updated_at` | TIMESTAMPTZ NULL | the overwritten row's `updated_at` |
| `snapshot_at` | TIMESTAMPTZ DEFAULT now() | snapshot time |

Index:

- `ix_dashboard_entry_history_lookup(node_id, period_kind, project_id, period, snapshot_at DESC)`

### Destructive-empty optimistic-concurrency guard

`WeeklyUpsert` / `MonthlyUpsert` accept an optional `base_updated_at`, and
`WeeklyEntry` / `MonthlyEntry` expose `updated_at`. This is NOT full OCC: only
the data-destroying case is guarded. If the incoming plan+retro are both empty
AND the current row is non-empty AND `base_updated_at` is missing or does not
match the current row's `updated_at`, the write is blocked with HTTP 409 and the
current (unchanged) entry is returned in the body. An intentional clear works
because the client echoes the loaded `updated_at`; non-empty edits always apply,
even without `base_updated_at` (backward compatible).

---

## 10. Auth / Session

### `auth_identities`

External identity binding.

| Column | Type | Notes |
|---|---|---|
| `identity_id` | UUID PK | identity row |
| `user_id` | UUID FK users ON DELETE CASCADE | local user |
| `provider` | TEXT NOT NULL | `google`, `dev`, `magic_link` |
| `provider_subject` | TEXT NOT NULL | provider subject |
| `email` | TEXT NOT NULL | normalized email |
| `email_verified` | BOOLEAN DEFAULT false | provider verification |
| `display_name` | TEXT NULL | provider display |
| `created_at` | TIMESTAMPTZ DEFAULT now() | created time |
| `last_login_at` | TIMESTAMPTZ NULL | latest login |

Constraints/indexes:

- `UNIQUE(provider, provider_subject)`
- `idx_auth_identities_user(user_id)`
- `idx_auth_identities_email(email)`

### `auth_sessions`

Browser session store. DB stores token hash only.

| Column | Type | Notes |
|---|---|---|
| `session_id` | UUID PK | session id |
| `token_hash` | TEXT UNIQUE NOT NULL | hash only |
| `user_id` | UUID FK users ON DELETE CASCADE | user |
| `node_id` | TEXT NOT NULL | node session belongs to |
| `issued_at` | TIMESTAMPTZ DEFAULT now() | issue time |
| `last_seen_at` | TIMESTAMPTZ DEFAULT now() | sliding touch |
| `expires_at` | TIMESTAMPTZ NOT NULL | sliding expiry |
| `absolute_expires_at` | TIMESTAMPTZ NOT NULL | absolute expiry |
| `revoked_at` | TIMESTAMPTZ NULL | revoked/expired |
| `user_agent_hash` | TEXT NULL | hashed UA |
| `ip_hash` | TEXT NULL | hashed IP |

Indexes:

- `idx_auth_sessions_lookup(token_hash, node_id) WHERE revoked_at IS NULL`
- `idx_auth_sessions_user(user_id)`

### `auth_magic_links`

One-time email login link store. DB stores token hash only.

| Column | Type | Notes |
|---|---|---|
| `magic_link_id` | UUID PK | login link id |
| `token_hash` | TEXT UNIQUE NOT NULL | hash only |
| `node_id` | TEXT NOT NULL | node link belongs to |
| `email` | TEXT NOT NULL | normalized allowlist email |
| `next_path` | TEXT NOT NULL | safe post-login path |
| `issued_at` | TIMESTAMPTZ DEFAULT now() | issue time |
| `expires_at` | TIMESTAMPTZ NOT NULL | short TTL expiry |
| `consumed_at` | TIMESTAMPTZ NULL | set atomically on first consume attempt |
| `request_user_agent_hash` | TEXT NULL | hashed UA |
| `request_ip_hash` | TEXT NULL | hashed IP |

Indexes:

- `idx_auth_magic_links_lookup(token_hash, node_id) WHERE consumed_at IS NULL`
- `idx_auth_magic_links_email(node_id, email, issued_at DESC)`

### `auth_allowlist`

Invite-only access list per node.

| Column | Type | Notes |
|---|---|---|
| `allowlist_id` | UUID PK | row id |
| `node_id` | TEXT NOT NULL | node |
| `email` | TEXT NOT NULL | normalized email |
| `role` | TEXT NOT NULL | `owner`, `admin`, `member`, `viewer` |
| `created_by` | UUID FK users NULL | actor |
| `created_at` | TIMESTAMPTZ DEFAULT now() | created time |
| `revoked_at` | TIMESTAMPTZ NULL | revoked time |

Constraints/indexes:

- `UNIQUE(node_id, email)`
- `idx_auth_allowlist_active(node_id, email) WHERE revoked_at IS NULL`

Runtime policy prevents last active owner/admin revoke/demote and self-revoke.

---

## 11. Migration Lineage

| Migration | Current meaning |
|---|---|
| `0001_base.py` | `users`, `embeddings`, `audit_log`, pgvector extension |
| `0002_archive_secretary.py` | `documents`, `corpus_chunks`, `query_runs`; old `data_sources` created here |
| `0003_llm_wiki.py` | `wiki_pages`, `wiki_chunks`, `wiki_links`; `embeddings.kind` includes `wiki_chunk` |
| `0004_tenant_scope.py` | `scope` columns and `wiki_pages.owner_id`; central hardening, not personal privacy boundary |
| `0005_structured_store.py` | `notion_rows`; drops `data_sources`; `query_runs.source_id` nullable |
| `0006_project_dimension.py` | `project` columns and project/scope indexes |
| `0007_project_overrides.py` | `project_overrides`, `documents.source_db_name` |
| `0008_task_states.py` | `/board` status overlay |
| `0009_connector_substrate.py` | connector account/state/run/item tables; account-scoped document idempotency |
| `0010_promote_gate.py` | personal→central `promote_staging` |
| `0011_auth_sessions.py` | session auth tables and allowlist |
| `0012_document_canonical_source.py` | document canonical source id + Notion multi-account dedupe indexes |
| `0013_personal_board.py` | personal node workspace board base tables |
| `0014_personal_board_fks.py` | personal board user/workspace/task foreign key hardening |
| `0015_personal_board_folders_integrations.py` | personal board folders, integrations, active integration preference |
| `0016_structured_rows.py` | generic structured rows for non-Notion facts such as Slack contacts/links/actions/events/decisions |
| `0017_data_gaps.py` | `/ask` answer-insufficiency `data_gaps` backlog |
| `0018_agent_work_items.py` | P3 Agent Work node-local queue |
| `0019_agent_work_decisions.py` | P3 Agent Work reviewer decision log |
| `0020_agent_policy_observations.py` | P3 Agent Work policy observation memory |
| `0023_auth_magic_links.py` | allowlisted email magic-link login table |
| `0035_dashboard_entry_history.py` | weekly/monthly plan pre-overwrite snapshot + `base_updated_at` 409 guard |
| `0044_collector_tokens_commands.py` | P8.2 collector token auth + central→personal command queue |
| `0045_collector_tokens_scopes.py` | P8.7a `collector_tokens.scopes` TEXT[] (default `{ingest}`) |
| `0047_collector_liveness.py` | `collector_tokens` poll heartbeat + scheduler status fields |
| `0047_kg_outbox.py` | K3 KG transactional outbox (§4b) — legacy 동명 테이블과 무관한 신규 정의 |
| `0079_ask_event_jobs.py` | Phase 3-B MA.8a 이벤트 트리거 오케스트레이션 큐(§4b `ask_event_jobs`) |

---

## 12. Removed / Non-Current Schemas

These names may appear in old proposals or roadmap history. They are not current
Alembic head and must not be treated as active schema without a new spec decision.

| Name | Current status |
|---|---|
| `data_sources` | removed by `0005_structured_store`; external DSN registry is gone |
| `raw_events` | legacy P0/P1 event pipeline, not current |
| `decisions` | legacy confidence routing, not current |
| `execution_attempts` | legacy actuator execution, not current |
| `feedback_events` | legacy persona training loop, not current |
| `persona_snapshots` | legacy persona layer, not current |
| `kg_outbox`, `kg_change_log` | legacy persona-era 스키마는 폐기 유지. `kg_outbox`는 K3에서 wiki consolidate/document publish/promote approve 이벤트 기준의 **동명 신규 스키마**로 재정의 완료(migration `0047`, §4b — legacy 부활 아님). `kg_change_log`는 재정의 없음 — `docs/kg-model.md` §3 |
| `erasure_log` | legacy batch erasure table, not current |
| Neo4j KG schema | K-series rebuildable index로 재정의. K1 이후 컨테이너/스키마 신규 추가 — `docs/kg-model.md` |

---

## 13. Version / Redaction Rules

- `schema_version` remains on document/query/wiki/vector tables that serialize structured content.
- `embeddings.model_version` identifies embedding model used for vector rows.
- `query_runs.nl_question` and `query_runs.compiled_sql` are redacted before storage.
- `audit_log.output`, `audit_log.meta`, `audit_log.error_message`, connector output,
  wiki page/task content, and promote payloads must pass redaction before storage
  when they contain user/source text.
- Retention and erasure procedure live in `docs/operations.md`.
