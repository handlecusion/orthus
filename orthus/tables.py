"""SQLAlchemy Core table objects for app queries. Mirrors the hand-written
Alembic DDL (migrations/postgres/versions). Migrations are source of truth for
DDL; these are query handles only."""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
    LargeBinary,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("external_id", Text, unique=True),
    Column("display_name", Text, nullable=False),
    Column("preferred_timezone", Text, nullable=False, server_default="UTC"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

auth_identities = Table(
    "auth_identities",
    metadata,
    Column("identity_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_subject", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("email_verified", Boolean, nullable=False, server_default="false"),
    Column("display_name", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_login_at", DateTime(timezone=True)),
)

auth_sessions = Table(
    "auth_sessions",
    metadata,
    Column("session_id", UUID(as_uuid=True), primary_key=True),
    Column("token_hash", Text, nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("node_id", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("user_agent_hash", Text),
    Column("ip_hash", Text),
)

auth_magic_links = Table(
    "auth_magic_links",
    metadata,
    Column("magic_link_id", UUID(as_uuid=True), primary_key=True),
    Column("token_hash", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("next_path", Text, nullable=False),
    Column("issued_at", DateTime(timezone=True), server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("request_user_agent_hash", Text),
    Column("request_ip_hash", Text),
)

auth_allowlist = Table(
    "auth_allowlist",
    metadata,
    Column("allowlist_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True)),
)

# Single-use cross-node SSO ticket ids. A relying-party node records each
# consumed ticket jti so a leaked ticket cannot be replayed within its short
# TTL. The jti is random (not a secret); no email/token material is stored.
auth_sso_tickets = Table(
    "auth_sso_tickets",
    metadata,
    Column("jti", Text, primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("consumed_at", DateTime(timezone=True), server_default=func.now()),
)

embeddings = Table(
    "embeddings",
    metadata,
    Column("embedding_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("ref_id", UUID(as_uuid=True), nullable=False),
    Column("vec", Vector(1024), nullable=False),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("schema_version", Integer, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("audit_id", BigInteger, primary_key=True, autoincrement=True),
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("node_run_id", UUID(as_uuid=True), nullable=False),
    Column("node", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("output", JSONB),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("error_class", Text),
    Column("error_message", Text),
    Column("occurred_at", DateTime(timezone=True), server_default=func.now()),
)

documents = Table(
    "documents",
    metadata,
    Column("doc_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("block_json", JSONB, nullable=False),
    Column("markdown", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("source_account_id", UUID(as_uuid=True)),
    Column("source_external_id", Text),
    Column("source_canonical_id", Text),
    Column("source_db_name", Text),
    Column("source_last_edited_at", DateTime(timezone=True)),
    Column("schema_version", Integer, nullable=False),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

connector_accounts = Table(
    "connector_accounts",
    metadata,
    Column("account_id", UUID(as_uuid=True), primary_key=True),
    Column("connector_slug", Text, nullable=False),
    Column("account_kind", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True)),
    Column("project", Text),
    Column("auth_mode", Text, nullable=False),
    Column("account_label", Text),
    Column("status", Text, nullable=False, server_default="active"),
    Column("settings_redacted", JSONB, nullable=False, server_default="{}"),
    # Per-token machine identity (migration 0070). "" keeps the deterministic
    # account_id byte-identical to before; a non-empty device_id splits one
    # owner's accounts per machine.
    Column("device_id", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

connector_sync_state = Table(
    "connector_sync_state",
    metadata,
    Column("account_id", UUID(as_uuid=True), primary_key=True),
    Column("cursor_json", JSONB, nullable=False, server_default="{}"),
    Column("seen_json", JSONB, nullable=False, server_default="{}"),
    Column("daily_budget_json", JSONB, nullable=False, server_default="{}"),
    Column("last_seen_id", Text),
    Column("last_sync_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

connector_runs = Table(
    "connector_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("account_id", UUID(as_uuid=True)),
    Column("connector_slug", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("fetched", Integer, nullable=False, server_default="0"),
    Column("created", Integer, nullable=False, server_default="0"),
    Column("updated", Integer, nullable=False, server_default="0"),
    Column("skipped", Integer, nullable=False, server_default="0"),
    Column("errors", Integer, nullable=False, server_default="0"),
    Column("error_message", Text),
    Column("started_at", DateTime(timezone=True), server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
)

connector_items = Table(
    "connector_items",
    metadata,
    Column("account_id", UUID(as_uuid=True), primary_key=True),
    Column("external_id", Text, primary_key=True),
    Column("external_version", Text),
    Column("content_hash", Text),
    Column("doc_id", UUID(as_uuid=True)),
    Column("ingested_at", DateTime(timezone=True), server_default=func.now()),
)

corpus_chunks = Table(
    "corpus_chunks",
    metadata,
    Column("chunk_id", UUID(as_uuid=True), primary_key=True),
    Column("doc_id", UUID(as_uuid=True), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding_id", UUID(as_uuid=True)),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

notion_rows = Table(
    "notion_rows",
    metadata,
    Column("row_id", UUID(as_uuid=True), primary_key=True),
    Column("db_id", Text, nullable=False),
    Column("db_name", Text, nullable=False),
    Column("properties", JSONB, nullable=False, server_default="{}"),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("owner_id", UUID(as_uuid=True)),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

structured_rows = Table(
    "structured_rows",
    metadata,
    Column("row_id", UUID(as_uuid=True), primary_key=True),
    Column("source", Text, nullable=False),
    Column("record_type", Text, nullable=False),
    Column("source_doc_id", UUID(as_uuid=True)),
    Column("source_external_id", Text),
    Column("source_account_id", UUID(as_uuid=True)),
    Column("record_key", Text, nullable=False),
    Column("properties", JSONB, nullable=False, server_default="{}"),
    Column("evidence", Text),
    Column("confidence", Text, nullable=False, server_default="medium"),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("owner_id", UUID(as_uuid=True)),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

query_runs = Table(
    "query_runs",
    metadata,
    Column("query_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True)),
    Column("nl_question", Text, nullable=False),
    Column("compiled_sql", Text),
    Column("validation", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("result_meta", JSONB),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

wiki_pages = Table(
    "wiki_pages",
    metadata,
    Column("page_id", UUID(as_uuid=True), primary_key=True),
    Column("slug", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("confidence", Text),
    # KG claim 노드 사람이 읽는 헤드라인 라벨(nullable). NULL이면 그래프 투영이
    # 기존 title(=claim slug)로 폴백한다. LLM은 wiki 저작/백필에서만 생성해 저장하고,
    # KG 투영은 저장값을 slug처럼 복사만 한다(투영 LLM 0회 유지).
    Column("display_title", Text),
    Column("content_hash", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text, nullable=False, server_default="atlas"),
    Column("owner_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index(
        "uq_wiki_pages_slug_scope_owner",
        "slug",
        "scope",
        "owner_id",
        unique=True,
        postgresql_nulls_not_distinct=True,
    ),
)

# Phase 3-A semantic answer cache (MA.7a/MA.7b) — docs/company-agent-orchestration.md §P3A.
# company-scope grounded /ask answers only. `question_embedding`/`question_redacted` back
# the MA.7b embedding-similarity match (populated only when the semantic sub-flag is on).
# `question_embedding` carries NO index — the semantic lookup ranks a selective, GC-bounded
# partition by exact cosine distance (migration 0078 dropped the 0077 ivfflat index so the
# ranking stays exact/deterministic, 불변식 25; see cache.py::_semantic_lookup).
ask_cache = Table(
    "ask_cache",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("project", Text, nullable=False),
    Column("federation", Boolean, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("question_key", Text, nullable=False),
    Column("question_embedding", Vector(1024)),
    Column(
        "question_redacted", Text
    ),  # redacted+normalized question text (MA.7b; NULL when sub-flag off)
    Column("watermark", Text, nullable=False),
    Column("answer_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index(
        "uq_ask_cache_key",
        "owner_id",
        "scope",
        "project",
        "federation",
        "node_id",
        "question_key",
        unique=True,
    ),
)

wiki_chunks = Table(
    "wiki_chunks",
    metadata,
    Column("chunk_id", UUID(as_uuid=True), primary_key=True),
    Column("page_id", UUID(as_uuid=True), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

wiki_links = Table(
    "wiki_links",
    metadata,
    Column("src_page_id", UUID(as_uuid=True), nullable=False),
    Column("dst_slug", Text, nullable=False),
    Column("rel", Text, nullable=False),
)

project_overrides = Table(
    "project_overrides",
    metadata,
    Column("db_name", Text, primary_key=True),
    Column("project", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

task_states = Table(
    "task_states",
    metadata,
    Column("row_id", UUID(as_uuid=True), primary_key=True),
    Column("status", Text, nullable=False),
    Column("updated_by", UUID(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("schema_version", Integer, nullable=False, server_default="1"),
)

promote_staging = Table(
    "promote_staging",
    metadata,
    Column("stage_id", UUID(as_uuid=True), primary_key=True),
    Column("source_node_id", Text, nullable=False),
    Column("source_doc_id", UUID(as_uuid=True), nullable=False),
    Column("source_owner_id", UUID(as_uuid=True)),
    Column("source_scope", Text, nullable=False),
    Column("source_title", Text, nullable=False),
    Column("sanitized_title", Text, nullable=False),
    Column("sanitized_markdown", Text, nullable=False),
    Column("source_meta", JSONB, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("created_by", UUID(as_uuid=True), nullable=False),
    Column("approved_by", UUID(as_uuid=True)),
    Column("promoted_doc_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("decided_at", DateTime(timezone=True)),
)

personal_board_workspaces = Table(
    "personal_board_workspaces",
    metadata,
    Column("workspace_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("timezone", Text, nullable=False, server_default="Asia/Seoul"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_projects = Table(
    "personal_board_projects",
    metadata,
    Column("project_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("color", Text),
    # 'personal'(사적 채널) | 'company'(담당 회사 프로젝트와 연결된 채널).
    # company_project_id = 연결된 dashboard_projects.project_id (kind='company'일 때).
    Column("kind", Text, nullable=False, server_default="personal"),
    Column("company_project_id", UUID(as_uuid=True)),
    Column("archived_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_folders = Table(
    "personal_board_folders",
    metadata,
    Column("folder_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("order_index", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_backlog_buckets = Table(
    "personal_board_backlog_buckets",
    metadata,
    Column("bucket_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("key", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("badge", Text, nullable=False),
    Column("color", Text, nullable=False),
    Column("order_index", Integer, nullable=False),
    Column("collapsed", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_tasks = Table(
    "personal_board_tasks",
    metadata,
    Column("task_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("backlog_bucket_id", UUID(as_uuid=True)),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="open"),
    Column("priority", Text, nullable=False, server_default="normal"),
    Column("scheduled_date", Date),
    Column("scheduled_time", Time),
    # Optional end of the scheduled time block (start -> end). Valid only when
    # scheduled_time (start) is set; NULL = no end / point-in-time start.
    Column("scheduled_end_time", Time),
    # "복귀불가" flag: the person is not coming back after this schedule. Independent
    # of scheduled_end_time (may be set with or without an end time). Surfaced as a
    # team-schedule card badge.
    Column("no_return", Boolean, nullable=False, server_default="false"),
    # Optional deadline (due date). due_time may be set only when due_date is set;
    # NULL due_time = whole-day deadline. Independent of scheduled_date placement.
    Column("due_date", Date),
    Column("due_time", Time),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("source_kind", Text, nullable=False, server_default="manual"),
    Column("source_label", Text),
    # Mirrored team calendar event (board task <-> team schedule link). NULL = not linked.
    Column("team_event_id", UUID(as_uuid=True)),
    # 'personal'(소유자 전용) | 'company'(회사 프로젝트 채널 업무 → 회사 구성원 전체가
    # 데이터로 조회). company_project_id = 연결된 dashboard_projects.project_id
    # (scope='company'일 때). 채널의 kind에서 파생되며 회사쪽 집계의 fail-closed 경계다.
    Column("scope", Text, nullable=False, server_default="personal"),
    Column("company_project_id", UUID(as_uuid=True)),
    # 티켓 상세의 자유 노트(0093). NULL = 노트 없음.
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 티켓 상세 팝업 댓글(0093). task 삭제 시 함께 삭제(FK CASCADE는 DDL에만 존재).
personal_board_task_comments = Table(
    "personal_board_task_comments",
    metadata,
    Column("comment_id", UUID(as_uuid=True), primary_key=True),
    Column("task_id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_subtasks = Table(
    "personal_board_subtasks",
    metadata,
    Column("subtask_id", UUID(as_uuid=True), primary_key=True),
    Column("task_id", UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("completed", Boolean, nullable=False, server_default="false"),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_fixed_events = Table(
    "personal_board_fixed_events",
    metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("title", Text, nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("ends_at", DateTime(timezone=True), nullable=False),
    Column("source_kind", Text, nullable=False, server_default="manual"),
    Column("source_label", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_notes = Table(
    "personal_board_notes",
    metadata,
    Column("note_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("note_date", Date, nullable=False),
    Column("kind", Text, nullable=False, server_default="note"),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_integrations = Table(
    "personal_board_integrations",
    metadata,
    Column("integration_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default="false"),
    Column("has_notification", Boolean, nullable=False, server_default="false"),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_board_preferences = Table(
    "personal_board_preferences",
    metadata,
    Column("workspace_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("selected_date", Date),
    Column("right_panel", Text, nullable=False, server_default="backlog"),
    Column("active_integration", Text),
    Column("filter_mode", Text, nullable=False, server_default="all"),
    Column("sort_mode", Text, nullable=False, server_default="manual"),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_weekly_objectives = Table(
    "personal_weekly_objectives",
    metadata,
    Column("objective_id", UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("title", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("day_allocations", JSONB, nullable=False, server_default="[]"),
    Column("completed", Boolean, nullable=False, server_default="false"),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("note", Text),
    # 'manual'(사용자 직접 생성) | 'company_plan'(회사 주간계획 담당 항목에서 자동 생성).
    # 회사 부여 목표는 제목/프로젝트가 회사 소유라 동기화되고, day_allocations/완료/메모는
    # 개인이 소유한다. source_plan_item_id = 원본 weekly_entries.plan_items[i].id(멱등 키).
    Column("source_kind", Text, nullable=False, server_default="manual"),
    Column("source_plan_item_id", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_objective_subtasks = Table(
    "personal_objective_subtasks",
    metadata,
    Column("subtask_id", UUID(as_uuid=True), primary_key=True),
    Column("objective_id", UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("completed", Boolean, nullable=False, server_default="false"),
    Column("order_index", Integer, nullable=False, server_default="0"),
    Column("weekday", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

personal_objective_comments = Table(
    "personal_objective_comments",
    metadata,
    Column("comment_id", UUID(as_uuid=True), primary_key=True),
    Column("objective_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("body", Text, nullable=False),
    Column("weekday", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Data-gap backlog: questions the /ask wiki path could not ground well, recorded
# so a data owner can see what knowledge is missing and add it. See orthus/wiki/gap.py.
data_gaps = Table(
    "data_gaps",
    metadata,
    Column("gap_id", UUID(as_uuid=True), primary_key=True),
    Column("scope", Text, nullable=False, server_default="company"),
    Column("owner_id", UUID(as_uuid=True)),
    Column("node_id", Text, nullable=False),
    Column("question_norm", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("top_score", Float),
    Column("suggested_target", Text),
    Column("suggested_connector", Text),
    Column("context_wiki_slug", Text),
    Column("suggested_fields", JSONB, nullable=False, server_default="[]"),
    Column("suggestion_status", Text, nullable=False, server_default="pending"),
    Column("hit_count", Integer, nullable=False, server_default="1"),
    Column("status", Text, nullable=False, server_default="open"),
    Column("source", Text, nullable=False, server_default="auto"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), server_default=func.now()),
)

# P3 Agent Work: node-local review queue for deterministic policy-gated work.
# P3.1a adapts data_gaps; P3.1c also adapts WikiTask rows.
agent_work_items = Table(
    "agent_work_items",
    metadata,
    Column("work_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("node_kind", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True)),
    Column("source_kind", Text, nullable=False),
    Column("source_ref_id", Text, nullable=False),
    Column("action_family", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("state", Text, nullable=False, server_default="pending"),
    Column("policy_outcome", Text),
    Column("policy_reason", Text),
    Column("reason_codes", JSONB, nullable=False, server_default="[]"),
    Column("evidence", JSONB, nullable=False, server_default="[]"),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("last_run_id", UUID(as_uuid=True)),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

agent_work_decisions = Table(
    "agent_work_decisions",
    metadata,
    Column("decision_id", UUID(as_uuid=True), primary_key=True),
    Column("work_id", UUID(as_uuid=True), nullable=False),
    Column("node_id", Text, nullable=False),
    Column("reviewer_id", UUID(as_uuid=True), nullable=False),
    Column("decision", Text, nullable=False),
    Column("note", Text),
    Column("from_state", Text, nullable=False),
    Column("to_state", Text, nullable=False),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("node_run_id", UUID(as_uuid=True)),
    Column("decided_at", DateTime(timezone=True), server_default=func.now()),
)

agent_policy_observations = Table(
    "agent_policy_observations",
    metadata,
    Column("observation_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("node_kind", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True)),
    Column("work_id", UUID(as_uuid=True), nullable=False),
    Column("decision_id", UUID(as_uuid=True), nullable=False),
    Column("reviewer_id", UUID(as_uuid=True), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_ref_id", Text, nullable=False),
    Column("action_family", Text, nullable=False),
    Column("policy_outcome", Text),
    Column("reason_codes", JSONB, nullable=False, server_default="[]"),
    Column("reviewer_decision", Text, nullable=False),
    Column("from_state", Text, nullable=False),
    Column("to_state", Text, nullable=False),
    Column("note_present", Boolean, nullable=False, server_default="false"),
    Column("bucket_key", Text, nullable=False),
    Column("meta", JSONB, nullable=False, server_default="{}"),
    Column("observed_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_agent_policy_obs_node_bucket", "node_id", "bucket_key", "observed_at"),
    Index("idx_agent_policy_obs_node_observed", "node_id", "observed_at"),
)

email_send_log = Table(
    "email_send_log",
    metadata,
    Column("send_id", UUID(as_uuid=True), primary_key=True),
    # P3 agent-work auto-sends carry a work_id; P6.3 manual sends set origin
    # "manual" with work_id NULL.
    Column("work_id", UUID(as_uuid=True)),
    Column("node_id", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("recipient_hash", Text, nullable=False),
    Column("subject_hash", Text, nullable=False),
    Column("body_hash", Text, nullable=False),
    Column("sender_kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("origin", Text, nullable=False, server_default="agent_work"),
    Column("sent_at", DateTime(timezone=True), server_default=func.now()),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("error_message", Text),
    Index("idx_email_send_log_node_recipient", "node_id", "owner_id", "recipient_hash", "sent_at"),
    Index("idx_email_send_log_work", "work_id", "status"),
)

# 메일 명함(서명) — 작성한 메일 끝에 넣는 본인 비즈니스 카드. owner-scope 개인
# 데이터로 (node_id, owner_id, from_addr)당 1행 upsert. from_addr=''는
# legacy/default 명함이다. 본문 HTML 렌더는 FE가 필드에서 만들고 서버는 구조화
# 필드만 보관한다(migration 0082, 0088).
mail_signatures = Table(
    "mail_signatures",
    metadata,
    Column("signature_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("from_addr", Text, nullable=False, server_default=""),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("title", Text, nullable=False, server_default=""),
    Column("company", Text, nullable=False, server_default=""),
    Column("email", Text, nullable=False, server_default=""),
    Column("phone", Text, nullable=False, server_default=""),
    Column("website", Text, nullable=False, server_default=""),
    # 외부/소셜 링크: [{"label": "LinkedIn", "url": "https://..."}, ...]
    Column("links", JSONB, nullable=False, server_default="[]"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index("uq_mail_signatures_owner_from", "node_id", "owner_id", "from_addr", unique=True),
)

# 메일 열람 추적(읽음) — 보낸 HTML 메일 끝에 orthus 공개 픽셀을 심고, 수신자가
# 열 때 픽셀 로드를 1건 기록한다. provider 협조 불필요(orthus가 본문 HTML을
# 직접 조립). email_send_log와 동일하게 hash-only 컬럼만 보관해 평문 PII는
# 저장하지 않는다(recipient/subject는 sha256). token은 추측 불가 랜덤이고
# 공개 픽셀 엔드포인트가 이 token만으로 open을 증가시킨다(정보 비노출).
# fail-closed: `ORTHUS_MAIL_OPEN_TRACKING_ENABLED`가 켜지고 공개 base URL이
# 설정된 경우에만 행이 생성된다(migration 0083).
mail_tracking = Table(
    "mail_tracking",
    metadata,
    Column("tracking_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("token", Text, nullable=False),
    Column("recipient_hash", Text, nullable=False),
    Column("subject_hash", Text, nullable=False),
    Column("sender_kind", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("open_count", Integer, nullable=False, server_default="0"),
    Column("first_opened_at", DateTime(timezone=True)),
    Column("last_opened_at", DateTime(timezone=True)),
    Index("uq_mail_tracking_token", "token", unique=True),
    Index("idx_mail_tracking_owner", "node_id", "owner_id", "created_at"),
)

# --- Company dashboard (migration 0023) ---

team_members = Table(
    "team_members",
    metadata,
    Column("member_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("user_id", UUID(as_uuid=True)),
    Column("name", Text, nullable=False),
    Column("title", Text),
    Column("department", Text),
    Column("email", Text),
    Column("phone", Text),
    Column("join_date", Date),
    Column("birthday", Date),
    Column("address", Text),
    Column("emergency_contact", Text),
    Column("bank_account", Text),
    Column("color", Text),
    Column("bio", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("source", Text, nullable=False, server_default="manual"),
    Column("source_ref", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 인재영입(recruiting) 후보 리스트 — 앞으로 영입하거나 킵인터치할 사람들의 CRM.
# Notion '영입 후보' DB를 본떴으되 검증 메모는 제외하고 전화번호를 추가했다.
recruiting_candidates = Table(
    "recruiting_candidates",
    metadata,
    Column("candidate_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("role", Text),  # 분야 / 포지션
    Column("education", Text),  # 학력(전공)
    Column("phone", Text),  # 전화번호
    Column("email", Text),
    Column("linkedin", Text),  # 링크드인
    Column("send_status", Text, nullable=False, server_default="컨택전"),  # 상태
    Column("note", Text),  # 메모 — 정리 + 논문·작업물·프로필 링크 포함
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 후보별 팀원 코멘트 — 팀원이 각자 메모를 남겨 영입 여부를 함께 정리한다.
recruiting_candidate_comments = Table(
    "recruiting_candidate_comments",
    metadata,
    Column("comment_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("candidate_id", UUID(as_uuid=True), nullable=False),
    Column("author_member_id", UUID(as_uuid=True)),  # team_members.member_id (optional)
    Column("author_name", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# 회사 KPI — OKR + North Star. 단일 테이블 + parent_id 계층(NSM→Objective→KR→Target).
# FK는 migration에서 선언(여기는 query handle). cadence/period로 월간·분기·년간 관리.
dashboard_kpis = Table(
    "dashboard_kpis",
    metadata,
    Column("kpi_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("parent_id", UUID(as_uuid=True)),  # self ref (FK in migration, CASCADE)
    Column("level", Text, nullable=False),  # north_star|objective|key_result|target
    Column("cadence", Text, nullable=False),  # annual|quarterly|monthly
    Column("period_start", Date),  # 정규화 기준일(연 1/1, 분기 첫달 1일, 월 1일)
    Column("fiscal_year", Integer, nullable=False),
    Column("quarter", Integer),  # 1~4
    Column("month", Integer),  # 1~12
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("metric_type", Text, nullable=False, server_default="number"),
    Column("unit", Text),
    Column("baseline", Numeric(18, 4)),
    Column("target", Numeric(18, 4)),
    Column("current_value", Numeric(18, 4)),
    Column("direction", Text, nullable=False, server_default="up"),  # up|down
    Column("project_id", UUID(as_uuid=True)),  # dashboard_projects (FK in migration)
    Column("owner_member_id", UUID(as_uuid=True)),  # team_members (FK in migration)
    Column("status", Text, nullable=False, server_default="on_track"),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    # 주기말 공식 채점(0~10 정수, NULL=미채점 — 0은 유효). 재채점=덮어쓰기,
    # 이력 없음(grade_note가 근거 담당). 마이그레이션 0099.
    Column("grade", Integer),
    Column("grade_note", Text),
    Column("graded_at", DateTime(timezone=True)),
    Column("graded_by", UUID(as_uuid=True)),  # team_members (FK in migration, SET NULL)
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# KR 주간 신뢰도 체크인 — (node, kpi, 일요일 시작 주) 당 1값 upsert. 1~10(0 없음).
# progress/rollup에 절대 유입하지 않는 표시 전용 신호. 마이그레이션 0099.
dashboard_kpi_confidence = Table(
    "dashboard_kpi_confidence",
    metadata,
    Column("confidence_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("kpi_id", UUID(as_uuid=True), nullable=False),  # FK in migration, CASCADE
    Column("week_start", Date, nullable=False),
    Column("confidence", Integer, nullable=False),
    Column("note", Text),
    Column("author_member_id", UUID(as_uuid=True)),  # FK in migration, SET NULL
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# KPI 진척 체크인 로그(append) — 작성 시 dashboard_kpis.current_value/status 동기화.
dashboard_kpi_checkins = Table(
    "dashboard_kpi_checkins",
    metadata,
    Column("checkin_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("kpi_id", UUID(as_uuid=True), nullable=False),
    Column("value", Numeric(18, 4)),
    Column("status", Text),
    Column("note", Text),
    Column("author_member_id", UUID(as_uuid=True)),
    Column("checkin_date", Date, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

dashboard_projects = Table(
    "dashboard_projects",
    metadata,
    Column("project_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    # 하위 프로젝트 계층: 루트는 NULL, 하위는 부모 project_id (self-FK, 마이그레이션
    # 0089, ON DELETE CASCADE). 깊이 2단계 제한은 앱 레이어(orthus/dashboard.py).
    Column("parent_project_id", UUID(as_uuid=True)),
    # SE 단계 (srr|sdr|pdr|cdr|vnv|ops, 마이그레이션 0090). NULL=SE 미적용.
    Column("se_stage", Text),
    Column("name", Text, nullable=False),
    Column("color", Text),
    Column("description", Text),
    Column("body", Text),
    Column("status", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true"),
    # 트래킹 필드 (마이그레이션 0094): 기간/오너(DRI)/건강 신호. health는
    # on_track|at_risk|off_track (앱 레이어 검증), owner_member_id는 team_members 참조(느슨).
    Column("start_date", Date),
    Column("target_date", Date),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("health", Text),
    Column("updated_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# 프로젝트 활동 로그 (마이그레이션 0094): 필드 변경·요구조건·배정·보드 행
# 생성/삭제를 append-only로 남긴다. before/after는 표시용 문자열(민감값 없음).
project_activity = Table(
    "project_activity",
    metadata,
    Column("activity_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("actor_user_id", UUID(as_uuid=True)),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text),
    Column("action", Text, nullable=False),
    Column("field", Text),
    Column("before", Text),
    Column("after", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# 프로젝트 SE 요구조건 대장 (마이그레이션 0090, docs/project-se-management.md).
# kind='constraint'(외부 제약조건)|'goal'(프로젝트 목표). num은 kind별 순번.
# parent_requirement_id는 상위 프로젝트 요구조건에서 flow-down된 원본 링크.
project_requirements = Table(
    "project_requirements",
    metadata,
    Column("requirement_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("num", Integer, nullable=False),
    Column("kind", Text, nullable=False, server_default="goal"),
    Column("text", Text, nullable=False),
    Column("verify_method", Text),
    Column("notes", Text),
    Column("status", Text, nullable=False, server_default="open"),
    Column("parent_requirement_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 노션식 페이지. kind='memo'는 메모 목록의 최상위, 'page'는 본문 안 중첩 하위
# 페이지(subpage 블록이 page_id로 참조)다.
dashboard_pages = Table(
    "dashboard_pages",
    metadata,
    Column("page_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="page"),
    Column("title", Text, nullable=False, server_default="새 페이지"),
    Column("icon", Text),
    Column("body", Text),
    # 메모 소유자 user_id (migration 0095). NULL = 레거시 회사 공용(보존).
    # 접근 게이트는 최상위 메모(kind='memo') 단위이고, 본문 안 subpage(kind='page')는
    # 여전히 node 스코프다(부모 메모를 열 수 있어야만 도달).
    Column("owner_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 메모 공유 (migration 0095). 소유자가 특정 팀원(team_members.user_id)에게 메모를
# view/edit로 공유하고, 수신자는 acknowledged_at로 "얼라인 확인"을 남긴다.
note_shares = Table(
    "note_shares",
    metadata,
    Column("share_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("page_id", UUID(as_uuid=True), nullable=False),
    Column("shared_with", UUID(as_uuid=True), nullable=False),
    # 'view' | 'edit'
    Column("permission", Text, nullable=False, server_default="view"),
    Column("created_by", UUID(as_uuid=True), nullable=False),
    Column("acknowledged_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_note_shares_target", "node_id", "shared_with"),
    Index("ix_note_shares_page", "page_id"),
)

# 메모 ↔ 회사 프로젝트(dashboard_projects) N:N 링크 (migration 0095).
note_project_links = Table(
    "note_project_links",
    metadata,
    Column("page_id", UUID(as_uuid=True), primary_key=True),
    Column("project_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_note_project_links_project", "project_id"),
)

# 노션식 인라인 데이터베이스. 한 프로젝트(또는 페이지)에 임베드되는 행 컬렉션으로,
# properties(타입 있는 속성 정의)와 views(표/보드)를 가진다. 칸반 보드는 select/status
# 속성으로 group_by 한 board view일 뿐이고, 표와 같은 데이터를 다르게 본다(노션 모델).
project_databases = Table(
    "project_databases",
    metadata,
    Column("database_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    # 소유 프로젝트(dashboard_projects.project_id). 페이지 임베드도 가능하게 nullable.
    Column("project_id", UUID(as_uuid=True)),
    Column("title", Text, nullable=False, server_default="데이터베이스"),
    Column("icon", Text),
    # 속성 정의 배열: [{id,name,type,options?:[{id,name,color}]}]. type ∈
    # title|text|number|select|status|multi_select|date|checkbox|person|url.
    Column("properties", JSONB, nullable=False, server_default="[]"),
    # 뷰 배열: [{id,name,type:'table'|'board',group_by?:propId,hidden?:[propId]}].
    Column("views", JSONB, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 데이터베이스의 한 행(= 노션의 한 페이지). props는 property id → 값 맵.
project_database_rows = Table(
    "project_database_rows",
    metadata,
    Column("row_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("database_id", UUID(as_uuid=True), nullable=False),
    Column("props", JSONB, nullable=False, server_default="{}"),
    # 노션처럼 행 자체가 페이지다: icon + 커버 이미지 URL + 자유 본문(BlockNote JSON 문자열).
    Column("icon", Text),
    Column("cover", Text),
    Column("body", Text),
    # 보드 컬럼/표 내 수동 정렬용. 사이 삽입을 위해 double precision.
    Column("sort_order", Float, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_project_database_rows_db", "database_id"),
)

project_database_files = Table(
    "project_database_files",
    metadata,
    Column("file_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("database_id", UUID(as_uuid=True), nullable=False),
    Column("row_id", UUID(as_uuid=True), nullable=False),
    Column("filename", Text, nullable=False),
    Column("mime_type", Text),
    Column("kind", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_project_database_files_row", "node_id", "database_id", "row_id"),
    Index("ix_project_database_files_db", "database_id"),
)

project_assignments = Table(
    "project_assignments",
    metadata,
    Column("assignment_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("member_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 노드별 역할 옵션(노션 선택 속성처럼 추가/이름변경/색/삭제). project_assignments.role은
# 여전히 자유 텍스트이고, 이 테이블은 드롭다운 옵션의 SoR이다.
project_roles = Table(
    "project_roles",
    metadata,
    Column("role_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("color", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

weekly_entries = Table(
    "weekly_entries",
    metadata,
    Column("entry_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("plan_items", JSONB, nullable=False, server_default="[]"),
    Column("retro_items", JSONB, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

monthly_entries = Table(
    "monthly_entries",
    metadata,
    Column("entry_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("month", Date, nullable=False),
    Column("plan_items", JSONB, nullable=False, server_default="[]"),
    Column("retro_items", JSONB, nullable=False, server_default="[]"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# Pre-overwrite snapshot of weekly/monthly plan content. Each upsert that
# actually changes content first captures the PRIOR row here so an accidental
# wipe is recoverable. No FK/cascade: history must survive project/entry delete.
dashboard_entry_history = Table(
    "dashboard_entry_history",
    metadata,
    Column("history_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("period_kind", Text, nullable=False),
    Column("period", Date, nullable=False),
    Column("plan_items", JSONB, nullable=False, server_default="[]"),
    Column("retro_items", JSONB, nullable=False, server_default="[]"),
    Column("prev_updated_at", DateTime(timezone=True)),
    Column("snapshot_at", DateTime(timezone=True), server_default=func.now()),
    Index(
        "ix_dashboard_entry_history_lookup",
        "node_id",
        "period_kind",
        "project_id",
        "period",
        "snapshot_at",
    ),
)

team_calendar_events = Table(
    "team_calendar_events",
    metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("member_id", UUID(as_uuid=True)),
    Column("member_ids", JSONB, nullable=False, server_default="[]"),
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("all_day", Boolean, nullable=False, server_default="true"),
    Column("event_date", Date, nullable=False),
    Column("end_date", Date),
    Column("start_time", Time),
    Column("end_time", Time),
    # Mirrors personal_board_tasks.no_return so the "복귀불가" badge survives the
    # board <-> team-schedule round-trip sync.
    Column("no_return", Boolean, nullable=False, server_default="false"),
    # "복귀 시간" — 담당자가 복귀하는 시각(NULL=미지정). no_return과 상호 배타적이다
    # (no_return=true면 앱 레이어가 NULL로 정리). 복귀 불가는 UI에서 집 이모지 표시.
    Column("return_time", Time),
    # 반복 일정(루틴): 마스터 행 하나만 저장하고 조회 시 회차로 펼친다.
    # repeat_freq NULL=반복 없음 | daily|weekly|biweekly|monthly.
    # repeat_weekdays는 Python date.weekday() 규약(0=월…6=일) int 목록.
    Column("repeat_freq", Text),
    Column("repeat_weekdays", JSONB, nullable=False, server_default="[]"),
    Column("repeat_until", Date),
    # 추가 날짜: 반복 규칙과 별개로, 같은 일정을 붙일 임의 시작일(ISO date 문자열)
    # 목록. event_date + 반복 회차 + extra_dates가 조회 시 함께 펼쳐진다.
    Column("extra_dates", JSONB, nullable=False, server_default="[]"),
    Column("starts_at", DateTime(timezone=True)),
    Column("ends_at", DateTime(timezone=True)),
    Column("event_type", Text, nullable=False, server_default="event"),
    Column("color", Text),
    Column("project_id", UUID(as_uuid=True)),
    Column("location", Text),
    Column("created_by", UUID(as_uuid=True)),
    # Originating personal board task, when this event mirrors one (NULL = native event).
    Column("source_task_id", UUID(as_uuid=True)),
    Column("source", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

finance_subscriptions = Table(
    "finance_subscriptions",
    metadata,
    Column("sub_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("vendor", Text),
    Column("plan", Text),
    Column("billing_cycle", Text, nullable=False, server_default="monthly"),
    Column("amount", Numeric(14, 2), nullable=False, server_default="0"),
    Column("currency", Text, nullable=False, server_default="KRW"),
    Column("next_billing_date", Date),
    Column("status", Text, nullable=False, server_default="active"),
    Column("category", Text),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("masked_account", Text),
    Column("notes", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

finance_api_keys = Table(
    "finance_api_keys",
    metadata,
    Column("key_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("service_name", Text, nullable=False),
    Column("label", Text),
    Column("key_last4", Text),
    Column("environment", Text, nullable=False, server_default="prod"),
    Column("monthly_cost", Numeric(14, 2), nullable=False, server_default="0"),
    Column("currency", Text, nullable=False, server_default="KRW"),
    Column("status", Text, nullable=False, server_default="active"),
    Column("rotated_at", Date),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("notes", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

finance_accounts = Table(
    "finance_accounts",
    metadata,
    Column("account_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("account_name", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="login"),
    Column("masked_identifier", Text),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("notes", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

finance_ledger = Table(
    "finance_ledger",
    metadata,
    Column("ledger_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("entry_type", Text, nullable=False),
    Column("amount", Numeric(14, 2), nullable=False, server_default="0"),
    Column("currency", Text, nullable=False, server_default="KRW"),
    Column("category", Text),
    Column("description", Text),
    Column("project_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

company_culture = Table(
    "company_culture",
    metadata,
    Column("node_id", Text, primary_key=True),
    Column("content", JSONB, nullable=False, server_default="{}"),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

infra_resources = Table(
    "infra_resources",
    metadata,
    Column("resource_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="gpu"),
    Column("name", Text, nullable=False),
    Column("vendor", Text),
    Column("model", Text),
    Column("location", Text),
    Column("status", Text, nullable=False, server_default="active"),
    Column("capacity", Numeric(14, 2)),
    Column("used", Numeric(14, 2)),
    Column("unit", Text),
    Column("usage_percent", Integer),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("link", Text),
    Column("notes", Text),
    Column("period", Text),
    # Legacy self-group (unused); provider_id is the current grouping.
    Column("parent_id", UUID(as_uuid=True)),
    Column("unit_price", Numeric(14, 2)),  # 단가 (numeric)
    Column("balance", Numeric(14, 2)),  # legacy per-service balance (unused)
    Column("provider_id", UUID(as_uuid=True)),  # 제공처 group
    Column("price_unit", Text),  # per_call | per_1m | monthly | hourly | other
    Column("color", Text),  # graph line color (hex)
    Column("currency", Text),  # KRW | USD (for unit_price)
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# 제공처(provider) = service group holding the prepaid balance (잔여량).
infra_providers = Table(
    "infra_providers",
    metadata,
    Column("provider_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("balance", Numeric(14, 2)),  # 잔여량
    Column("currency", Text),  # KRW | USD (for balance)
    Column("link", Text),
    Column("notes", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# N:M links between an api_key (sub-)service and a dashboard_project.
# Simple connection, no per-link weight.
infra_resource_projects = Table(
    "infra_resource_projects",
    metadata,
    Column("link_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

meeting_notes = Table(
    "meeting_notes",
    metadata,
    Column("note_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("partner_id", UUID(as_uuid=True)),
    Column("meeting_kind", Text, nullable=False, server_default="internal"),
    Column("meeting_date", Date, nullable=False),
    Column("attendee_ids", JSONB, nullable=False, server_default="[]"),
    Column("body", Text),
    Column("source", Text, nullable=False, server_default="manual"),
    Column("source_ref", Text),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

meeting_attachments = Table(
    "meeting_attachments",
    metadata,
    Column("attachment_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("note_id", UUID(as_uuid=True), nullable=False),
    Column("filename", Text, nullable=False),
    Column("media_filename", Text, nullable=False),
    Column("mime_type", Text),
    Column("size_bytes", BigInteger, nullable=False, server_default="0"),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

partner_companies = Table(
    "partner_companies",
    metadata,
    Column("partner_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("org_type", Text),
    Column("address", Text),
    Column("representative", Text),
    Column("project_ids", JSONB, nullable=False, server_default="[]"),
    Column("field_tags", JSONB, nullable=False, server_default="[]"),
    Column("status", Text),
    Column("memo", Text),
    Column("next_action", Text),
    Column("last_contact", Date),
    Column("link", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("source", Text, nullable=False, server_default="manual"),
    Column("source_ref", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

partner_contacts = Table(
    "partner_contacts",
    metadata,
    Column("contact_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("partner_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("role", Text),
    Column("phone", Text),
    Column("email", Text),
    Column("channels", JSONB, nullable=False, server_default="[]"),
    Column("link", Text),
    Column("memo", Text),
    Column("is_primary", Boolean, nullable=False, server_default="false"),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("source", Text, nullable=False, server_default="manual"),
    Column("source_ref", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

support_programs = Table(
    "support_programs",
    metadata,
    Column("program_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="시작 전"),
    Column("project_id", UUID(as_uuid=True)),
    Column("company", Text),
    Column("deadline", Date),
    Column("task_number", Text),
    Column("url", Text),
    Column("follow_up", Text),
    Column("body", Text),
    Column("presentation_deadline", Date),
    Column("presentation_date", Date),
    Column("presentation_time", Time),
    Column("owner_member_id", UUID(as_uuid=True)),
    Column("calendar_event_id", UUID(as_uuid=True)),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

support_notes = Table(
    "support_notes",
    metadata,
    Column("note_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="tip"),
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("url", Text),
    Column("body", Text),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# P8.2 thin collector daemon credentials. The DB stores only the sha256 hash of
# the `dct_` bearer token; the plaintext lives in the operator's local Keychain.
# These tokens authenticate the personal collector against ingestion/queue
# endpoints only and never grant a browser session.
collector_tokens = Table(
    "collector_tokens",
    metadata,
    Column("token_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("node_id", Text, nullable=False),
    Column("name", Text, nullable=False, server_default=""),
    Column("token_hash", Text, nullable=False, unique=True),
    # P8.7a: app-enforced scope vocabulary {ingest,commands,knowledge,
    # knowledge:write}. Pre-P8.7a tokens default to {ingest}; see
    # orthus/collector/auth.py effective_scopes (ingest implies commands).
    Column("scopes", ARRAY(Text), nullable=False, server_default="{ingest}"),
    # Per-token machine identity (migration 0070). "" = legacy/deviceless
    # token; a partial unique index keeps two LIVE non-empty device_ids from
    # colliding for one (user_id, node_id).
    Column("device_id", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_used_at", DateTime(timezone=True)),
    Column("last_polled_at", DateTime(timezone=True)),
    Column("last_status_at", DateTime(timezone=True)),
    Column("scheduler_installed", Boolean),
    Column("scheduler_loaded", Boolean),
    Column("scheduler_interval_seconds", Integer),
    Column("last_status_error", Text),
    # migration 0073: daemon-reported agent_task run folders for this token —
    # {"default": <configured workspace or null>, "recent": [<cwd>, ...]}.
    # NULL until the daemon reports any folders.
    Column("agent_workspaces", JSONB, nullable=True),
    Column("revoked_at", DateTime(timezone=True)),
)

# P10 C-7 — 게이트웨이 잡 정의의 owner-scoped central 미러(조회 전용, migration
# 0103). 데몬 로컬 SQLite `agent_jobs`가 실행 SoR이고, 이 테이블은 잡 정의
# (스케줄·지시·enabled·last_status)의 best-effort push 사본이다. seen-set
# 항목(민감 URL 가능)과 chat_id는 미러하지 않으며, central 편집이 로컬 실행을
# 바꾸는 인바운드 경로는 없다(단방향).
agent_gateway_jobs = Table(
    "agent_gateway_jobs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("owner_user_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("job_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("instruction", Text, nullable=False),
    Column("schedule", JSONB, nullable=False),
    Column("schedule_human", Text),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("last_run_at", DateTime(timezone=True)),
    Column("last_status", Text),
    Column("last_error", Text),
    Column("next_run_at", DateTime(timezone=True)),
    Column("mirrored_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("owner_user_id", "job_id", name="uq_agent_gateway_jobs_owner_job"),
)

# K3 KG transactional outbox (docs/kg-model.md §3). wiki/document 쓰기와 같은
# PG 트랜잭션에서 enqueue되고 KGOutboxWorker가 Neo4j에 반영한다. legacy
# persona-era 동명 테이블과 무관한 신규 정의(migration 0047).
kg_outbox = Table(
    "kg_outbox",
    metadata,
    Column("outbox_id", UUID(as_uuid=True), primary_key=True),
    Column("entity_kind", Text, nullable=False),
    Column("entity_id", UUID(as_uuid=True), nullable=False),
    Column("op", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("lease_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("enqueued_at", DateTime(timezone=True), server_default=func.now()),
    # K7.1 (migration 0057) — exact-scope worker DELETE는 이벤트의 scope/owner_id를
    # 읽어 해당 owner의 노드만 지운다. company → owner_id NULL.
    Column("scope", Text, nullable=False, server_default="company"),
    Column("owner_id", UUID(as_uuid=True)),
    Index("idx_kg_outbox_status_enqueued", "status", "enqueued_at"),
)

# Phase 3-B MA.8a — event-triggered decompose orchestration job queue
# (docs/company-agent-orchestration.md §P3B.2). An inbound company mail with a
# compound/action signal enqueues one row here; a lifespan worker claims it
# (FOR UPDATE SKIP LOCKED + lease, modeled on kg_outbox) and runs the knowledge
# brief offline. The sink (an event_orchestration AgentWork item) is NOT a trigger
# source, so the queue is acyclic-by-construction (불변식 29, R16). The unique
# (source_kind, source_ref) is the idempotency key — re-pulling the same mail does
# not re-enqueue.
ask_event_jobs = Table(
    "ask_event_jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column("source_kind", Text, nullable=False),  # "mail" (only trigger source in MA.8a)
    Column("source_ref", Text, nullable=False),  # idempotency key, e.g. "mail:<canonical_id>"
    Column("seed_question", Text, nullable=False),  # redacted knowledge-framed seed
    Column("scope", Text, nullable=False, server_default="company"),
    Column("project", Text),
    Column("created_by", UUID(as_uuid=True), nullable=False),  # acting user for the orchestration
    Column("owner_id", UUID(as_uuid=True)),  # company → NULL (item is company-shared)
    Column("meta", JSONB, nullable=False, server_default="{}"),  # mail ref for the brief payload
    Column("status", Text, nullable=False, server_default="pending"),  # pending|done|dead
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("lease_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("result_work_id", UUID(as_uuid=True)),  # the AgentWork item the worker created
    Column("correlation_id", UUID(as_uuid=True)),
    Column("enqueued_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_ask_event_jobs_status_enqueued", "status", "enqueued_at"),
    # Partial index for the claimable-pending predicate (storm-cap COUNT + claim()).
    Index(
        "idx_ask_event_jobs_pending_lease",
        "lease_until",
        postgresql_where=text("status = 'pending'"),
    ),
    UniqueConstraint("source_kind", "source_ref", name="uq_ask_event_jobs_source"),
)

# K4 — KG 읽기 게이트 run log (docs/kg-model.md §4). 모든 템플릿 실행(reject
# 포함)이 한 행씩 남는다. structured `query_runs`와 동형 패턴이지만 별도
# 테이블이다(기존 query_runs 미확장 결정, migration 0048).
kg_query_runs = Table(
    "kg_query_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("template_name", Text, nullable=False),
    Column("params_redacted", JSONB, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False),
    Column("reject_reason", Text),
    Column("duration_ms", Integer),
    Column("result_count", Integer),
    Column("user_id", UUID(as_uuid=True)),
    Column("correlation_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_kg_query_runs_created", "created_at"),
    Index("idx_kg_query_runs_template", "template_name"),
)

# K6 — entity 레이어 SoR (docs/kg-model.md §2, 구현 명세 §9.1, migration 0049).
# LLM은 distill에서 이름만 추출하고 결정론 코드가 여기 적재 → KG가 :Entity/
# MENTIONED_IN/RELATES_TO로 결정론 투영(rebuild 시 LLM 0회). FK/UNIQUE는 DB
# 레벨(migration)에 둔다 — 기존 kg 테이블과 동일 스타일.
kg_entities = Table(
    "kg_entities",
    metadata,
    Column("entity_id", UUID(as_uuid=True), primary_key=True),
    Column("entity_key", Text, nullable=False, unique=True),  # "{entity_kind}:{name_norm}"
    Column("entity_kind", Text, nullable=False),  # person|org|project|system (DB CHECK)
    Column("name_norm", Text, nullable=False),
    Column("display_name", Text, nullable=False),  # person은 redact_pii 통과 후 저장
    Column("scope", Text, nullable=False, server_default="company"),
    Column("owner_id", UUID(as_uuid=True)),  # K7 대비 — K6에서는 항상 NULL
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_kg_entities_name_norm", "name_norm"),
)

# (entity_id, page_id) UNIQUE + entity_id FK(ON DELETE CASCADE)는 DB 레벨.
kg_entity_mentions = Table(
    "kg_entity_mentions",
    metadata,
    Column("mention_id", UUID(as_uuid=True), primary_key=True),
    Column("entity_id", UUID(as_uuid=True), nullable=False),
    Column("page_id", UUID(as_uuid=True), nullable=False),  # company wiki_pages(page|claim)
    Column("evidence_slug", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_kg_entity_mentions_page", "page_id"),
)

# P8.2 central->personal command queue. central appends rows; the personal
# collector daemon polls its own owner's pending commands and reports results
# (outbound-only). Lifecycle: pending -> claimed -> done|failed.
collector_commands = Table(
    "collector_commands",
    metadata,
    Column("command_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="pending"),
    # Target a specific device (migration 0070). NULL = broadcast to every
    # device of the owner; a non-NULL value routes to one device's daemon.
    Column("device_id", Text),
    Column("result", JSONB),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("claimed_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
)

# Agent-chat conversation sessions (migration 0074). DB-backed, restart-safe,
# strictly owner-scoped Agent-chat threads. Replaces the prior browser
# sessionStorage flat message list. Each session is a thread; messages belong to
# one session. node-local + owner-scoped: every read/write filters
# (owner_id, node_id).
agent_chat_sessions = Table(
    "agent_chat_sessions",
    metadata,
    Column("session_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index(
        "idx_agent_chat_sessions_owner_node_updated",
        "owner_id",
        "node_id",
        "updated_at",
    ),
)

agent_chat_messages = Table(
    "agent_chat_messages",
    metadata,
    Column("message_id", UUID(as_uuid=True), primary_key=True),
    Column("session_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text, nullable=False),  # "user" | "agent"
    Column("text", Text, nullable=False),
    Column("work_id", UUID(as_uuid=True)),
    # HITL (migration 0101): kind discriminates a plain "text" message from a
    # structured "user_input_request" / "user_input_response" turn; payload carries
    # the structured question/answer (UserInputRequest). Legacy rows default to
    # "text"/NULL so existing behavior is unchanged.
    Column("kind", Text, nullable=False, server_default="text"),
    Column("payload", JSONB),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("idx_agent_chat_messages_session_created", "session_id", "created_at"),
)

# Native runner session mapping for delegated agent_task turns (migration 0075).
# One Agent-chat session can have multiple native threads when assignee, runner,
# mode, or cwd differs. The logical thread_id is central-owned; runner_session_id
# is the runner's native resume id when known.
agent_task_threads = Table(
    "agent_task_threads",
    metadata,
    Column("thread_id", UUID(as_uuid=True), primary_key=True),
    Column("node_id", Text, nullable=False),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("chat_session_id", UUID(as_uuid=True), nullable=False),
    Column("assignee_user_id", UUID(as_uuid=True), nullable=False),
    Column("runner", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("cwd", Text, nullable=False, server_default=""),
    Column("runner_session_id", Text),
    Column("runner_session_ready", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index(
        "uq_agent_task_threads_identity",
        "node_id",
        "owner_id",
        "chat_session_id",
        "assignee_user_id",
        "runner",
        "mode",
        "cwd",
        unique=True,
    ),
    Index("idx_agent_task_threads_chat", "owner_id", "node_id", "chat_session_id"),
)
