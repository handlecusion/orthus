"""Agent Work persistence and source adapters."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from orthus.agentwork.state import TERMINAL_STATES, classify_candidate, review_transition
from orthus.agentwork.autosend import evaluate_auto_send_candidacy
from orthus.agentwork.wiki_link import (
    AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT,
    enrich_agent_work_items,
)
from orthus.audit import audit, redact_pii_text
from orthus.models.orchestration import TASK_EMAIL_DRAFT, get_chat_model_for
from orthus.connectors.registry import (
    get_connector_provider,
    register_default_connector_providers,
    registered_connector_slugs,
)
from orthus.connectors.runner import run_connector_account_sync
from orthus.agentwork.delegation import (
    parse_chat_delegation,
    resolve_assignee,
    resolve_enrolled_daemon,
)
from orthus.collector.commands import create_command
from orthus.db import session
from orthus.documents import create_agent_draft_document
from orthus.email.log import (
    email_hash as _email_hash,
    email_send_rate_limited as _email_send_rate_limited_impl,
    insert_email_send_log as _insert_email_send_log_impl,
)
from orthus.email.sender import SendRequest, get_email_sender
from orthus.personal_board import (
    TaskPatch,
    create_delegation_task,
    ensure_workspace,
    update_task,
)
from orthus.promote import list_promotion_stages
from orthus.schemas.canonical import (
    AgentWorkCandidate,
    AgentWorkDecision,
    AgentWorkInboxAgentWorkBucket,
    AgentWorkInboxAgentWorkItem,
    AgentWorkInboxBuckets,
    AgentWorkInboxDataGapBucket,
    AgentWorkInboxDataGapItem,
    AgentWorkInboxPromotionBucket,
    AgentWorkInboxPromotionItem,
    AgentWorkInboxSummary,
    AgentWorkInboxWikiTaskBucket,
    AgentWorkInboxWikiTaskItem,
    AgentWorkItem,
    AgentWorkNotifyState,
    AgentPolicyBucketSummary,
    AgentPolicyWikiSummary,
    AgentPolicyMemoryContext,
    AgentWorkReviewAction,
    AgentWorkReviewDecision,
    AgentWorkReviewDecisionResult,
    AgentWorkState,
    AgentWorkSyncResult,
    DataGap,
    EmailDraftPayload,
    MailReplyContext,
    ReplyDraftPayload,
    WikiPage,
    WikiTask,
)
from orthus.settings import Settings, get_settings
from orthus.tables import (
    agent_policy_observations,
    agent_task_threads,
    agent_work_decisions,
    agent_work_items,
    connector_accounts,
    connector_runs,
    data_gaps,
    email_send_log,
    personal_board_tasks,
    promote_staging,
    users,
)
from orthus.wiki.gap import list_gaps
from orthus.wiki import store as wiki_store

logger = logging.getLogger(__name__)

_WIKI_TASK_CLEANUP_KINDS = {"stale_audit", "dedup", "provenance_fix"}
_AGENT_TASK_OPERATOR_ROLES = {"owner", "admin", "scheduler"}
# Who may *approve* an LLM-inferred delegation into a real dispatch. Deliberately excludes
# "scheduler": the machine that queued the candidate cannot also be the human that clears it.
_AGENT_TASK_REVIEWER_ROLES = {"owner", "admin"}
_AGENT_TASK_ALLOWED_MODES = {"code", "knowledge"}
_AGENT_TASK_ALLOWED_RUNNERS = {"claude", "codex", "hermes"}
_AGENT_TASK_AWAITING_USER_INPUT_REASON = "awaiting_user_input"
_AGENT_TASK_ANSWERED_USER_INPUT_REASON = "awaiting_user_input_answered"
_AGENT_TASK_CHOICE_RE = re.compile(
    r"(?m)^\s*(?:\d{1,2}[\).\:]|[A-Da-d][\).\:]|[①-⑳]|[-*]\s*(?:\d{1,2}|[A-Da-d])[\).\:]?)\s+"
)
_AGENT_TASK_INLINE_CHOICE_RE = re.compile(
    r"\b(?:1\s*[,/]\s*2\s*[,/]\s*3|A\s*[,/]\s*B\s*[,/]\s*C)\b",
    re.I,
)
_AGENT_TASK_INPUT_TERMS = (
    "선택",
    "골라",
    "고르",
    "번호",
    "답변",
    "알려줘",
    "확인",
    "진행할까요",
    "어떻게 할까요",
    "어느 쪽",
    "어떤 옵션",
    "필요",
    "choose",
    "select",
    "option",
    "which",
    "confirm",
    "proceed",
    "reply",
    "answer",
)
_POLICY_MEMORY_RECENT_WINDOW_DAYS = 60
_EMAIL_AUTO_SEND_MIN_RECENT_OBSERVATIONS = 20
_EMAIL_AUTO_SEND_MIN_NO_EDIT_APPROVAL_RATE = 0.95
_EMAIL_AUTO_SEND_POLICY_BUCKET = (
    "email_send|assistant_command|draft_for_review|"
    "email_draft_first,policy_memory_observation_gate_required"
)
_EMAIL_AUTO_SEND_OPERATOR_ROLES = {"owner", "admin"}
_INBOX_ITEM_LIMIT = 5
_INBOX_OPEN_AGENT_WORK_STATES: tuple[str, ...] = (
    "pending",
    "classified",
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "rejected",
)
_INBOX_OUTCOME_KEYS: tuple[str, ...] = (
    "pending",
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "reject",
)
_COMMAND_VERBS = (
    "해줘",
    "해 줘",
    "처리",
    "작성",
    "만들",
    "정리",
    "보내",
    "전송",
    "동기화",
    "가져와",
    "불러와",
    "반영",
    "sync",
    "draft",
    # §S3 keyword 커버리지 확장(2026-07-05): keyword-miss 명령절이 read로 강등돼
    # 유실되던 갭(docs/command-split-hold-followup.md §S3)을 좁힌다. 후보는 실측
    # miss 목록에서 뽑되, 명사 collocation으로 질문을 명령으로 오검출할 수 있는
    # 광의어("업데이트"/"연동"/"공유")는 의도적으로 제외했다 — 검출 확장보다
    # 질문→큐잉 오검출 방지가 우선(불변식: 명령형 지식 질문은 검색으로 폴스루).
    "써줘",
    "써 줘",
    "부탁",
    "회신",
    "정돈",
    "청소",
)
_INFO_QUERY_TERMS = (
    "?",
    "알려줘",
    "알려 줘",
    "어떻게",
    "뭐야",
    "무엇",
    "가이드",
    "방법",
    # Read-intent verbs: a "요약/설명/정리" ASK is a knowledge question, not a command.
    # Without these, "회사 위키에 정리된 내용 요약해줘" matched the central_wiki_task_cleanup
    # family (위키 + "정리" from "정리된") and was queued as agent_work instead of answered.
    "요약",
    "설명",
)
_CONNECTOR_TERMS = (
    "gmail",
    "drive",
    "notion",
    "slack",
    "github",
    "connector",
    "커넥터",
    "노션",
    "슬랙",
    "깃허브",
)
# §S3 확장: "싱크"/"최신화"는 sync 동의어로 추가. "연동"/"업데이트"는 제외 —
# "슬랙 연동 문제 정리해줘" 같은 명사 collocation이 connector_sync(strong)로
# 오검출돼 지식 요청이 sync 명령으로 큐잉되는 FP를 만들기 때문.
_SYNC_TERMS = ("sync", "동기화", "수집", "가져와", "불러와", "import", "싱크", "최신화")
_EMAIL_TERMS = ("email", "이메일", "메일", "gmail", "답장")
# §S3 확장: "작성"/"써"/"회신" — "메일 작성해줘"/"메일 써줘"/"메일 회신해줘"가
# keyword-miss로 read 강등되던 갭. 질문형("메일 회신 방법 알려줘")은 non-strong
# email이라 _INFO_QUERY_TERMS 가드가 그대로 막는다.
_EMAIL_ACTION_TERMS = ("보내", "전송", "답장", "초안", "draft", "작성", "써", "회신")
# A literal email address is the strongest recipient signal. The recipient run
# below is intentionally space-free so a compound command ("...요약해서 X로 메일")
# captures only the token before the particle, not the whole preceding clause.
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_EMAIL_RECIPIENT_KO = re.compile(
    r"(?P<recipient>[A-Za-z0-9._*@가-힣][A-Za-z0-9._*@가-힣-]{0,38}?)"
    r"(?:에게|한테|께|에|으로|로)\s*(?:이메일|메일|email|gmail|답장)"
)
_EMAIL_RECIPIENT_EN = re.compile(
    r"(?:email|mail|reply|draft)\s+(?:to\s+)?(?P<recipient>[A-Za-z0-9._*@-]{2,40})",
    re.IGNORECASE,
)
# §S3 확장: 보고서/리포트/회의록 — 실무 문서 초안 요청의 대표 표면형. "메모"/"노트"는
# 개인 노트·지식 캡처와 겹쳐 제외(문서 초안 큐잉 오검출 방지).
_DOCUMENT_TERMS = ("문서", "document", "doc", "draft", "초안", "보고서", "리포트", "회의록")
# §S3 확장: 태스크/task — "완료된 태스크 정리해줘"가 board cleanup으로 잡히도록.
# "작업"/"카드"는 광의어라 제외.
_BOARD_TERMS = ("보드", "board", "칸반", "todo", "할일", "할 일", "태스크", "task")
_WIKI_TERMS = ("wiki", "위키")
_DESTRUCTIVE_TERMS = ("삭제", "delete", "remove", "지워", "제거")
_EXTERNAL_BOARD_WRITE_TERMS = ("calendar", "캘린더", "email", "이메일", "메일", "github", "깃허브")
_CONNECTOR_COMMAND_ALIASES: tuple[tuple[str, str], ...] = (
    ("gws_gmail", "gws_gmail"),
    ("gwsgmail", "gws_gmail"),
    ("gmail", "gws_gmail"),
    ("지메일", "gws_gmail"),
    ("gws_drive", "gws_drive"),
    ("gwsdrive", "gws_drive"),
    ("googledrive", "gws_drive"),
    ("google드라이브", "gws_drive"),
    ("구글드라이브", "gws_drive"),
    ("drive", "gws_drive"),
    ("드라이브", "gws_drive"),
    ("notion", "notion"),
    ("노션", "notion"),
    ("slack", "slack"),
    ("슬랙", "slack"),
    ("github", "github"),
    ("깃허브", "github"),
    ("local_files", "local_files"),
    ("localfiles", "local_files"),
    ("로컬파일", "local_files"),
    ("codex_sessions", "codex_sessions"),
    ("codexsessions", "codex_sessions"),
    ("codex", "codex_sessions"),
    ("claude_sessions", "claude_sessions"),
    ("claudesessions", "claude_sessions"),
    ("claude", "claude_sessions"),
    ("chat_exports", "chat_exports"),
    ("chatexports", "chat_exports"),
    ("chatgpt", "chat_exports"),
    ("email_exports", "email_exports"),
    ("emailexports", "email_exports"),
)


def sync_data_gaps_to_agent_work(
    user_id: UUID, *, limit: int = 100, settings: Settings | None = None
) -> AgentWorkSyncResult:
    """Backfill open data gaps into Agent Work, idempotently.

    This writes only node-local queue rows. It does not sync connectors, send
    email, write central wiki pages, or execute any external side effect.
    """
    settings = settings or get_settings()
    items: list[AgentWorkItem] = []
    for gap in list_gaps(user_id, status="open", limit=limit):
        candidate = data_gap_candidate(gap)
        candidate, decision, correlation_id, _classify_run_id = (
            classify_candidate_with_policy_memory(candidate, user_id, settings=settings)
        )
        items.append(
            persist_agent_work_item(
                candidate,
                decision,
                user_id=user_id,
                correlation_id=correlation_id,
                settings=settings,
            )
        )
    return AgentWorkSyncResult(
        source_kind="data_gap",
        created_or_updated=len(items),
        items=items,
    )


def sync_wiki_tasks_to_agent_work(
    user_id: UUID, *, limit: int = 100, settings: Settings | None = None
) -> AgentWorkSyncResult:
    """Backfill unresolved WikiTask rows into Agent Work, idempotently.

    This reads node-local wiki tasks only. Cleanup-only tasks are safe auto
    actions: they resolve the task row itself, not company wiki knowledge pages.
    Open questions/conflicts still require review.
    """
    settings = settings or get_settings()
    scope, owner_id = _wiki_scope(user_id, settings)
    items: list[AgentWorkItem] = []
    for slug in wiki_store.list_slugs("task", scope=scope, owner_id=owner_id):
        task = wiki_store.load_task(slug, scope=scope, owner_id=owner_id)
        if task is None or task.resolved:
            continue
        candidate = wiki_task_candidate(task, scope=scope, node_id=settings.node_id)
        candidate, decision, correlation_id, _classify_run_id = (
            classify_candidate_with_policy_memory(candidate, user_id, settings=settings)
        )
        item = persist_agent_work_item(
            candidate,
            decision,
            user_id=user_id,
            correlation_id=correlation_id,
            settings=settings,
        )
        if item.state == "auto_execute":
            item = execute_auto_wiki_task_cleanup(user_id, item, settings=settings)
        items.append(item)
        if len(items) >= limit:
            break
    return AgentWorkSyncResult(
        source_kind="wiki_task",
        created_or_updated=len(items),
        items=items,
    )


def execute_auto_wiki_task_cleanup(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    payload = item.payload or {}
    if (
        item.action_family != "central_wiki_task_cleanup"
        or item.state != "auto_execute"
        or payload.get("cleanup_only") is not True
        or payload.get("company_wiki_write") is True
    ):
        return item

    task_slug = str(payload.get("task_slug") or item.source_ref_id)
    scope, owner_id = _wiki_scope(user_id, settings)
    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        task = wiki_store.load_task(task_slug, scope=scope, owner_id=owner_id)
        if task is None:
            raise KeyError(f"wiki task not found: {task_slug}")
        if not task.resolved:
            updated_task = task.model_copy(update={"resolved": True})
            wiki_store.write_task(
                updated_task,
                user_id=user_id,
                scope=scope,
                owner_id=owner_id,
                project="company" if scope == "company" else "personal",
            )

        now = datetime.now(UTC)
        next_payload = dict(item.payload)
        next_payload["auto_execution"] = {
            "kind": "wiki_task_cleanup",
            "task_slug": task_slug,
            "resolved": True,
            "executed_at": now.isoformat(),
            "used_for_outcome": False,
        }
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="resolved",
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            task_slug=task_slug,
            scope=scope,
            owner_id=str(owner_id) if owner_id is not None else None,
            cleanup_only=True,
            company_wiki_write=False,
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "task_slug": task_slug,
            }
        )
        return updated_item


def execute_auto_connector_sync(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    payload = item.payload or {}
    if (
        item.action_family != "connector_sync"
        or item.state != "auto_execute"
        or payload.get("secret_state") != "configured"
        or payload.get("node_policy_allows") is not True
        or payload.get("actor_role") not in {"owner", "admin", "scheduler"}
    ):
        return item

    raw_account_id = payload.get("account_id")
    connector_slug = str(payload.get("connector_slug") or "").strip().lower()
    if raw_account_id is None or not connector_slug:
        return _mark_auto_connector_sync_failed(
            item,
            error_message="connector sync payload is missing account_id or connector_slug",
            settings=settings,
        )
    try:
        account_id = UUID(str(raw_account_id))
    except ValueError:
        return _mark_auto_connector_sync_failed(
            item,
            error_message="connector sync payload has invalid account_id",
            settings=settings,
        )
    account = _find_connector_account_for_execution(
        account_id,
        connector_slug=connector_slug,
        user_id=user_id,
        settings=settings,
    )
    if account is None or not _connector_supported(connector_slug, str(account["account_kind"])):
        return _mark_auto_connector_sync_failed(
            item,
            error_message="connector account is no longer available for this node/user",
            settings=settings,
        )

    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with audit("connector.command", correlation_id=span.correlation_id) as command_span:
            command_span.add_meta(
                action="sync",
                trigger="agent_work",
                work_id=str(item.work_id),
                connector_slug=connector_slug,
                account_id=str(account_id),
                node_id=settings.node_id,
                user_id=str(user_id),
            )
            result = run_connector_account_sync(
                account_id,
                user_id,
                reason="manual",
                continue_on_error=True,
                concurrency=_connector_sync_concurrency(payload),
            )
            command_span.set_output(
                {
                    "account_id": str(result.account_id),
                    "run_id": str(result.run_id),
                    "status": result.status,
                }
            )
        now = datetime.now(UTC)
        report = result.report
        execution = {
            "kind": "connector_sync",
            "connector_slug": result.connector_slug,
            "account_id": str(result.account_id),
            "run_id": str(result.run_id),
            "status": result.status,
            "executed_at": now.isoformat(),
        }
        if report is not None:
            execution["report"] = {
                "created": report.created,
                "updated": report.updated,
                "skipped": report.skipped,
                "errors": report.errors,
                "total": len(report.doc_ids),
            }
        if result.error_message is not None:
            execution["error_message"] = _redacted_note(result.error_message)

        next_payload = dict(item.payload)
        next_payload["auto_execution"] = execution
        next_state: AgentWorkState = (
            "resolved" if result.status == "succeeded" else "request_more_data"
        )
        next_reason_codes = list(item.reason_codes)
        if result.status != "succeeded" and "auto_execute_failed" not in next_reason_codes:
            next_reason_codes.append("auto_execute_failed")

        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state=next_state,
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            connector_slug=result.connector_slug,
            account_id=str(result.account_id),
            connector_run_id=str(result.run_id),
            connector_status=result.status,
            node_id=settings.node_id,
            owner_id=str(updated_item.owner_id) if updated_item.owner_id is not None else None,
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "connector_run_id": str(result.run_id),
                "connector_status": result.status,
            }
        )
        return updated_item


def _normalize_agent_task_cwd(cwd: str | None) -> str | None:
    """Clean a delegated agent_task cwd before it enters the work-item payload.

    The chat/agent-work delegate controls render ``@`` as a folder-picker
    affordance; a hand-typed ``@~/Code`` means ``~/Code``. Strip a leading ``@``
    (and following space) so the stored payload — and the dispatched collector
    command — carries a real path, not a literal the daemon would chdir into and
    fail. ``~`` is left for the daemon to expand in its own filesystem.
    """
    if not isinstance(cwd, str):
        return None
    cleaned = cwd.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].lstrip()
    return cleaned or None


def _preallocated_runner_session_id(runner: str, thread_id: UUID) -> str | None:
    """Native runner id known before first subprocess turn, if supported."""
    if runner == "claude":
        return str(thread_id)
    return None


def _ensure_agent_task_thread(
    *,
    owner_id: UUID,
    node_id: str,
    chat_session_id: UUID,
    assignee_user_id: UUID,
    runner: str,
    mode: str,
    cwd: str | None,
) -> dict[str, Any]:
    """Return one logical/native thread mapping for a delegated Agent-chat turn."""
    cwd_key = cwd or ""
    now = datetime.now(UTC)
    clauses = [
        agent_task_threads.c.node_id == node_id,
        agent_task_threads.c.owner_id == owner_id,
        agent_task_threads.c.chat_session_id == chat_session_id,
        agent_task_threads.c.assignee_user_id == assignee_user_id,
        agent_task_threads.c.runner == runner,
        agent_task_threads.c.mode == mode,
        agent_task_threads.c.cwd == cwd_key,
    ]
    with session() as s:
        row = s.execute(select(agent_task_threads).where(*clauses)).first()
        if row is not None:
            s.execute(
                update(agent_task_threads)
                .where(agent_task_threads.c.thread_id == row.thread_id)
                .values(updated_at=now)
            )
            s.commit()
            mapped = dict(row._mapping)
            mapped["_created"] = False
            return mapped

        thread_id = uuid4()
        runner_session_id = _preallocated_runner_session_id(runner, thread_id)
        try:
            inserted = s.execute(
                insert(agent_task_threads)
                .values(
                    thread_id=thread_id,
                    node_id=node_id,
                    owner_id=owner_id,
                    chat_session_id=chat_session_id,
                    assignee_user_id=assignee_user_id,
                    runner=runner,
                    mode=mode,
                    cwd=cwd_key,
                    runner_session_id=runner_session_id,
                    runner_session_ready=False,
                    created_at=now,
                    updated_at=now,
                )
                .returning(agent_task_threads)
            ).first()
            s.commit()
            mapped = dict(inserted._mapping)
            mapped["_created"] = True
            return mapped
        except IntegrityError:
            s.rollback()
            row = s.execute(select(agent_task_threads).where(*clauses)).first()
            if row is None:
                raise
            mapped = dict(row._mapping)
            mapped["_created"] = False
            return mapped


def _store_runner_session_id(thread_id: str | None, runner_session_id: str | None) -> None:
    if not thread_id or not runner_session_id:
        return
    try:
        tid = UUID(str(thread_id))
    except (ValueError, TypeError):
        return
    native = str(runner_session_id).strip()
    if not native:
        return
    now = datetime.now(UTC)
    with session() as s:
        row = s.execute(
            select(agent_task_threads.c.runner_session_id).where(
                agent_task_threads.c.thread_id == tid
            )
        ).first()
        if row is None:
            return
        existing = str(row.runner_session_id or "").strip()
        if existing and existing != native:
            return
        s.execute(
            update(agent_task_threads)
            .where(agent_task_threads.c.thread_id == tid)
            .values(runner_session_id=native, runner_session_ready=True, updated_at=now)
        )
        s.commit()


def create_agent_task_work_item(
    actor_user_id: UUID,
    *,
    actor_role: str | None,
    assignee: str,
    mode: str,
    runner: str,
    instruction: str,
    cwd: str | None = None,
    timeout_s: int | None = None,
    device_id: str | None = None,
    mail_origin: dict[str, Any] | None = None,
    chat_session_id: UUID | None = None,
    redact_instruction: bool = True,
    llm_inferred: bool = False,
    settings: Settings | None = None,
) -> AgentWorkItem | None:
    """Create + classify a delegated agent_task work item, dispatching when allowed.

    `llm_inferred=True` marks a candidate whose "this is a delegation" judgment came
    from an LLM reading untrusted inbound text (inbound mail). The gate never
    auto-executes those — they land in draft_for_review and dispatch on approve.

    Mirrors create_assistant_command_work_item: build a candidate, classify
    through the deterministic policy gate, persist, then auto-execute the dispatch
    when the gate returns auto_execute. Returns None when the actor lacks operator
    authority (no queue row is created, matching the assistant_command pattern for
    non-operator command-shaped intake).
    """
    settings = settings or get_settings()
    if actor_role not in _AGENT_TASK_OPERATOR_ROLES:
        return None
    candidate = agent_task_candidate(
        actor_user_id,
        actor_role=actor_role,
        assignee=assignee,
        mode=mode,
        runner=runner,
        instruction=instruction,
        cwd=cwd,
        timeout_s=timeout_s,
        device_id=device_id,
        mail_origin=mail_origin,
        chat_session_id=chat_session_id,
        redact_instruction=redact_instruction,
        llm_inferred=llm_inferred,
        settings=settings,
    )
    if candidate is None:
        return None
    candidate, decision, correlation_id, _classify_run_id = classify_candidate_with_policy_memory(
        candidate,
        actor_user_id,
        settings=settings,
    )
    item = persist_agent_work_item(
        candidate,
        decision,
        user_id=actor_user_id,
        correlation_id=correlation_id,
        settings=settings,
    )
    if item.state == "auto_execute":
        item = execute_auto_agent_task(actor_user_id, item, settings=settings)
    return item


def agent_task_candidate(
    actor_user_id: UUID,
    *,
    actor_role: str | None,
    assignee: str,
    mode: str,
    runner: str,
    instruction: str,
    cwd: str | None = None,
    timeout_s: int | None = None,
    device_id: str | None = None,
    mail_origin: dict[str, Any] | None = None,
    chat_session_id: UUID | None = None,
    redact_instruction: bool = True,
    llm_inferred: bool = False,
    settings: Settings | None = None,
) -> AgentWorkCandidate | None:
    settings = settings or get_settings()
    # §5-B mail carve-out: a self-assigned compose/reply draft runs on the owner's
    # own machine from the owner's own thread, so the caller may opt out of PII
    # redaction (matching the central sync `/mail/draft`). All other delegation —
    # including cross-user task delegation — keeps the default redaction.
    cleaned_instruction = (instruction or "").strip()
    redacted_instruction = (
        redact_pii_text(cleaned_instruction) if redact_instruction else cleaned_instruction
    )
    if not redacted_instruction:
        return None
    requested_device = (device_id or "").strip()
    assignee_user_id = resolve_assignee(assignee)
    resolved = (
        resolve_enrolled_daemon(assignee_user_id, device_id=requested_device, settings=settings)
        if assignee_user_id is not None
        else None
    )
    assignee_node_id = resolved[0] if resolved else None
    # An explicitly requested device that does not resolve to an enrolled daemon
    # is a hard stop (the delegate endpoint 422s) so a caller can never silently
    # broadcast to all of the assignee's daemons when they meant one device.
    if requested_device and resolved is None:
        return None
    digest = hashlib.sha256(
        f"{settings.node_id}:{actor_user_id}:{assignee}:{redacted_instruction}".encode("utf-8")
    ).hexdigest()[:16]
    normalized_cwd = _normalize_agent_task_cwd(cwd)
    thread: dict[str, Any] | None = None
    if chat_session_id is not None and assignee_user_id is not None:
        thread = _ensure_agent_task_thread(
            owner_id=actor_user_id,
            node_id=settings.node_id,
            chat_session_id=chat_session_id,
            assignee_user_id=assignee_user_id,
            runner=runner,
            mode=mode,
            cwd=normalized_cwd,
        )
    payload: dict[str, Any] = {
        "kind": "agent_task",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
        "agent_task_enabled": settings.agent_task_enabled,
        "assignee_input": redact_pii_text(str(assignee).strip()),
        "assignee_user_id": str(assignee_user_id) if assignee_user_id is not None else None,
        "assignee_node_id": assignee_node_id,
        "mode": mode,
        "runner": runner,
        "instruction": redacted_instruction,
    }
    if llm_inferred:
        # The delegation intent was *inferred* by an LLM from untrusted inbound text
        # rather than stated by a human. The policy gate reads this and routes to
        # draft_for_review instead of auto_execute (state.py) — see that comment for
        # the false-positive measurement that motivated it.
        payload["llm_inferred"] = True
    if actor_role is not None:
        payload["actor_role"] = actor_role
    if normalized_cwd:
        payload["cwd"] = normalized_cwd
    if thread is not None:
        payload["thread_id"] = str(thread["thread_id"])
        payload["chat_session_id"] = str(thread["chat_session_id"])
        if thread.get("runner_session_id"):
            payload["runner_session_id"] = str(thread["runner_session_id"])
            payload["runner_session_resume"] = bool(thread.get("runner_session_ready"))
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s
    if requested_device:
        # Pin the dispatch to one device only when explicitly requested; an
        # unpinned command stays claimable by any of the owner's daemons.
        payload["assignee_device_id"] = requested_device
    sanitized_origin = _sanitize_mail_origin(mail_origin)
    if sanitized_origin is not None:
        payload["mail_origin"] = sanitized_origin
    evidence = [
        {
            "kind": "agent_task",
            "ref": f"agent-task-{digest}",
            "runner": runner,
            "mode": mode,
        }
    ]
    return AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id=f"agent-task-{digest}",
        action_family="agent_task",
        title=f"Agent task ({runner}/{mode}): {redacted_instruction[:80]}",
        payload=payload,
        evidence=evidence,
    )


def execute_reviewed_agent_task(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    reviewer_role: str | None,
    settings: Settings | None = None,
) -> AgentWorkItem:
    """Dispatch an agent_task that a human reviewer approved.

    The LLM-inferred mail delegation path deliberately does NOT auto-execute
    (`state.py`: an LLM decided "this is a delegation" over untrusted inbound text, and
    a false positive runs a full-access agent on someone else's machine). Those land in
    `draft_for_review`, so the dispatch has to happen on approve instead of at create.

    Two guards beyond the auto path:

    - **The reviewer** must hold operator authority. The payload's `actor_role` is
      `"scheduler"` — a machine — and must never be sufficient on its own. That is the
      whole point of routing through review.
    - **Idempotent.** A second call after a dispatch is a no-op. `decide_agent_work_item`
      already rejects a second decision, but a dispatch is irreversible, so it is
      re-checked here rather than assumed.
    """
    payload = item.payload or {}
    if (
        reviewer_role not in _AGENT_TASK_REVIEWER_ROLES
        or item.action_family != "agent_task"
        or payload.get("llm_inferred") is not True
        or isinstance(payload.get("auto_execution"), dict)  # already dispatched
    ):
        return item
    # `execute_auto_agent_task` re-checks every remaining guard (flag, node, enrolled
    # daemon, runner/mode allowlist) against the row as it stands *now* — a daemon that
    # un-enrolled between queueing and approval must not receive the command.
    return execute_auto_agent_task(user_id, item, settings=settings, allowed_states=("resolved",))


def execute_auto_agent_task(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
    allowed_states: tuple[str, ...] = ("auto_execute",),
) -> AgentWorkItem:
    """Dispatch an auto_execute agent_task to the assignee's collector daemon.

    Re-checks every gate guard before enqueueing the collector command (defence
    in depth against a stale row). Enqueues the command with the work_id in its
    payload so /complete can route the result back, then records the dispatched
    command_id in the work item payload and leaves the item in auto_execute
    (in-progress) until the daemon completes it.

    `allowed_states` exists for the reviewed path (`execute_reviewed_agent_task`), where
    the item is `resolved` by the approve decision rather than `auto_execute`. The state
    column is not written here, so a reviewed item stays `resolved` while the daemon
    runs; its progress is visible through `payload["auto_execution"]` exactly as the auto
    path's is.
    """
    settings = settings or get_settings()
    payload = item.payload or {}
    if (
        item.action_family != "agent_task"
        or item.state not in allowed_states
        or settings.node_kind != "company"
        or settings.agent_task_enabled is not True
        or payload.get("actor_role") not in _AGENT_TASK_OPERATOR_ROLES
        or payload.get("mode") not in _AGENT_TASK_ALLOWED_MODES
        or payload.get("runner") not in _AGENT_TASK_ALLOWED_RUNNERS
    ):
        return item

    assignee_node_id = payload.get("assignee_node_id")
    raw_assignee_user_id = payload.get("assignee_user_id")
    assignee_device_id = payload.get("assignee_device_id") or None
    instruction = str(payload.get("instruction") or "").strip()
    if not assignee_node_id or not raw_assignee_user_id or not instruction:
        return _mark_auto_agent_task_failed(
            item,
            error_message="agent_task payload is missing assignee, daemon, or instruction",
            settings=settings,
        )
    try:
        assignee_user_id = UUID(str(raw_assignee_user_id))
    except ValueError:
        return _mark_auto_agent_task_failed(
            item,
            error_message="agent_task payload has invalid assignee_user_id",
            settings=settings,
        )

    command_payload: dict[str, Any] = {
        "mode": payload["mode"],
        "runner": payload["runner"],
        "instruction": instruction,
        "work_item_id": str(item.work_id),
    }
    for key in ("thread_id", "chat_session_id", "runner_session_id", "runner_session_resume"):
        if payload.get(key) is not None:
            command_payload[key] = payload[key]
    if payload.get("cwd"):
        command_payload["cwd"] = payload["cwd"]
    if payload.get("timeout_s") is not None:
        command_payload["timeout_s"] = payload["timeout_s"]

    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with audit("agent_task.dispatch", correlation_id=span.correlation_id) as dispatch_span:
            command = create_command(
                settings,
                user_id=assignee_user_id,
                created_by=user_id,
                kind="agent_task",
                payload=command_payload,
                device_id=assignee_device_id,
            )
            dispatch_span.add_meta(
                work_id=str(item.work_id),
                command_id=str(command.command_id),
                assignee_user_id=str(assignee_user_id),
                assignee_node_id=str(assignee_node_id),
                assignee_device_id=assignee_device_id,
                runner=payload["runner"],
                mode=payload["mode"],
            )
            dispatch_span.set_output(
                {
                    "work_id": str(item.work_id),
                    "command_id": str(command.command_id),
                    "status": command.status,
                }
            )
        # 위임 도착을 assignee 보드 backlog에 티켓으로 남긴다(best-effort 파생
        # evidence — 실패해도 dispatch SoR인 collector command는 이미 성공했다).
        board_task_evidence = _create_delegation_board_task_evidence(
            item,
            assignee_user_id=assignee_user_id,
            instruction=instruction,
            settings=settings,
            correlation_id=span.correlation_id,
        )
        now = datetime.now(UTC)
        next_payload = dict(item.payload)
        if board_task_evidence is not None:
            next_payload["delegation_board_task"] = board_task_evidence
        next_payload["auto_execution"] = {
            "kind": "agent_task",
            "command_id": str(command.command_id),
            "assignee_user_id": str(assignee_user_id),
            "assignee_node_id": str(assignee_node_id),
            "status": "dispatched",
            "dispatched_at": now.isoformat(),
        }
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            command_id=str(command.command_id),
            assignee_node_id=str(assignee_node_id),
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "command_id": str(command.command_id),
            }
        )
        return updated_item


def _create_delegation_board_task_evidence(
    item: AgentWorkItem,
    *,
    assignee_user_id: UUID,
    instruction: str,
    settings: Settings,
    correlation_id: UUID | None,
) -> dict[str, Any] | None:
    """dispatch 성공 직후 assignee 보드 backlog에 수신 위임 티켓을 만든다.

    결정론 파생 evidence 전용(calendar materialize 동형) — policy gate/outcome을
    바꾸지 않고 LLM을 호출하지 않는다. 규칙:

    - self-delegation(assignee == work item creator)은 만들지 않는다 — 알림은
      "남에게서 받은 일" 전용이다. creator를 알 수 없으면 만들지 않는다(보수적).
    - idempotent: payload에 `delegation_board_task` marker가 이미 있으면 skip.
    - best-effort: 어떤 실패도 삼키고 None을 반환한다. dispatch SoR은 collector
      command이며 이 티켓은 부가 evidence다.

    성공 시 `{"task_id": ...}` marker를 반환한다(호출자가 payload에 보존).
    """
    try:
        payload = item.payload or {}
        if isinstance(payload.get("delegation_board_task"), dict):
            return None  # 이미 만들었다(재dispatch/approve 재시도 멱등)
        creator = item.created_by
        if creator is None or assignee_user_id == creator:
            return None
        delegator_name: str | None = None
        with session() as s:
            delegator_name = s.execute(
                select(users.c.display_name).where(users.c.user_id == creator)
            ).scalar()
        source_label = f"위임 — {delegator_name}" if delegator_name else "위임받은 작업"
        short = instruction[:80] + ("…" if len(instruction) > 80 else "")
        with audit("personal_board.delegation_task", correlation_id=correlation_id) as span:
            task_id = create_delegation_task(
                assignee_user_id,
                settings.node_id,
                title=f"위임: {short}",
                note=f"{instruction}\n\nagent work: {item.work_id}",
                source_label=source_label,
            )
            span.add_meta(
                work_id=str(item.work_id),
                task_id=str(task_id),
                assignee_user_id=str(assignee_user_id),
            )
            span.set_output({"task_id": str(task_id)})
        # 열려 있는 /board가 새로고침 없이 반영되게 owner 전용 SSE 핑을 보낸다
        # (내용 없음 — 실제 데이터는 구독자 본인의 인증된 재조회로만 온다).
        from orthus import realtime

        realtime.publish(
            {
                "type": "personal_board_changed",
                "node_id": settings.node_id,
                "owner_id": str(assignee_user_id),
            }
        )
        return {"task_id": str(task_id)}
    except Exception:
        logger.warning(
            "delegation board task creation failed for work %s (dispatch unaffected)",
            item.work_id,
            exc_info=True,
        )
        return None


def _mark_auto_agent_task_failed(
    item: AgentWorkItem,
    *,
    error_message: str,
    settings: Settings,
) -> AgentWorkItem:
    now = datetime.now(UTC)
    next_payload = dict(item.payload)
    next_payload["auto_execution"] = {
        "kind": "agent_task",
        "status": "failed",
        "error_message": _redacted_note(error_message),
        "executed_at": now.isoformat(),
    }
    next_reason_codes = list(item.reason_codes)
    if "auto_execute_failed" not in next_reason_codes:
        next_reason_codes.append("auto_execute_failed")
    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="request_more_data",
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(work_id=str(updated_item.work_id), agent_task_status="failed")
        span.set_output({"work_id": str(updated_item.work_id), "state": updated_item.state})
        return updated_item


def resolve_agent_task_work_item_result(
    command_payload: dict[str, Any],
    *,
    command_status: str,
    result: dict[str, Any] | None,
    settings: Settings | None = None,
) -> AgentWorkItem | None:
    """Reflect a completed agent_task collector command onto its work item.

    Called from the collector /complete handler. Returns the updated item, or
    None when the work_item_id is absent or its row no longer exists (the command
    completion itself stays successful — a missing work item is silently skipped).
    """
    settings = settings or get_settings()
    raw_work_id = (command_payload or {}).get("work_item_id")
    if not raw_work_id:
        return None
    try:
        work_id = UUID(str(raw_work_id))
    except ValueError:
        return None

    existing = _find_by_source_work_id(work_id)
    if existing is None or existing.action_family != "agent_task":
        return None

    now = datetime.now(UTC)
    existing_origin = (existing.payload or {}).get("mail_origin")
    is_compose_draft = (
        isinstance(existing_origin, dict) and existing_origin.get("kind") == "compose_draft"
    )
    # Compose drafts are deliverable text, not a conversation: never treat a "?" in
    # the draft body as a request for operator input (no HITL on a draft).
    needs_user_input = (
        not is_compose_draft
        and command_status == "done"
        and _agent_task_result_needs_user_input(result or {})
    )
    next_state: AgentWorkState = (
        "request_more_data" if needs_user_input or command_status != "done" else "resolved"
    )
    next_payload = dict(existing.payload or {})
    runner_session_id = None
    if isinstance(result, dict):
        raw_runner_session_id = result.get("runner_session_id")
        if isinstance(raw_runner_session_id, str) and raw_runner_session_id.strip():
            runner_session_id = raw_runner_session_id.strip()
            _store_runner_session_id(str(next_payload.get("thread_id") or ""), runner_session_id)
    auto_execution = dict(next_payload.get("auto_execution") or {})
    auto_execution.update(
        {
            "kind": "agent_task",
            "status": "awaiting_user_input" if needs_user_input else command_status,
            "completed_at": now.isoformat(),
            "result": result or {},
        }
    )
    if runner_session_id:
        auto_execution["runner_session_id"] = runner_session_id
    next_payload["auto_execution"] = auto_execution
    # compose_draft: parse the agent's {subject, body} answer onto THIS work item so
    # the compose box can read payload.mail_draft. No completion email is created
    # (the user sends manually through /mail/send). An empty body downgrades the
    # state to request_more_data so the FE can surface a retry.
    if is_compose_draft and command_status == "done":
        from orthus.mail.compose import coerce_mail_draft

        origin = existing_origin if isinstance(existing_origin, dict) else {}
        draft = coerce_mail_draft(
            _agent_output_text(result or {}),
            mode=str(origin.get("draft_mode") or "compose"),
            subject=str(origin.get("subject") or ""),
        )
        next_payload["mail_draft"] = {"subject": draft.subject, "body": draft.body}
        if not draft.body.strip():
            next_state = "request_more_data"
    if needs_user_input:
        next_payload["pending_user_input"] = {
            "status": "waiting",
            "requested_at": now.isoformat(),
            "chat_session_id": str(next_payload.get("chat_session_id") or ""),
            "thread_id": str(next_payload.get("thread_id") or ""),
            "runner": str(next_payload.get("runner") or ""),
            "mode": str(next_payload.get("mode") or ""),
            "prompt": _redact_trunc(_agent_output_text(result or {}), 1200),
            "used_for_outcome": False,
        }
    next_reason_codes = list(existing.reason_codes)
    if needs_user_input and _AGENT_TASK_AWAITING_USER_INPUT_REASON not in next_reason_codes:
        next_reason_codes.append(_AGENT_TASK_AWAITING_USER_INPUT_REASON)
    if command_status != "done" and "auto_execute_failed" not in next_reason_codes:
        next_reason_codes.append("auto_execute_failed")

    with audit("agent_task.complete", correlation_id=existing.correlation_id) as span:
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == work_id)
                .values(
                    payload=next_payload,
                    state=next_state,
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        if row is None:
            return None
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            command_status=command_status,
            state=updated_item.state,
        )
        completion_work_id = None
        if command_status == "done" and not needs_user_input and not is_compose_draft:
            try:
                completion_work_id = _maybe_create_completion_email_draft(
                    updated_item, result or {}, settings=settings
                )
            except Exception as exc:  # noqa: BLE001 — completion draft must not break /complete
                span.add_meta(completion_email_error=type(exc).__name__)
        if completion_work_id is not None:
            span.add_meta(completion_email_work_id=str(completion_work_id))
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "completion_email_work_id": str(completion_work_id)
                if completion_work_id is not None
                else None,
            }
        )
        return updated_item


def is_agent_task_awaiting_user_input(item: AgentWorkItem) -> bool:
    """True when an agent_task completed by asking the chat owner for input."""
    if item.action_family != "agent_task" or item.state != "request_more_data":
        return False
    if _AGENT_TASK_AWAITING_USER_INPUT_REASON not in (item.reason_codes or []):
        return False
    pending = item.payload.get("pending_user_input")
    return isinstance(pending, dict) and pending.get("status") == "waiting"


def continue_agent_task_after_user_input(
    actor_user_id: UUID,
    awaiting_item: AgentWorkItem,
    *,
    answer: str,
    actor_role: str | None,
    settings: Settings | None = None,
) -> AgentWorkItem | None:
    """Continue a turn-based HITL agent_task after the chat owner answers.

    This does not write into a still-running subprocess stdin. It creates the
    next agent_task turn in the same Agent-chat session; the thread mapping then
    resumes the runner's native session (Claude/Codex/Hermes) when available.
    """
    settings = settings or get_settings()
    if not is_agent_task_awaiting_user_input(awaiting_item):
        return None
    answer_text = redact_pii_text((answer or "").strip())
    if not answer_text:
        return None
    payload = awaiting_item.payload or {}
    try:
        chat_session_id = UUID(str(payload.get("chat_session_id") or ""))
    except (ValueError, TypeError):
        return None
    assignee_user_id = str(payload.get("assignee_user_id") or "").strip()
    runner = str(payload.get("runner") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    if not assignee_user_id or runner not in _AGENT_TASK_ALLOWED_RUNNERS:
        return None
    if mode not in _AGENT_TASK_ALLOWED_MODES:
        return None

    instruction = (
        "이전 실행에서 사용자 입력을 요청했다. 아래 사용자 답변을 반영해 같은 작업을 계속 진행해.\n\n"
        f"사용자 답변:\n{answer_text}"
    )
    next_item = create_agent_task_work_item(
        actor_user_id,
        actor_role=actor_role,
        assignee=assignee_user_id,
        mode=mode,
        runner=runner,
        instruction=instruction,
        cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        chat_session_id=chat_session_id,
        settings=settings,
    )
    if next_item is not None:
        _mark_agent_task_user_input_answered(awaiting_item, next_item.work_id)
    return next_item


def mark_work_item_user_input_answered(
    item: AgentWorkItem,
    *,
    answer_ref: str,
    answer_ref_key: str = "answer_work_id",
    reason_code: str = _AGENT_TASK_ANSWERED_USER_INPUT_REASON,
    audit_name: str = "agent_task.user_input",
) -> None:
    """Mark a work item whose `payload.pending_user_input` was waiting as answered
    and resolve it.

    Shared by the delegated agent_task turn-based HITL (answer_ref=next work_id,
    answer_ref_key="answer_work_id") and the orchestrator gate-origin HITL
    (answer_ref=answer message id, answer_ref_key="answer_message_id",
    reason_code="hitl_answered"). Append-only status transition; the row moves to
    "resolved" so it leaves the review queue.
    """
    now = datetime.now(UTC)
    next_payload = dict(item.payload or {})
    pending = dict(next_payload.get("pending_user_input") or {})
    pending.update(
        {
            "status": "answered",
            "answered_at": now.isoformat(),
            answer_ref_key: answer_ref,
        }
    )
    next_payload["pending_user_input"] = pending
    next_reason_codes = list(item.reason_codes or [])
    if reason_code not in next_reason_codes:
        next_reason_codes.append(reason_code)
    with audit(audit_name, correlation_id=item.correlation_id) as span:
        with session() as s:
            s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="resolved",
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
            )
            s.commit()
        span.add_meta(work_id=str(item.work_id), **{answer_ref_key: answer_ref})
        span.set_output(
            {
                "work_id": str(item.work_id),
                "state": "resolved",
                answer_ref_key: answer_ref,
            }
        )


def _mark_agent_task_user_input_answered(item: AgentWorkItem, answer_work_id: UUID) -> None:
    """Thin wrapper preserving the agent_task caller's signature."""
    mark_work_item_user_input_answered(item, answer_ref=str(answer_work_id))


def _sanitize_mail_origin(mail_origin: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only the mail-origin keys slice 5 needs, redacted where free-text.

    `sender` and `subject` come from an untrusted mail; they are redacted before
    storage. message/external ids and backend are identifiers, kept as-is for the
    reply linkage. Returns None when no usable origin is present (operator direct
    enqueue), which keeps a non-mail agent_task free of a completion email.
    """
    if not isinstance(mail_origin, dict):
        return None
    sender = str(mail_origin.get("sender") or "").strip()
    if not sender:
        return None
    origin: dict[str, Any] = {
        "sender": redact_pii_text(sender),
        "sender_hint": _normalise_recipient_hint(sender) or redact_pii_text(sender),
        "subject": redact_pii_text(str(mail_origin.get("subject") or "").strip()),
    }
    for key in (
        "source_canonical_id",
        "backend",
        "message_id",
        "external_id",
        "reply_from",
        # reply_to_addr / reply_subject are the real outgoing-reply To address and
        # subject; kept unredacted (like reply_from) so an approved reply draft can
        # actually be sent. The reply body is likewise stored unredacted.
        "reply_to_addr",
        "reply_subject",
        "kind",
        # compose_draft: "compose" | "reply" — drives the fallback subject when the
        # agent's JSON parse fails in the completion handler.
        "draft_mode",
    ):
        value = mail_origin.get(key)
        if value:
            origin[key] = str(value)
    return origin


def _maybe_create_completion_email_draft(
    item: AgentWorkItem,
    result: dict[str, Any],
    *,
    settings: Settings,
) -> UUID | None:
    """Slice 5: surface a done mail-originated agent_task back to the sender.

    Builds a deterministic completion-summary email_send draft_for_review work item
    addressed to the original mail sender. Reversible (draft only) — the real send
    still requires the existing email_send approval gate. No-op when the agent_task
    has no mail origin (operator direct enqueue) or the sender hint is unusable.
    """
    if item.created_by is None:
        return None
    origin = item.payload.get("mail_origin")
    if not isinstance(origin, dict):
        return None
    recipient_hint = _normalise_recipient_hint(
        str(origin.get("sender_hint") or origin.get("sender") or "")
    )
    if recipient_hint is None:
        return None

    instruction = str(item.payload.get("instruction") or "").strip()
    subject_hint = _completion_email_subject(origin, instruction)
    body_template = _completion_email_body(recipient_hint, instruction, result, origin=origin)

    # For a reply-draft completion, build the real outgoing-reply routing context
    # so an APPROVED draft actually sends through the existing email_send approval
    # path (decide_work_item -> send_approved_reply), under all the usual send
    # guards. Only reply_draft completions get a reply_context; generic agent_task
    # completions stay draft-only (no reply_context -> approve never sends).
    send_context = _completion_reply_context(origin, subject_hint)
    if send_context is not None:
        subject_hint = send_context.subject
        email_draft = ReplyDraftPayload(
            recipient_hint=send_context.to_addr,
            subject_hint=send_context.subject,
            body_template=body_template,
            reply_context=send_context,
            llm_drafted=bool(_agent_output_text(result).strip()),
        ).model_dump(mode="json")
    else:
        email_draft = EmailDraftPayload(
            recipient_hint=recipient_hint,
            subject_hint=subject_hint,
            body_template=body_template,
        ).model_dump(mode="json")

    candidate_payload: dict[str, Any] = {
        "question": f"위임 작업 완료 알림: {instruction[:80] or item.title}",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
        "recipient_hint": send_context.to_addr if send_context is not None else recipient_hint,
        "completion_origin": {
            "agent_task_work_id": str(item.work_id),
            "source_canonical_id": origin.get("source_canonical_id"),
        },
        "email_draft": email_draft,
    }
    if send_context is not None:
        # Top-level reply_context is the exact trigger the approval route checks
        # before calling send_approved_reply (orthus/api/routes/agent_work.py).
        candidate_payload["reply_context"] = send_context.model_dump(mode="json")
    candidate = AgentWorkCandidate(
        source_kind="agent_task",
        source_ref_id=f"agent-task-complete-{item.work_id}",
        action_family="email_send",
        title=f"답장 초안 · {subject_hint}"[:96],
        payload=candidate_payload,
        evidence=[
            {
                "kind": "agent_task_completion",
                "ref": str(item.work_id),
                "source_canonical_id": origin.get("source_canonical_id"),
            }
        ],
    )
    classified, decision, classify_correlation_id, _classify_run_id = (
        classify_candidate_with_policy_memory(candidate, item.created_by, settings=settings)
    )
    completion_item = persist_agent_work_item(
        classified,
        decision,
        user_id=item.created_by,
        correlation_id=classify_correlation_id,
        settings=settings,
    )
    return completion_item.work_id


def _completion_email_subject(origin: dict[str, Any], instruction: str) -> str:
    subject = str(origin.get("subject") or "").strip()
    base = subject or (instruction[:60] if instruction else "위임 작업")
    base = " ".join(base.split())
    if base.lower().startswith("re:"):
        return base[:120]
    return f"Re: {base}"[:120]


def _reply_subject_line(subject: str) -> str:
    compact = " ".join((subject or "").split())
    if not compact:
        return "Re: (제목 없음)"
    if compact.lower().startswith("re:"):
        return compact[:120]
    return f"Re: {compact}"[:120]


def _completion_reply_context(origin: dict[str, Any], subject_hint: str) -> MailReplyContext | None:
    """Outgoing-reply routing context for a reply_draft completion, or None.

    Returns None (draft-only — approve will NOT send) unless this is a
    `reply_draft` origin carrying a routable company From (`reply_from`) and a
    real recipient address (`reply_to_addr`). An unsupported backend just fails
    MailReplyContext validation here and also falls back to a non-sending draft.
    Final routing/guard enforcement stays in send_approved_reply.
    """
    if origin.get("kind") != "reply_draft":
        return None
    from_addr = str(origin.get("reply_from") or "").strip()
    to_addr = str(origin.get("reply_to_addr") or "").strip()
    if not from_addr or not to_addr:
        return None
    subject = _reply_subject_line(str(origin.get("reply_subject") or "")) or subject_hint
    try:
        return MailReplyContext(
            backend=str(origin.get("backend") or ""),
            from_addr=from_addr,
            to_addr=to_addr,
            in_reply_to_message_id=origin.get("message_id"),
            in_reply_to_external_id=origin.get("external_id"),
            subject=subject,
        )
    except ValueError:
        return None


def _completion_email_body(
    recipient_hint: str,
    instruction: str,
    result: dict[str, Any],
    *,
    origin: dict[str, Any] | None = None,
) -> str:
    # A reply-draft agent_task's deliverable IS the reply: surface the agent's
    # draft verbatim (the owner reviews + edits before send), with no
    # "task completed" wrapper and no re-redaction (it is the outgoing reply,
    # matching the P7.1 reply-draft body).
    if isinstance(origin, dict) and origin.get("kind") == "reply_draft":
        draft = _agent_output_text(result).strip()
        return draft or "(초안 생성에 실패했습니다 — 검토자가 직접 작성해 주세요.)"
    instruction_line = instruction or "(작업 내용 없음)"
    summary = _completion_result_summary(result)
    return (
        f"{recipient_hint}님,\n\n"
        "요청하신 작업이 완료되었습니다.\n\n"
        f"작업: {instruction_line}\n\n"
        f"결과 요약:\n{summary}\n\n"
        "[검토자가 본문을 확인하고 필요한 세부 내용을 채웁니다.]\n\n"
        "감사합니다."
    )


def _agent_output_text(result: dict[str, Any]) -> str:
    """The agent's actual output text from a runner result.

    Claude streams `--output-format stream-json` (JSONL); the daemon pre-extracts
    the final answer into `result_text`, which is preferred here. Older claude runs
    used `--output-format json` (a single envelope with `.result`); codex/hermes
    emit plain text. Order: result_text -> stream-json `result` event -> single
    envelope -> raw stdout.
    """
    if not isinstance(result, dict):
        return ""
    pre = result.get("result_text")
    if isinstance(pre, str) and pre.strip():
        return pre.strip()
    raw = str(result.get("stdout") or "").strip()
    if not raw.startswith("{"):
        return raw
    # stream-json JSONL: the trailing {"type":"result","result":...} event wins.
    if "\n" in raw:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                text = event.get("result")
                if isinstance(text, str) and text.strip():
                    return text.strip()
                break
    # Single-envelope (legacy --output-format json).
    try:
        envelope = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(envelope, dict):
        text = envelope.get("result")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return raw


def _agent_task_result_needs_user_input(result: dict[str, Any]) -> bool:
    text = _agent_output_text(result).strip()
    if not text:
        return False
    lowered = text.lower()
    has_input_term = any(term in lowered for term in _AGENT_TASK_INPUT_TERMS)
    has_question_mark = "?" in text or "？" in text
    choice_count = len(_AGENT_TASK_CHOICE_RE.findall(text))
    has_choice_list = choice_count >= 2 or _AGENT_TASK_INLINE_CHOICE_RE.search(text) is not None
    if has_choice_list and (has_input_term or has_question_mark):
        return True
    if has_question_mark and has_input_term:
        return True
    return any(
        phrase in lowered
        for phrase in (
            "reply with",
            "answer with",
            "choose one",
            "select one",
            "which option",
            "let me know",
            "사용자 입력",
            "답변을 기다",
            "선택해 주세요",
            "골라 주세요",
        )
    )


def _completion_result_summary(result: dict[str, Any], *, max_chars: int = 800) -> str:
    if not isinstance(result, dict) or not result:
        return "(결과 없음)"
    text = (_agent_output_text(result) or str(result.get("summary") or "")).strip()
    if not text:
        exit_code = result.get("exit_code")
        return (
            f"작업이 종료되었습니다 (exit_code={exit_code})."
            if exit_code is not None
            else "(결과 없음)"
        )
    redacted = redact_pii_text(text)
    if len(redacted) > max_chars:
        return redacted[:max_chars] + "…"
    return redacted


def _completion_failure_summary(result: dict[str, Any], *, max_chars: int = 400) -> str:
    """Human error text for a FAILED delegated agent_task.

    Covers the collector daemon's three failure shapes:
    a pre-flight ``reason`` (agent_task disabled / unsupported runner|mode /
    missing instruction or cwd / runner not installed), a timeout, and a non-zero
    exit carrying ``stderr``. Free text is PII-redacted and truncated so a runner
    error surfaces in chat without leaking instruction/path content.
    """
    if not isinstance(result, dict) or not result:
        return "에이전트 실행이 실패했습니다."
    reason = result.get("reason")
    if isinstance(reason, str) and reason.strip():
        return _redact_trunc(reason.strip(), max_chars)
    if result.get("timed_out") is True:
        return "에이전트 실행이 시간 초과로 중단되었습니다."
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        # stderr is often multi-line; the last non-empty line is usually the error.
        lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
        if lines:
            exit_code = result.get("exit_code")
            suffix = f" (exit_code={exit_code})" if exit_code is not None else ""
            return _redact_trunc(lines[-1], max_chars) + suffix
    exit_code = result.get("exit_code")
    if exit_code is not None:
        return f"에이전트 실행이 실패했습니다 (exit_code={exit_code})."
    return "에이전트 실행이 실패했습니다."


def _redact_trunc(text: str, max_chars: int) -> str:
    redacted = redact_pii_text(text)
    return redacted[:max_chars] + "…" if len(redacted) > max_chars else redacted


def _find_by_source_work_id(work_id: UUID) -> AgentWorkItem | None:
    with session() as s:
        row = s.execute(
            select(agent_work_items).where(agent_work_items.c.work_id == work_id)
        ).first()
    return _row_to_item(row) if row is not None else None


def execute_auto_personal_board_cleanup(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    payload = item.payload or {}
    if (
        item.action_family != "personal_board_cleanup"
        or item.state != "auto_execute"
        or settings.node_kind != "personal"
        or payload.get("cleanup_kind") != "archive_done_tasks"
        or payload.get("reversibility") != "reversible"
        or payload.get("external_write") is True
        or payload.get("delete") is True
        or payload.get("actor_role") not in {"owner", "admin", "scheduler"}
    ):
        return item

    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with audit("personal_board.cleanup", correlation_id=span.correlation_id) as cleanup_span:
            tasks = _done_personal_board_tasks(
                user_id,
                node_id=settings.node_id,
                limit=_personal_board_cleanup_limit(payload),
            )
            archived: list[dict[str, str | None]] = []
            for task in tasks:
                update_task(
                    user_id,
                    settings.node_id,
                    task["task_id"],
                    TaskPatch(status="archived"),
                )
                archived.append(
                    {
                        "task_id": str(task["task_id"]),
                        "title": task["title"],
                        "scheduled_date": _date_iso(task["scheduled_date"]),
                        "placement": "scheduled"
                        if task["scheduled_date"] is not None
                        else "backlog",
                    }
                )
            cleanup_span.add_meta(
                cleanup_kind="archive_done_tasks",
                archived_count=len(archived),
                node_id=settings.node_id,
                user_id=str(user_id),
            )
            cleanup_span.set_output(
                {
                    "cleanup_kind": "archive_done_tasks",
                    "archived_count": len(archived),
                }
            )

        now = datetime.now(UTC)
        next_payload = dict(item.payload)
        next_payload["auto_execution"] = {
            "kind": "personal_board_cleanup",
            "cleanup_kind": "archive_done_tasks",
            "archived_count": len(archived),
            "archived_tasks": archived,
            "reversible": True,
            "external_write": False,
            "executed_at": now.isoformat(),
        }
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="resolved",
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            cleanup_kind="archive_done_tasks",
            archived_count=len(archived),
            node_id=settings.node_id,
            owner_id=str(updated_item.owner_id) if updated_item.owner_id is not None else None,
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "archived_count": len(archived),
            }
        )
        return updated_item


def execute_auto_email_send(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    if settings.email_sender == "none":
        return item
    if item.action_family != "email_send" or item.state != "draft_for_review":
        return item
    if settings.node_kind != "personal" or item.owner_id != user_id:
        return item

    payload = item.payload or {}
    if payload.get("actor_role") not in _EMAIL_AUTO_SEND_OPERATOR_ROLES:
        return item
    gate = payload.get("email_auto_send_gate")
    if not isinstance(gate, dict):
        return item
    if gate.get("eligible") is not True or gate.get("used_for_outcome") is not False:
        return item
    if gate.get("missing_checks") not in ([], None):
        return item

    draft_payload = payload.get("email_draft")
    if not isinstance(draft_payload, dict):
        return item
    try:
        draft = EmailDraftPayload.model_validate(draft_payload)
    except ValueError as exc:
        return _mark_auto_email_send_failed(item, str(exc), settings=settings)
    recipient_hash = _email_hash(draft.recipient_hint)
    subject_hash = _email_hash(draft.subject_hint)
    body_hash = _email_hash(draft.body_template)
    payload_recipient_hash = payload.get("recipient_hash")
    if payload_recipient_hash is not None and str(payload_recipient_hash) != recipient_hash:
        return _mark_auto_email_send_failed(
            item,
            "email draft recipient hash does not match gate payload",
            reason_code="email_recipient_hash_mismatch",
            settings=settings,
        )

    try:
        sender = get_email_sender(settings.email_sender)
    except ValueError as exc:
        return _mark_auto_email_send_failed(item, str(exc), settings=settings)
    if sender is None:
        return item

    now = datetime.now(UTC)
    if _email_send_already_sent(item.work_id):
        return get_agent_work_item(user_id, item.work_id, settings=settings) or item
    if _email_send_rate_limited(item, recipient_hash, now):
        _insert_email_send_log(
            item,
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
            body_hash=body_hash,
            sender_kind=sender.sender_kind,
            status="rate_limited",
            correlation_id=item.correlation_id,
            error_message="rate limited",
            sent_at=now,
        )
        return _mark_auto_email_send_failed(
            item,
            "email send rate limited",
            reason_code="email_send_rate_limited",
            settings=settings,
        )

    request = SendRequest(
        work_id=item.work_id,
        node_id=settings.node_id,
        owner_id=item.owner_id,
        recipient_hash=recipient_hash,
        subject_hash=subject_hash,
        body_hash=body_hash,
    )
    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with audit("email.send", correlation_id=span.correlation_id) as send_span:
            send_span.add_meta(
                work_id=str(item.work_id),
                node_id=settings.node_id,
                owner_id=str(item.owner_id),
                sender_kind=sender.sender_kind,
            )
            result = sender.send(request)
            send_span.set_output(
                {
                    "work_id": str(item.work_id),
                    "sender_kind": result.sender_kind,
                    "status": result.status,
                }
            )
        status = result.status
        error_message = _redacted_note(result.error_message)
        send_id = _insert_email_send_log(
            item,
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
            body_hash=body_hash,
            sender_kind=result.sender_kind,
            status=status,
            correlation_id=span.correlation_id,
            error_message=error_message,
            sent_at=now,
        )
        if status != "sent":
            return _mark_auto_email_send_failed(
                item,
                error_message or "email sender failed",
                settings=settings,
            )

        next_payload = dict(item.payload)
        next_payload["auto_execution"] = {
            "kind": "email_send",
            "send_id": str(send_id),
            "sender_kind": result.sender_kind,
            "status": status,
            "executed_at": now.isoformat(),
        }
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="resolved",
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            node_id=settings.node_id,
            owner_id=str(updated_item.owner_id),
            sender_kind=result.sender_kind,
            send_id=str(send_id),
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "state": updated_item.state,
                "send_id": str(send_id),
                "sender_kind": result.sender_kind,
            }
        )
        return updated_item


def _email_send_already_sent(work_id: UUID) -> bool:
    with session() as s:
        return (
            s.execute(
                select(email_send_log.c.send_id)
                .where(email_send_log.c.work_id == work_id, email_send_log.c.status == "sent")
                .limit(1)
            ).first()
            is not None
        )


def _email_send_rate_limited(item: AgentWorkItem, recipient_hash: str, now: datetime) -> bool:
    return _email_send_rate_limited_impl(
        node_id=item.node_id,
        owner_id=item.owner_id,
        recipient_hash=recipient_hash,
        now=now,
    )


def _insert_email_send_log(
    item: AgentWorkItem,
    *,
    recipient_hash: str,
    subject_hash: str,
    body_hash: str,
    sender_kind: str,
    status: str,
    correlation_id: UUID | None,
    error_message: str | None,
    sent_at: datetime,
) -> UUID:
    return _insert_email_send_log_impl(
        node_id=item.node_id,
        owner_id=item.owner_id,
        recipient_hash=recipient_hash,
        subject_hash=subject_hash,
        body_hash=body_hash,
        sender_kind=sender_kind,
        status=status,
        sent_at=sent_at,
        origin="agent_work",
        work_id=item.work_id,
        correlation_id=correlation_id,
        error_message=error_message,
    )


def _mark_auto_email_send_failed(
    item: AgentWorkItem,
    error_message: str,
    *,
    reason_code: str = "auto_execute_failed",
    settings: Settings | None = None,
) -> AgentWorkItem:
    settings = settings or get_settings()
    reason_codes = list(item.reason_codes)
    if reason_code not in reason_codes:
        reason_codes.append(reason_code)
    payload = dict(item.payload)
    payload["auto_execution_error"] = {
        "kind": "email_send",
        "error_message": _redacted_note(error_message),
    }
    with session() as s:
        row = s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == item.work_id)
            .values(payload=payload, reason_codes=reason_codes, updated_at=datetime.now(UTC))
            .returning(agent_work_items)
        ).first()
        s.commit()
    return _row_to_item(row)


def sync_promote_staging_to_agent_work(
    user_id: UUID, *, limit: int = 100, settings: Settings | None = None
) -> AgentWorkSyncResult:
    """Backfill pending central promotion stages into Agent Work.

    This creates review queue rows only. It does not approve, reject, import, or
    write promoted company documents.
    """
    settings = settings or get_settings()
    if settings.node_kind != "company":
        return AgentWorkSyncResult(source_kind="promote_staging", created_or_updated=0)

    with session() as s:
        rows = s.execute(
            select(
                promote_staging.c.stage_id,
                promote_staging.c.source_node_id,
                promote_staging.c.source_doc_id,
                promote_staging.c.source_owner_id,
                promote_staging.c.source_scope,
                promote_staging.c.source_title,
                promote_staging.c.sanitized_title,
                func.length(promote_staging.c.sanitized_markdown).label("sanitized_markdown_chars"),
                promote_staging.c.source_meta,
                promote_staging.c.status,
                promote_staging.c.created_by,
                promote_staging.c.approved_by,
                promote_staging.c.promoted_doc_id,
                promote_staging.c.created_at,
                promote_staging.c.updated_at,
                promote_staging.c.decided_at,
            )
            .where(promote_staging.c.status == "pending")
            .order_by(promote_staging.c.created_at.desc())
            .limit(limit)
        ).all()

    items: list[AgentWorkItem] = []
    for row in rows:
        candidate = promote_staging_candidate(row._mapping)
        candidate, decision, correlation_id, _classify_run_id = (
            classify_candidate_with_policy_memory(candidate, user_id, settings=settings)
        )
        items.append(
            persist_agent_work_item(
                candidate,
                decision,
                user_id=user_id,
                correlation_id=correlation_id,
                settings=settings,
            )
        )
    return AgentWorkSyncResult(
        source_kind="promote_staging",
        created_or_updated=len(items),
        items=items,
    )


def sync_connector_runs_to_agent_work(
    user_id: UUID, *, limit: int = 100, settings: Settings | None = None
) -> AgentWorkSyncResult:
    """Backfill failed node-local connector runs into Agent Work.

    This is failure triage only. It does not retry connector syncs or execute
    connector commands.
    """
    settings = settings or get_settings()
    register_default_connector_providers(replace=True)
    join_condition = connector_runs.c.account_id == connector_accounts.c.account_id
    conditions = [
        connector_runs.c.status == "failed",
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.account_kind == settings.node_kind,
    ]
    if settings.node_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        rows = s.execute(
            select(
                connector_runs.c.run_id,
                connector_runs.c.account_id,
                connector_runs.c.connector_slug,
                connector_runs.c.reason,
                connector_runs.c.status,
                connector_runs.c.fetched,
                connector_runs.c.created,
                connector_runs.c.updated,
                connector_runs.c.skipped,
                connector_runs.c.errors,
                connector_runs.c.error_message,
                connector_runs.c.started_at,
                connector_runs.c.finished_at,
                connector_accounts.c.account_kind,
                connector_accounts.c.node_id,
                connector_accounts.c.scope,
                connector_accounts.c.owner_id,
                connector_accounts.c.account_label,
            )
            .select_from(connector_runs.join(connector_accounts, join_condition))
            .where(*conditions)
            .order_by(connector_runs.c.started_at.desc())
            .limit(limit)
        ).all()

    items: list[AgentWorkItem] = []
    for row in rows:
        if not _connector_supported(row.connector_slug, row.account_kind):
            continue
        candidate = connector_run_candidate(row._mapping)
        candidate, decision, correlation_id, _classify_run_id = (
            classify_candidate_with_policy_memory(candidate, user_id, settings=settings)
        )
        items.append(
            persist_agent_work_item(
                candidate,
                decision,
                user_id=user_id,
                correlation_id=correlation_id,
                settings=settings,
            )
        )
    return AgentWorkSyncResult(
        source_kind="connector_run",
        created_or_updated=len(items),
        items=items,
    )


def detect_assistant_command_action(question: str) -> str | None:
    # Delegation (slice 6) is detected by an EXPLICIT deterministic prefix only
    # (parse_chat_delegation, no LLM). When that matches it takes priority over the
    # keyword command families so "위임: gmail 동기화해줘" delegates instead of syncing.
    if parse_chat_delegation(question) is not None:
        return "agent_task"
    compact = question.lower().replace(" ", "")
    if not compact:
        return None
    family = _detect_command_family(compact)
    # A "요약/설명" read verb usually marks a knowledge question. But a compound
    # command ("...요약해서 X로 메일 초안 작성해") also carries an unambiguous action
    # signal, so the read-verb guard must not swallow those strong command families.
    if _is_strong_command(family, question):
        return family
    # A recipient-less email send is still a command when the send verb is the
    # head-final predicate ("해당 내용 요약된 거 메일 보내줘"). Without this the read-verb
    # guard below swallows it into a wiki query and the command vanishes entirely
    # (prod 2026-07-16). It is deliberately NOT routed through _is_strong_command:
    # that predicate drives the command-preservation guard (BLOCKER F) whose contract
    # is "email is strong only with a recipient literal"
    # (test_command_keyword_coverage.py::test_strong_command_semantics_unchanged).
    # Reaching the gate is enough — it stops at request_more_data + required_data and
    # asks who to send to (P3.4c), which is the intended UX here.
    if (
        family == "email_send"
        and not any(term in compact for term in _COMPOUND_CONNECTIVES)
        and _email_action_is_head_final(compact)
    ):
        return family
    if any(term.replace(" ", "") in compact for term in _INFO_QUERY_TERMS):
        return None
    return family


def has_strong_command(question: str) -> bool:
    """command-split 명령 보존 가드(BLOCKER F)용 결정론 선판정.

    원문에 강한 명령 신호(connector_sync, 또는 recipient literal이 붙은 email_send)가
    있는지 LLM 없이 판정한다. True면 LLM split이 명령절을 질문화/recipient 탈락시켜 소실할
    위험이 있으므로 orchestrate가 decompose를 우회하고 535 single-queue로 강등한다."""
    compact = question.lower().replace(" ", "")
    if not compact:
        return False
    # C8: 명시적 위임 프리픽스는 전체 텍스트가 하나의 agent_task 지시다 —
    # detect_assistant_command_action과 같은 우선순위로 strong 취급해, weak family
    # 조합("위임: 문서 초안 ... 그리고 ...")이 LLM split로 조각나며 위임 프리픽스가
    # 소실되는 경로를 막는다(535 single-queue → delegation intake가 통째로 처리).
    if parse_chat_delegation(question) is not None:
        return True
    return _is_strong_command(_detect_command_family(compact), question)


def _last_index(compact: str, terms: tuple[str, ...]) -> int:
    """`terms` 중 가장 뒤에 나타나는 것의 위치 (없으면 -1). compact = 공백 제거 소문자."""
    return max((compact.rfind(t.replace(" ", "")) for t in terms), default=-1)


# 등위 접속 신호. "분기실적 요약하고 메일 보내줘"는 읽기+명령 **복합 입력**이라 decompose가
# 나눠야 한다(요약 리프는 채팅에 답하고 명령 리프만 큐잉). head-final 규칙이 그걸 통째로
# 535 single-queue로 가로채면 사용자는 요약을 못 받는다. head-final은 목적어에 읽기 동사
# 수식이 붙었을 뿐인 **단일** 명령("요약된 거 메일 보내줘")을 위한 것이다.
#
# decompose의 `command_split_signal`/`_CONNECTIVE_TOKENS`를 재사용하지 않는다: decompose가
# service를 import하므로 순환이고, 그 토큰셋은 모집단 불변이 계약이다
# (docs/decompose-prefilter-ext.md "공유 토큰셋 격리"). "해서"는 등위가 아니라 수단/방법
# 수식("요약해서 메일 초안 작성해줘" = 단일 명령)이라 의도적으로 제외한다.
_COMPOUND_CONNECTIVES = ("하고", "그리고", "그다음", "또한", "동시에", "그후")


def _email_action_is_head_final(compact: str) -> bool:
    """한국어 head-final 규칙: 문장의 **마지막** 서술어가 메일 발송 동사인가?

    "해당 내용 요약된 거 메일 보내줘"는 '요약'이 관형 수식이고 '보내'가 주동사라 명령이다.
    반대로 "메일 답장 요약해줘"는 '답장'이 목적어고 '요약'이 주동사라 질문이다. 둘 다
    _EMAIL_TERMS + _EMAIL_ACTION_TERMS + _INFO_QUERY_TERMS를 모두 갖고 있어 집합 검사로는
    구분되지 않는다 — 순서만이 구분한다(2026-07-16 prod: 전자가 read로 강등돼 메일 명령이
    통째로 사라졌다).
    """
    return _last_index(compact, _EMAIL_ACTION_TERMS) > _last_index(compact, _INFO_QUERY_TERMS)


def _is_strong_command(family: str | None, question: str) -> bool:
    if family == "connector_sync":
        return True
    if family == "email_send":
        # An explicit recipient turns "summarize and email" into a real command;
        # without one it stays ambiguous (e.g. "메일 답장 요약해줘") and reads as a query.
        return _email_recipient_hint(question) is not None
    return False


def _detect_command_family(compact: str) -> str | None:
    if not any(term.replace(" ", "") in compact for term in _COMMAND_VERBS):
        return None
    if any(term in compact for term in _CONNECTOR_TERMS) and any(
        term in compact for term in _SYNC_TERMS
    ):
        return "connector_sync"
    if any(term in compact for term in _EMAIL_TERMS) and any(
        term in compact for term in _EMAIL_ACTION_TERMS
    ):
        return "email_send"
    if any(term in compact for term in _DOCUMENT_TERMS):
        return "document_draft"
    if any(term in compact for term in _BOARD_TERMS) and any(
        # §S3 확장: 정돈/청소 — "할일 정돈해줘"/"칸반 청소해줘"도 cleanup 요청.
        term in compact
        for term in ("정리", "처리", "정돈", "청소")
    ):
        return "personal_board_cleanup"
    if any(term in compact for term in _WIKI_TERMS) and any(
        term in compact for term in ("정리", "반영", "처리", "해결")
    ):
        return "central_wiki_task_cleanup"
    return None


def _chat_agent_task_candidate(
    question: str,
    *,
    user_id: UUID,
    actor_role: str | None,
    chat_session_id: UUID | None = None,
    settings: Settings,
) -> AgentWorkCandidate | None:
    """Build a delegation agent_task candidate from a chat (/ask) command.

    Deterministically parses the explicit delegation prefix (parse_chat_delegation,
    no LLM), defaults an unnamed assignee to the requester (self-delegation), then
    reuses agent_task_candidate to build the same gate-field payload the mail path
    uses. Returns None (so /ask falls through to a normal answer) when the text is
    not an explicit delegation.
    """
    parsed = parse_chat_delegation(question)
    if parsed is None:
        return None
    assignee = parsed["assignee"] or str(user_id)
    return agent_task_candidate(
        user_id,
        actor_role=actor_role,
        assignee=assignee,
        mode=parsed["mode"],
        runner=parsed["runner"],
        instruction=parsed["instruction"],
        cwd=parsed.get("cwd"),
        chat_session_id=chat_session_id,
        settings=settings,
    )


def _hold_command_split_auto_execute(
    item: AgentWorkItem,
    *,
    settings: Settings,
) -> AgentWorkItem:
    """command-split fan-out으로 만든 auto_execute 아이템을 실행하지 않고 리뷰로 보류한다.

    BLOCKER C: 복합 "명령+질문" 입력의 명령 fragment는 사용자 검토 없이 외부 쓰기(커넥터 sync,
    board archive, agent_task dispatch)를 자동 실행하면 안 된다. 실행 결과 state를
    request_more_data로 강등하고, **policy_outcome도 request_more_data로 함께 맞춘다** —
    state≠outcome 이중진실(dual-truth)을 남기면 policy_outcome으로 버킷팅하는 inbox 요약
    (`_inbox_agent_work`)과 policy memory 버킷 키(`_policy_bucket_key`)가 held 아이템을
    'auto_execute'로 오분류/오염하기 때문이다(review #5/#6). 게이트의 원 판정은
    `payload.command_split_held_outcome`에 보존해, 리뷰어 approve 시 원래 실행을 재개할 수 있게
    한다(review #2, `resume_held_command_split_action`). request_more_data는 이미
    reviewable(state.py REVIEWABLE_STATES) 의미론이라 새 state를 만들지 않는다(불변식 — held는
    별도 state 아님). email_send/document_draft처럼 애초에 auto_execute가 아닌 아이템은 그대로
    통과시킨다."""
    if item.state != "auto_execute":
        return item
    now = datetime.now(UTC)
    next_reason_codes = list(item.reason_codes or [])
    if "command_split_hold" not in next_reason_codes:
        next_reason_codes.append("command_split_hold")
    next_payload = dict(item.payload)
    next_payload["command_split_hold"] = True
    # Preserve the gate's original verdict so approve can resume the real execution (#2).
    next_payload["command_split_held_outcome"] = item.policy_outcome
    with audit("agent_work.command_split_hold", correlation_id=item.correlation_id) as span:
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="request_more_data",
                    # Keep policy_outcome consistent with state (no dual-truth) — the original
                    # auto_execute verdict lives in payload.command_split_held_outcome.
                    policy_outcome="request_more_data",
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        if row is None:
            # The row was deleted between persist and hold (concurrent delete). Nothing to
            # hold — return the pre-hold item rather than dereferencing None (review #6).
            span.add_meta(work_id=str(item.work_id), held=False, node_id=settings.node_id)
            return item
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            action_family=str(updated_item.action_family),
            held=True,
            node_id=settings.node_id,
        )
        return updated_item


def resume_held_command_split_action(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    """review #2: approve된 held command-split 외부쓰기 fragment의 실제 실행을 재개한다.

    `_hold_command_split_auto_execute`는 커넥터 sync / board archive / agent_task dispatch를
    큐 시점에 **유보만** 하고(state→request_more_data, 원 판정은
    `payload.command_split_held_outcome`에 보존) 실행하지 않았다. 리뷰어 approve는 곧 실행
    재개이므로(email reply의 approve→send와 동일 패턴), 여기서 원래의 typed execute를 돌린다.
    execute_auto_* 는 `state=="auto_execute"` + payload 가드를 **재검증**(defence in depth)하므로
    in-memory로 원 판정을 복원해 호출하고, 그 함수가 DB row 상태(resolved/실패)를 다시 영속한다.
    LLM은 관여하지 않는다 — 결정론 gate가 이미 auto_execute를 판정했고 그 판정을 payload에
    보존했을 뿐이다(LLM-only 실행 금지 유지). held가 아닌 아이템/원 판정이 auto_execute가 아닌
    아이템은 그대로 통과시킨다(idempotent)."""
    settings = settings or get_settings()
    payload = item.payload or {}
    if (
        "command_split_hold" not in (item.reason_codes or [])
        or payload.get("command_split_held_outcome") != "auto_execute"
    ):
        return item
    # Restore the gate's original verdict in-memory so the typed execute guards (keyed on
    # state=="auto_execute") fire; each execute_auto_* re-persists the real result state.
    exec_item = item.model_copy(update={"state": "auto_execute", "policy_outcome": "auto_execute"})
    if exec_item.action_family == "agent_task":
        return execute_auto_agent_task(user_id, exec_item, settings=settings)
    if exec_item.action_family == "connector_sync":
        return execute_auto_connector_sync(user_id, exec_item, settings=settings)
    if exec_item.action_family == "personal_board_cleanup":
        return execute_auto_personal_board_cleanup(user_id, exec_item, settings=settings)
    # email_send is never held (it resolves to draft_for_review, not auto_execute); no other
    # external-write family executes on approve.
    return item


def create_assistant_command_work_item(
    user_id: UUID,
    question: str,
    *,
    actor_role: str | None = None,
    context_wiki_slug: str | None = None,
    upstream_context: str | None = None,
    chat_session_id: UUID | None = None,
    action_family: str | None = None,
    leaf_index: int | None = None,
    command_split_origin: bool = False,
    intake_source: str | None = None,
    settings: Settings | None = None,
) -> AgentWorkItem | None:
    settings = settings or get_settings()
    candidate = assistant_command_candidate(
        question,
        user_id=user_id,
        actor_role=actor_role,
        context_wiki_slug=context_wiki_slug,
        upstream_context=upstream_context,
        chat_session_id=chat_session_id,
        action_family=action_family,
        leaf_index=leaf_index,
        command_split_origin=command_split_origin,
        intake_source=intake_source,
        settings=settings,
    )
    if candidate is None:
        return None
    candidate, decision, correlation_id, _classify_run_id = classify_candidate_with_policy_memory(
        candidate,
        user_id,
        settings=settings,
    )
    item = persist_agent_work_item(
        candidate,
        decision,
        user_id=user_id,
        correlation_id=correlation_id,
        settings=settings,
    )
    if command_split_origin:
        # BLOCKER C: hold external-write auto-execution for review (no connector sync / board
        # archive / agent_task dispatch / email auto-send from a compound-split fragment).
        # Draft creation for review still runs; real execution is suppressed.
        item = _hold_command_split_auto_execute(item, settings=settings)
        # ensure_document_draft_for_work_item is deterministic (no LLM) — stays eager.
        # ensure_email_draft_for_work_item runs an LLM (_generate_command_email); it is
        # moved to lazy-on-review (GET/approve in api/routes/agent_work.py), mirroring the
        # P7.1 reply-draft path, so no LLM fires at queue time.
        item = ensure_document_draft_for_work_item(user_id, item, settings=settings)
        return item
    if item.state == "auto_execute" and item.action_family == "agent_task":
        item = execute_auto_agent_task(user_id, item, settings=settings)
    if item.state == "auto_execute":
        item = execute_auto_connector_sync(user_id, item, settings=settings)
    if item.state == "auto_execute":
        item = execute_auto_personal_board_cleanup(user_id, item, settings=settings)
    item = ensure_document_draft_for_work_item(user_id, item, settings=settings)
    # The lazy-on-review email-draft move (GET/approve in api/routes/agent_work.py) applies to
    # command-split fragments ONLY (they return early above). The pre-existing P3.2
    # assistant_command email path stays EAGER here so its queue-time behavior is byte-identical
    # to before the command-split feature — the lazy move is command-split-scoped, not a
    # flag-independent P3.2 timing change (review #8). The GET/approve lazy ensure is idempotent
    # (guards on an existing draft), so an eager draft here is not re-generated later.
    item = ensure_email_draft_for_work_item(user_id, item, settings=settings)
    # execute_auto_email_send is a no-op unless a draft exists + gates pass (email_send always
    # resolves to draft_for_review today, never auto_execute).
    return execute_auto_email_send(user_id, item, settings=settings)


def create_email_draft_slot_work_item(
    user_id: UUID,
    instruction: str,
    *,
    recipient: str | None = None,
    actor_role: str | None = None,
    intake_source: str | None = None,
    settings: Settings | None = None,
) -> AgentWorkItem | None:
    """P10 구조화 `kind=email_draft` slot intake (spec §13 Finding 3).

    `submit_email_draft` MCP tool이 recipient/instruction을 구조화 slot으로 보내면
    NL 분류기(`detect_assistant_command_action`)도, recipient 정규식 추출
    (`_email_recipient_hint`)도 호출하지 않고 결정론적으로 email_send 후보를 만든다 —
    recipient 추출 책임은 tool을 호출한 에이전트에게 있다. outcome은 여전히 기존
    P3 결정론 policy gate가 정한다(LLM-only 실행 금지 유지): recipient가 없으면
    P3.4c 계약대로 `request_more_data` + `payload.required_data`로 멈추고, 있으면
    `draft_for_review`로 간다. `source_kind="assistant_command"`를 유지해 P3.5의
    exact email auto-send policy bucket
    (email_send|assistant_command|draft_for_review|…)이 변하지 않게 한다.
    """
    settings = settings or get_settings()
    redacted_instruction = redact_pii_text(instruction.strip())
    if not redacted_instruction:
        return None
    recipient_hint = _normalise_recipient_hint(recipient or "")
    # 구조화 slot 전용 digest namespace — 같은 텍스트의 NL intake row와
    # (node_id, source_kind, source_ref_id) on-conflict upsert로 충돌하지 않게 분리한다.
    digest = hashlib.sha256(
        f"{settings.node_id}:{user_id}:email_draft_slot:"
        f"{recipient_hint or ''}:{redacted_instruction}".encode()
    ).hexdigest()[:16]
    payload: dict[str, Any] = {
        "question": redacted_instruction,
        "intake": "email_draft_slot",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
    }
    if intake_source:
        payload["intake_source"] = intake_source
    if actor_role is not None:
        payload["actor_role"] = actor_role
    if recipient_hint:
        # NL 경로와 동일하게 recipient는 운영 데이터라 unmasked로 저장한다
        # (redact하면 배달 불가능한 주소가 된다 — assistant_command_candidate 주석 참조).
        payload["recipient_hint"] = recipient_hint
    candidate = AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id=f"assistant-command-{digest}",
        action_family="email_send",
        title=f"Assistant command: {redacted_instruction[:96]}",
        payload=payload,
        evidence=[
            {
                "kind": "assistant_command",
                "ref": f"assistant-command-{digest}",
                "question": redacted_instruction,
            }
        ],
    )
    candidate, decision, correlation_id, _classify_run_id = classify_candidate_with_policy_memory(
        candidate,
        user_id,
        settings=settings,
    )
    item = persist_agent_work_item(
        candidate,
        decision,
        user_id=user_id,
        correlation_id=correlation_id,
        settings=settings,
    )
    # 기존 P3.2 email 경로와 동일하게 eager로 초안 본문을 채운다(draft_for_review +
    # recipient_hint 있을 때만 동작; 초안 본문 LLM은 분류가 아니라 draft 생성이다).
    item = ensure_email_draft_for_work_item(user_id, item, settings=settings)
    # email_send는 오늘날 항상 draft_for_review로 남는다 — auto_execute 가드 미통과 시 no-op.
    return execute_auto_email_send(user_id, item, settings=settings)


# BLOCKER A: only families that perform an external write get a per-leaf idempotency key.
# For these, two fragments of one compound with the same redacted question must NOT collapse
# into one row (that would silently drop N-1 external writes), so leaf_index enters the digest.
# Other families (document_draft, read/knowledge) keep the leaf_index out so identical text
# still de-dupes. agent_task is not here: it early-returns before this digest path.
_EXTERNAL_WRITE_FAMILIES = frozenset({"email_send", "connector_sync", "personal_board_cleanup"})


def assistant_command_candidate(
    question: str,
    *,
    user_id: UUID,
    actor_role: str | None = None,
    context_wiki_slug: str | None = None,
    upstream_context: str | None = None,
    chat_session_id: UUID | None = None,
    action_family: str | None = None,
    leaf_index: int | None = None,
    command_split_origin: bool = False,
    intake_source: str | None = None,
    settings: Settings | None = None,
) -> AgentWorkCandidate | None:
    settings = settings or get_settings()
    # `action_family` is the caller's authoritative decision (router.classify_intent —
    # may be an LLM verdict the keyword detector would not reproduce). Fall back to the
    # deterministic keyword detector only when the caller did not decide.
    if action_family is None:
        action_family = detect_assistant_command_action(question)
    if action_family is None:
        return None
    if action_family == "agent_task":
        return _chat_agent_task_candidate(
            question,
            user_id=user_id,
            actor_role=actor_role,
            chat_session_id=chat_session_id,
            settings=settings,
        )
    redacted_question = redact_pii_text(question.strip())
    if not redacted_question:
        return None
    # A command-split external-write fragment is HELD for review (request_more_data). Its
    # source_ref_id must never collide with a plain single-command intake of the same text —
    # otherwise persist_agent_work_item's on-conflict upsert would overwrite the held row back
    # to auto_execute and execute the external write for real (review #1 hold-bypass). So EVERY
    # command_split_origin external-write row gets a distinguishing digest suffix, never the ""
    # a non-split intake (command_split_origin=False) uses:
    #   - a fan-out leaf keys by position (":N"); STRICT `is not None` — leaf_index=0 is real;
    #   - the split-failure fall-through (leaf_index=None) keys by a stable ":f" token — WITHOUT
    #     it the fall-through shared the non-split digest and was the live hold-bypass path.
    # Gating on command_split_origin (not just leaf_index) is also load-bearing for flag-off
    # byte-identical: the pre-existing MA.4 decompose-action path passes leaf_index but keeps its
    # old suffix-free digest.
    if command_split_origin and action_family in _EXTERNAL_WRITE_FAMILIES:
        digest_leaf_suffix = f":{leaf_index}" if leaf_index is not None else ":f"
    else:
        digest_leaf_suffix = ""
    digest = hashlib.sha256(
        f"{settings.node_id}:{user_id}:{action_family}:{redacted_question}{digest_leaf_suffix}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    payload = {
        "question": redacted_question,
        "intake": "ask",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
    }
    # command-split fan-out marker + chat link (S2). Both are typed identifiers (enum/UUID,
    # not free text), so they bypass redact_pii_text. Gated on command_split_origin — NOT on
    # `chat_session_id is not None` — because the 535 single-command path and the pre-existing
    # MA.4 decompose-action path ALSO receive chat_session_id but must keep their pre-feature
    # payload shape (flag-off byte-identical, review B1). `intake_source` marks command-split
    # rows uniformly (incl. email drafts); `command_split_hold` is the suppression-specific
    # reason on auto-execute families only.
    if command_split_origin:
        if intake_source:
            payload["intake_source"] = intake_source
        if chat_session_id is not None:
            payload["chat_session_id"] = str(chat_session_id)
    if actor_role is not None:
        payload["actor_role"] = actor_role
    # P2.4: decompose upstream sub-answer context (read→act). Redacted; never raw.
    redacted_upstream = redact_pii_text((upstream_context or "").strip()) or None
    if context_wiki_slug or redacted_upstream:
        ctx: dict[str, str] = {}
        if context_wiki_slug:
            ctx["wiki_slug"] = context_wiki_slug
        if redacted_upstream:
            ctx["upstream"] = redacted_upstream
        payload["context"] = ctx
    if action_family == "connector_sync":
        payload.update(
            _connector_sync_command_payload(
                question,
                user_id=user_id,
                actor_role=actor_role,
                settings=settings,
            )
        )
    if action_family == "personal_board_cleanup":
        payload.update(
            _personal_board_cleanup_command_payload(
                question,
                actor_role=actor_role,
                settings=settings,
            )
        )
    if action_family == "email_send":
        # Extract the recipient from the RAW question, not the redacted one:
        # redact_pii_text masks email local-parts (john@x.com -> j***@x.com), which
        # would otherwise be stored as the draft recipient and never resolve to a
        # deliverable address. The recipient of an outbound mail is operational data
        # (same treatment as reply_context.to_addr), so it is stored unmasked.
        payload.update(_email_send_command_payload(question))
    evidence = [
        {
            "kind": "assistant_command",
            "ref": f"assistant-command-{digest}",
            "question": redacted_question,
        }
    ]
    return AgentWorkCandidate(
        source_kind="assistant_command",
        source_ref_id=f"assistant-command-{digest}",
        action_family=action_family,
        title=f"Assistant command: {redacted_question[:96]}",
        payload=payload,
        evidence=evidence,
    )


def _email_send_command_payload(question: str) -> dict[str, Any]:
    recipient_hint = _email_recipient_hint(question)
    if recipient_hint is None:
        return {}
    return {"recipient_hint": recipient_hint}


def _email_recipient_hint(question: str) -> str | None:
    # Prefer a literal address so "...요약해서 a@b.com로 메일" yields the address
    # instead of the particle heuristic over-capturing the preceding clause.
    address = _EMAIL_ADDRESS.search(question)
    if address is not None:
        hint = _normalise_recipient_hint(address.group(0))
        if hint:
            return hint
    for pattern in (_EMAIL_RECIPIENT_KO, _EMAIL_RECIPIENT_EN):
        for match in pattern.finditer(question):
            hint = _normalise_recipient_hint(match.group("recipient"))
            if hint:
                return hint
    return None


def _normalise_recipient_hint(value: str) -> str | None:
    hint = " ".join(value.strip(" ,.:;\"'()[]{}").split())
    if not hint:
        return None
    lowered = hint.lower()
    if lowered in {"email", "mail", "gmail", "이메일", "메일", "답장"}:
        return None
    return hint[:64]


def ensure_email_draft_for_work_item(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    if item.action_family != "email_send":
        return item
    if item.state != "draft_for_review":
        return item
    # A reply to a specific inbound mail carries a reply_context and is handled by
    # ensure_reply_draft_for_work_item (which keeps the "Re:" subject + grounds on the
    # original thread). This new-mail path only composes drafts with no reply_context.
    if isinstance(item.payload.get("reply_context"), dict):
        return item
    existing_draft = item.payload.get("email_draft")
    if isinstance(existing_draft, dict) and existing_draft.get("body_template"):
        return item

    recipient_hint = _normalise_recipient_hint(str(item.payload.get("recipient_hint") or ""))
    if recipient_hint is None:
        return item
    question = str(item.payload.get("question") or item.title).strip()
    # P2.4 read→act: a decompose command leaf carries the preceding leaf's answer in
    # payload.context.upstream. Feed it as grounding so "이 내용을 기반으로 …" drafts on the
    # actual content instead of a generic template (review: context injected but unused).
    ctx = item.payload.get("context")
    upstream_context = ctx.get("upstream") if isinstance(ctx, dict) else None
    subject, body, llm_drafted = _generate_command_email(recipient_hint, question, upstream_context)
    payload = dict(item.payload)
    payload["email_draft"] = EmailDraftPayload(
        recipient_hint=recipient_hint,
        subject_hint=subject,
        body_template=body,
        llm_drafted=llm_drafted,
    ).model_dump(mode="json")
    with session() as s:
        row = s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == item.work_id)
            .values(payload=payload, updated_at=datetime.now(UTC))
            .returning(agent_work_items)
        ).first()
        s.commit()
    return _row_to_item(row)


def persist_reply_candidate(
    owner_user_id: UUID,
    candidate: AgentWorkCandidate,
    *,
    settings: Settings | None = None,
    owner_scoped: bool = False,
) -> AgentWorkItem | None:
    """Classify + persist a mail reply candidate (P7.1).

    Idempotent on (node_id, source_kind, source_ref_id) via persist_agent_work_item.
    The LLM reply body is generated lazily on GET (ensure_reply_draft_for_work_item),
    not here, so the ingest webhook stays fast. Returns None for a non-candidate.

    `owner_scoped` binds the item to `owner_user_id` even on a company node so an
    individual-mailbox reply draft is visible only to its owner (the same fail-closed
    owner boundary used for personal rows; docs/p6-mail-individual-scope.md §3.4).
    """
    if candidate is None:
        return None
    settings = settings or get_settings()
    candidate, decision, correlation_id, _classify_run_id = classify_candidate_with_policy_memory(
        candidate,
        owner_user_id,
        settings=settings,
    )
    return persist_agent_work_item(
        candidate,
        decision,
        user_id=owner_user_id,
        correlation_id=correlation_id,
        settings=settings,
        owner_scoped=owner_scoped,
    )


_EVENT_BRIEF_MAX_CHARS = 4000


def persist_event_orchestration_brief(
    user_id: UUID,
    routed,
    *,
    source_ref: str,
    title: str,
    meta: dict | None = None,
    settings: Settings | None = None,
) -> AgentWorkItem:
    """Phase 3-B MA.8a — persist an event-triggered knowledge brief.

    The brief is the decompose synthesized answer (already grounded on compiled
    wiki pages; no new RAG path, 불변식 2). The item performs no external write — it
    surfaces the prepared company knowledge for the owner to review/act on, and the
    deterministic gate fixes it to draft_for_review (§P3B E3, 불변식 1/9). Idempotent
    on (node_id, source_kind, source_ref_id) via persist_agent_work_item, so the same
    mail re-orchestrated never duplicates the item.
    """
    settings = settings or get_settings()
    body = ""
    grounded = False
    parts = list(routed.parts) if routed is not None else []
    if routed is not None and routed.synthesized_body is not None:
        body = (routed.synthesized_body.answer or "").strip()
        grounded = bool(body)
    redacted_brief = redact_pii_text(body)[:_EVENT_BRIEF_MAX_CHARS]
    payload: dict[str, Any] = {
        "intake": "event_orchestration",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
        "mode": routed.mode if routed is not None else None,
        "grounded": grounded,
        "brief": redacted_brief,
        "part_total": len(parts),
        "grounded_parts": sum(1 for p in parts if p.grounded),
    }
    if meta:
        payload["mail"] = meta
    candidate = AgentWorkCandidate(
        source_kind="event_orchestration",
        source_ref_id=source_ref,
        action_family="event_orchestration",
        title=title[:200],
        payload=payload,
        evidence=[{"kind": "event_orchestration", "ref": source_ref}],
    )
    decision, correlation_id, _run_id = classify_candidate(candidate)
    return persist_agent_work_item(
        candidate,
        decision,
        user_id=user_id,
        correlation_id=correlation_id,
        settings=settings,
    )


def ensure_reply_draft_for_work_item(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    """Lazily generate the reply body for a reply-draft work item (P7.1).

    No-ops unless the item is an email_send draft_for_review carrying a
    reply_context and no existing email_draft body. Generates a professional
    Korean reply via the chat model, grounded on the inbound subject + thread
    excerpt; on ANY LLM error falls back to a deterministic template. Stores a
    ReplyDraftPayload in payload.email_draft and attaches fail-closed P7.5
    auto-send candidacy evidence (used_for_outcome=False).
    """
    if item.action_family != "email_send" or item.state != "draft_for_review":
        return item
    raw_context = item.payload.get("reply_context")
    if not isinstance(raw_context, dict):
        return item
    existing_draft = item.payload.get("email_draft")
    if isinstance(existing_draft, dict) and existing_draft.get("body_template"):
        return item
    try:
        reply_context = MailReplyContext.model_validate(raw_context)
    except ValueError:
        return item

    settings = settings or get_settings()
    recipient_hint = _normalise_recipient_hint(str(item.payload.get("recipient_hint") or ""))
    if recipient_hint is None:
        recipient_hint = reply_context.to_addr
    body, llm_drafted, grounding = _generate_reply_body(
        reply_context, recipient_hint, user_id=user_id
    )

    payload = dict(item.payload)
    payload["email_draft"] = ReplyDraftPayload(
        recipient_hint=recipient_hint,
        subject_hint=reply_context.subject,
        body_template=body,
        reply_context=reply_context,
        llm_drafted=llm_drafted,
    ).model_dump(mode="json")
    # 근거는 typed ReplyDraftPayload(allowlist, P3.4d) 밖에 별도 키로 둔다 — 그 스키마는
    # send 관련 키 유입을 막는 게 목적이라 확장하지 않는다. 리뷰어 화면이 "이 초안이 무엇에
    # 근거했는지"를 보여주는 용도이고 발송 경로는 이 값을 읽지 않는다.
    if grounding is not None:
        payload["wiki_grounding"] = grounding.model_dump(mode="json")
    candidacy = evaluate_auto_send_candidacy(
        item,
        settings,
        policy_memory=_policy_memory_from_payload(item.payload),
    )
    payload["auto_send_candidacy"] = candidacy.model_dump(mode="json")
    with session() as s:
        row = s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == item.work_id)
            .values(payload=payload, updated_at=datetime.now(UTC))
            .returning(agent_work_items)
        ).first()
        s.commit()
    return _row_to_item(row)


def update_email_draft(
    user_id: UUID,
    work_id: UUID,
    *,
    body_template: str,
    subject_hint: str | None = None,
    settings: Settings | None = None,
) -> AgentWorkItem:
    """Reviewer edit of a reply/email draft before approval.

    Only a ``draft_for_review`` ``email_send`` item the caller owns may be edited.
    The edited ``body_template`` is exactly what the approve→send path
    (``mail.reply.send_approved_reply``) transmits. The typed draft payload is
    re-validated (``extra='forbid'``) so no stray keys slip in.
    """
    item = get_agent_work_item(user_id, work_id, settings=settings)
    if item is None:
        raise KeyError(str(work_id))
    if item.action_family != "email_send" or item.state != "draft_for_review":
        raise ValueError("only a draft_for_review email_send item can be edited")
    body = (body_template or "").strip()
    if not body:
        raise ValueError("draft body must not be empty")
    existing = item.payload.get("email_draft")
    if not isinstance(existing, dict):
        raise ValueError("item has no email_draft to edit")
    next_draft = dict(existing)
    next_draft["body_template"] = body
    if subject_hint is not None and subject_hint.strip():
        next_draft["subject_hint"] = subject_hint.strip()
    if "reply_context" in next_draft:
        next_draft = ReplyDraftPayload.model_validate(next_draft).model_dump(mode="json")
    else:
        next_draft = EmailDraftPayload.model_validate(next_draft).model_dump(mode="json")
    payload = dict(item.payload)
    payload["email_draft"] = next_draft
    with audit("agent_work.draft_edit") as span:
        span.add_meta(work_id=str(work_id), body_chars=len(body))
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == work_id)
                .values(payload=payload, updated_at=datetime.now(UTC))
                .returning(agent_work_items)
            ).first()
            s.commit()
    return _row_to_item(row)


def _generate_reply_body(
    context: MailReplyContext,
    recipient_hint: str,
    *,
    user_id: UUID | None = None,
) -> tuple[str, bool, object | None]:
    """Return (body, llm_drafted, wiki_grounding). Falls back to a deterministic
    template on any LLM error or empty completion.

    `user_id`가 있고 위키 그라운딩 flag가 켜져 있으면 받은 메일 내용으로 회사 위키를
    먼저 조회해 확인된 사실을 프롬프트에 얹는다 — 사내에 답이 있는 문의에 빈 초안이
    나가지 않게 한다. 조회 실패는 fail-open(`orthus/mail/grounding.py`).
    """
    # 지연 import: `orthus.agentwork`는 `orthus.mail`을 최상위에서 import하지 않는다
    # (모듈 상단 import 경계 — orthus/mail/reply.py 참조). grounding 자체는 wiki만
    # 의존하지만 `orthus.mail` 패키지 __init__이 backends/ingest/send를 끌어와 순환이 된다.
    from orthus.mail.grounding import gather_wiki_grounding, grounding_prompt_block

    grounding = gather_wiki_grounding(user_id, subject=context.subject, body=context.thread_excerpt)
    system = (
        "당신은 회사 직원의 이메일 답장 초안을 작성하는 비서다. "
        "정중하고 전문적인 한국어 답장 본문만 작성한다. 받은 메일과 (주어졌다면) 회사 위키에서 "
        "확인된 사실에 근거해 작성하고, 확실하지 않은 사실은 지어내지 않는다. "
        "머리말/꼬리말 메타설명 없이 본문만 출력한다."
    )
    user = (
        f"받는 사람: {recipient_hint}\n"
        f"답장 제목: {context.subject}\n\n"
        f"원본 메일 발췌:\n{context.thread_excerpt}\n"
        f"{grounding_prompt_block(grounding)}\n"
        "위 메일에 대한 답장 본문을 한국어로 작성하라."
    )
    try:
        with audit("agent_work.reply_draft") as span:
            span.add_meta(
                backend=context.backend,
                has_excerpt=bool(context.thread_excerpt),
                wiki_grounded=grounding is not None,
            )
            chat = get_chat_model_for(TASK_EMAIL_DRAFT)
            completion = (chat.complete(system, user) or "").strip()
            span.set_output({"llm_drafted": bool(completion)})
        if completion:
            return completion, True, grounding
    except Exception:  # noqa: BLE001 — any LLM failure falls back deterministically
        pass
    return _reply_body_template(recipient_hint, context), False, grounding


def _reply_body_template(recipient_hint: str, context: MailReplyContext) -> str:
    return (
        f"{recipient_hint}님,\n\n"
        "보내주신 메일 잘 받았습니다.\n\n"
        "[검토자가 본문을 확인하고 필요한 세부 내용을 채웁니다.]\n\n"
        f"원 메일 제목: {context.subject.removeprefix('Re: ').strip()}\n\n"
        "감사합니다."
    )


def _policy_memory_from_payload(payload: dict) -> AgentPolicyMemoryContext | None:
    raw = (payload or {}).get("policy_memory")
    if not isinstance(raw, dict):
        return None
    try:
        return AgentPolicyMemoryContext.model_validate(raw)
    except ValueError:
        return None


def _generate_command_email(
    recipient_hint: str, instruction: str, upstream_context: str | None = None
) -> tuple[str, str, bool]:
    """Compose a NEW outbound email (subject + body) from a chat command.

    Returns (subject, body, llm_drafted). Mirrors _generate_reply_body: the LLM
    actually writes the draft from the user's request so the reviewer has real
    content to review; on ANY LLM error/parse failure it falls back to a
    deterministic first-draft template. Unlike the reply path there is no source
    mail, so the subject is generated (no "Re:" prefix).

    `upstream_context` is the answer from a preceding decompose leaf (P2.4 read→act):
    when the command is "이 내용을 기반으로 …메일 초안" the referenced content lives here,
    so it is fed to the LLM as grounding. Without it the draft can only be a generic
    template because "이 내용" has no referent."""
    # F4a (analysis/f4-email-quality-prereg.md): 구조 요구를 명시한 프롬프트.
    # 아레나 실측에서 짧고 골자만 있는 초안이 완결형 초안에 밀리는 패턴이 확인돼
    # (Sonnet 451자 vs solar 336자, decided 32%) 완결성 요구를 결정론으로 고정한다.
    # 사실 지어내기 금지·빈칸 대괄호·JSON-only 계약은 불변.
    system = (
        "당신은 회사 직원을 대신해 새 이메일 초안을 작성하는 비서다. "
        "사용자의 요청에 근거해 정중하고 전문적인 한국어 이메일을 작성한다. "
        "본문은 다음 구조를 갖춘 완결된 초안이어야 한다: "
        "(1) 받는 사람과의 맥락을 여는 인사·도입 1-2문장, "
        "(2) 요청과 참고 자료의 구체 내용으로 채운 본문 — 참고 자료가 있으면 핵심 수치·"
        "항목을 본문에 직접 반영한다, "
        "(3) 상대가 해야 할 일이나 확인 요청이 있으면 명시적 목록이나 문장으로 정리, "
        "(4) 정중한 맺음 인사와 보내는 사람 서명 자리. "
        "확실하지 않은 사실은 지어내지 않고, 필요한 빈칸은 대괄호 표시로 남긴다. "
        '오직 JSON만 출력한다: {"subject": "<제목>", "body": "<본문>"}'
    )
    context_block = ""
    upstream = (upstream_context or "").strip()
    if upstream:
        context_block = f"\n[참고 자료 — 요청의 '이 내용/이걸' 등이 가리키는 배경]\n{upstream}\n"
    user = (
        f"받는 사람: {recipient_hint}\n"
        f"요청: {instruction}\n"
        f"{context_block}\n"
        "위 요청에 맞는 이메일 제목과 본문을 작성하라. 참고 자료가 있으면 그 내용을 근거로 "
        "본문을 구체적으로 채운다. 제목에 'Re:'를 붙이지 마라."
    )
    try:
        with audit("agent_work.email_draft") as span:
            # This one is `json_only`, and that is what decides the model. A.X returns
            # unparseable JSON on 13 of 30 drafts — every one of those silently becomes
            # the deterministic template below, so the reviewer sees boilerplate and the
            # LLM call was spent for nothing. Solar and EXAONE: 0 failures.
            chat = get_chat_model_for(TASK_EMAIL_DRAFT)
            raw = (chat.complete(system, user, json_only=True) or "").strip()
            parsed = json.loads(raw)
            subject = " ".join(str(parsed.get("subject") or "").split())
            body = str(parsed.get("body") or "").strip()
            span.set_output({"llm_drafted": bool(subject and body)})
        if subject and body:
            return subject, body, True
    except Exception:  # noqa: BLE001 — any LLM/parse failure falls back deterministically
        pass
    return (
        _email_subject_fallback(instruction),
        _email_body_fallback(recipient_hint, instruction, upstream),
        False,
    )


def _email_subject_fallback(instruction: str) -> str:
    compact = " ".join(instruction.split())
    return compact[:72] or "이메일 초안"


def _email_body_fallback(
    recipient_hint: str, instruction: str, upstream_context: str | None = None
) -> str:
    compact_instruction = " ".join(instruction.split())
    upstream = " ".join((upstream_context or "").split())
    context_line = f"참고 내용: {upstream}\n\n" if upstream else ""
    return (
        f"{recipient_hint}님,\n\n"
        "안녕하세요. 아래 내용으로 연락드립니다.\n\n"
        "[검토자가 본문을 확인하고 필요한 세부 내용을 채웁니다.]\n\n"
        f"{context_line}"
        f"원 요청: {compact_instruction}\n\n"
        "감사합니다."
    )


def ensure_document_draft_for_work_item(
    user_id: UUID,
    item: AgentWorkItem,
    *,
    settings: Settings | None = None,
) -> AgentWorkItem:
    if item.action_family != "document_draft":
        return item
    existing_draft = item.payload.get("draft_document")
    if isinstance(existing_draft, dict) and existing_draft.get("doc_id"):
        return item

    settings = settings or get_settings()
    title = _document_draft_title(item)
    markdown = _document_draft_markdown(item, title)
    scope = "personal" if settings.node_kind == "personal" else "company"
    project = str(item.payload.get("target_project") or "company")
    doc_id = create_agent_draft_document(
        user_id,
        title,
        _document_draft_blocks(item, title),
        markdown,
        scope=scope,
        project=project,
    )
    payload = dict(item.payload)
    payload["draft_document"] = {
        "doc_id": str(doc_id),
        "title": title,
        "source": "agent_draft",
        "status": "draft",
        "scope": scope,
        "project": project,
    }
    with session() as s:
        row = s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == item.work_id)
            .values(payload=payload, updated_at=datetime.now(UTC))
            .returning(agent_work_items)
        ).first()
        s.commit()
    return _row_to_item(row)


def _document_draft_title(item: AgentWorkItem) -> str:
    question = str(item.payload.get("question") or item.title).strip()
    if question.startswith("Assistant command:"):
        question = question.removeprefix("Assistant command:").strip()
    return f"Draft: {question[:80] or 'Untitled'}"


def _document_draft_markdown(item: AgentWorkItem, title: str) -> str:
    question = str(item.payload.get("question") or item.title).strip()
    return (
        f"# {title}\n\n"
        "Status: agent draft for reviewer editing.\n\n"
        "Original request:\n\n"
        f"> {question}\n"
    )


def _document_draft_blocks(item: AgentWorkItem, title: str) -> list[dict]:
    question = str(item.payload.get("question") or item.title).strip()
    return [
        _paragraph_block(title),
        _paragraph_block("Status: agent draft for reviewer editing."),
        _paragraph_block("Original request:"),
        _paragraph_block(question),
    ]


def _paragraph_block(text: str) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "styles": {}}],
    }


def data_gap_candidate(gap: DataGap) -> AgentWorkCandidate:
    fields = [section.model_dump() for section in gap.suggested_fields]
    payload: dict[str, Any] = {
        "gap_id": str(gap.gap_id),
        "question": gap.question,
        "reason": gap.reason,
        "top_score": gap.top_score,
        "hit_count": gap.hit_count,
        "status": gap.status,
        "source": gap.source,
        "suggestion_status": gap.suggestion_status,
        "suggested_target": gap.suggested_target,
        "suggested_connector": gap.suggested_connector,
        "suggested_fields": fields,
        "created_at": gap.created_at.isoformat(),
        "updated_at": gap.updated_at.isoformat(),
        "last_seen_at": gap.last_seen_at.isoformat(),
    }
    evidence = [
        {
            "kind": "data_gap",
            "ref": str(gap.gap_id),
            "question": gap.question,
            "reason": gap.reason,
            "hit_count": gap.hit_count,
            "source": gap.source,
        }
    ]
    return AgentWorkCandidate(
        source_kind="data_gap",
        source_ref_id=str(gap.gap_id),
        action_family="data_request",
        title=f"Data gap: {gap.question}",
        payload=payload,
        evidence=evidence,
    )


def wiki_task_candidate(task: WikiTask, *, scope: str, node_id: str) -> AgentWorkCandidate:
    cleanup_only = task.kind in _WIKI_TASK_CLEANUP_KINDS
    company_wiki_write = scope == "company" and task.kind in {"open_question", "conflict"}
    payload: dict[str, Any] = {
        "task_slug": task.slug,
        "kind": task.kind,
        "description": task.description,
        "related": task.related,
        "resolved": task.resolved,
        "scope": scope,
        "node_id": node_id,
        "cleanup_only": cleanup_only,
        "company_wiki_write": company_wiki_write,
        "created_at": task.created_at.isoformat(),
    }
    evidence = [
        {
            "kind": "wiki_task",
            "ref": task.slug,
            "task_kind": task.kind,
            "related": task.related,
        }
    ]
    return AgentWorkCandidate(
        source_kind="wiki_task",
        source_ref_id=task.slug,
        action_family="central_wiki_task_cleanup",
        title=f"Wiki task: {task.kind} - {task.slug}",
        payload=payload,
        evidence=evidence,
    )


def promote_staging_candidate(row: Any) -> AgentWorkCandidate:
    meta = dict(row["source_meta"] or {})
    payload: dict[str, Any] = {
        "stage_id": str(row["stage_id"]),
        "source_node_id": row["source_node_id"],
        "source_doc_id": str(row["source_doc_id"]),
        "source_owner_id": str(row["source_owner_id"]) if row["source_owner_id"] else None,
        "source_scope": row["source_scope"],
        "source_title": row["source_title"],
        "sanitized_title": row["sanitized_title"],
        "sanitized_markdown_chars": int(row["sanitized_markdown_chars"] or 0),
        "target_project": meta.get("target_project") or meta.get("project") or "company",
        "source_meta": meta,
        "status": row["status"],
        "created_by": str(row["created_by"]),
        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        "promoted_doc_id": str(row["promoted_doc_id"]) if row["promoted_doc_id"] else None,
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "decided_at": _iso(row["decided_at"]),
    }
    evidence = [
        {
            "kind": "promote_staging",
            "ref": str(row["stage_id"]),
            "source_node_id": row["source_node_id"],
            "source_doc_id": str(row["source_doc_id"]),
            "status": row["status"],
        }
    ]
    return AgentWorkCandidate(
        source_kind="promote_staging",
        source_ref_id=str(row["stage_id"]),
        action_family="promote_review",
        title=f"Promote review: {row['sanitized_title']}",
        payload=payload,
        evidence=evidence,
    )


def connector_run_candidate(row: Any) -> AgentWorkCandidate:
    error_message = _redacted_note(row["error_message"])
    retry_guard = {
        "auto_retry_allowed": False,
        "reason": "failed_connector_run_triage_only",
        "requires_operator_review": True,
        "source_run_status": row["status"],
        "used_for_outcome": False,
    }
    payload: dict[str, Any] = {
        "run_id": str(row["run_id"]),
        "account_id": str(row["account_id"]),
        "connector_slug": row["connector_slug"],
        "account_kind": row["account_kind"],
        "node_id": row["node_id"],
        "scope": row["scope"],
        "owner_id": str(row["owner_id"]) if row["owner_id"] else None,
        "account_label": row["account_label"],
        "reason": row["reason"],
        "run_status": row["status"],
        "fetched": row["fetched"],
        "created": row["created"],
        "updated": row["updated"],
        "skipped": row["skipped"],
        "errors": row["errors"],
        "error_message": error_message,
        "retry_guard": retry_guard,
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
    }
    evidence = [
        {
            "kind": "connector_run",
            "ref": str(row["run_id"]),
            "connector_slug": row["connector_slug"],
            "status": row["status"],
            "errors": row["errors"],
            "retry_guard": retry_guard,
        }
    ]
    return AgentWorkCandidate(
        source_kind="connector_run",
        source_ref_id=str(row["run_id"]),
        action_family="connector_sync",
        title=f"Connector run failed: {row['connector_slug']}",
        payload=payload,
        evidence=evidence,
    )


def persist_agent_work_item(
    candidate: AgentWorkCandidate,
    decision: AgentWorkDecision,
    *,
    user_id: UUID,
    correlation_id: UUID,
    settings: Settings | None = None,
    owner_scoped: bool = False,
) -> AgentWorkItem:
    settings = settings or get_settings()
    # Company (central) nodes store company-shared items with owner_id=NULL. When
    # `owner_scoped` is set (e.g. an individual-mailbox reply draft) the item is
    # bound to `user_id` so the fail-closed owner predicate hides it from other
    # users (docs/p6-mail-individual-scope.md §3.4).
    owner_id = user_id if (settings.node_kind == "personal" or owner_scoped) else None
    now = datetime.now(UTC)
    existing = _find_by_source(
        settings.node_id,
        str(candidate.source_kind),
        candidate.source_ref_id,
    )
    state: AgentWorkState = decision.state
    outcome = decision.outcome
    policy_reason = decision.policy_reason
    reason_codes = decision.reason_codes
    payload = dict(candidate.payload)
    if existing is not None and isinstance(existing.payload.get("draft_document"), dict):
        payload.setdefault("draft_document", existing.payload["draft_document"])
    if existing is not None and isinstance(existing.payload.get("email_draft"), dict):
        payload.setdefault("email_draft", existing.payload["email_draft"])
    if existing is not None and isinstance(existing.payload.get("auto_execution"), dict):
        payload.setdefault("auto_execution", existing.payload["auto_execution"])
    if existing is not None and isinstance(existing.payload.get("source_writeback"), dict):
        payload.setdefault("source_writeback", existing.payload["source_writeback"])
    if decision.required_data:
        payload["required_data"] = decision.required_data
    elif existing is not None and isinstance(existing.payload.get("required_data"), list):
        payload.setdefault("required_data", existing.payload["required_data"])
    if existing is not None and existing.state in TERMINAL_STATES:
        state = existing.state
        outcome = existing.policy_outcome
        policy_reason = existing.policy_reason or decision.policy_reason
        reason_codes = existing.reason_codes or decision.reason_codes

    with audit("agent_work.persist", correlation_id=correlation_id) as span:
        values = {
            "work_id": existing.work_id if existing is not None else uuid4(),
            "node_id": settings.node_id,
            "node_kind": settings.node_kind,
            "owner_id": owner_id,
            "source_kind": str(candidate.source_kind),
            "source_ref_id": candidate.source_ref_id,
            "action_family": str(candidate.action_family),
            "title": candidate.title,
            "payload": payload,
            "state": state,
            "policy_outcome": outcome,
            "policy_reason": policy_reason,
            "reason_codes": reason_codes,
            "evidence": candidate.evidence,
            "correlation_id": correlation_id,
            "last_run_id": span.node_run_id,
            "created_by": user_id,
            "created_at": existing.created_at if existing is not None else now,
            "updated_at": now,
        }
        stmt = (
            pg_insert(agent_work_items)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    agent_work_items.c.node_id,
                    agent_work_items.c.source_kind,
                    agent_work_items.c.source_ref_id,
                ],
                set_={
                    "node_kind": values["node_kind"],
                    "owner_id": values["owner_id"],
                    "action_family": values["action_family"],
                    "title": values["title"],
                    "payload": values["payload"],
                    "state": values["state"],
                    "policy_outcome": values["policy_outcome"],
                    "policy_reason": values["policy_reason"],
                    "reason_codes": values["reason_codes"],
                    "evidence": values["evidence"],
                    "correlation_id": values["correlation_id"],
                    "last_run_id": values["last_run_id"],
                    "updated_at": values["updated_at"],
                },
            )
            .returning(agent_work_items)
        )
        with session() as s:
            row = s.execute(stmt).first()
            s.commit()
        item = _row_to_item(row)
        span.add_meta(
            source_kind=item.source_kind,
            source_ref_id=item.source_ref_id,
            work_id=str(item.work_id),
            outcome=item.policy_outcome,
            state=item.state,
        )
        span.set_output(
            {
                "work_id": str(item.work_id),
                "state": item.state,
                "policy_outcome": item.policy_outcome,
            }
        )
        return item


def extract_command_recipient(payload: dict | None) -> str | None:
    """S4 chip enrichment: pull the email recipient from an assistant_command / reply-draft
    payload, covering BOTH shapes (reviewer Z4): the assistant_command intake stores it at
    top-level `recipient_hint` (`_email_send_command_payload`), a reused P7.1 reply draft keeps
    it under `email_draft.recipient_hint` or `reply_context.to_addr`. Returns None when absent
    (non-email family, or recipient not yet extracted)."""
    if not isinstance(payload, dict):
        return None
    hint = payload.get("recipient_hint")
    if isinstance(hint, str) and hint.strip():
        return hint
    draft = payload.get("email_draft")
    if isinstance(draft, dict) and isinstance(draft.get("recipient_hint"), str):
        if draft["recipient_hint"].strip():
            return draft["recipient_hint"]
    reply_ctx = payload.get("reply_context")
    if isinstance(reply_ctx, dict) and isinstance(reply_ctx.get("to_addr"), str):
        if reply_ctx["to_addr"].strip():
            return reply_ctx["to_addr"]
    return None


def list_agent_work_items(
    user_id: UUID,
    *,
    state: str | None = None,
    source_kind: str | None = None,
    outcome: str | None = None,
    scope: str | None = None,
    ids: list[UUID] | None = None,
    limit: int = 100,
    settings: Settings | None = None,
) -> list[AgentWorkItem]:
    settings = settings or get_settings()
    conditions = [agent_work_items.c.node_id == settings.node_id]
    conditions.append(_agent_work_owner_predicate(user_id, settings))
    # S4 batch fetch: restrict to the given work_ids, still AND-ed with the fail-closed owner
    # predicate — non-owned / non-existent ids are silently dropped (no enumeration oracle; the
    # client treats "not returned" as terminal-unknown and keeps polling to a bounded cap).
    if ids is not None:
        conditions.append(agent_work_items.c.work_id.in_(ids))
    # `scope` narrows within the fail-closed owner predicate above (which already
    # bars other users' personal rows): "personal" → only the caller's own rows,
    # "company" → only shared company rows. It can only tighten, never widen.
    if scope == "personal":
        conditions.append(agent_work_items.c.owner_id == user_id)
    elif scope == "company":
        conditions.append(agent_work_items.c.owner_id.is_(None))
    if state:
        conditions.append(agent_work_items.c.state == state)
    if source_kind:
        conditions.append(agent_work_items.c.source_kind == source_kind)
    if outcome:
        conditions.append(agent_work_items.c.policy_outcome == outcome)
    with session() as s:
        rows = s.execute(
            select(agent_work_items)
            .where(*conditions)
            .order_by(agent_work_items.c.updated_at.desc())
            .limit(limit)
        ).all()
    return [_row_to_item(row) for row in rows]


def get_agent_work_item(
    user_id: UUID, work_id: UUID, *, settings: Settings | None = None
) -> AgentWorkItem | None:
    settings = settings or get_settings()
    conditions = [
        agent_work_items.c.node_id == settings.node_id,
        agent_work_items.c.work_id == work_id,
        _agent_work_owner_predicate(user_id, settings),
    ]
    with session() as s:
        row = s.execute(select(agent_work_items).where(*conditions)).first()
    return _row_to_item(row) if row is not None else None


def find_reply_draft_for_mail(
    user_id: UUID, mail_canonical_id: str, *, settings: Settings | None = None
) -> AgentWorkItem | None:
    """MA.3b (D path): surface the P7.1 reply draft already created for an inbound mail.

    P7.1 creates the `mail_reply` draft at ingest, idempotent on
    `(node_id, source_kind, source_ref_id=mail canonical id)`, so the reply draft is a
    property of the mail (at most one). The decompose action-intake leaf looks it up by
    the `/ask` context_mail_id instead of rebuilding the original MailIngestRequest.

    Owner-scoped, fail-closed (`_agent_work_owner_predicate`): on a company node a
    company-shared draft (owner_id NULL) or the caller's own owner-bound draft is
    visible; never another user's owner-bound draft (individual mailbox boundary,
    docs/p6-mail-individual-scope.md). Returns None when no draft exists (P7.1 flag off
    / mail not auto-drafted), so the leaf falls back to queue_agent_work.
    """
    settings = settings or get_settings()
    conditions = [
        agent_work_items.c.node_id == settings.node_id,
        agent_work_items.c.source_kind == "mail_reply",
        agent_work_items.c.source_ref_id == mail_canonical_id,
        _agent_work_owner_predicate(user_id, settings),
    ]
    with session() as s:
        row = s.execute(select(agent_work_items).where(*conditions)).first()
    return _row_to_item(row) if row is not None else None


def build_inbox_summary(
    user_id: UUID,
    *,
    settings: Settings | None = None,
) -> AgentWorkInboxSummary:
    settings = settings or get_settings()
    return AgentWorkInboxSummary(
        generated_at=datetime.now(UTC),
        node_id=settings.node_id,
        node_kind=settings.node_kind,
        buckets=AgentWorkInboxBuckets(
            wiki_tasks=_inbox_wiki_tasks(user_id, settings=settings),
            promote_staging=_inbox_promote_staging(settings=settings),
            data_gaps=_inbox_data_gaps(user_id),
            agent_work=_inbox_agent_work(user_id, settings=settings),
        ),
    )


def agent_work_notify_state(
    user_id: UUID,
    *,
    settings: Settings | None = None,
) -> AgentWorkNotifyState:
    """Cheap checklist-badge signal for notifications (PR-N1).

    Server-side mirror of the FE checklist badge semantics
    (web/src/components/AppShell.tsx isChecklistBadgeItem): open agent-work
    items excluding `wiki_task` source rows and `agent_task` (chat delegation)
    family rows. Owner-scoped fail-closed via `_agent_work_owner_predicate`,
    same as every other agent-work list read. Read-only — mirrors the
    GET /mail/notify-state precedent (no audit span, plain SELECT).
    """
    settings = settings or get_settings()
    conditions = [
        agent_work_items.c.node_id == settings.node_id,
        _agent_work_owner_predicate(user_id, settings),
        agent_work_items.c.state.in_(_INBOX_OPEN_AGENT_WORK_STATES),
        agent_work_items.c.source_kind != "wiki_task",
        agent_work_items.c.action_family != "agent_task",
    ]
    with session() as s:
        agg = s.execute(
            select(
                func.count().label("total"),
                func.max(agent_work_items.c.created_at).label("latest_at"),
            ).where(*conditions)
        ).one()
        total = int(agg.total or 0)
        latest_title: str | None = None
        if total:
            latest_title = s.execute(
                select(agent_work_items.c.title)
                .where(*conditions)
                .order_by(agent_work_items.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
    return AgentWorkNotifyState(total=total, latest_at=agg.latest_at, latest_title=latest_title)


def _inbox_wiki_tasks(
    user_id: UUID,
    *,
    settings: Settings,
) -> AgentWorkInboxWikiTaskBucket:
    scope = "personal" if settings.node_kind == "personal" else "company"
    owner_id = user_id if settings.node_kind == "personal" else None
    tasks: list[WikiTask] = []
    for slug in wiki_store.list_slugs("task", scope=scope, owner_id=owner_id):
        task = wiki_store.load_task(slug, scope=scope, owner_id=owner_id)
        if task is None or task.resolved:
            continue
        tasks.append(task)
    ordered = sorted(tasks, key=lambda task: task.created_at, reverse=True)
    return AgentWorkInboxWikiTaskBucket(
        count=len(ordered),
        items=[
            AgentWorkInboxWikiTaskItem(
                slug=task.slug,
                kind=task.kind,
                title=task.description or task.slug,
                created_at=task.created_at,
                href="/wiki/tasks",
            )
            for task in ordered[:_INBOX_ITEM_LIMIT]
        ],
    )


def _inbox_promote_staging(*, settings: Settings) -> AgentWorkInboxPromotionBucket:
    if settings.node_kind != "company":
        return AgentWorkInboxPromotionBucket(count=0, supported=False, items=[])
    stages = list_promotion_stages(status="pending", settings=settings)
    return AgentWorkInboxPromotionBucket(
        count=len(stages),
        supported=True,
        items=[
            AgentWorkInboxPromotionItem(
                stage_id=stage.stage_id,
                source_title=stage.source_title,
                status=stage.status,
                created_at=stage.created_at,
                href="/promote",
            )
            for stage in stages[:_INBOX_ITEM_LIMIT]
        ],
    )


def _inbox_data_gaps(user_id: UUID) -> AgentWorkInboxDataGapBucket:
    gaps = list_gaps(user_id, status="open", limit=AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT)
    return AgentWorkInboxDataGapBucket(
        count=len(gaps),
        count_capped=len(gaps) >= AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT,
        items=[
            AgentWorkInboxDataGapItem(
                gap_id=gap.gap_id,
                missing_topic=gap.question,
                hit_count=gap.hit_count,
                last_seen_at=gap.last_seen_at,
                href="/gaps",
            )
            for gap in gaps[:_INBOX_ITEM_LIMIT]
        ],
    )


def _inbox_agent_work(
    user_id: UUID,
    *,
    settings: Settings,
) -> AgentWorkInboxAgentWorkBucket:
    # 6개 state × 500행 전건 로드 + 전건 enrich(위키 파일 read) 대신 최신 5건 SELECT 1회
    # + outcome별 집계 COUNT 1회로 답한다. 응답 JSON 모양(count/count_capped/by_outcome/
    # items)은 불변이고, count는 이제 정확한 총계라 count_capped는 항상 False다.
    # enrich(위키 slug projection)는 반환되는 ≤5건에만 수행한다.
    conditions = [
        agent_work_items.c.node_id == settings.node_id,
        _agent_work_owner_predicate(user_id, settings),
        agent_work_items.c.state.in_(_INBOX_OPEN_AGENT_WORK_STATES),
    ]
    with session() as s:
        rows = s.execute(
            select(agent_work_items)
            .where(*conditions)
            .order_by(agent_work_items.c.updated_at.desc())
            .limit(_INBOX_ITEM_LIMIT)
        ).all()
        outcome_rows = s.execute(
            select(agent_work_items.c.policy_outcome, func.count())
            .where(*conditions)
            .group_by(agent_work_items.c.policy_outcome)
        ).all()
    by_outcome = dict.fromkeys(_INBOX_OUTCOME_KEYS, 0)
    total = 0
    for outcome, outcome_count in outcome_rows:
        key = outcome or "pending"
        by_outcome[key] = by_outcome.get(key, 0) + int(outcome_count)
        total += int(outcome_count)
    enriched = enrich_agent_work_items(
        [_row_to_item(row) for row in rows], user_id=user_id, settings=settings
    )
    return AgentWorkInboxAgentWorkBucket(
        count=total,
        count_capped=False,
        by_outcome=by_outcome,
        items=[
            AgentWorkInboxAgentWorkItem(
                work_id=item.work_id,
                title=item.title,
                source_kind=item.source_kind,
                action_family=item.action_family,
                state=item.state,
                policy_outcome=item.policy_outcome,
                updated_at=item.updated_at,
                wiki_slugs=item.wiki_slugs,
                href=f"/agent-work?work_id={item.work_id}",
            )
            for item in enriched
        ],
    )


def list_policy_memory_summaries(
    user_id: UUID,
    *,
    limit: int = 50,
    settings: Settings | None = None,
) -> list[AgentPolicyBucketSummary]:
    settings = settings or get_settings()
    conditions = [agent_policy_observations.c.node_id == settings.node_id]
    conditions.append(_owner_row_predicate(agent_policy_observations.c.owner_id, user_id, settings))

    approvals = func.sum(
        case((agent_policy_observations.c.reviewer_decision == "approve", 1), else_=0)
    ).label("approvals")
    dismissals = func.sum(
        case((agent_policy_observations.c.reviewer_decision == "dismiss", 1), else_=0)
    ).label("dismissals")
    request_more_data = func.sum(
        case(
            (agent_policy_observations.c.reviewer_decision == "request_more_data", 1),
            else_=0,
        )
    ).label("request_more_data")
    note_present_count = func.sum(
        case((agent_policy_observations.c.note_present.is_(True), 1), else_=0)
    ).label("note_present_count")
    no_edit_approval = agent_policy_observations.c.meta["no_edit_approval"].as_boolean().is_(True)
    no_edit_approvals = func.sum(case((no_edit_approval, 1), else_=0)).label("no_edit_approvals")
    recent_cutoff = datetime.now(UTC) - timedelta(days=_POLICY_MEMORY_RECENT_WINDOW_DAYS)
    recent_observation = agent_policy_observations.c.observed_at >= recent_cutoff
    recent_total = func.sum(case((recent_observation, 1), else_=0)).label("recent_total")
    recent_no_edit_approvals = func.sum(
        case((recent_observation & no_edit_approval, 1), else_=0)
    ).label("recent_no_edit_approvals")
    last_observed_at = func.max(agent_policy_observations.c.observed_at).label("last_observed_at")
    with session() as s:
        rows = s.execute(
            select(
                agent_policy_observations.c.bucket_key,
                agent_policy_observations.c.node_id,
                agent_policy_observations.c.action_family,
                agent_policy_observations.c.source_kind,
                func.count().label("total"),
                approvals,
                dismissals,
                request_more_data,
                note_present_count,
                no_edit_approvals,
                recent_total,
                recent_no_edit_approvals,
                last_observed_at,
            )
            .where(*conditions)
            .group_by(
                agent_policy_observations.c.bucket_key,
                agent_policy_observations.c.node_id,
                agent_policy_observations.c.action_family,
                agent_policy_observations.c.source_kind,
            )
            .order_by(last_observed_at.desc())
            .limit(limit)
        ).all()
    return [_row_to_policy_bucket_summary(row) for row in rows]


def generate_policy_memory_wiki_summary(
    user_id: UUID,
    *,
    limit: int = 50,
    settings: Settings | None = None,
) -> AgentPolicyWikiSummary:
    """Write a deterministic policy-memory summary page into the node wiki.

    P3.5a makes reviewer memory visible in wiki form only. It deliberately does
    not feed the summary back into policy outcomes.
    """
    settings = settings or get_settings()
    rows = list_policy_memory_summaries(user_id, limit=limit, settings=settings)
    scope, owner_id = _wiki_scope(user_id, settings)
    page_slug = "agent-policy-memory"
    title = "Agent Policy Memory"
    now = datetime.now(UTC)
    observation_count = sum(row.total for row in rows)
    overview = _policy_memory_summary_overview(rows, settings=settings, updated_at=now)
    page = WikiPage(
        slug=page_slug,
        title=title,
        definition=(
            "Deterministic summary of node-local Agent Work reviewer decisions. "
            "This page is operator context; policy outcomes still come from code."
        ),
        overview=overview,
        relations=["agent-work", "policy-memory", "deterministic-policy-gate"],
        evidence=[row.bucket_key for row in rows],
        sources=["agent_policy_observations"],
        backlinks=[],
    )

    with audit("agent_work.policy_memory_wiki") as span:
        wiki_store.write_page(
            page,
            user_id=user_id,
            scope=scope,
            owner_id=owner_id,
            project="company",
        )
        span.add_meta(
            page_slug=page_slug,
            scope=scope,
            owner_id=str(owner_id) if owner_id is not None else None,
            bucket_count=len(rows),
            observation_count=observation_count,
            used_for_outcome=False,
        )
        span.set_output(
            {
                "page_slug": page_slug,
                "bucket_count": len(rows),
                "observation_count": observation_count,
                "used_for_outcome": False,
            }
        )

    return AgentPolicyWikiSummary(
        page_slug=page_slug,
        title=title,
        scope=scope,
        owner_id=owner_id,
        project="company",
        bucket_count=len(rows),
        observation_count=observation_count,
        updated_at=now,
    )


def classify_candidate_with_policy_memory(
    candidate: AgentWorkCandidate,
    user_id: UUID,
    *,
    settings: Settings | None = None,
) -> tuple[AgentWorkCandidate, AgentWorkDecision, UUID, UUID]:
    """Classify candidate and attach read-only policy-memory context.

    Policy memory is deliberately attached after base classification. It is
    reviewer context only in P3.2c and must not alter the deterministic outcome.
    """
    settings = settings or get_settings()
    decision, correlation_id, classify_run_id = classify_candidate(candidate)
    context = get_policy_memory_context_for_decision(
        user_id,
        candidate,
        decision,
        settings=settings,
    )
    payload = dict(candidate.payload or {})
    payload["policy_memory"] = context.model_dump(mode="json")
    if candidate.action_family == "email_send" and decision.state == "draft_for_review":
        payload["email_auto_send_gate"] = _email_auto_send_gate_evidence(
            candidate,
            context,
            settings=settings,
        )
    return (
        candidate.model_copy(update={"payload": payload, "policy_memory": context}),
        decision.model_copy(update={"policy_memory": context}),
        correlation_id,
        classify_run_id,
    )


def _email_auto_send_gate_evidence(
    candidate: AgentWorkCandidate,
    context: AgentPolicyMemoryContext,
    *,
    settings: Settings,
) -> dict[str, Any]:
    payload = candidate.payload or {}
    checks = {
        "source_kind_assistant_command": candidate.source_kind == "assistant_command",
        "base_policy_bucket": context.bucket_key == _EMAIL_AUTO_SEND_POLICY_BUCKET,
        "personal_node": settings.node_kind == "personal"
        and payload.get("node_kind") == "personal",
        "operator_role": payload.get("actor_role") in _EMAIL_AUTO_SEND_OPERATOR_ROLES,
        "observation_threshold": context.email_auto_send_observation_threshold_met is True,
        "recipient_present": bool(payload.get("recipient_hash") or payload.get("recipient_id")),
        "recipient_allowed": payload.get("recipient_allowed") is True,
        "domain_allowed": payload.get("domain_allowed") is True,
        "template_stable": payload.get("template_stable") is True,
        "rate_limit_ok": payload.get("rate_limit_ok") is True,
        "sensitive_content_absent": payload.get("sensitive_content") is not True,
        "attachment_absent": payload.get("attachment") is not True,
    }
    # S5 #3 (future-proof): a command-split fan-out fragment (intake_source
    # "decompose_fanout") must never become auto-send-eligible if P7.5 is ever
    # activated — a self-composed fragment of a compound is not a reviewer-vetted
    # single command. Defensive only: the gate is already structurally eligible=False
    # today (observation_threshold + kill switch), and no auto-send path exists. Added
    # conditionally so a non-command-split (flag-off) gate payload stays byte-identical.
    if payload.get("intake_source") == "decompose_fanout":
        checks["not_command_split"] = False
    missing = [name for name, ok in checks.items() if not ok]
    return {
        "eligible": not missing,
        "used_for_outcome": False,
        "reason": "preflight_gate",
        "bucket_key": context.bucket_key,
        "recent_window_days": context.recent_window_days,
        "recent_total": context.recent_total,
        "recent_no_edit_approvals": context.recent_no_edit_approvals,
        "recent_no_edit_approval_rate": context.recent_no_edit_approval_rate,
        "threshold_met": context.email_auto_send_observation_threshold_met,
        "checks": checks,
        "missing_checks": missing,
    }


def get_policy_memory_context_for_decision(
    user_id: UUID,
    candidate: AgentWorkCandidate,
    decision: AgentWorkDecision,
    *,
    settings: Settings | None = None,
) -> AgentPolicyMemoryContext:
    settings = settings or get_settings()
    bucket_key = _policy_bucket_key_for_values(
        action_family=str(candidate.action_family),
        source_kind=str(candidate.source_kind),
        policy_outcome=decision.outcome,
        reason_codes=decision.reason_codes,
    )
    conditions = [
        agent_policy_observations.c.node_id == settings.node_id,
        agent_policy_observations.c.bucket_key == bucket_key,
        _owner_row_predicate(agent_policy_observations.c.owner_id, user_id, settings),
    ]

    approvals = func.sum(
        case((agent_policy_observations.c.reviewer_decision == "approve", 1), else_=0)
    ).label("approvals")
    dismissals = func.sum(
        case((agent_policy_observations.c.reviewer_decision == "dismiss", 1), else_=0)
    ).label("dismissals")
    request_more_data = func.sum(
        case(
            (agent_policy_observations.c.reviewer_decision == "request_more_data", 1),
            else_=0,
        )
    ).label("request_more_data")
    note_present_count = func.sum(
        case((agent_policy_observations.c.note_present.is_(True), 1), else_=0)
    ).label("note_present_count")
    no_edit_approval = agent_policy_observations.c.meta["no_edit_approval"].as_boolean().is_(True)
    no_edit_approvals = func.sum(case((no_edit_approval, 1), else_=0)).label("no_edit_approvals")
    recent_cutoff = datetime.now(UTC) - timedelta(days=_POLICY_MEMORY_RECENT_WINDOW_DAYS)
    recent_observation = agent_policy_observations.c.observed_at >= recent_cutoff
    recent_total = func.sum(case((recent_observation, 1), else_=0)).label("recent_total")
    recent_no_edit_approvals = func.sum(
        case((recent_observation & no_edit_approval, 1), else_=0)
    ).label("recent_no_edit_approvals")
    with session() as s:
        row = s.execute(
            select(
                func.count().label("total"),
                approvals,
                dismissals,
                request_more_data,
                note_present_count,
                no_edit_approvals,
                recent_total,
                recent_no_edit_approvals,
                func.max(agent_policy_observations.c.observed_at).label("last_observed_at"),
            ).where(*conditions)
        ).first()
    data = row._mapping if row is not None else {}
    recent_total_value = int(data.get("recent_total") or 0)
    recent_no_edit_value = int(data.get("recent_no_edit_approvals") or 0)
    recent_no_edit_rate = _rate_float(recent_no_edit_value, recent_total_value)
    return AgentPolicyMemoryContext(
        bucket_key=bucket_key,
        total=int(data.get("total") or 0),
        approvals=int(data.get("approvals") or 0),
        dismissals=int(data.get("dismissals") or 0),
        request_more_data=int(data.get("request_more_data") or 0),
        note_present_count=int(data.get("note_present_count") or 0),
        no_edit_approvals=int(data.get("no_edit_approvals") or 0),
        recent_window_days=_POLICY_MEMORY_RECENT_WINDOW_DAYS,
        recent_total=recent_total_value,
        recent_no_edit_approvals=recent_no_edit_value,
        recent_no_edit_approval_rate=recent_no_edit_rate,
        email_auto_send_observation_threshold_met=_email_auto_send_threshold_met(
            recent_total_value, recent_no_edit_rate
        ),
        last_observed_at=data.get("last_observed_at"),
        used_for_outcome=False,
    )


def decide_agent_work_item(
    user_id: UUID,
    work_id: UUID,
    decision: AgentWorkReviewAction,
    *,
    note: str | None = None,
    no_edit: bool | None = None,
    settings: Settings | None = None,
) -> AgentWorkReviewDecisionResult:
    settings = settings or get_settings()
    item = get_agent_work_item(user_id, work_id, settings=settings)
    if item is None:
        raise KeyError("agent work item not found")

    with audit("agent_work.decision", correlation_id=item.correlation_id) as span:
        with session() as s:
            row = s.execute(
                select(agent_work_items)
                .where(*_work_item_conditions(user_id, work_id, settings))
                .with_for_update()
            ).first()
            if row is None:
                raise KeyError("agent work item not found")
            current_item = _row_to_item(row)
            existing_decision = s.execute(
                select(agent_work_decisions.c.decision_id).where(
                    agent_work_decisions.c.work_id == current_item.work_id
                )
            ).first()
            if existing_decision is not None:
                raise ValueError("agent work item already decided")

            target_state = review_transition(current_item.state, decision)
            redacted_note = _redacted_note(note)
            decision_values = {
                "decision_id": uuid4(),
                "work_id": current_item.work_id,
                "node_id": current_item.node_id,
                "reviewer_id": user_id,
                "decision": decision,
                "note": redacted_note,
                "from_state": current_item.state,
                "to_state": target_state,
                "correlation_id": span.correlation_id,
                "node_run_id": span.node_run_id,
                "decided_at": datetime.now(UTC),
            }
            try:
                decision_row = s.execute(
                    insert(agent_work_decisions)
                    .values(**decision_values)
                    .returning(agent_work_decisions)
                ).first()
            except IntegrityError as exc:
                s.rollback()
                raise ValueError("agent work item already decided") from exc

            observation_no_edit = _policy_observation_no_edit_value(
                current_item,
                reviewer_decision=decision,
                no_edit=no_edit,
            )
            s.execute(
                insert(agent_policy_observations).values(
                    **_policy_observation_values(
                        current_item,
                        decision_id=decision_values["decision_id"],
                        reviewer_id=user_id,
                        reviewer_decision=decision,
                        from_state=current_item.state,
                        to_state=target_state,
                        note_present=redacted_note is not None,
                        no_edit=observation_no_edit,
                        observed_at=decision_values["decided_at"],
                    )
                )
            )

            source_writeback = _review_decision_source_writeback(
                s,
                user_id=user_id,
                item=current_item,
                decision=decision,
                settings=settings,
                decided_at=decision_values["decided_at"],
            )
            next_payload = dict(current_item.payload or {})
            if source_writeback is not None:
                next_payload["source_writeback"] = source_writeback

            item_row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == current_item.work_id)
                .values(
                    payload=next_payload,
                    state=target_state,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=datetime.now(UTC),
                )
                .returning(agent_work_items)
            ).first()
            s.commit()

        updated_item = _row_to_item(item_row)
        review_decision = _row_to_review_decision(decision_row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            reviewer_id=str(user_id),
            decision=review_decision.decision,
            from_state=review_decision.from_state,
            to_state=review_decision.to_state,
            source_writeback=updated_item.payload.get("source_writeback"),
        )
        span.set_output(
            {
                "work_id": str(updated_item.work_id),
                "decision_id": str(review_decision.decision_id),
                "decision": review_decision.decision,
                "state": updated_item.state,
                "source_writeback": updated_item.payload.get("source_writeback"),
            }
        )
        return AgentWorkReviewDecisionResult(item=updated_item, decision=review_decision)


def _find_by_source(node_id: str, source_kind: str, source_ref_id: str) -> AgentWorkItem | None:
    with session() as s:
        row = s.execute(
            select(agent_work_items).where(
                agent_work_items.c.node_id == node_id,
                agent_work_items.c.source_kind == source_kind,
                agent_work_items.c.source_ref_id == source_ref_id,
            )
        ).first()
    return _row_to_item(row) if row is not None else None


def _owner_row_predicate(owner_col, user_id: UUID, settings: Settings):
    """Fail-closed owner visibility for a row's owner_id column (P8.1).

    Personal node: only the owner's own rows. Company (central) node: shared
    company rows (owner_id NULL) plus the caller's own personal rows; never
    another user's personal rows. This is the permanent privacy boundary and is
    NOT gated on owner_scope_enabled (docs/p8-central-consolidation.md §3/§5-A).
    """
    if settings.node_kind == "personal":
        return owner_col == user_id
    return or_(owner_col.is_(None), owner_col == user_id)


def _agent_work_owner_predicate(user_id: UUID, settings: Settings):
    return _owner_row_predicate(agent_work_items.c.owner_id, user_id, settings)


def _wiki_scope(user_id: UUID, settings: Settings) -> tuple[str, UUID | None]:
    if settings.node_kind == "personal":
        return "personal", user_id
    return "company", None


def _work_item_conditions(user_id: UUID, work_id: UUID, settings: Settings) -> list:
    return [
        agent_work_items.c.node_id == settings.node_id,
        agent_work_items.c.work_id == work_id,
        _agent_work_owner_predicate(user_id, settings),
    ]


def _redacted_note(note: str | None) -> str | None:
    if note is None:
        return None
    stripped = note.strip()
    if not stripped:
        return None
    return redact_pii_text(stripped)


def _review_decision_source_writeback(
    s,
    *,
    user_id: UUID,
    item: AgentWorkItem,
    decision: AgentWorkReviewAction,
    settings: Settings,
    decided_at: datetime,
) -> dict[str, Any] | None:
    if item.source_kind != "data_gap":
        return None
    status_by_decision = {
        "approve": "resolved",
        "dismiss": "dismissed",
    }
    target_status = status_by_decision.get(decision)
    if target_status is None:
        return None

    payload: dict[str, Any] = {
        "kind": "data_gap",
        "source_ref_id": item.source_ref_id,
        "decision": decision,
        "status": target_status,
        "updated": False,
        "decided_at": decided_at.isoformat(),
    }
    try:
        gap_id = UUID(item.source_ref_id)
    except ValueError:
        payload["reason"] = "invalid_source_ref_id"
        return payload

    scope, owner_id = _wiki_scope(user_id, settings)
    conditions = [
        data_gaps.c.gap_id == gap_id,
        data_gaps.c.scope == scope,
        data_gaps.c.owner_id.is_(None) if owner_id is None else data_gaps.c.owner_id == owner_id,
    ]
    updated = s.execute(
        update(data_gaps)
        .where(*conditions)
        .values(status=target_status, updated_at=decided_at)
        .returning(data_gaps.c.gap_id, data_gaps.c.status)
    ).first()
    if updated is None:
        payload["reason"] = "source_not_found"
        return payload

    payload["updated"] = True
    return payload


def _policy_observation_values(
    item: AgentWorkItem,
    *,
    decision_id: UUID,
    reviewer_id: UUID,
    reviewer_decision: AgentWorkReviewAction,
    from_state: AgentWorkState,
    to_state: AgentWorkState,
    note_present: bool,
    no_edit: bool | None,
    observed_at: datetime,
) -> dict[str, Any]:
    no_edit_approval = reviewer_decision == "approve" and no_edit is True
    return {
        "observation_id": uuid4(),
        "node_id": item.node_id,
        "node_kind": item.node_kind,
        "owner_id": item.owner_id,
        "work_id": item.work_id,
        "decision_id": decision_id,
        "reviewer_id": reviewer_id,
        "source_kind": item.source_kind,
        "source_ref_id": item.source_ref_id,
        "action_family": item.action_family,
        "policy_outcome": item.policy_outcome,
        "reason_codes": item.reason_codes,
        "reviewer_decision": reviewer_decision,
        "from_state": from_state,
        "to_state": to_state,
        "note_present": note_present,
        "bucket_key": _policy_bucket_key(item),
        "meta": {
            "policy_reason": item.policy_reason,
            "memory_version": "p3.2b",
            "no_edit": no_edit,
            "no_edit_approval": no_edit_approval,
        },
        "observed_at": observed_at,
    }


def _policy_observation_no_edit_value(
    item: AgentWorkItem,
    *,
    reviewer_decision: AgentWorkReviewAction,
    no_edit: bool | None,
) -> bool | None:
    if no_edit is None:
        return None
    if reviewer_decision != "approve":
        return None
    if item.state != "draft_for_review":
        return None
    if _policy_bucket_key(item) != _EMAIL_AUTO_SEND_POLICY_BUCKET:
        return None
    return no_edit


def _policy_bucket_key(item: AgentWorkItem) -> str:
    return _policy_bucket_key_for_values(
        action_family=item.action_family,
        source_kind=item.source_kind,
        policy_outcome=item.policy_outcome,
        reason_codes=item.reason_codes,
    )


def _policy_bucket_key_for_values(
    *,
    action_family: str,
    source_kind: str,
    policy_outcome: str | None,
    reason_codes: list[str],
) -> str:
    normalized_reason_codes = ",".join(sorted(str(code) for code in reason_codes))
    return "|".join(
        [
            action_family,
            source_kind,
            policy_outcome or "none",
            normalized_reason_codes or "none",
        ]
    )


def _policy_memory_summary_overview(
    rows: list[AgentPolicyBucketSummary],
    *,
    settings: Settings,
    updated_at: datetime,
) -> str:
    lines = [
        f"Generated at: {updated_at.isoformat()}",
        f"Node: {settings.node_kind} · {settings.node_id}",
        "Used for outcome: false",
        "",
        "Policy gate status:",
        "- Deterministic code remains the only source of action outcome.",
        "- This summary is reviewer context and wiki-visible memory only.",
        "- Email auto-send remains disabled until a later gate uses explicit no-edit telemetry.",
        "",
        "Buckets:",
    ]
    if not rows:
        lines.append("- No policy observations yet.")
        return "\n".join(lines)

    for row in rows:
        approval_rate = _rate(row.approvals, row.total)
        dismiss_rate = _rate(row.dismissals, row.total)
        request_rate = _rate(row.request_more_data, row.total)
        note_rate = _rate(row.note_present_count, row.total)
        no_edit_rate = _rate(row.no_edit_approvals, row.total)
        lines.extend(
            [
                f"- {row.bucket_key}",
                f"  - action/source: {row.action_family} · {row.source_kind}",
                f"  - total: {row.total}",
                f"  - approve: {row.approvals} ({approval_rate})",
                f"  - dismiss: {row.dismissals} ({dismiss_rate})",
                f"  - request_more_data: {row.request_more_data} ({request_rate})",
                f"  - note_present: {row.note_present_count} ({note_rate})",
                f"  - no_edit_approval: {row.no_edit_approvals} ({no_edit_rate})",
                (
                    "  - recent_no_edit_approval: "
                    f"{row.recent_no_edit_approvals}/{row.recent_total} "
                    f"({row.recent_no_edit_approval_rate:.3f}, "
                    f"window_days={row.recent_window_days}, "
                    f"threshold_met={str(row.email_auto_send_observation_threshold_met).lower()})"
                ),
                f"  - last_observed_at: {row.last_observed_at.isoformat()}",
            ]
        )
    return "\n".join(lines)


def _rate(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(part / total) * 100:.1f}%"


def _rate_float(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return part / total


def _email_auto_send_threshold_met(recent_total: int, recent_no_edit_rate: float) -> bool:
    return (
        recent_total >= _EMAIL_AUTO_SEND_MIN_RECENT_OBSERVATIONS
        and recent_no_edit_rate > _EMAIL_AUTO_SEND_MIN_NO_EDIT_APPROVAL_RATE
    )


def _connector_sync_command_payload(
    question: str,
    *,
    user_id: UUID,
    actor_role: str | None,
    settings: Settings,
) -> dict[str, Any]:
    _ensure_connector_registry_for_agent_work()
    slug = _connector_slug_from_question(question)
    payload: dict[str, Any] = {
        "secret_state": "missing",
        "node_policy_allows": False,
    }
    if actor_role is not None:
        payload["actor_role"] = actor_role
    if slug is None:
        return payload

    payload["connector_slug"] = slug
    account = _find_active_connector_account(slug, user_id=user_id, settings=settings)
    if account is None:
        return payload

    account_kind = str(account["account_kind"])
    owner_id = account["owner_id"]
    payload.update(
        {
            "account_id": str(account["account_id"]),
            "account_kind": account_kind,
            "scope": account["scope"],
            "owner_id": str(owner_id) if owner_id is not None else None,
            "account_label": account["account_label"],
            "auth_mode": account["auth_mode"],
            "secret_state": "configured",
            "node_policy_allows": _connector_supported(slug, account_kind),
        }
    )
    return payload


def _personal_board_cleanup_command_payload(
    question: str,
    *,
    actor_role: str | None,
    settings: Settings,
) -> dict[str, Any]:
    compact = question.lower().replace(" ", "")
    destructive = any(term in compact for term in _DESTRUCTIVE_TERMS)
    external_write = any(term in compact for term in _EXTERNAL_BOARD_WRITE_TERMS)
    payload: dict[str, Any] = {
        "cleanup_kind": "archive_done_tasks",
        "target": "personal_board",
        "node_kind": settings.node_kind,
        "node_id": settings.node_id,
        "reversibility": "reversible",
        "external_write": external_write,
        "delete": destructive,
        "limit": 50,
    }
    if actor_role is not None:
        payload["actor_role"] = actor_role
    if settings.node_kind != "personal":
        payload["reversibility"] = "requires_personal_node"
    if destructive:
        payload["reversibility"] = "destructive_request"
    if external_write:
        payload["reversibility"] = "external_write_request"
    return payload


def _done_personal_board_tasks(
    user_id: UUID,
    *,
    node_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        rows = s.execute(
            select(
                personal_board_tasks.c.task_id,
                personal_board_tasks.c.title,
                personal_board_tasks.c.scheduled_date,
                personal_board_tasks.c.backlog_bucket_id,
                personal_board_tasks.c.updated_at,
            )
            .where(
                personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                personal_board_tasks.c.user_id == user_id,
                personal_board_tasks.c.status == "done",
            )
            .order_by(personal_board_tasks.c.updated_at.asc(), personal_board_tasks.c.task_id.asc())
            .limit(limit)
        ).all()
    return [dict(row._mapping) for row in rows]


def _personal_board_cleanup_limit(payload: dict[str, Any]) -> int:
    raw = payload.get("limit")
    try:
        value = int(raw) if raw is not None else 50
    except (TypeError, ValueError):
        return 50
    return min(max(value, 1), 100)


def _date_iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _connector_slug_from_question(question: str) -> str | None:
    compact = question.lower().replace(" ", "").replace("-", "_")
    for alias, slug in _CONNECTOR_COMMAND_ALIASES:
        if alias in compact:
            return slug
    for slug in sorted(registered_connector_slugs(), key=len, reverse=True):
        normalized = slug.replace("-", "_")
        if normalized in compact or normalized.replace("_", "") in compact:
            return slug
    return None


def _find_active_connector_account(
    connector_slug: str,
    *,
    user_id: UUID,
    settings: Settings,
) -> dict[str, Any] | None:
    conditions = [
        connector_accounts.c.connector_slug == connector_slug,
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.status == "active",
        connector_accounts.c.account_kind == settings.node_kind,
    ]
    if settings.node_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        row = s.execute(
            select(connector_accounts)
            .where(*conditions)
            .order_by(connector_accounts.c.updated_at.desc())
        ).first()
    return dict(row._mapping) if row is not None else None


def _find_connector_account_for_execution(
    account_id: UUID,
    *,
    connector_slug: str,
    user_id: UUID,
    settings: Settings,
) -> dict[str, Any] | None:
    conditions = [
        connector_accounts.c.account_id == account_id,
        connector_accounts.c.connector_slug == connector_slug,
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.status == "active",
        connector_accounts.c.account_kind == settings.node_kind,
    ]
    if settings.node_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        row = s.execute(select(connector_accounts).where(*conditions)).first()
    return dict(row._mapping) if row is not None else None


def _mark_auto_connector_sync_failed(
    item: AgentWorkItem,
    *,
    error_message: str,
    settings: Settings,
) -> AgentWorkItem:
    now = datetime.now(UTC)
    next_payload = dict(item.payload)
    next_payload["auto_execution"] = {
        "kind": "connector_sync",
        "connector_slug": item.payload.get("connector_slug"),
        "account_id": item.payload.get("account_id"),
        "status": "failed",
        "error_message": _redacted_note(error_message),
        "executed_at": now.isoformat(),
    }
    next_reason_codes = list(item.reason_codes)
    if "auto_execute_failed" not in next_reason_codes:
        next_reason_codes.append("auto_execute_failed")
    with audit("agent_work.auto_execute", correlation_id=item.correlation_id) as span:
        with session() as s:
            row = s.execute(
                update(agent_work_items)
                .where(agent_work_items.c.work_id == item.work_id)
                .values(
                    payload=next_payload,
                    state="request_more_data",
                    reason_codes=next_reason_codes,
                    correlation_id=span.correlation_id,
                    last_run_id=span.node_run_id,
                    updated_at=now,
                )
                .returning(agent_work_items)
            ).first()
            s.commit()
        updated_item = _row_to_item(row)
        span.add_meta(
            work_id=str(updated_item.work_id),
            connector_slug=item.payload.get("connector_slug"),
            account_id=item.payload.get("account_id"),
            connector_status="failed",
            node_id=settings.node_id,
        )
        span.set_output({"work_id": str(updated_item.work_id), "state": updated_item.state})
        return updated_item


def _connector_sync_concurrency(payload: dict[str, Any]) -> int:
    raw = payload.get("concurrency")
    try:
        value = int(raw) if raw is not None else 8
    except (TypeError, ValueError):
        return 8
    return min(max(value, 1), 32)


def _ensure_connector_registry_for_agent_work() -> None:
    if not registered_connector_slugs():
        register_default_connector_providers(replace=True)


def _connector_supported(slug: str, account_kind: str) -> bool:
    provider = get_connector_provider(slug)
    return provider is not None and provider.manifest.supports_account_kind(account_kind)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_review_decision(row) -> AgentWorkReviewDecision:
    if row is None:
        raise ValueError("agent work decision row is required")
    data = row._mapping
    return AgentWorkReviewDecision(
        decision_id=data["decision_id"],
        work_id=data["work_id"],
        node_id=data["node_id"],
        reviewer_id=data["reviewer_id"],
        decision=data["decision"],
        note=data["note"],
        from_state=data["from_state"],
        to_state=data["to_state"],
        correlation_id=data["correlation_id"],
        node_run_id=data["node_run_id"],
        decided_at=data["decided_at"],
    )


def _row_to_policy_bucket_summary(row) -> AgentPolicyBucketSummary:
    data = row._mapping
    recent_total = int(data["recent_total"] or 0)
    recent_no_edit_approvals = int(data["recent_no_edit_approvals"] or 0)
    recent_no_edit_rate = _rate_float(recent_no_edit_approvals, recent_total)
    return AgentPolicyBucketSummary(
        bucket_key=data["bucket_key"],
        node_id=data["node_id"],
        action_family=data["action_family"],
        source_kind=data["source_kind"],
        total=int(data["total"]),
        approvals=int(data["approvals"] or 0),
        dismissals=int(data["dismissals"] or 0),
        request_more_data=int(data["request_more_data"] or 0),
        note_present_count=int(data["note_present_count"] or 0),
        no_edit_approvals=int(data["no_edit_approvals"] or 0),
        recent_window_days=_POLICY_MEMORY_RECENT_WINDOW_DAYS,
        recent_total=recent_total,
        recent_no_edit_approvals=recent_no_edit_approvals,
        recent_no_edit_approval_rate=recent_no_edit_rate,
        email_auto_send_observation_threshold_met=_email_auto_send_threshold_met(
            recent_total, recent_no_edit_rate
        ),
        last_observed_at=data["last_observed_at"],
    )


def _row_to_item(row) -> AgentWorkItem:
    if row is None:
        raise ValueError("agent work row is required")
    data = row._mapping
    return AgentWorkItem(
        work_id=data["work_id"],
        node_id=data["node_id"],
        node_kind=data["node_kind"],
        owner_id=data["owner_id"],
        source_kind=data["source_kind"],
        source_ref_id=data["source_ref_id"],
        action_family=data["action_family"],
        title=data["title"],
        payload=dict(data["payload"] or {}),
        state=data["state"],
        policy_outcome=data["policy_outcome"],
        policy_reason=data["policy_reason"],
        reason_codes=list(data["reason_codes"] or []),
        evidence=list(data["evidence"] or []),
        correlation_id=data["correlation_id"],
        last_run_id=data["last_run_id"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
