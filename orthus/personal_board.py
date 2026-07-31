"""Personal workspace board: daily tasks, fixed events, notes, and wiki sync."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from logging import getLogger
from zoneinfo import ZoneInfo
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit.logger import audit
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.schemas.canonical import (
    InternalDocument,
    PersonalBoardNotificationItem,
    PersonalBoardNotificationList,
    PersonalBoardNotifyState,
)
from orthus.settings import get_settings
from orthus.tables import (
    dashboard_projects,
    personal_board_backlog_buckets,
    personal_board_fixed_events,
    personal_board_folders,
    personal_board_integrations,
    personal_board_notes,
    personal_board_preferences,
    personal_board_projects,
    personal_board_subtasks,
    personal_board_task_comments,
    personal_board_tasks,
    personal_board_workspaces,
    personal_objective_comments,
    personal_objective_subtasks,
    personal_weekly_objectives,
    project_assignments,
    team_calendar_events,
    team_members,
    users,
    weekly_entries,
)

BOARD_PROJECT = "company"
logger = getLogger(__name__)

DEFAULT_BACKLOG_BUCKETS = [
    ("next_week", "Someday in the next week", "W", "#65a46c"),
    ("next_month", "Someday in the next month", "M", "#9fc96d"),
    ("next_quarter", "Someday in the next quarter", "Q", "#f3c150"),
    ("next_year", "Someday in the next year", "Y", "#f4ae55"),
    ("someday", "Someday", "S", "#89a6b3"),
    ("never", "Never", "N", "#8b8e90"),
]
DEFAULT_INTEGRATIONS = [
    ("calendar", "Calendar", True, False, 0),
    ("github", "GitHub", True, False, 1),
    ("gmail", "Gmail", False, False, 2),
    ("notion", "Notion", False, True, 3),
    ("target", "Goals", False, False, 4),
    ("backlog", "Backlog", True, False, 5),
    ("reflection", "Review", False, False, 6),
    ("search", "Search", True, False, 7),
    ("automation", "Automation", False, True, 8),
]
SYSTEM_FOLDER_NAMES = {"backlog"}


class PersonalBoardProject(BaseModel):
    project_id: UUID
    name: str
    color: str | None = None
    # 'personal'(사적 채널) | 'company'(담당 회사 프로젝트와 연결된 채널). 회사 채널은
    # 보드에서 이름/색을 못 바꾸고 삭제도 못 한다(담당이 빠지면 자동으로 정리). 그 안의
    # 업무는 회사 구성원 전체가 데이터로 본다.
    kind: str = "personal"
    company_project_id: UUID | None = None


class PersonalBoardFolder(BaseModel):
    folder_id: UUID
    name: str
    kind: str
    order_index: int


class PersonalBoardIntegration(BaseModel):
    integration_id: UUID
    kind: str
    label: str
    enabled: bool
    has_notification: bool
    order_index: int


class PersonalBoardSubtask(BaseModel):
    subtask_id: UUID
    task_id: UUID
    title: str
    completed: bool
    order_index: int


class PersonalBoardTask(BaseModel):
    task_id: UUID
    title: str
    status: str
    completed: bool
    priority: str
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    # Optional end of the scheduled time block. Set only when scheduled_time (start)
    # is set, and strictly after it (same day). None = no end / point-in-time start.
    scheduled_end_time: time | None = None
    # "복귀불가" — the person is not coming back. Independent of scheduled_end_time
    # (may be set with or without an end time). Surfaced as a team-schedule badge.
    no_return: bool = False
    # Optional deadline. due_time set only when due_date is set; None = whole-day due.
    due_date: date | None = None
    due_time: time | None = None
    backlog_bucket_id: UUID | None = None
    order_index: int
    source_kind: str
    source_label: str | None = None
    # Linked team calendar event (board task <-> team schedule). None = not on team schedule.
    team_event_id: UUID | None = None
    # 'personal'(소유자 전용) | 'company'(회사 프로젝트 채널 업무 → 회사 구성원 전체 가시).
    # 채널(project)의 kind에서 파생된다.
    scope: str = "personal"
    company_project_id: UUID | None = None
    # 티켓 상세의 자유 노트. FE 임시 상태였던 것을 서버 영속화(0093) — None = 없음.
    note: str | None = None
    project: PersonalBoardProject | None = None
    subtasks: list[PersonalBoardSubtask] = Field(default_factory=list)


class PersonalBoardFixedEvent(BaseModel):
    event_id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    source_kind: str
    source_label: str | None = None
    project: PersonalBoardProject | None = None


class PersonalBoardNote(BaseModel):
    note_id: UUID
    note_date: date
    kind: str
    title: str
    body: str
    order_index: int
    created_at: datetime


class PersonalBoardBacklogBucket(BaseModel):
    bucket_id: UUID
    key: str
    label: str
    badge: str
    color: str
    order_index: int
    collapsed: bool
    tasks: list[PersonalBoardTask] = Field(default_factory=list)


class PersonalBoardDay(BaseModel):
    date: date
    tasks: list[PersonalBoardTask]
    fixed_events: list[PersonalBoardFixedEvent]
    notes: list[PersonalBoardNote]


class PersonalBoardPreferences(BaseModel):
    selected_date: date
    right_panel: str
    active_integration: str | None = None
    filter_mode: str
    sort_mode: str


class PersonalBoardBootstrap(BaseModel):
    node_id: str
    workspace_id: UUID
    workspace_name: str
    timezone: str
    selected_date: date
    folders: list[PersonalBoardFolder]
    days: list[PersonalBoardDay]
    backlog_buckets: list[PersonalBoardBacklogBucket]
    integrations: list[PersonalBoardIntegration]
    projects: list[PersonalBoardProject]
    preferences: PersonalBoardPreferences


class TaskCreate(BaseModel):
    title: str
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    scheduled_end_time: time | None = None
    # "복귀불가" — independent of scheduled_end_time. Defaults to false on create.
    no_return: bool = False
    due_date: date | None = None
    due_time: time | None = None
    backlog_bucket_id: UUID | None = None
    priority: str = "normal"
    project_id: UUID | None = None
    # Optional client-generated id for offline-first creation. When provided and
    # a task with this id already exists for the owner, the create is idempotent
    # (returns the existing task) so a queued offline mutation can be replayed
    # safely on reconnect. (docs/offline-personal-board.md 2단계)
    task_id: UUID | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    project_id: UUID | None = None
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    scheduled_end_time: time | None = None
    # "복귀불가" — independent flag. None = leave unchanged on a partial patch.
    no_return: bool | None = None
    due_date: date | None = None
    due_time: time | None = None
    backlog_bucket_id: UUID | None = None
    order_index: int | None = None
    # 티켓 노트. 명시적 ""/None 전송 = 노트 비우기, 미전송 = 유지(exclude_unset).
    note: str | None = None


class ProjectCreate(BaseModel):
    name: str
    color: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    color: str | None = None


class FolderCreate(BaseModel):
    name: str


class BacklogBucketPatch(BaseModel):
    collapsed: bool | None = None


class PreferencesPatch(BaseModel):
    selected_date: date | None = None
    right_panel: str | None = None
    active_integration: str | None = None
    filter_mode: str | None = None
    sort_mode: str | None = None


class SubtaskCreate(BaseModel):
    title: str
    # Optional client-generated id for offline-first creation (idempotent replay).
    subtask_id: UUID | None = None


class SubtaskPatch(BaseModel):
    title: str | None = None
    completed: bool | None = None


class FixedEventCreate(BaseModel):
    title: str
    starts_at: datetime
    ends_at: datetime
    project_id: UUID | None = None
    source_kind: str = "manual"
    source_label: str | None = None


class FixedEventPatch(BaseModel):
    title: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    project_id: UUID | None = None


class NoteCreate(BaseModel):
    note_date: date
    title: str
    body: str
    kind: str = "note"


class TaskComment(BaseModel):
    """티켓 상세 팝업 댓글(0093) — FE 메모리 상태였던 것을 서버 영속화."""

    comment_id: UUID
    task_id: UUID
    user_id: UUID
    author_name: str | None = None
    body: str
    created_at: datetime


class TaskCommentCreate(BaseModel):
    body: str


class DayAllocation(BaseModel):
    weekday: int = Field(ge=0, le=6)
    minutes: int | None = None
    # Per-day position (fractional) so an objective card can interleave between
    # that day's tasks on the Home board. Stored in day_allocations JSONB (no
    # migration); tasks keep their integer order_index untouched.
    order: float | None = None
    # Per-day note (source of truth for the day-card detail). Lives in the
    # day_allocations JSONB alongside minutes/order — no migration needed.
    note: str | None = None


class ObjectiveSubtask(BaseModel):
    subtask_id: UUID
    title: str
    completed: bool
    order_index: int
    weekday: int


class ObjectiveComment(BaseModel):
    comment_id: UUID
    user_id: UUID
    author_name: str | None = None
    body: str
    weekday: int
    created_at: datetime


class PersonalWeeklyObjective(BaseModel):
    objective_id: UUID
    week_start: date
    title: str
    project_id: UUID | None = None
    project: PersonalBoardProject | None = None
    day_allocations: list[DayAllocation] = Field(default_factory=list)
    completed: bool
    order_index: int
    note: str | None = None
    # 'manual' | 'company_plan'. company_plan = 회사 주간계획 담당 항목에서 자동 생성된
    # 목표('회사 부여'). 개인은 제목/프로젝트를 못 바꾸고 요일 분배·완료만 한다.
    source_kind: str = "manual"
    created_at: datetime
    subtasks: list[ObjectiveSubtask] = Field(default_factory=list)
    comments: list[ObjectiveComment] = Field(default_factory=list)


class ObjectiveCreate(BaseModel):
    week_start: date
    title: str
    project_id: UUID | None = None


class ObjectivePatch(BaseModel):
    title: str | None = None
    project_id: UUID | None = None
    day_allocations: list[DayAllocation] | None = None
    completed: bool | None = None
    order_index: int | None = None
    note: str | None = None


class ObjectiveSubtaskCreate(BaseModel):
    title: str
    weekday: int = Field(ge=0, le=6)


class ObjectiveSubtaskPatch(BaseModel):
    title: str | None = None
    completed: bool | None = None
    order_index: int | None = None


class ObjectiveCommentCreate(BaseModel):
    body: str
    weekday: int = Field(ge=0, le=6)


class ObjectiveMoveDay(BaseModel):
    from_weekday: int = Field(ge=0, le=6)
    to_weekday: int = Field(ge=0, le=6)


class AssignedCompanyItem(BaseModel):
    """회사 계획·회고에서 이 사용자에게 부여된 항목(읽기전용). 개인 보드 주간
    플랜에 '회사에서 부여받은 일'로 표시된다."""

    item_id: str
    text: str
    done: bool = False
    project: str | None = None
    project_id: str | None = None
    week_start: date


class WeeklyPlanResponse(BaseModel):
    week_start: date
    objectives: list[PersonalWeeklyObjective]
    # 회사 계획·회고에서 본인에게 부여된 항목들(team_members.user_id 매핑 기준).
    assigned_company_items: list[AssignedCompanyItem] = Field(default_factory=list)


class WeeklyReviewDay(BaseModel):
    date: date
    weekday: int
    done_count: int
    open_count: int
    done_tasks: list[PersonalBoardTask]


class WeeklyReviewProject(BaseModel):
    project_id: UUID | None = None
    project_name: str | None = None
    color: str | None = None
    done_count: int


class WeeklyReviewResponse(BaseModel):
    week_start: date
    total_done: int
    total_open: int
    daily: list[WeeklyReviewDay]
    by_project: list[WeeklyReviewProject]


class DaysResponse(BaseModel):
    days: list[PersonalBoardDay]


def selected_date_or_today(selected: date | None, timezone: str) -> date:
    if selected is not None:
        return selected
    return datetime.now(ZoneInfo(timezone)).date()


def event_local_days(starts_at: datetime, ends_at: datetime, timezone: ZoneInfo) -> list[date]:
    start_day = starts_at.astimezone(timezone).date()
    end_day = (ends_at.astimezone(timezone) - timedelta(microseconds=1)).date()
    days: list[date] = []
    current = start_day
    while current <= end_day:
        days.append(current)
        current += timedelta(days=1)
    return days


def bootstrap(
    user_id: UUID,
    *,
    node_id: str,
    selected_date: date | None = None,
    email: str | None = None,
) -> PersonalBoardBootstrap:
    with audit("personal_board.bootstrap") as span:
        workspace = ensure_workspace(user_id, node_id)
        day0 = selected_date_or_today(selected_date, workspace["timezone"])
        ensure_default_folders(workspace["workspace_id"])
        ensure_default_buckets(workspace["workspace_id"])
        ensure_default_integrations(workspace["workspace_id"])
        ensure_preferences(workspace["workspace_id"], user_id, day0)
        # 담당 회사 프로젝트를 회사 채널로 동기화한다(회사 노드 + owner-scope에서만 동작).
        ensure_company_channels(workspace["workspace_id"], user_id, node_id, email=email)
        # 전날까지 못 끝낸 일은 오늘로 자동 이월한다. 과거 날짜를 일부러 들여다보는
        # 경우(day0 < 오늘)에는 건드리지 않는다. 보드를 여는 시점(bootstrap)에만 동작.
        real_today = datetime.now(ZoneInfo(workspace["timezone"])).date()
        if day0 >= real_today:
            rollover_overdue_tasks(workspace["workspace_id"], user_id, real_today)
        sync_team_events_to_board(
            user_id, node_id, [day0 + timedelta(days=i) for i in range(3)], email=email
        )
        payload = _load_bootstrap(user_id, workspace, day0)
        span.set_output(
            {
                "workspace_id": str(payload.workspace_id),
                "selected_date": payload.selected_date.isoformat(),
                "tasks": sum(len(day.tasks) for day in payload.days),
                "events": sum(len(day.fixed_events) for day in payload.days),
                "notes": sum(len(day.notes) for day in payload.days),
            }
        )
        return payload


def ensure_workspace(user_id: UUID, node_id: str) -> dict:
    workspace_id = uuid.uuid4()
    with session() as s:
        stmt = (
            pg_insert(personal_board_workspaces)
            .values(
                workspace_id=workspace_id,
                user_id=user_id,
                node_id=node_id,
                name="개인 워크스페이스",
                timezone="Asia/Seoul",
            )
            .on_conflict_do_update(
                index_elements=["user_id", "node_id"],
                set_={"updated_at": func.now()},
            )
            .returning(
                personal_board_workspaces.c.workspace_id,
                personal_board_workspaces.c.name,
                personal_board_workspaces.c.timezone,
            )
        )
        row = s.execute(stmt).one()
        s.commit()
    return {
        "workspace_id": row.workspace_id,
        "name": row.name,
        "timezone": row.timezone,
        "node_id": node_id,
    }


def ensure_default_folders(workspace_id: UUID) -> None:
    with session() as s:
        stmt = (
            pg_insert(personal_board_folders)
            .values(
                folder_id=uuid.uuid4(),
                workspace_id=workspace_id,
                name="Backlog",
                kind="system",
                order_index=0,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "name"],
                set_={"kind": "system", "order_index": 0, "updated_at": func.now()},
            )
        )
        s.execute(stmt)
        s.commit()


def ensure_default_buckets(workspace_id: UUID) -> None:
    with session() as s:
        for index, (key, label, badge, color) in enumerate(DEFAULT_BACKLOG_BUCKETS):
            stmt = (
                pg_insert(personal_board_backlog_buckets)
                .values(
                    bucket_id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    key=key,
                    label=label,
                    badge=badge,
                    color=color,
                    order_index=index,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "key"],
                    set_={
                        "label": label,
                        "badge": badge,
                        "color": color,
                        "order_index": index,
                        "updated_at": func.now(),
                    },
                )
            )
            s.execute(stmt)
        s.commit()


DELEGATION_BACKLOG_BUCKET_KEY = "next_week"


def create_delegation_task(
    assignee_user_id: UUID,
    node_id: str,
    *,
    title: str,
    note: str | None = None,
    source_label: str | None = None,
) -> UUID:
    """위임 dispatch 도착을 assignee 개인 보드 backlog에 티켓으로 남긴다.

    이미 결정론 policy gate를 통과해 dispatch된 위임의 파생 evidence다
    (calendar materialize와 동형의 system-created row) — LLM 호출 없음, 정책
    판단 없음. 배치는 backlog `next_week` bucket 고정(스케줄 날짜 없음),
    source_kind='delegation'으로 수신 위임임을 표시한다. 호출자가 best-effort로
    감싼다(실패해도 dispatch는 영향 없음). 멱등성은 호출자의 work-item payload
    marker에만 의존한다(DB unique constraint 없음 — marker 저장 전 crash 시
    중복 row 1건 가능, best-effort 설계상 허용).

    회사 노드에서 owner-scope flag가 꺼져 있으면 만들지 않는다 — 그 상태에선
    개인 보드 read 표면이 404라 유령 row만 쌓인다(fail-closed, operator 리뷰
    지적 반영).
    """
    settings = get_settings()
    if settings.node_kind == "company" and not settings.owner_scope_enabled:
        raise RuntimeError("owner scope disabled on company node — delegation board task skipped")
    workspace = ensure_workspace(assignee_user_id, node_id)
    workspace_id = workspace["workspace_id"]
    ensure_default_buckets(workspace_id)
    with session() as s:
        bucket_id = s.execute(
            select(personal_board_backlog_buckets.c.bucket_id).where(
                personal_board_backlog_buckets.c.workspace_id == workspace_id,
                personal_board_backlog_buckets.c.key == DELEGATION_BACKLOG_BUCKET_KEY,
            )
        ).scalar_one()
    task_id = uuid.uuid4()
    order_index = _next_task_order(workspace_id, None, bucket_id)
    with session() as s:
        s.execute(
            insert(personal_board_tasks).values(
                task_id=task_id,
                workspace_id=workspace_id,
                user_id=assignee_user_id,
                backlog_bucket_id=bucket_id,
                title=title,
                order_index=order_index,
                source_kind="delegation",
                source_label=source_label,
                note=note,
            )
        )
        s.commit()
    return task_id


def ensure_default_integrations(workspace_id: UUID) -> None:
    with session() as s:
        for kind, label, enabled, has_notification, order_index in DEFAULT_INTEGRATIONS:
            stmt = (
                pg_insert(personal_board_integrations)
                .values(
                    integration_id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    kind=kind,
                    label=label,
                    enabled=enabled,
                    has_notification=has_notification,
                    order_index=order_index,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "kind"],
                    set_={
                        "label": label,
                        "enabled": enabled,
                        "has_notification": has_notification,
                        "order_index": order_index,
                        "updated_at": func.now(),
                    },
                )
            )
            s.execute(stmt)
        s.commit()


def ensure_preferences(workspace_id: UUID, user_id: UUID, selected_date: date) -> None:
    with session() as s:
        stmt = (
            pg_insert(personal_board_preferences)
            .values(
                workspace_id=workspace_id,
                user_id=user_id,
                selected_date=selected_date,
                right_panel="backlog",
                active_integration=None,
                filter_mode="all",
                sort_mode="time",
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "user_id"],
                set_={"selected_date": selected_date, "updated_at": func.now()},
            )
        )
        s.execute(stmt)
        s.commit()


# ---------------------------------------------------------------------------
# 회사 프로젝트 채널 (개인/회사 업무 분리)
#
# 내 보드의 채널은 두 종류다:
#   - 개인 채널(kind='personal'): 내가 직접 만든 사적 채널. 그 안의 업무는
#     scope='personal'이라 소유자(나)만 본다.
#   - 회사 프로젝트 채널(kind='company'): 내가 담당(project_assignments)인 회사
#     프로젝트와 1:1로 연결된 채널. 그 안의 업무는 scope='company'라 회사 구성원
#     전체가 데이터로 본다(`dashboard.list_project_board_tasks`).
#
# 회사 채널은 사용자가 직접 만들지 않는다. 회사 노드 + owner-scope에서 bootstrap이
# 담당 프로젝트를 읽어 자동 생성/연결하고, 담당이 빠지면 다시 개인 채널로 되돌린다.
# personal_board_projects는 (workspace_id, name) 유니크라, 같은 이름의 기존 개인
# 채널이 있으면 그게 회사 채널로 전환되고 그 안의 기존 업무도 회사 공개로 승격된다
# (사용자 요청: "회사 프로젝트랑 이름 같은 개인 채널은 회사 업무로 동작하게").
# team_calendar_events 미러링(link_task_to_team_event)과 같은, promote 게이트를
# 거치지 않는 운영 데이터의 회사-가시화 경로다.
# ---------------------------------------------------------------------------
def ensure_company_channels(
    workspace_id: UUID, user_id: UUID, node_id: str, email: str | None = None
) -> None:
    """담당 회사 프로젝트를 내 보드의 회사 채널로 동기화한다(멱등).

    회사 노드 + owner-scope가 아니거나 팀멤버로 연결된 계정이 아니면 no-op라,
    개인 노드/일반 계정의 보드 동작은 전혀 바뀌지 않는다.
    """
    settings = get_settings()
    if settings.node_kind != "company" or not settings.owner_scope_enabled:
        return
    from orthus.dashboard import resolve_member_id

    member_id = resolve_member_id(node_id, user_id, email)
    if member_id is None:
        return

    with audit("personal_board.company_channels.sync") as span:
        with session() as s:
            assigned = s.execute(
                select(
                    dashboard_projects.c.project_id,
                    dashboard_projects.c.name,
                    dashboard_projects.c.color,
                )
                .select_from(
                    project_assignments.join(
                        dashboard_projects,
                        dashboard_projects.c.project_id == project_assignments.c.project_id,
                    )
                )
                .where(
                    project_assignments.c.node_id == node_id,
                    project_assignments.c.member_id == member_id,
                )
            ).all()
            assigned_ids = {r.project_id for r in assigned}

            migrated_tasks = 0
            now = datetime.now(UTC)
            # 1) 담당 프로젝트 → 회사 채널 upsert(이름 매칭 연결) + 기존 업무 회사 공개 승격.
            for r in assigned:
                set_cols = {
                    "kind": "company",
                    "company_project_id": r.project_id,
                    "archived_at": None,
                    "updated_at": func.now(),
                }
                if r.color is not None:
                    set_cols["color"] = r.color
                # 동명 매칭은 '활성' 채널만 대상으로 한다(index_where). 소프트삭제된 동명
                # 채널은 되살리지 않고, 새 회사 채널을 별도 row로 만든다(삭제 채널의 잔존
                # 개인 업무가 회사로 새지 않도록).
                channel_id = s.execute(
                    pg_insert(personal_board_projects)
                    .values(
                        project_id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        name=r.name,
                        color=r.color,
                        kind="company",
                        company_project_id=r.project_id,
                    )
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "name"],
                        index_where=personal_board_projects.c.archived_at.is_(None),
                        set_=set_cols,
                    )
                    .returning(personal_board_projects.c.project_id)
                ).scalar_one()
                result = s.execute(
                    update(personal_board_tasks)
                    .where(
                        personal_board_tasks.c.workspace_id == workspace_id,
                        personal_board_tasks.c.project_id == channel_id,
                        personal_board_tasks.c.scope != "company",
                        personal_board_tasks.c.status != "archived",
                    )
                    .values(scope="company", company_project_id=r.project_id, updated_at=now)
                )
                migrated_tasks += result.rowcount or 0

            # 2) 더 이상 담당이 아닌 회사 채널 → 개인 채널 복귀(그 업무도 scope='personal').
            #    되돌릴 수 있는 경계: 담당이 빠지면 그 일은 다시 내 사적 영역으로 돌아온다.
            reverted = 0
            stale = s.execute(
                select(
                    personal_board_projects.c.project_id,
                    personal_board_projects.c.company_project_id,
                ).where(
                    personal_board_projects.c.workspace_id == workspace_id,
                    personal_board_projects.c.kind == "company",
                )
            ).all()
            for row in stale:
                if row.company_project_id in assigned_ids:
                    continue
                s.execute(
                    update(personal_board_tasks)
                    .where(
                        personal_board_tasks.c.workspace_id == workspace_id,
                        personal_board_tasks.c.project_id == row.project_id,
                        personal_board_tasks.c.scope == "company",
                    )
                    .values(scope="personal", company_project_id=None, updated_at=now)
                )
                s.execute(
                    update(personal_board_projects)
                    .where(personal_board_projects.c.project_id == row.project_id)
                    .values(kind="personal", company_project_id=None, updated_at=now)
                )
                reverted += 1
            s.commit()
        span.set_output(
            {
                "assigned": len(assigned),
                "migrated_tasks": migrated_tasks,
                "reverted": reverted,
            }
        )


def _channel_meta(s, workspace_id: UUID, project_id: UUID | None) -> tuple[str, UUID | None]:
    """채널(project_id)에 놓이는 업무의 (scope, company_project_id)를 파생한다.

    회사 프로젝트 채널(kind='company')에 놓이면 회사 공개(scope='company'), 그 외(채널
    없음 또는 개인 채널)는 소유자 전용(scope='personal')이다. 업무의 가시성은 항상
    채널의 kind에서 파생되므로 회사/개인 전환이 한 곳에서 결정된다."""
    if project_id is None:
        return "personal", None
    row = s.execute(
        select(
            personal_board_projects.c.kind,
            personal_board_projects.c.company_project_id,
        ).where(
            personal_board_projects.c.project_id == project_id,
            personal_board_projects.c.workspace_id == workspace_id,
        )
    ).first()
    if row is not None and row.kind == "company" and row.company_project_id is not None:
        return "company", row.company_project_id
    return "personal", None


def _project_from_row(row) -> PersonalBoardProject:
    """Build a channel model from a row that selected kind/company_project_id."""
    return PersonalBoardProject(
        project_id=row.project_id,
        name=row.name,
        color=row.color,
        kind=row.kind,
        company_project_id=row.company_project_id,
    )


_PROJECT_COLS = (
    personal_board_projects.c.project_id,
    personal_board_projects.c.name,
    personal_board_projects.c.color,
    personal_board_projects.c.kind,
    personal_board_projects.c.company_project_id,
)


def create_task(user_id: UUID, node_id: str, body: TaskCreate) -> PersonalBoardTask:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    if body.priority not in {"urgent", "priority", "normal", "low"}:
        raise ValueError("invalid priority")
    if (body.scheduled_date is None) == (body.backlog_bucket_id is None):
        raise ValueError("task must have exactly one placement")
    if body.due_time is not None and body.due_date is None:
        raise ValueError("due_time requires due_date")
    # Optional time block (start -> end). A backlog task (no date) carries no time
    # block; an end is valid only with a start and must be strictly after it.
    scheduled_time = body.scheduled_time
    scheduled_end_time = body.scheduled_end_time
    if body.scheduled_date is None:
        scheduled_time = None
        scheduled_end_time = None
    if scheduled_end_time is not None and scheduled_time is None:
        raise ValueError("scheduled_end_time requires scheduled_time")
    if (
        scheduled_end_time is not None
        and scheduled_time is not None
        and scheduled_end_time <= scheduled_time
    ):
        raise ValueError("scheduled_end_time must be after scheduled_time")
    workspace = ensure_workspace(user_id, node_id)
    # Idempotent replay: if the client supplied an id that already exists for this
    # owner, return it unchanged instead of inserting a duplicate.
    if body.task_id is not None:
        with session() as s:
            existing = s.execute(
                select(personal_board_tasks.c.task_id).where(
                    personal_board_tasks.c.task_id == body.task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
        if existing is not None:
            return _load_task(body.task_id)
    _validate_project(workspace["workspace_id"], body.project_id)
    _validate_bucket(workspace["workspace_id"], body.backlog_bucket_id)
    order_index = _next_task_order(
        workspace["workspace_id"], body.scheduled_date, body.backlog_bucket_id
    )
    task_id = body.task_id or uuid.uuid4()
    with audit("personal_board.task.create") as span:
        with session() as s:
            # 회사 프로젝트 채널에 만든 업무는 회사 공개(scope='company')로 생성된다.
            task_scope, task_company_project_id = _channel_meta(
                s, workspace["workspace_id"], body.project_id
            )
            s.execute(
                insert(personal_board_tasks).values(
                    task_id=task_id,
                    workspace_id=workspace["workspace_id"],
                    user_id=user_id,
                    project_id=body.project_id,
                    backlog_bucket_id=body.backlog_bucket_id,
                    title=title,
                    priority=body.priority,
                    scheduled_date=body.scheduled_date,
                    scheduled_time=scheduled_time,
                    scheduled_end_time=scheduled_end_time,
                    no_return=body.no_return,
                    due_date=body.due_date,
                    due_time=body.due_time,
                    order_index=order_index,
                    scope=task_scope,
                    company_project_id=task_company_project_id,
                )
            )
            s.commit()
        if body.scheduled_date is not None:
            sync_day_to_wiki(user_id, node_id, body.scheduled_date)
        task = _load_task(task_id)
        span.set_output({"task_id": str(task_id), "scheduled_date": str(body.scheduled_date)})
        return task


def update_task(user_id: UUID, node_id: str, task_id: UUID, body: TaskPatch) -> PersonalBoardTask:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        existing = s.execute(
            select(
                personal_board_tasks.c.scheduled_date,
                personal_board_tasks.c.scheduled_time,
                personal_board_tasks.c.scheduled_end_time,
                personal_board_tasks.c.due_date,
                personal_board_tasks.c.due_time,
                personal_board_tasks.c.backlog_bucket_id,
                personal_board_tasks.c.order_index,
                personal_board_tasks.c.status,
            ).where(
                personal_board_tasks.c.task_id == task_id,
                personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                personal_board_tasks.c.user_id == user_id,
            )
        ).first()
    if existing is None:
        raise LookupError("task not found")

    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise ValueError("title required")
    if "status" in values and values["status"] not in {"open", "done", "archived"}:
        raise ValueError("invalid status")
    if "priority" in values and values["priority"] not in {"urgent", "priority", "normal", "low"}:
        raise ValueError("invalid priority")
    if "note" in values and values["note"] is not None:
        # 공백/개행은 노트 내용의 일부라 strip하지 않는다. 빈 문자열은 삭제(NULL)로,
        # 폭주 입력만 길이 캡으로 막는다.
        if len(values["note"]) > 20_000:
            raise ValueError("note too long")
        if not values["note"].strip():
            values["note"] = None
    if "project_id" in values:
        _validate_project(workspace["workspace_id"], values["project_id"])
        # 채널을 바꾸면 가시성도 그 채널에서 다시 파생한다. 회사 채널로 옮기면 회사
        # 공개(scope='company')가 되고, 개인 채널/미지정으로 옮기면 다시 소유자 전용이다.
        with session() as s:
            next_scope, next_company_project_id = _channel_meta(
                s, workspace["workspace_id"], values["project_id"]
            )
        values["scope"] = next_scope
        values["company_project_id"] = next_company_project_id
    if "backlog_bucket_id" in values:
        _validate_bucket(workspace["workspace_id"], values["backlog_bucket_id"])
    order_index_requested = "order_index" in values and values["order_index"] is not None
    requested_order_index = values.pop("order_index", None)
    if requested_order_index is not None and requested_order_index < 0:
        raise ValueError("invalid order_index")
    next_scheduled_date = values.get("scheduled_date", existing.scheduled_date)
    next_backlog_bucket_id = values.get("backlog_bucket_id", existing.backlog_bucket_id)
    next_status = values.get("status", existing.status)
    placement_changed = (
        next_scheduled_date != existing.scheduled_date
        or next_backlog_bucket_id != existing.backlog_bucket_id
    )
    if "scheduled_date" in values or "backlog_bucket_id" in values:
        if (next_scheduled_date is None) == (next_backlog_bucket_id is None):
            raise ValueError("task must have exactly one placement")
        if next_backlog_bucket_id is not None and "scheduled_time" not in values:
            # Moving to backlog drops the whole time block (start + end).
            values["scheduled_time"] = None
            values["scheduled_end_time"] = None
    # Optional time block (start -> end). Clearing the start clears the end; an end
    # is valid only with a start and must be strictly after it (same day).
    if "scheduled_time" in values and values["scheduled_time"] is None:
        values["scheduled_end_time"] = None
    next_scheduled_time = values.get("scheduled_time", existing.scheduled_time)
    next_scheduled_end = values.get("scheduled_end_time", existing.scheduled_end_time)
    if next_scheduled_end is not None and next_scheduled_time is None:
        raise ValueError("scheduled_end_time requires scheduled_time")
    if (
        next_scheduled_end is not None
        and next_scheduled_time is not None
        and next_scheduled_end <= next_scheduled_time
    ):
        raise ValueError("scheduled_end_time must be after scheduled_time")
    # Due date (deadline) is independent of placement. Clearing the date clears the
    # time; a time without a date is rejected.
    if "due_date" in values and values["due_date"] is None:
        values["due_time"] = None
    next_due_date = values.get("due_date", existing.due_date)
    next_due_time = values.get("due_time", existing.due_time)
    if next_due_time is not None and next_due_date is None:
        raise ValueError("due_time requires due_date")
    # 토큰 경로(P9 orthus ticket)에도 열린 쓰기라 create/comment와 동형의 audit span을
    # 남긴다(운영 리뷰 지적 — 기존 세션 경로 누락분 동반 수정).
    with audit("personal_board.task.update") as span:
        if values:
            values["updated_at"] = datetime.now(UTC)
            with session() as s:
                s.execute(
                    update(personal_board_tasks)
                    .where(
                        personal_board_tasks.c.task_id == task_id,
                        personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                        personal_board_tasks.c.user_id == user_id,
                    )
                    .values(**values)
                )
                # 본 태스크를 완료(done)하면 미완료 서브태스크도 같은 트랜잭션에서
                # 완료 처리한다. 어떤 완료 경로(체크박스/메뉴/상세)든 일관 적용된다.
                if values.get("status") == "done":
                    s.execute(
                        update(personal_board_subtasks)
                        .where(
                            personal_board_subtasks.c.task_id == task_id,
                            personal_board_subtasks.c.completed.is_(False),
                        )
                        .values(completed=True, updated_at=values["updated_at"])
                    )
                if placement_changed:
                    _rebalance_task_order(
                        s,
                        workspace["workspace_id"],
                        user_id,
                        existing.scheduled_date,
                        existing.backlog_bucket_id,
                        updated_at=values["updated_at"],
                    )
                if placement_changed or order_index_requested:
                    _rebalance_task_order(
                        s,
                        workspace["workspace_id"],
                        user_id,
                        next_scheduled_date,
                        next_backlog_bucket_id,
                        moving_task_id=task_id if next_status != "archived" else None,
                        insert_index=requested_order_index,
                        updated_at=values["updated_at"],
                    )
                elif next_status == "archived":
                    _rebalance_task_order(
                        s,
                        workspace["workspace_id"],
                        user_id,
                        existing.scheduled_date,
                        existing.backlog_bucket_id,
                        updated_at=values["updated_at"],
                    )
                s.commit()
        elif order_index_requested:
            updated_at = datetime.now(UTC)
            with session() as s:
                _rebalance_task_order(
                    s,
                    workspace["workspace_id"],
                    user_id,
                    next_scheduled_date,
                    next_backlog_bucket_id,
                    moving_task_id=task_id if next_status != "archived" else None,
                    insert_index=requested_order_index,
                    updated_at=updated_at,
                )
                s.commit()
        span.set_output({"task_id": str(task_id), "fields": sorted(values)})

    dates_to_sync = {existing.scheduled_date}
    if body.scheduled_date is not None:
        dates_to_sync.add(body.scheduled_date)
    if "scheduled_date" in values and values["scheduled_date"] is not None:
        dates_to_sync.add(values["scheduled_date"])
    for day in dates_to_sync:
        if day is not None:
            sync_day_to_wiki(user_id, node_id, day)
    _resync_linked_event_after_update(user_id, node_id, task_id)
    return _load_task(task_id)


# ---------------------------------------------------------------------------
# Team calendar sync (board task <-> team schedule)
#
# A board task may mirror a company `team_calendar_events` row. The link is the
# 1:1 pair (personal_board_tasks.team_event_id, team_calendar_events.source_task_id).
# Push: task edits flow to the event (_resync_linked_event_after_update).
# Pull: the logged-in member's team events materialize as 'calendar' board tasks
#       (sync_team_events_to_board, run on every bootstrap/days read — idempotent).
# Only works where both tables share one DB (company node + owner scope).
# ---------------------------------------------------------------------------
def link_task_to_team_event(
    user_id: UUID, node_id: str, task_id: UUID, email: str | None = None
) -> PersonalBoardTask:
    """Register (or re-sync) a dated board task onto the company team schedule."""
    from orthus.dashboard import member_color, resolve_member_id

    workspace = ensure_workspace(user_id, node_id)
    with audit("personal_board.team_event.link") as span:
        with session() as s:
            row = s.execute(
                select(personal_board_tasks).where(
                    personal_board_tasks.c.task_id == task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if row is None:
                raise LookupError("task not found")
            task = row._mapping
            if task["scheduled_date"] is None:
                raise ValueError("날짜가 있는 태스크만 팀 일정에 등록할 수 있어요")
            member_id = resolve_member_id(node_id, user_id, email)
            if member_id is None:
                raise ValueError("팀멤버로 연결된 계정이 아니에요")

            event_values = {
                "title": task["title"],
                "event_date": task["scheduled_date"],
                "start_time": task["scheduled_time"],
                "end_time": task["scheduled_end_time"],
                # no_return은 팀 일정(team_calendar_events) 소유 필드다 — 보드에서 더 이상
                # 설정하지 않으므로 보드→이벤트 push에서 빼서, 대시보드 팀 일정에서 켠
                # "복귀불가"가 보드 편집 resync에 덮어써지지 않게 한다.
                "all_day": task["scheduled_time"] is None,
                "updated_at": datetime.now(UTC),
            }
            existing_event_id = task["team_event_id"]
            event_id = existing_event_id
            event_exists = existing_event_id is not None and (
                s.execute(
                    select(team_calendar_events.c.event_id).where(
                        team_calendar_events.c.event_id == existing_event_id,
                        team_calendar_events.c.node_id == node_id,
                    )
                ).first()
                is not None
            )
            if event_exists:
                s.execute(
                    update(team_calendar_events)
                    .where(team_calendar_events.c.event_id == existing_event_id)
                    .values(
                        member_ids=[str(member_id)],
                        source="personal_board",
                        source_task_id=task_id,
                        **event_values,
                    )
                )
            else:
                event_id = uuid.uuid4()
                s.execute(
                    insert(team_calendar_events).values(
                        event_id=event_id,
                        node_id=node_id,
                        member_ids=[str(member_id)],
                        event_type="event",
                        color=member_color(node_id, member_id),
                        created_by=user_id,
                        source="personal_board",
                        source_task_id=task_id,
                        **event_values,
                    )
                )
                s.execute(
                    update(personal_board_tasks)
                    .where(personal_board_tasks.c.task_id == task_id)
                    .values(team_event_id=event_id, updated_at=datetime.now(UTC))
                )
            s.commit()
        span.set_output({"task_id": str(task_id), "event_id": str(event_id)})
    return _load_task(task_id)


def unlink_task_from_team_event(user_id: UUID, node_id: str, task_id: UUID) -> PersonalBoardTask:
    """Remove a board task from the team schedule (delete the mirrored event)."""
    workspace = ensure_workspace(user_id, node_id)
    with audit("personal_board.team_event.unlink") as span:
        with session() as s:
            row = s.execute(
                select(personal_board_tasks.c.team_event_id).where(
                    personal_board_tasks.c.task_id == task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if row is None:
                raise LookupError("task not found")
            event_id = row.team_event_id
            if event_id is not None:
                s.execute(
                    delete(team_calendar_events).where(
                        team_calendar_events.c.event_id == event_id,
                        team_calendar_events.c.node_id == node_id,
                    )
                )
                s.execute(
                    update(personal_board_tasks)
                    .where(personal_board_tasks.c.task_id == task_id)
                    .values(team_event_id=None, updated_at=datetime.now(UTC))
                )
                s.commit()
        span.set_output({"task_id": str(task_id), "event_id": str(event_id) if event_id else None})
    return _load_task(task_id)


def _resync_linked_event_after_update(user_id: UUID, node_id: str, task_id: UUID) -> None:
    """Push board-task edits to its linked team event; unlink if no longer eligible.

    A task that loses its date (moved to backlog) or is archived is removed from
    the team schedule. Otherwise title/date/start-time are pushed to the event.
    """
    with session() as s:
        row = s.execute(
            select(
                personal_board_tasks.c.team_event_id,
                personal_board_tasks.c.title,
                personal_board_tasks.c.scheduled_date,
                personal_board_tasks.c.scheduled_time,
                personal_board_tasks.c.scheduled_end_time,
                personal_board_tasks.c.no_return,
                personal_board_tasks.c.status,
            ).where(personal_board_tasks.c.task_id == task_id)
        ).first()
        if row is None or row.team_event_id is None:
            return
        event_id = row.team_event_id
        if row.scheduled_date is None or row.status == "archived":
            s.execute(
                delete(team_calendar_events).where(
                    team_calendar_events.c.event_id == event_id,
                    team_calendar_events.c.node_id == node_id,
                )
            )
            s.execute(
                update(personal_board_tasks)
                .where(personal_board_tasks.c.task_id == task_id)
                .values(team_event_id=None)
            )
        else:
            s.execute(
                update(team_calendar_events)
                .where(
                    team_calendar_events.c.event_id == event_id,
                    team_calendar_events.c.node_id == node_id,
                )
                .values(
                    title=row.title,
                    event_date=row.scheduled_date,
                    start_time=row.scheduled_time,
                    end_time=row.scheduled_end_time,
                    # no_return은 팀 일정 소유 — 보드 resync가 덮어쓰지 않는다(위 참조).
                    all_day=row.scheduled_time is None,
                    updated_at=datetime.now(UTC),
                )
            )
        s.commit()


def sync_team_events_to_board(
    user_id: UUID, node_id: str, days: list[date], *, email: str | None = None
) -> None:
    """Materialize the logged-in member's team-calendar events as board tasks.

    For each team event on `days` that lists this member, create a linked
    'calendar'-source board task, or refresh title/date/start-time on the
    existing linked task. Idempotent via team_event_id; never resurrects an
    archived task. No-op when the account is not linked to a team member.
    """
    if not days:
        return
    from orthus.dashboard import resolve_member_id

    member_id = resolve_member_id(node_id, user_id, email)
    if member_id is None:
        return
    workspace = ensure_workspace(user_id, node_id)
    workspace_id = workspace["workspace_id"]
    member_str = str(member_id)
    with session() as s:
        # member_id -> name 매핑(동행자 표기용). 같은 팀 일정에 다른 멤버가 있으면
        # 개인 일정에 "팀 일정 · 이개발"처럼 동행자를 보여주고, 혼자면 "단독"으로 표기.
        name_by_id = {
            str(r.member_id): r.name
            for r in s.execute(
                select(team_members.c.member_id, team_members.c.name).where(
                    team_members.c.node_id == node_id
                )
            ).all()
        }

        def _companion_label(member_strs: list[str]) -> str:
            others = [name_by_id.get(m, "알 수 없음") for m in member_strs if m != member_str]
            return f"팀 일정 · {', '.join(others)}" if others else "팀 일정 · 단독"

        event_rows = s.execute(
            select(
                team_calendar_events.c.event_id,
                team_calendar_events.c.title,
                team_calendar_events.c.event_date,
                team_calendar_events.c.start_time,
                team_calendar_events.c.end_time,
                team_calendar_events.c.no_return,
                team_calendar_events.c.member_ids,
                team_calendar_events.c.source_task_id,
            ).where(
                team_calendar_events.c.node_id == node_id,
                team_calendar_events.c.event_date.in_(days),
                # 반복 일정(루틴)은 마스터 행 1개가 여러 회차로 펼쳐지는데, 보드 미러는
                # team_event_id 1:1 링크라 회차별 task를 표현할 수 없다 → 미러 제외.
                team_calendar_events.c.repeat_freq.is_(None),
            )
        ).all()
        for ev in event_rows:
            members = [str(m) for m in (ev.member_ids or [])]
            if member_str not in members:
                continue
            label = _companion_label(members)
            # team_calendar_events에는 end>start·end-requires-start 불변식이 없다(CHECK 없음,
            # cross-midnight·start 없는 이벤트 가능). board의 scheduled_end_time은 그 불변식을
            # 강제하므로 pull 경계에서 정규화해 둔다 — 안 그러면 미러된 task가 update_task
            # 재검증에 걸려 어떤 편집도 거부되는 상태로 갇힌다.
            sched_end = (
                ev.end_time
                if (
                    ev.start_time is not None
                    and ev.end_time is not None
                    and ev.end_time > ev.start_time
                )
                else None
            )
            existing = s.execute(
                select(
                    personal_board_tasks.c.task_id,
                    personal_board_tasks.c.status,
                    personal_board_tasks.c.title,
                    personal_board_tasks.c.scheduled_date,
                    personal_board_tasks.c.scheduled_time,
                    personal_board_tasks.c.scheduled_end_time,
                    personal_board_tasks.c.no_return,
                    personal_board_tasks.c.source_label,
                ).where(
                    personal_board_tasks.c.team_event_id == ev.event_id,
                    personal_board_tasks.c.workspace_id == workspace_id,
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if existing is None:
                # Event that originated from a (now missing) board task — don't
                # recreate a phantom mirror.
                if ev.source_task_id is not None:
                    continue
                new_task_id = uuid.uuid4()
                order_index = _next_task_order(workspace_id, ev.event_date, None)
                # ON CONFLICT: 같은 페이지 로드에서 bootstrap+list_days가 동시에
                # 돌아도 (workspace_id, team_event_id) unique index가 두 번째
                # insert를 무시하게 한다(중복 미러 방지). 실제로 삽입된 경우에만
                # 캘린더 이벤트에 source_task_id를 역링크한다.
                inserted = s.execute(
                    pg_insert(personal_board_tasks)
                    .values(
                        task_id=new_task_id,
                        workspace_id=workspace_id,
                        user_id=user_id,
                        title=ev.title,
                        scheduled_date=ev.event_date,
                        scheduled_time=ev.start_time,
                        scheduled_end_time=sched_end,
                        no_return=ev.no_return,
                        order_index=order_index,
                        source_kind="calendar",
                        source_label=label,
                        team_event_id=ev.event_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "team_event_id"],
                        index_where=personal_board_tasks.c.team_event_id.isnot(None),
                    )
                    .returning(personal_board_tasks.c.task_id)
                ).first()
                if inserted is not None:
                    s.execute(
                        update(team_calendar_events)
                        .where(team_calendar_events.c.event_id == ev.event_id)
                        .values(source_task_id=new_task_id)
                    )
            elif existing.status != "archived" and (
                existing.title != ev.title
                or existing.scheduled_date != ev.event_date
                or existing.scheduled_time != ev.start_time
                or existing.scheduled_end_time != sched_end
                or existing.no_return != ev.no_return
                or existing.source_label != label
            ):
                s.execute(
                    update(personal_board_tasks)
                    .where(personal_board_tasks.c.task_id == existing.task_id)
                    .values(
                        title=ev.title,
                        scheduled_date=ev.event_date,
                        scheduled_time=ev.start_time,
                        scheduled_end_time=sched_end,
                        no_return=ev.no_return,
                        source_label=label,
                        updated_at=datetime.now(UTC),
                    )
                )
        s.commit()


def create_project(user_id: UUID, node_id: str, body: ProjectCreate) -> PersonalBoardProject:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    workspace = ensure_workspace(user_id, node_id)
    project_id = uuid.uuid4()
    with session() as s:
        # 같은 이름의 활성 채널이 이미 회사 프로젝트 채널이면 개인 채널로 못 만든다
        # (update/delete와 같은 가드). 안 그러면 upsert가 회사 채널을 덮어 색을 지우고
        # kind='company'를 그대로 돌려줘 의도와 다르게 회사 공개가 된다.
        existing = s.execute(
            select(personal_board_projects.c.kind).where(
                personal_board_projects.c.workspace_id == workspace["workspace_id"],
                personal_board_projects.c.name == name,
                personal_board_projects.c.archived_at.is_(None),
            )
        ).first()
        if existing is not None and existing.kind == "company":
            raise ValueError("이미 회사 프로젝트 채널로 존재해요")
        # 동명 매칭은 활성 채널만(index_where). color는 값이 있을 때만 덮어쓴다.
        set_cols: dict = {"archived_at": None, "updated_at": func.now()}
        if body.color is not None:
            set_cols["color"] = body.color
        row = s.execute(
            pg_insert(personal_board_projects)
            .values(
                project_id=project_id,
                workspace_id=workspace["workspace_id"],
                name=name,
                color=body.color,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "name"],
                index_where=personal_board_projects.c.archived_at.is_(None),
                set_=set_cols,
            )
            .returning(*_PROJECT_COLS)
        ).one()
        s.commit()
    return _project_from_row(row)


def update_project(
    user_id: UUID, node_id: str, project_id: UUID, body: ProjectPatch
) -> PersonalBoardProject:
    workspace = ensure_workspace(user_id, node_id)
    # 회사 프로젝트 채널은 보드에서 못 고친다(이름·색은 회사 프로젝트를 미러링하고,
    # 담당이 빠지면 자동으로 정리된다).
    existing = _load_project(workspace["workspace_id"], project_id)
    if existing.kind == "company":
        raise ValueError("회사 프로젝트 채널은 보드에서 수정할 수 없어요")
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "name" in values:
        values["name"] = values["name"].strip()
        if not values["name"]:
            raise ValueError("name required")
    if not values:
        return existing
    values["updated_at"] = datetime.now(UTC)
    with session() as s:
        result = s.execute(
            update(personal_board_projects)
            .where(
                personal_board_projects.c.project_id == project_id,
                personal_board_projects.c.workspace_id == workspace["workspace_id"],
                personal_board_projects.c.kind == "personal",
            )
            .values(**values)
            .returning(*_PROJECT_COLS)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("project not found")
    return _project_from_row(result)


def delete_project(user_id: UUID, node_id: str, project_id: UUID) -> None:
    """Archive a board project (channel). Soft-delete via ``archived_at``.

    List/load queries already filter ``archived_at IS NULL`` and task→project
    joins do the same, so assigned tasks fall back to Unassigned without an FK
    cascade. Re-creating a channel with the same name un-archives it.
    """
    workspace = ensure_workspace(user_id, node_id)
    # 회사 프로젝트 채널은 직접 못 지운다(담당이 빠지면 다음 보드 로드에서 자동 정리).
    if _load_project(workspace["workspace_id"], project_id).kind == "company":
        raise ValueError("회사 프로젝트 채널은 담당이 빠지면 자동으로 정리돼요")
    with session() as s:
        result = s.execute(
            update(personal_board_projects)
            .where(
                personal_board_projects.c.project_id == project_id,
                personal_board_projects.c.workspace_id == workspace["workspace_id"],
                personal_board_projects.c.archived_at.is_(None),
                personal_board_projects.c.kind == "personal",
            )
            .values(archived_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            .returning(personal_board_projects.c.project_id)
        ).first()
        s.commit()
    if result is None:
        raise LookupError("project not found")


def create_folder(user_id: UUID, node_id: str, body: FolderCreate) -> PersonalBoardFolder:
    name = body.name.strip()
    if not name:
        raise ValueError("name required")
    if name.casefold() in SYSTEM_FOLDER_NAMES:
        raise ValueError("reserved folder name")
    workspace = ensure_workspace(user_id, node_id)
    ensure_default_folders(workspace["workspace_id"])
    folder_id = uuid.uuid4()
    order_index = _next_folder_order(workspace["workspace_id"])
    with session() as s:
        row = s.execute(
            pg_insert(personal_board_folders)
            .values(
                folder_id=folder_id,
                workspace_id=workspace["workspace_id"],
                name=name,
                kind="custom",
                order_index=order_index,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "name"],
                set_={"kind": "custom", "updated_at": func.now()},
            )
            .returning(
                personal_board_folders.c.folder_id,
                personal_board_folders.c.name,
                personal_board_folders.c.kind,
                personal_board_folders.c.order_index,
            )
        ).one()
        s.commit()
    return PersonalBoardFolder(
        folder_id=row.folder_id,
        name=row.name,
        kind=row.kind,
        order_index=row.order_index,
    )


def update_backlog_bucket(
    user_id: UUID, node_id: str, bucket_id: UUID, body: BacklogBucketPatch
) -> PersonalBoardBacklogBucket:
    workspace = ensure_workspace(user_id, node_id)
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not values:
        return _load_backlog_bucket(workspace["workspace_id"], bucket_id)
    values["updated_at"] = datetime.now(UTC)
    with session() as s:
        result = s.execute(
            update(personal_board_backlog_buckets)
            .where(
                personal_board_backlog_buckets.c.bucket_id == bucket_id,
                personal_board_backlog_buckets.c.workspace_id == workspace["workspace_id"],
            )
            .values(**values)
            .returning(
                personal_board_backlog_buckets.c.bucket_id,
                personal_board_backlog_buckets.c.key,
                personal_board_backlog_buckets.c.label,
                personal_board_backlog_buckets.c.badge,
                personal_board_backlog_buckets.c.color,
                personal_board_backlog_buckets.c.order_index,
                personal_board_backlog_buckets.c.collapsed,
            )
        ).first()
        s.commit()
    if result is None:
        raise LookupError("bucket not found")
    return PersonalBoardBacklogBucket(
        bucket_id=result.bucket_id,
        key=result.key,
        label=result.label,
        badge=result.badge,
        color=result.color,
        order_index=result.order_index,
        collapsed=result.collapsed,
    )


def update_preferences(
    user_id: UUID, node_id: str, body: PreferencesPatch
) -> PersonalBoardPreferences:
    workspace = ensure_workspace(user_id, node_id)
    ensure_default_integrations(workspace["workspace_id"])
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "right_panel" in values and values["right_panel"] not in {
        "backlog",
        "integrations",
        "schedule",
        "hidden",
    }:
        raise ValueError("invalid right_panel")
    if values.get("active_integration") is not None:
        _validate_integration_kind(workspace["workspace_id"], values["active_integration"])
    if "filter_mode" in values and values["filter_mode"] not in {
        "all",
        "open",
        "done",
        "high_priority",
    }:
        raise ValueError("invalid filter_mode")
    if "sort_mode" in values and values["sort_mode"] not in {"manual", "priority", "time"}:
        raise ValueError("invalid sort_mode")
    default_selected_date = selected_date_or_today(
        values.get("selected_date"), workspace["timezone"]
    )
    values["updated_at"] = datetime.now(UTC)
    with session() as s:
        s.execute(
            pg_insert(personal_board_preferences)
            .values(
                workspace_id=workspace["workspace_id"],
                user_id=user_id,
                selected_date=default_selected_date,
                right_panel="backlog",
                active_integration=None,
                filter_mode="all",
                sort_mode="time",
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "user_id"],
            )
        )
        row = s.execute(
            update(personal_board_preferences)
            .where(
                personal_board_preferences.c.workspace_id == workspace["workspace_id"],
                personal_board_preferences.c.user_id == user_id,
            )
            .values(**values)
            .returning(personal_board_preferences)
        ).one()
        s.commit()
    return PersonalBoardPreferences(
        selected_date=row.selected_date,
        right_panel=row.right_panel,
        active_integration=row.active_integration,
        filter_mode=row.filter_mode,
        sort_mode=row.sort_mode,
    )


def create_subtask(
    user_id: UUID, node_id: str, task_id: UUID, body: SubtaskCreate
) -> PersonalBoardSubtask:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    workspace = ensure_workspace(user_id, node_id)
    task_day = _task_day_for_user(workspace["workspace_id"], user_id, task_id)
    # Idempotent replay for offline-queued creates.
    if body.subtask_id is not None:
        with session() as s:
            existing = s.execute(
                select(personal_board_subtasks.c.subtask_id)
                .select_from(
                    personal_board_subtasks.join(
                        personal_board_tasks,
                        personal_board_subtasks.c.task_id == personal_board_tasks.c.task_id,
                    )
                )
                .where(
                    personal_board_subtasks.c.subtask_id == body.subtask_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
        if existing is not None:
            return _load_subtask(body.subtask_id)
    subtask_id = body.subtask_id or uuid.uuid4()
    order_index = _next_subtask_order(task_id)
    with audit("personal_board.subtask.create") as span:
        with session() as s:
            s.execute(
                insert(personal_board_subtasks).values(
                    subtask_id=subtask_id,
                    task_id=task_id,
                    title=title,
                    order_index=order_index,
                )
            )
            s.commit()
        if task_day is not None:
            sync_day_to_wiki(user_id, node_id, task_day)
        item = _load_subtask(subtask_id)
        span.set_output({"subtask_id": str(subtask_id), "task_id": str(task_id)})
        return item


def update_subtask(
    user_id: UUID, node_id: str, subtask_id: UUID, body: SubtaskPatch
) -> PersonalBoardSubtask:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        existing = s.execute(
            select(
                personal_board_subtasks.c.task_id,
                personal_board_tasks.c.scheduled_date,
            )
            .select_from(
                personal_board_subtasks.join(
                    personal_board_tasks,
                    personal_board_subtasks.c.task_id == personal_board_tasks.c.task_id,
                )
            )
            .where(
                personal_board_subtasks.c.subtask_id == subtask_id,
                personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                personal_board_tasks.c.user_id == user_id,
            )
        ).first()
    if existing is None:
        raise LookupError("subtask not found")

    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise ValueError("title required")
    if values:
        values["updated_at"] = datetime.now(UTC)
        with session() as s:
            s.execute(
                update(personal_board_subtasks)
                .where(personal_board_subtasks.c.subtask_id == subtask_id)
                .values(**values)
            )
            s.commit()
        if existing.scheduled_date is not None:
            sync_day_to_wiki(user_id, node_id, existing.scheduled_date)
    return _load_subtask(subtask_id)


def delete_subtask(user_id: UUID, node_id: str, subtask_id: UUID) -> None:
    workspace = ensure_workspace(user_id, node_id)
    with audit("personal_board.subtask.delete") as span:
        with session() as s:
            existing = s.execute(
                select(personal_board_tasks.c.scheduled_date)
                .select_from(
                    personal_board_subtasks.join(
                        personal_board_tasks,
                        personal_board_subtasks.c.task_id == personal_board_tasks.c.task_id,
                    )
                )
                .where(
                    personal_board_subtasks.c.subtask_id == subtask_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if existing is None:
                raise LookupError("subtask not found")
            s.execute(
                personal_board_subtasks.delete().where(
                    personal_board_subtasks.c.subtask_id == subtask_id
                )
            )
            s.commit()
        if existing.scheduled_date is not None:
            sync_day_to_wiki(user_id, node_id, existing.scheduled_date)
        span.set_output({"subtask_id": str(subtask_id)})


def create_fixed_event(
    user_id: UUID, node_id: str, body: FixedEventCreate
) -> PersonalBoardFixedEvent:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    if body.source_kind not in {"manual", "calendar", "ai_session"}:
        raise ValueError("invalid source_kind")
    if body.ends_at <= body.starts_at:
        raise ValueError("ends_at must be after starts_at")
    workspace = ensure_workspace(user_id, node_id)
    _validate_project(workspace["workspace_id"], body.project_id)
    event_id = uuid.uuid4()
    with audit("personal_board.event.create") as span:
        with session() as s:
            s.execute(
                insert(personal_board_fixed_events).values(
                    event_id=event_id,
                    workspace_id=workspace["workspace_id"],
                    user_id=user_id,
                    project_id=body.project_id,
                    title=title,
                    starts_at=body.starts_at,
                    ends_at=body.ends_at,
                    source_kind=body.source_kind,
                    source_label=body.source_label,
                )
            )
            s.commit()
        timezone = ZoneInfo(workspace["timezone"])
        for day in event_local_days(body.starts_at, body.ends_at, timezone):
            sync_day_to_wiki(user_id, node_id, day)
        event = _load_event(event_id)
        span.set_output({"event_id": str(event_id)})
        return event


def update_fixed_event(
    user_id: UUID, node_id: str, event_id: UUID, body: FixedEventPatch
) -> PersonalBoardFixedEvent:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        existing = s.execute(
            select(personal_board_fixed_events).where(
                personal_board_fixed_events.c.event_id == event_id,
                personal_board_fixed_events.c.workspace_id == workspace["workspace_id"],
                personal_board_fixed_events.c.user_id == user_id,
            )
        ).first()
    if existing is None:
        raise LookupError("fixed event not found")

    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise ValueError("title required")
    if "project_id" in values:
        _validate_project(workspace["workspace_id"], values["project_id"])

    next_starts_at = values.get("starts_at", existing.starts_at)
    next_ends_at = values.get("ends_at", existing.ends_at)
    if next_ends_at <= next_starts_at:
        raise ValueError("ends_at must be after starts_at")

    if not values:
        return _load_event(event_id)

    values["updated_at"] = datetime.now(UTC)
    timezone = ZoneInfo(workspace["timezone"])
    old_days = set(event_local_days(existing.starts_at, existing.ends_at, timezone))
    new_days = set(event_local_days(next_starts_at, next_ends_at, timezone))

    with audit("personal_board.event.update") as span:
        with session() as s:
            result = s.execute(
                update(personal_board_fixed_events)
                .where(
                    personal_board_fixed_events.c.event_id == event_id,
                    personal_board_fixed_events.c.workspace_id == workspace["workspace_id"],
                    personal_board_fixed_events.c.user_id == user_id,
                )
                .values(**values)
                .returning(personal_board_fixed_events.c.event_id)
            ).first()
            s.commit()
        if result is None:
            raise LookupError("fixed event not found")
        for day in sorted(old_days | new_days):
            sync_day_to_wiki(user_id, node_id, day)
        event = _load_event(event_id)
        span.set_output({"event_id": str(event_id)})
        return event


def list_fixed_events(
    user_id: UUID, node_id: str, frm: date | None, to: date | None
) -> list[PersonalBoardFixedEvent]:
    """Owner-scoped personal fixed events overlapping [frm, to] (inclusive days).

    Read mirror of the team-calendar list so an MCP/CLI caller can find an
    event_id before updating. Rows stay isolated by workspace + user_id; day
    bounds use the workspace timezone to match how events map onto local days."""
    workspace = ensure_workspace(user_id, node_id)
    tz = ZoneInfo(workspace["timezone"])
    clauses = [
        personal_board_fixed_events.c.workspace_id == workspace["workspace_id"],
        personal_board_fixed_events.c.user_id == user_id,
    ]
    if frm is not None:
        clauses.append(personal_board_fixed_events.c.ends_at >= datetime.combine(frm, time.min, tz))
    if to is not None:
        clauses.append(
            personal_board_fixed_events.c.starts_at
            < datetime.combine(to, time.min, tz) + timedelta(days=1)
        )
    with session() as s:
        rows = s.execute(
            select(personal_board_fixed_events)
            .where(and_(*clauses))
            .order_by(personal_board_fixed_events.c.starts_at)
        ).all()
        project_ids = {r._mapping["project_id"] for r in rows if r._mapping["project_id"]}
        projects: dict = {}
        if project_ids:
            prows = s.execute(
                select(*_PROJECT_COLS).where(
                    personal_board_projects.c.project_id.in_(project_ids),
                    personal_board_projects.c.archived_at.is_(None),
                )
            ).all()
            for pr in prows:
                project = _project_from_row(pr)
                projects[project.project_id] = project
    return [_event_from_row(r._mapping, projects) for r in rows]


def create_note(user_id: UUID, node_id: str, body: NoteCreate) -> PersonalBoardNote:
    title = body.title.strip()
    body_text = body.body.strip()
    if not title:
        raise ValueError("title required")
    if not body_text:
        raise ValueError("body required")
    if body.kind not in {"note", "incident", "decision"}:
        raise ValueError("invalid kind")
    workspace = ensure_workspace(user_id, node_id)
    order_index = _next_note_order(workspace["workspace_id"], body.note_date)
    note_id = uuid.uuid4()
    with audit("personal_board.note.create") as span:
        with session() as s:
            s.execute(
                insert(personal_board_notes).values(
                    note_id=note_id,
                    workspace_id=workspace["workspace_id"],
                    user_id=user_id,
                    note_date=body.note_date,
                    kind=body.kind,
                    title=title,
                    body=body_text,
                    order_index=order_index,
                )
            )
            s.commit()
        sync_day_to_wiki(user_id, node_id, body.note_date)
        note = _load_note(note_id)
        span.set_output({"note_id": str(note_id), "note_date": body.note_date.isoformat()})
        return note


def _assert_task_owned(workspace_id: UUID, user_id: UUID, task_id: UUID) -> None:
    with session() as s:
        row = s.execute(
            select(personal_board_tasks.c.task_id).where(
                personal_board_tasks.c.task_id == task_id,
                personal_board_tasks.c.workspace_id == workspace_id,
                personal_board_tasks.c.user_id == user_id,
            )
        ).first()
    if row is None:
        raise LookupError("task not found")


def _task_comment_from_row(row) -> TaskComment:
    return TaskComment(
        comment_id=row.comment_id,
        task_id=row.task_id,
        user_id=row.user_id,
        author_name=row.display_name,
        body=row.body,
        created_at=row.created_at,
    )


def create_task_comment(
    user_id: UUID, node_id: str, task_id: UUID, body: TaskCommentCreate
) -> TaskComment:
    text = body.body.strip()
    if not text:
        raise ValueError("body required")
    if len(text) > 4_000:
        raise ValueError("body too long")
    workspace = ensure_workspace(user_id, node_id)
    _assert_task_owned(workspace["workspace_id"], user_id, task_id)
    comment_id = uuid.uuid4()
    with audit("personal_board.task.comment.create") as span:
        with session() as s:
            s.execute(
                insert(personal_board_task_comments).values(
                    comment_id=comment_id,
                    task_id=task_id,
                    workspace_id=workspace["workspace_id"],
                    user_id=user_id,
                    body=text,
                )
            )
            s.commit()
        span.set_output({"comment_id": str(comment_id), "task_id": str(task_id)})
    comments = list_task_comments(user_id, node_id, task_id)
    for comment in comments:
        if comment.comment_id == comment_id:
            return comment
    raise LookupError("comment not found")


def list_task_comments(user_id: UUID, node_id: str, task_id: UUID) -> list[TaskComment]:
    workspace = ensure_workspace(user_id, node_id)
    _assert_task_owned(workspace["workspace_id"], user_id, task_id)
    with session() as s:
        rows = s.execute(
            select(
                personal_board_task_comments.c.comment_id,
                personal_board_task_comments.c.task_id,
                personal_board_task_comments.c.user_id,
                personal_board_task_comments.c.body,
                personal_board_task_comments.c.created_at,
                users.c.display_name,
            )
            .select_from(
                personal_board_task_comments.join(
                    users,
                    personal_board_task_comments.c.user_id == users.c.user_id,
                    isouter=True,
                )
            )
            .where(
                personal_board_task_comments.c.task_id == task_id,
                # _assert_task_owned가 선행하지만, workspace 필터를 겹쳐 방어한다
                # (owner 격리 defense-in-depth — operator review 후속).
                personal_board_task_comments.c.workspace_id == workspace["workspace_id"],
            )
            .order_by(personal_board_task_comments.c.created_at)
        ).all()
    return [_task_comment_from_row(row) for row in rows]


TASK_STATUSES = {"open", "done", "archived"}
TASK_LIST_MAX_LIMIT = 200


def _normalize_id_prefix(id_prefix: str) -> str:
    """Validate a short task-id prefix (git-SHA style) into dashless lowercase hex.

    Callers (orthus CLI/MCP) pass the first chars of a task UUID with or without
    dashes; matching strips dashes on both sides so either form works."""
    normalized = id_prefix.strip().lower().replace("-", "")
    if (
        not normalized
        or len(normalized) > 32
        or any(c not in "0123456789abcdef" for c in normalized)
    ):
        raise ValueError("invalid id_prefix (expected leading hex chars of a task id)")
    return normalized


def list_tasks(
    user_id: UUID,
    node_id: str,
    *,
    status: str | None = None,
    project_id: UUID | None = None,
    id_prefix: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
) -> list[PersonalBoardTask]:
    """Flat owner-scoped ticket list for the orthus CLI/MCP surface.

    bootstrap() ships the whole board (days/buckets/integrations) for the FE;
    an agent only needs its own tickets with a few filters. status=None returns
    every status (open/done/archived)."""
    if status is not None and status not in TASK_STATUSES:
        raise ValueError(f"invalid status (expected one of {sorted(TASK_STATUSES)})")
    limit = max(1, min(limit, TASK_LIST_MAX_LIMIT))
    workspace = ensure_workspace(user_id, node_id)
    conditions = [
        personal_board_tasks.c.workspace_id == workspace["workspace_id"],
        personal_board_tasks.c.user_id == user_id,
    ]
    if status is not None:
        conditions.append(personal_board_tasks.c.status == status)
    if project_id is not None:
        conditions.append(personal_board_tasks.c.project_id == project_id)
    if id_prefix is not None:
        prefix = _normalize_id_prefix(id_prefix)
        conditions.append(
            func.replace(cast(personal_board_tasks.c.task_id, String), "-", "").like(f"{prefix}%")
        )
    if date_from is not None:
        conditions.append(personal_board_tasks.c.scheduled_date >= date_from)
    if date_to is not None:
        conditions.append(personal_board_tasks.c.scheduled_date <= date_to)
    with session() as s:
        task_rows = s.execute(
            select(personal_board_tasks)
            .where(*conditions)
            .order_by(
                personal_board_tasks.c.scheduled_date.asc().nulls_last(),
                personal_board_tasks.c.order_index.asc(),
                personal_board_tasks.c.created_at.asc(),
            )
            .limit(limit)
        ).all()
        task_ids = [row._mapping["task_id"] for row in task_rows]
        subtask_rows = (
            s.execute(
                select(personal_board_subtasks)
                .where(personal_board_subtasks.c.task_id.in_(task_ids))
                .order_by(personal_board_subtasks.c.order_index.asc())
            ).all()
            if task_ids
            else []
        )
        project_rows = s.execute(
            select(*_PROJECT_COLS).where(
                personal_board_projects.c.workspace_id == workspace["workspace_id"],
                personal_board_projects.c.archived_at.is_(None),
            )
        ).all()
    subtasks_by_task: dict[UUID, list[PersonalBoardSubtask]] = {}
    for row in subtask_rows:
        item = _subtask_from_row(row._mapping)
        subtasks_by_task.setdefault(item.task_id, []).append(item)
    project_by_id = {row.project_id: _project_from_row(row) for row in project_rows}
    return [_task_from_row(row._mapping, project_by_id, subtasks_by_task) for row in task_rows]


# --- Owner-scope notification read surface (PR-N1) ---------------------------
# Mirrors the mail notify-state precedent (`orthus/mail/notify.py`): read-only
# count/max the client polls to decide whether to raise a native notification.
# Calendar-materialized auto-rows (`source_kind='calendar'`, see
# materialize_calendar_tasks) are excluded — recurring schedule mirrors are not
# "new work arrived" signals. No workspace is created on read: a caller without
# a workspace gets the zero-state.

BOARD_NOTIFICATION_MAX_LIMIT = 50
BOARD_NOTIFICATION_DEFAULT_LIMIT = 20


def _find_workspace_id(s, user_id: UUID, node_id: str) -> UUID | None:
    """Read-only workspace lookup — unlike ensure_workspace, never creates one."""
    return s.execute(
        select(personal_board_workspaces.c.workspace_id).where(
            personal_board_workspaces.c.user_id == user_id,
            personal_board_workspaces.c.node_id == node_id,
        )
    ).scalar_one_or_none()


def _notification_conditions(workspace_id: UUID, user_id: UUID) -> list:
    # Owner predicate first: workspace + user_id (fail-closed owner scope),
    # then the calendar auto-row exclusion.
    return [
        personal_board_tasks.c.workspace_id == workspace_id,
        personal_board_tasks.c.user_id == user_id,
        personal_board_tasks.c.source_kind != "calendar",
    ]


def board_notify_state(user_id: UUID, node_id: str) -> PersonalBoardNotifyState:
    """Return total/latest-created/latest-title over the caller's board tickets."""
    with session() as s:
        workspace_id = _find_workspace_id(s, user_id, node_id)
        if workspace_id is None:
            return PersonalBoardNotifyState()
        conditions = _notification_conditions(workspace_id, user_id)
        agg = s.execute(
            select(
                func.count().label("total"),
                func.max(personal_board_tasks.c.created_at).label("latest_at"),
            ).where(*conditions)
        ).one()
        total = int(agg.total or 0)
        latest_title: str | None = None
        if total:
            latest_title = s.execute(
                select(personal_board_tasks.c.title)
                .where(*conditions)
                .order_by(personal_board_tasks.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
    return PersonalBoardNotifyState(total=total, latest_at=agg.latest_at, latest_title=latest_title)


def list_board_notifications(
    user_id: UUID,
    node_id: str,
    *,
    limit: int = BOARD_NOTIFICATION_DEFAULT_LIMIT,
) -> PersonalBoardNotificationList:
    """Recent non-calendar board tickets for the caller, newest first.

    `total` is the full matching count (same population as board_notify_state),
    not len(items)."""
    limit = max(1, min(limit, BOARD_NOTIFICATION_MAX_LIMIT))
    with session() as s:
        workspace_id = _find_workspace_id(s, user_id, node_id)
        if workspace_id is None:
            return PersonalBoardNotificationList()
        conditions = _notification_conditions(workspace_id, user_id)
        rows = s.execute(
            select(
                personal_board_tasks.c.task_id,
                personal_board_tasks.c.title,
                personal_board_tasks.c.created_at,
                personal_board_tasks.c.source_kind,
                personal_board_tasks.c.source_label,
                personal_board_tasks.c.status,
                personal_board_tasks.c.backlog_bucket_id,
                personal_board_tasks.c.scheduled_date,
            )
            .where(*conditions)
            .order_by(personal_board_tasks.c.created_at.desc())
            .limit(limit)
        ).all()
        total = s.execute(select(func.count()).where(*conditions)).scalar_one()
    return PersonalBoardNotificationList(
        items=[
            PersonalBoardNotificationItem(
                task_id=row.task_id,
                title=row.title,
                created_at=row.created_at,
                source_kind=row.source_kind,
                source_label=row.source_label,
                status=row.status,
                backlog_bucket_id=row.backlog_bucket_id,
                scheduled_date=row.scheduled_date,
            )
            for row in rows
        ],
        total=int(total or 0),
    )


def list_projects(
    user_id: UUID, node_id: str, *, email: str | None = None
) -> list[PersonalBoardProject]:
    """Active board channels for the orthus CLI/MCP surface (`--project` name resolve).

    Syncs assigned company projects into channels first — company channels
    otherwise only appear after the owner opens the FE board (bootstrap)."""
    workspace = ensure_workspace(user_id, node_id)
    ensure_company_channels(workspace["workspace_id"], user_id, node_id, email=email)
    with session() as s:
        rows = s.execute(
            select(*_PROJECT_COLS)
            .where(
                personal_board_projects.c.workspace_id == workspace["workspace_id"],
                personal_board_projects.c.archived_at.is_(None),
            )
            .order_by(personal_board_projects.c.name.asc())
        ).all()
    return [_project_from_row(row) for row in rows]


def list_backlog_buckets(user_id: UUID, node_id: str) -> list[PersonalBoardBacklogBucket]:
    """Backlog buckets (empty task lists) for `--bucket <key>` resolve."""
    workspace = ensure_workspace(user_id, node_id)
    ensure_default_buckets(workspace["workspace_id"])
    with session() as s:
        rows = s.execute(
            select(personal_board_backlog_buckets)
            .where(personal_board_backlog_buckets.c.workspace_id == workspace["workspace_id"])
            .order_by(personal_board_backlog_buckets.c.order_index.asc())
        ).all()
    return [
        PersonalBoardBacklogBucket(
            bucket_id=row._mapping["bucket_id"],
            key=row._mapping["key"],
            label=row._mapping["label"],
            badge=row._mapping["badge"],
            color=row._mapping["color"],
            order_index=row._mapping["order_index"],
            collapsed=row._mapping["collapsed"],
        )
        for row in rows
    ]


def _week_sunday(d: date) -> date:
    """Return the Sunday (weekday 0) of the week containing d.

    개인 보드 주 시작은 일요일이다(회사 weekly_entries와 동일). weekday 인덱스는
    0=일요일 .. 6=토요일로 day_allocations / subtasks / comments / weekly review와
    공유한다."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _objective_from_row(
    row,
    project_by_id: dict,
    subtasks_by_objective: dict[UUID, list[ObjectiveSubtask]] | None = None,
    comments_by_objective: dict[UUID, list[ObjectiveComment]] | None = None,
) -> PersonalWeeklyObjective:
    allocations = [DayAllocation.model_validate(item) for item in (row["day_allocations"] or [])]
    objective_id = row["objective_id"]
    return PersonalWeeklyObjective(
        objective_id=objective_id,
        week_start=row["week_start"],
        title=row["title"],
        project_id=row["project_id"],
        project=project_by_id.get(row["project_id"]),
        day_allocations=allocations,
        completed=row["completed"],
        order_index=row["order_index"],
        note=row["note"],
        source_kind=row["source_kind"],
        created_at=row["created_at"],
        subtasks=(subtasks_by_objective or {}).get(objective_id, []),
        comments=(comments_by_objective or {}).get(objective_id, []),
    )


def _objective_subtask_from_row(row) -> ObjectiveSubtask:
    return ObjectiveSubtask(
        subtask_id=row["subtask_id"],
        title=row["title"],
        completed=row["completed"],
        order_index=row["order_index"],
        weekday=row["weekday"],
    )


def _objective_comment_from_row(row) -> ObjectiveComment:
    return ObjectiveComment(
        comment_id=row["comment_id"],
        user_id=row["user_id"],
        author_name=row["author_name"],
        body=row["body"],
        weekday=row["weekday"],
        created_at=row["created_at"],
    )


def _objective_subtasks_by_objective(
    s, objective_ids: list[UUID]
) -> dict[UUID, list[ObjectiveSubtask]]:
    if not objective_ids:
        return {}
    rows = s.execute(
        select(personal_objective_subtasks)
        .where(personal_objective_subtasks.c.objective_id.in_(objective_ids))
        .order_by(personal_objective_subtasks.c.order_index.asc())
    ).all()
    grouped: dict[UUID, list[ObjectiveSubtask]] = {}
    for row in rows:
        mapping = row._mapping
        grouped.setdefault(mapping["objective_id"], []).append(_objective_subtask_from_row(mapping))
    return grouped


def _objective_comments_by_objective(
    s, objective_ids: list[UUID]
) -> dict[UUID, list[ObjectiveComment]]:
    if not objective_ids:
        return {}
    rows = s.execute(
        select(
            personal_objective_comments.c.comment_id,
            personal_objective_comments.c.objective_id,
            personal_objective_comments.c.user_id,
            personal_objective_comments.c.body,
            personal_objective_comments.c.weekday,
            personal_objective_comments.c.created_at,
            users.c.display_name.label("author_name"),
        )
        .select_from(
            personal_objective_comments.join(
                users,
                personal_objective_comments.c.user_id == users.c.user_id,
                isouter=True,
            )
        )
        .where(personal_objective_comments.c.objective_id.in_(objective_ids))
        .order_by(personal_objective_comments.c.created_at.asc())
    ).all()
    grouped: dict[UUID, list[ObjectiveComment]] = {}
    for row in rows:
        mapping = row._mapping
        grouped.setdefault(mapping["objective_id"], []).append(_objective_comment_from_row(mapping))
    return grouped


def _projects_by_id(s, workspace_id: UUID) -> dict[UUID, PersonalBoardProject]:
    rows = s.execute(
        select(*_PROJECT_COLS).where(
            personal_board_projects.c.workspace_id == workspace_id,
            personal_board_projects.c.archived_at.is_(None),
        )
    ).all()
    return {r.project_id: _project_from_row(r) for r in rows}


def list_weekly_plan(
    user_id: UUID, node_id: str, week_start: date | None = None, email: str | None = None
) -> WeeklyPlanResponse:
    workspace = ensure_workspace(user_id, node_id)
    target_week = week_start or _week_sunday(selected_date_or_today(None, workspace["timezone"]))
    # 회사 주간계획 담당 항목을 '회사 부여' 목표로 멱등 반영(개인 소유 day_allocations 보존).
    sync_company_plan_to_objectives(user_id, node_id, target_week, email=email)
    with session() as s:
        project_by_id = _projects_by_id(s, workspace["workspace_id"])
        rows = s.execute(
            select(personal_weekly_objectives)
            .where(
                personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
                personal_weekly_objectives.c.week_start == target_week,
            )
            .order_by(
                personal_weekly_objectives.c.order_index.asc(),
                personal_weekly_objectives.c.created_at.asc(),
            )
        ).all()
        objective_ids = [row._mapping["objective_id"] for row in rows]
        subtasks_by_objective = _objective_subtasks_by_objective(s, objective_ids)
        comments_by_objective = _objective_comments_by_objective(s, objective_ids)
    objectives = [
        _objective_from_row(
            row._mapping, project_by_id, subtasks_by_objective, comments_by_objective
        )
        for row in rows
    ]
    # 회사 부여 항목은 이제 위 objectives에 '회사 부여' 목표로 들어오므로 별도 읽기전용
    # 목록(assigned_company_items)은 비운다(응답 필드는 하위호환 위해 유지).
    return WeeklyPlanResponse(
        week_start=target_week, objectives=objectives, assigned_company_items=[]
    )


def _assigned_company_items(
    user_id: UUID, node_id: str, target_week: date, email: str | None = None
) -> list[AssignedCompanyItem]:
    """회사 주간 '계획'(weekly_entries.plan_items)에서 이 사용자에게 부여된 항목을 모은다.

    계획(plan_items)만 반영하고 회고(retro_items)는 의도적으로 제외한다: 회고는 해당 주가
    끝난 뒤 '다음 주'에 지난 주 계획을 돌아보는 것이라, 같은-주차(week_start) projection
    모델과 맞지 않는다.

    멤버 매핑은 resolve_member_id로 푼다(team_members.user_id 직접 링크 + 로그인 이메일
    매칭 백필). 보드 bootstrap을 거치지 않고 주간플랜만 직접 열어도 동작한다. 회사 주차와
    개인 주간플랜 모두 일요일 시작이라 같은 주의 week_start(target_week)가 그대로 매칭된다."""
    from orthus.dashboard import resolve_member_id

    out: list[AssignedCompanyItem] = []
    member_id = resolve_member_id(node_id, user_id, email)
    if member_id is None:
        return out
    mid = str(member_id)
    with session() as s:
        weeks = [target_week]
        rows = s.execute(
            select(
                weekly_entries.c.project_id,
                weekly_entries.c.week_start,
                weekly_entries.c.plan_items,
            ).where(
                weekly_entries.c.node_id == node_id,
                weekly_entries.c.week_start.in_(weeks),
            )
        ).all()
        names = dict(
            s.execute(
                select(dashboard_projects.c.project_id, dashboard_projects.c.name).where(
                    dashboard_projects.c.node_id == node_id
                )
            ).all()
        )
    for r in rows:
        for it in r.plan_items or []:
            if str(it.get("assignee_member_id") or "") != mid:
                continue
            out.append(
                AssignedCompanyItem(
                    item_id=str(it.get("id") or ""),
                    text=str(it.get("text") or ""),
                    done=bool(it.get("done")),
                    project=names.get(r.project_id),
                    project_id=str(r.project_id),
                    week_start=r.week_start,
                )
            )
    return out


def sync_company_plan_to_objectives(
    user_id: UUID, node_id: str, target_week: date, email: str | None = None
) -> None:
    """회사 주간계획에서 이 사용자에게 부여된 plan 항목을 개인 주간 목표로 멱등 반영한다.

    각 담당 항목 → personal_weekly_objective(source_kind='company_plan',
    source_plan_item_id=원본 항목 id). 회사가 소유하는 건 제목·프로젝트뿐이고, 요일
    분배(day_allocations)·완료·메모는 개인이 소유해 재동기화에도 보존된다. 담당이 빠진
    항목은 stale 제거한다(회사쪽 미할당 = 목표 사라짐). retro_items는 제외(plan_items만).
    sync_team_events_to_board 패턴을 따른다.

    회사 노드 + 팀멤버로 연결된 계정이 아니면 no-op라 개인/일반 계정 동작은 불변이다.

    이건 pull 경로다(담당자가 자기 보드를 열 때 자기 것만 반영). 지정 즉시 담당자 보드에
    push하는 경로는 sync_company_plan_for_members(assigner 저장 시 호출).
    """
    from orthus.dashboard import resolve_member_id

    member_id = resolve_member_id(node_id, user_id, email)
    if member_id is None:
        return
    workspace = ensure_workspace(user_id, node_id)
    _materialize_company_plan(node_id, workspace["workspace_id"], str(member_id), target_week)


def sync_company_plan_for_members(
    node_id: str, target_week: date, member_ids: Iterable[str | UUID]
) -> None:
    """회사 주간계획 저장 시, 담당자로 지정된 각 팀원의 보드에 즉시 push한다(멱등).

    기존 sync_company_plan_to_objectives는 담당자가 자기 보드를 열 때만(pull) 돌아,
    지정 직후에는 그 사람 보드에 바로 안 들어간다("자동으로 안 들어감"). 이 함수는 계획을
    저장하는 assigner 경로(dashboard.upsert_weekly)에서 호출돼, 영향받은 담당자들 각각의
    workspace에 목표를 미리 반영한다. team_members.user_id가 연결된 멤버만 push하고
    (미연결 멤버는 최초 로그인+보드 오픈 시 pull이 백필), 반영 로직은 pull과 동일하다.

    write는 항상 그 담당자 본인 workspace의 company_plan 목표(제목·프로젝트만 회사 소유)에
    한정된다 — 개인 소유 요일분배/완료/메모는 손대지 않는다. 개인 보드 owner-only 경계와
    일치한다(pull이 이미 하던 write를, 담당자가 열길 기다리지 않고 지정 즉시 할 뿐).
    """
    ids: set[UUID] = set()
    for m in member_ids:
        if not m:
            continue
        try:
            ids.add(m if isinstance(m, UUID) else UUID(str(m)))
        except (ValueError, TypeError):
            continue
    if not ids:
        return
    with session() as s:
        rows = s.execute(
            select(team_members.c.member_id, team_members.c.user_id).where(
                team_members.c.node_id == node_id,
                team_members.c.member_id.in_(list(ids)),
                team_members.c.user_id.isnot(None),
            )
        ).all()
    for r in rows:
        workspace = ensure_workspace(r.user_id, node_id)
        _materialize_company_plan(node_id, workspace["workspace_id"], str(r.member_id), target_week)


def _materialize_company_plan(
    node_id: str, workspace_id: UUID, mid: str, target_week: date
) -> None:
    """회사 주간계획의 담당 plan 항목을 한 workspace의 개인 주간 목표로 멱등 반영한다.

    sync_company_plan_to_objectives(pull) / sync_company_plan_for_members(push) 공용 코어.
    materialize(제목·프로젝트=회사 소유) + stale 제거(담당 빠진 회사 부여 목표 삭제)만 하며,
    day_allocations/완료/메모/순서 등 개인 소유 필드는 ON CONFLICT에서 절대 덮지 않는다.

    (workspace, week) 단위 advisory xact lock으로 직렬화한다: keep-set(assigned)은 함수
    시작 시점 snapshot에서 얼지만 마지막 stale-delete는 자기 statement snapshot을 다시
    본다. push(assigner 저장) + pull(담당자 열람) + 같은 주 다른 프로젝트 동시 저장이
    겹치면, 뒤늦게 도는 materialize의 DELETE가 방금 다른 materialize가 넣은 정당한 목표를
    stale로 오인해 지울 수 있다(같은 '몇개 안 들어감' 재현). 락으로 같은 (workspace, week)
    materialize를 한 번에 하나만 돌려, 진 쪽이 최신 커밋 상태를 다시 읽게 한다.
    """
    with audit("personal_board.company_plan_objectives.sync") as span:
        with session() as s:
            # 0) 같은 (workspace, week) materialize 직렬화(위 docstring의 stale-delete race).
            #    xact 락이라 commit 시 자동 해제되고, 다른 멤버/주차와는 경합하지 않는다.
            s.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext(str(workspace_id)),
                        func.hashtext(target_week.isoformat()),
                    )
                )
            )
            # 1) 담당 plan 항목 수집(같은 주차, 멱등 키 있는 항목만)
            rows = s.execute(
                select(
                    weekly_entries.c.project_id,
                    weekly_entries.c.plan_items,
                ).where(
                    weekly_entries.c.node_id == node_id,
                    weekly_entries.c.week_start == target_week,
                )
            ).all()
            assigned: dict[str, tuple[str, bool, UUID | None]] = {}
            for r in rows:
                for it in r.plan_items or []:
                    if str(it.get("assignee_member_id") or "") != mid:
                        continue
                    item_id = str(it.get("id") or "")
                    if not item_id:
                        continue
                    assigned[item_id] = (
                        str(it.get("text") or ""),
                        bool(it.get("done")),
                        r.project_id,
                    )

            # 2) 회사 dashboard project_id → 개인 회사채널 project_id(있는 것만; 없으면 NULL)
            channel_by_company: dict[UUID, UUID] = {}
            pids = {pid for (_t, _d, pid) in assigned.values() if pid is not None}
            if pids:
                chans = s.execute(
                    select(
                        personal_board_projects.c.project_id,
                        personal_board_projects.c.company_project_id,
                    ).where(
                        personal_board_projects.c.workspace_id == workspace_id,
                        personal_board_projects.c.kind == "company",
                        personal_board_projects.c.archived_at.is_(None),
                        personal_board_projects.c.company_project_id.in_(pids),
                    )
                ).all()
                channel_by_company = {c.company_project_id: c.project_id for c in chans}

            # 3) 신규 목표용 order_index 시작값(기존 max 뒤에 붙인다)
            current_max = s.execute(
                select(func.max(personal_weekly_objectives.c.order_index)).where(
                    personal_weekly_objectives.c.workspace_id == workspace_id,
                    personal_weekly_objectives.c.week_start == target_week,
                )
            ).scalar()
            next_order = 0 if current_max is None else int(current_max) + 1

            # 4) upsert: 제목·프로젝트는 회사 소유(동기화). 완료·day_allocations·메모·순서는
            #    개인 소유라 ON CONFLICT에서 절대 덮지 않는다(요일 분배가 보존되는 지점).
            for item_id, (text, done, dashboard_pid) in assigned.items():
                channel_id = channel_by_company.get(dashboard_pid) if dashboard_pid else None
                s.execute(
                    pg_insert(personal_weekly_objectives)
                    .values(
                        objective_id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        week_start=target_week,
                        title=text,
                        project_id=channel_id,
                        completed=done,
                        day_allocations=[],
                        order_index=next_order,
                        source_kind="company_plan",
                        source_plan_item_id=item_id,
                    )
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "source_plan_item_id"],
                        index_where=personal_weekly_objectives.c.source_plan_item_id.isnot(None),
                        set_={
                            "title": text,
                            "project_id": channel_id,
                            # 항목이 다른 주차로 옮겨졌으면 목표도 따라간다. week_start를 안
                            # 옮기면 옛 주차에 남았다가 그 주차 stale-delete로 사라진다(양쪽
                            # 실종). unique key는 (workspace, source_item)라 주차 무관 1:1.
                            "week_start": target_week,
                            "updated_at": func.now(),
                        },
                    )
                )
                next_order += 1

            # 5) stale 제거: 더 이상 담당이 아닌 회사 부여 목표 삭제(같은 주차). manual·다른
            #    주차 목표는 건드리지 않는다. 자식(subtask/comment)은 FK ON DELETE CASCADE.
            stale_where = [
                personal_weekly_objectives.c.workspace_id == workspace_id,
                personal_weekly_objectives.c.week_start == target_week,
                personal_weekly_objectives.c.source_kind == "company_plan",
            ]
            if assigned:
                stale_where.append(
                    personal_weekly_objectives.c.source_plan_item_id.notin_(list(assigned.keys()))
                )
            s.execute(personal_weekly_objectives.delete().where(*stale_where))
            s.commit()
        span.set_output({"assigned": len(assigned)})


def _no_objective_progress() -> dict:
    return {"has_objective": False, "subtasks_total": 0, "subtasks_done": 0, "completed": False}


def assignee_progress_for_plan_items(
    node_id: str, items: list[tuple[str, str]], week_start: date
) -> dict[str, dict]:
    """회사 주간계획 항목별 담당자 진행률(집계만) — 회사 plans 보드 read-only 표시용.

    items = [(plan_item_id, assignee_member_id), ...]. 각 항목의 담당자가 materialize한
    개인 주간 목표(source_plan_item_id == plan_item_id)의 서브태스크 완료수/전체수 + 완료
    여부를 돌려준다. **집계(카운트·완료여부)만 노출하고 서브태스크 제목·메모 등 사적 내용은
    절대 내보내지 않는다** — 개인 보드 owner-only 경계의 진행률-only carve-out(회사가 시킨
    일의 진행 공유는 승인됨, 사적 breakdown은 보호).

    반환: {item_id: {has_objective, subtasks_total, subtasks_done, completed}}.
    담당자가 아직 주간플랜을 안 열어 목표가 없으면 has_objective=False.
    """
    pairs = [(i, m) for (i, m) in items if i and m]
    if not pairs:
        return {}
    member_uuids: dict[str, UUID] = {}
    for _i, m in pairs:
        if m not in member_uuids:
            try:
                member_uuids[m] = UUID(m)
            except (ValueError, TypeError):
                continue
    if not member_uuids:
        return {i: _no_objective_progress() for (i, _m) in pairs}

    with session() as s:
        # member_id -> user_id. 담당자가 주간플랜을 한 번이라도 열었으면 user_id가 백필돼
        # 있다(목표 자체도 그때 생긴다). user_id 없으면 목표도 없음.
        mrows = s.execute(
            select(team_members.c.member_id, team_members.c.user_id).where(
                team_members.c.node_id == node_id,
                team_members.c.member_id.in_(list(member_uuids.values())),
                team_members.c.user_id.isnot(None),
            )
        ).all()
        user_by_member = {str(r.member_id): r.user_id for r in mrows}
        ws_by_user: dict[UUID, UUID] = {}
        obj_by_key: dict[tuple, tuple] = {}
        counts_by_obj: dict[UUID, tuple[int, int]] = {}
        if user_by_member:
            wrows = s.execute(
                select(
                    personal_board_workspaces.c.user_id,
                    personal_board_workspaces.c.workspace_id,
                ).where(
                    personal_board_workspaces.c.node_id == node_id,
                    personal_board_workspaces.c.user_id.in_(list(set(user_by_member.values()))),
                )
            ).all()
            ws_by_user = {r.user_id: r.workspace_id for r in wrows}
            ws_ids = list(set(ws_by_user.values()))
            item_ids = list({i for (i, _m) in pairs})
            if ws_ids:
                orows = s.execute(
                    select(
                        personal_weekly_objectives.c.objective_id,
                        personal_weekly_objectives.c.workspace_id,
                        personal_weekly_objectives.c.source_plan_item_id,
                        personal_weekly_objectives.c.completed,
                    ).where(
                        personal_weekly_objectives.c.workspace_id.in_(ws_ids),
                        personal_weekly_objectives.c.week_start == week_start,
                        personal_weekly_objectives.c.source_kind == "company_plan",
                        personal_weekly_objectives.c.source_plan_item_id.in_(item_ids),
                    )
                ).all()
                for r in orows:
                    obj_by_key[(r.workspace_id, r.source_plan_item_id)] = (
                        r.objective_id,
                        r.completed,
                    )
            obj_ids = [oid for (oid, _c) in obj_by_key.values()]
            if obj_ids:
                crows = s.execute(
                    select(
                        personal_objective_subtasks.c.objective_id,
                        func.count().label("total"),
                        func.count().filter(personal_objective_subtasks.c.completed).label("done"),
                    )
                    .where(personal_objective_subtasks.c.objective_id.in_(obj_ids))
                    .group_by(personal_objective_subtasks.c.objective_id)
                ).all()
                for r in crows:
                    counts_by_obj[r.objective_id] = (int(r.total or 0), int(r.done or 0))

    out: dict[str, dict] = {}
    for item_id, member in pairs:
        user = user_by_member.get(member)
        wsid = ws_by_user.get(user) if user else None
        obj = obj_by_key.get((wsid, item_id)) if wsid else None
        if obj is None:
            out[item_id] = _no_objective_progress()
            continue
        objective_id, completed = obj
        total, done = counts_by_obj.get(objective_id, (0, 0))
        out[item_id] = {
            "has_objective": True,
            "subtasks_total": total,
            "subtasks_done": done,
            "completed": bool(completed),
        }
    return out


def create_objective(user_id: UUID, node_id: str, body: ObjectiveCreate) -> PersonalWeeklyObjective:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    workspace = ensure_workspace(user_id, node_id)
    _validate_project(workspace["workspace_id"], body.project_id)
    objective_id = uuid.uuid4()
    with audit("personal_board.objective.create") as span:
        with session() as s:
            current = s.execute(
                select(func.max(personal_weekly_objectives.c.order_index)).where(
                    personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
                    personal_weekly_objectives.c.week_start == body.week_start,
                )
            ).scalar()
            order_index = 0 if current is None else int(current) + 1
            s.execute(
                insert(personal_weekly_objectives).values(
                    objective_id=objective_id,
                    workspace_id=workspace["workspace_id"],
                    week_start=body.week_start,
                    title=title,
                    project_id=body.project_id,
                    day_allocations=[],
                    order_index=order_index,
                )
            )
            s.commit()
        span.set_output(
            {"objective_id": str(objective_id), "week_start": body.week_start.isoformat()}
        )
    return _load_objective(workspace["workspace_id"], objective_id)


def update_objective(
    user_id: UUID, node_id: str, objective_id: UUID, body: ObjectivePatch
) -> PersonalWeeklyObjective:
    workspace = ensure_workspace(user_id, node_id)
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    # 회사 부여 목표(source_kind='company_plan')는 제목·프로젝트가 회사 소유라 개인이
    # 못 바꾼다. 요일 분배(day_allocations)·완료·메모·순서만 허용한다.
    if "title" in values or "project_id" in values:
        with session() as s:
            src_kind = s.execute(
                select(personal_weekly_objectives.c.source_kind).where(
                    personal_weekly_objectives.c.objective_id == objective_id,
                    personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
                )
            ).scalar()
        if src_kind == "company_plan":
            raise ValueError("회사 부여 목표는 제목·프로젝트를 바꿀 수 없어요")
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise ValueError("title required")
    if "project_id" in values:
        _validate_project(workspace["workspace_id"], values["project_id"])
    if "day_allocations" in values and values["day_allocations"] is None:
        values["day_allocations"] = []
    if "order_index" in values:
        if values["order_index"] is None:
            del values["order_index"]
        elif values["order_index"] < 0:
            raise ValueError("invalid order_index")
    if values:
        values["updated_at"] = datetime.now(UTC)
        with session() as s:
            result = s.execute(
                update(personal_weekly_objectives)
                .where(
                    personal_weekly_objectives.c.objective_id == objective_id,
                    personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
                )
                .values(**values)
                .returning(personal_weekly_objectives.c.objective_id)
            ).first()
            s.commit()
        if result is None:
            raise LookupError("objective not found")
    return _load_objective(workspace["workspace_id"], objective_id)


def move_objective_day(
    user_id: UUID, node_id: str, objective_id: UUID, body: ObjectiveMoveDay
) -> PersonalWeeklyObjective:
    if body.from_weekday == body.to_weekday:
        raise ValueError("from and to weekday must differ")
    workspace = ensure_workspace(user_id, node_id)
    workspace_id = workspace["workspace_id"]
    with audit("personal_board.objective.move_day") as span:
        with session() as s:
            row = s.execute(
                select(personal_weekly_objectives.c.day_allocations).where(
                    personal_weekly_objectives.c.objective_id == objective_id,
                    personal_weekly_objectives.c.workspace_id == workspace_id,
                )
            ).first()
            if row is None:
                raise LookupError("objective not found")
            day_allocations = list(row._mapping["day_allocations"] or [])
            source = next((a for a in day_allocations if a["weekday"] == body.from_weekday), None)
            if source is None:
                raise ValueError("no allocation on the source day")
            target = next((a for a in day_allocations if a["weekday"] == body.to_weekday), None)

            source_minutes = source.get("minutes")
            target_minutes = (target or {}).get("minutes")
            if source_minutes is None and target_minutes is None:
                merged_minutes = None
            else:
                merged_minutes = (source_minutes or 0) + (target_minutes or 0)
            merged_note = (
                "\n".join(n for n in [(target or {}).get("note"), source.get("note")] if n) or None
            )
            new_order = (
                target["order"]
                if target and target.get("order") is not None
                else source.get("order")
            )

            new_day_allocations = [
                a
                for a in day_allocations
                if a["weekday"] not in (body.from_weekday, body.to_weekday)
            ] + [
                {
                    "weekday": body.to_weekday,
                    "minutes": merged_minutes,
                    "order": new_order,
                    "note": merged_note,
                }
            ]
            new_day_allocations.sort(key=lambda a: a["weekday"])

            s.execute(
                update(personal_objective_comments)
                .where(
                    personal_objective_comments.c.objective_id == objective_id,
                    personal_objective_comments.c.weekday == body.from_weekday,
                )
                .values(weekday=body.to_weekday)
            )

            base = s.execute(
                select(func.max(personal_objective_subtasks.c.order_index)).where(
                    personal_objective_subtasks.c.objective_id == objective_id,
                    personal_objective_subtasks.c.weekday == body.to_weekday,
                )
            ).scalar()
            base = -1 if base is None else int(base)
            s.execute(
                update(personal_objective_subtasks)
                .where(
                    personal_objective_subtasks.c.objective_id == objective_id,
                    personal_objective_subtasks.c.weekday == body.from_weekday,
                )
                .values(
                    weekday=body.to_weekday,
                    order_index=personal_objective_subtasks.c.order_index + (base + 1),
                )
            )

            s.execute(
                update(personal_weekly_objectives)
                .where(
                    personal_weekly_objectives.c.objective_id == objective_id,
                    personal_weekly_objectives.c.workspace_id == workspace_id,
                )
                .values(day_allocations=new_day_allocations, updated_at=func.now())
            )
            s.commit()
        span.set_output(
            {
                "objective_id": str(objective_id),
                "from_weekday": body.from_weekday,
                "to_weekday": body.to_weekday,
            }
        )
    return _load_objective(workspace_id, objective_id)


def delete_objective(user_id: UUID, node_id: str, objective_id: UUID) -> None:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        # 회사 부여 목표는 개인이 직접 삭제할 수 없다 — 회사 주간계획에서 담당이 빠지면
        # 다음 동기화에서 stale 제거된다.
        src_kind = s.execute(
            select(personal_weekly_objectives.c.source_kind).where(
                personal_weekly_objectives.c.objective_id == objective_id,
                personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
            )
        ).scalar()
        if src_kind == "company_plan":
            raise ValueError(
                "회사 부여 목표는 삭제할 수 없어요 — 회사 주간계획에서 담당을 해제하면 사라져요"
            )
        result = s.execute(
            personal_weekly_objectives.delete().where(
                personal_weekly_objectives.c.objective_id == objective_id,
                personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("objective not found")


def _assert_objective_in_workspace(workspace_id: UUID, objective_id: UUID) -> None:
    with session() as s:
        row = s.execute(
            select(personal_weekly_objectives.c.objective_id).where(
                personal_weekly_objectives.c.objective_id == objective_id,
                personal_weekly_objectives.c.workspace_id == workspace_id,
            )
        ).first()
    if row is None:
        raise LookupError("objective not found")


def _recompute_objective_completion(s, objective_id: UUID) -> None:
    """서브태스크 진행으로 목표 완료를 파생한다(auto-complete).

    서브태스크가 있으면 완료 = (전체>0 AND 완료==전체) — 다 끝나면 목표 자동 완료,
    하나라도 미완이면 자동 재오픈. 서브태스크가 없으면 수동 완료를 보존(건드리지 않음).
    회사 부여 목표·수동 목표 모두 동일하게 적용된다. 호출자 트랜잭션 안에서 실행."""
    counts = s.execute(
        select(
            func.count().label("total"),
            func.count().filter(personal_objective_subtasks.c.completed).label("done"),
        ).where(personal_objective_subtasks.c.objective_id == objective_id)
    ).first()
    total = int(counts.total or 0)
    if total == 0:
        return  # 서브태스크 없으면 수동 완료를 보존
    done = int(counts.done or 0)
    s.execute(
        update(personal_weekly_objectives)
        .where(personal_weekly_objectives.c.objective_id == objective_id)
        .values(completed=(done == total), updated_at=func.now())
    )


def create_objective_subtask(
    user_id: UUID, node_id: str, objective_id: UUID, body: ObjectiveSubtaskCreate
) -> ObjectiveSubtask:
    title = body.title.strip()
    if not title:
        raise ValueError("title required")
    workspace = ensure_workspace(user_id, node_id)
    _assert_objective_in_workspace(workspace["workspace_id"], objective_id)
    subtask_id = uuid.uuid4()
    with audit("personal_board.objective.subtask.create") as span:
        with session() as s:
            current = s.execute(
                select(func.max(personal_objective_subtasks.c.order_index)).where(
                    personal_objective_subtasks.c.objective_id == objective_id,
                    personal_objective_subtasks.c.weekday == body.weekday,
                )
            ).scalar()
            order_index = 0 if current is None else int(current) + 1
            s.execute(
                insert(personal_objective_subtasks).values(
                    subtask_id=subtask_id,
                    objective_id=objective_id,
                    title=title,
                    order_index=order_index,
                    weekday=body.weekday,
                )
            )
            _recompute_objective_completion(s, objective_id)
            s.commit()
        span.set_output({"subtask_id": str(subtask_id), "objective_id": str(objective_id)})
    return _load_objective_subtask(subtask_id)


def update_objective_subtask(
    user_id: UUID, node_id: str, subtask_id: UUID, body: ObjectiveSubtaskPatch
) -> ObjectiveSubtask:
    workspace = ensure_workspace(user_id, node_id)
    with session() as s:
        existing = s.execute(
            select(
                personal_objective_subtasks.c.subtask_id,
                personal_objective_subtasks.c.objective_id,
            )
            .select_from(
                personal_objective_subtasks.join(
                    personal_weekly_objectives,
                    personal_objective_subtasks.c.objective_id
                    == personal_weekly_objectives.c.objective_id,
                )
            )
            .where(
                personal_objective_subtasks.c.subtask_id == subtask_id,
                personal_weekly_objectives.c.workspace_id == workspace["workspace_id"],
            )
        ).first()
    if existing is None:
        raise LookupError("subtask not found")

    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise ValueError("title required")
    if "order_index" in values:
        if values["order_index"] is None:
            del values["order_index"]
        elif values["order_index"] < 0:
            raise ValueError("invalid order_index")
    if values:
        values["updated_at"] = datetime.now(UTC)
        with session() as s:
            s.execute(
                update(personal_objective_subtasks)
                .where(personal_objective_subtasks.c.subtask_id == subtask_id)
                .values(**values)
            )
            # 완료 토글이 목표 완료로 자동 반영되게(auto-complete).
            _recompute_objective_completion(s, existing.objective_id)
            s.commit()
    return _load_objective_subtask(subtask_id)


def create_objective_comment(
    user_id: UUID, node_id: str, objective_id: UUID, body: ObjectiveCommentCreate
) -> ObjectiveComment:
    text = body.body.strip()
    if not text:
        raise ValueError("body required")
    workspace = ensure_workspace(user_id, node_id)
    _assert_objective_in_workspace(workspace["workspace_id"], objective_id)
    comment_id = uuid.uuid4()
    with audit("personal_board.objective.comment.create") as span:
        with session() as s:
            s.execute(
                insert(personal_objective_comments).values(
                    comment_id=comment_id,
                    objective_id=objective_id,
                    user_id=user_id,
                    body=text,
                    weekday=body.weekday,
                )
            )
            s.commit()
        span.set_output({"comment_id": str(comment_id), "objective_id": str(objective_id)})
    return _load_objective_comment(comment_id)


def _load_objective(workspace_id: UUID, objective_id: UUID) -> PersonalWeeklyObjective:
    with session() as s:
        row = s.execute(
            select(personal_weekly_objectives).where(
                personal_weekly_objectives.c.objective_id == objective_id,
                personal_weekly_objectives.c.workspace_id == workspace_id,
            )
        ).first()
        if row is None:
            raise LookupError("objective not found")
        project_by_id = _projects_by_id(s, workspace_id)
        subtasks_by_objective = _objective_subtasks_by_objective(s, [objective_id])
        comments_by_objective = _objective_comments_by_objective(s, [objective_id])
    return _objective_from_row(
        row._mapping, project_by_id, subtasks_by_objective, comments_by_objective
    )


def weekly_review(
    user_id: UUID, node_id: str, week_start: date | None = None
) -> WeeklyReviewResponse:
    workspace = ensure_workspace(user_id, node_id)
    target_week = week_start or _week_sunday(selected_date_or_today(None, workspace["timezone"]))
    week_days = [target_week + timedelta(days=i) for i in range(7)]
    with session() as s:
        project_by_id = _projects_by_id(s, workspace["workspace_id"])
        task_rows = s.execute(
            select(personal_board_tasks)
            .where(
                personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                personal_board_tasks.c.status != "archived",
                personal_board_tasks.c.scheduled_date.in_(week_days),
            )
            .order_by(
                personal_board_tasks.c.scheduled_date.asc(),
                personal_board_tasks.c.order_index.asc(),
                personal_board_tasks.c.created_at.asc(),
            )
        ).all()
        done_ids = [
            row._mapping["task_id"] for row in task_rows if row._mapping["status"] == "done"
        ]
        subtask_rows = (
            s.execute(
                select(personal_board_subtasks)
                .where(personal_board_subtasks.c.task_id.in_(done_ids))
                .order_by(personal_board_subtasks.c.order_index.asc())
            ).all()
            if done_ids
            else []
        )

    subtasks_by_task: dict[UUID, list[PersonalBoardSubtask]] = {}
    for row in subtask_rows:
        item = _subtask_from_row(row._mapping)
        subtasks_by_task.setdefault(item.task_id, []).append(item)

    done_by_day: dict[date, list[PersonalBoardTask]] = {day: [] for day in week_days}
    open_count_by_day: dict[date, int] = {day: 0 for day in week_days}
    done_count_by_project: dict[UUID | None, int] = {}
    total_done = 0
    total_open = 0
    for row in task_rows:
        mapping = row._mapping
        day = mapping["scheduled_date"]
        if mapping["status"] == "done":
            task = _task_from_row(mapping, project_by_id, subtasks_by_task)
            done_by_day[day].append(task)
            total_done += 1
            done_count_by_project[mapping["project_id"]] = (
                done_count_by_project.get(mapping["project_id"], 0) + 1
            )
        elif mapping["status"] == "open":
            open_count_by_day[day] += 1
            total_open += 1

    daily = [
        WeeklyReviewDay(
            date=day,
            weekday=(day.weekday() + 1) % 7,
            done_count=len(done_by_day[day]),
            open_count=open_count_by_day[day],
            done_tasks=done_by_day[day],
        )
        for day in week_days
    ]
    by_project = [
        WeeklyReviewProject(
            project_id=project_id,
            project_name=project_by_id[project_id].name if project_id in project_by_id else None,
            color=project_by_id[project_id].color if project_id in project_by_id else None,
            done_count=count,
        )
        for project_id, count in sorted(
            done_count_by_project.items(), key=lambda kv: (-kv[1], str(kv[0]))
        )
    ]
    return WeeklyReviewResponse(
        week_start=target_week,
        total_done=total_done,
        total_open=total_open,
        daily=daily,
        by_project=by_project,
    )


def list_days(
    user_id: UUID, node_id: str, start: date, days: int, *, email: str | None = None
) -> DaysResponse:
    days = max(1, min(days, 21))
    workspace = ensure_workspace(user_id, node_id)
    timezone = ZoneInfo(workspace["timezone"])
    day_list = [start + timedelta(days=i) for i in range(days)]
    sync_team_events_to_board(user_id, node_id, day_list, email=email)
    with session() as s:
        project_by_id = _projects_by_id(s, workspace["workspace_id"])
        day_models = _assemble_days(s, workspace["workspace_id"], day_list, timezone, project_by_id)
    return DaysResponse(days=day_models)


def duplicate_task(user_id: UUID, node_id: str, task_id: UUID) -> PersonalBoardTask:
    workspace = ensure_workspace(user_id, node_id)
    new_task_id = uuid.uuid4()
    with audit("personal_board.task.duplicate") as span:
        with session() as s:
            source = s.execute(
                select(personal_board_tasks).where(
                    personal_board_tasks.c.task_id == task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if source is None:
                raise LookupError("task not found")
            src = source._mapping
            order_index = _next_task_order(
                workspace["workspace_id"], src["scheduled_date"], src["backlog_bucket_id"]
            )
            s.execute(
                insert(personal_board_tasks).values(
                    task_id=new_task_id,
                    workspace_id=workspace["workspace_id"],
                    user_id=user_id,
                    project_id=src["project_id"],
                    backlog_bucket_id=src["backlog_bucket_id"],
                    title=src["title"],
                    status="open",
                    priority=src["priority"],
                    scheduled_date=src["scheduled_date"],
                    scheduled_time=src["scheduled_time"],
                    scheduled_end_time=src["scheduled_end_time"],
                    no_return=src["no_return"],
                    due_date=src["due_date"],
                    due_time=src["due_time"],
                    order_index=order_index,
                    source_kind=src["source_kind"],
                    source_label=src["source_label"],
                    scope=src["scope"],
                    company_project_id=src["company_project_id"],
                    note=src["note"],
                )
            )
            subtask_rows = s.execute(
                select(personal_board_subtasks)
                .where(personal_board_subtasks.c.task_id == task_id)
                .order_by(personal_board_subtasks.c.order_index.asc())
            ).all()
            for row in subtask_rows:
                sub = row._mapping
                s.execute(
                    insert(personal_board_subtasks).values(
                        subtask_id=uuid.uuid4(),
                        task_id=new_task_id,
                        title=sub["title"],
                        completed=False,
                        order_index=sub["order_index"],
                    )
                )
            s.commit()
        span.set_output({"task_id": str(new_task_id), "source_task_id": str(task_id)})
    return _load_task(new_task_id)


def delete_task(user_id: UUID, node_id: str, task_id: UUID) -> None:
    workspace = ensure_workspace(user_id, node_id)
    with audit("personal_board.task.delete") as span:
        with session() as s:
            exists = s.execute(
                select(
                    personal_board_tasks.c.task_id,
                    personal_board_tasks.c.team_event_id,
                ).where(
                    personal_board_tasks.c.task_id == task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                    personal_board_tasks.c.user_id == user_id,
                )
            ).first()
            if exists is None:
                raise LookupError("task not found")
            if exists.team_event_id is not None:
                s.execute(
                    delete(team_calendar_events).where(
                        team_calendar_events.c.event_id == exists.team_event_id,
                        team_calendar_events.c.node_id == node_id,
                    )
                )
            s.execute(
                personal_board_subtasks.delete().where(personal_board_subtasks.c.task_id == task_id)
            )
            s.execute(
                personal_board_tasks.delete().where(
                    personal_board_tasks.c.task_id == task_id,
                    personal_board_tasks.c.workspace_id == workspace["workspace_id"],
                )
            )
            s.commit()
        span.set_output({"task_id": str(task_id)})


def sync_day_to_wiki(user_id: UUID, node_id: str, day: date) -> UUID:
    workspace = ensure_workspace(user_id, node_id)
    bootstrap_data = _load_bootstrap(user_id, workspace, day)
    target = next((item for item in bootstrap_data.days if item.date == day), None)
    if target is None:
        target = PersonalBoardDay(date=day, tasks=[], fixed_events=[], notes=[])
    wiki_markdown = _daily_wiki_markdown(target)
    source_id = f"personal_board:daily:{node_id}:{user_id}:{day.isoformat()}"
    doc = InternalDocument(
        title=f"개인 워크스페이스 {day.isoformat()}",
        markdown=wiki_markdown or f"# 개인 워크스페이스 {day.isoformat()}\n",
        block_json=[],
        source="personal_board",
        source_external_id=source_id,
        source_canonical_id=source_id,
        project=BOARD_PROJECT,
        # 할 일/일정은 live 보드(ticket_list·personal_schedule_list)가 SoR라 위키에
        # distill하지 않는다 — 특이사항 노트가 있을 때만 claim으로 author한다.
        wiki_authoring=wiki_markdown is not None,
    )
    try:
        doc_id, _changed = upsert_source_document(user_id, doc, scope="personal")
    except Exception as exc:
        logger.warning(
            "personal_board wiki authoring failed; storing daily source document only",
            exc_info=True,
        )
        with audit("personal_board.wiki_sync.degraded") as span:
            span.set_output(
                {
                    "node_id": node_id,
                    "day": day.isoformat(),
                    "error_class": type(exc).__name__,
                }
            )
        doc_id, _changed = upsert_source_document(
            user_id, doc, scope="personal", defer_authoring=True
        )
    return doc_id


def _load_bootstrap(user_id: UUID, workspace: dict, selected_date: date) -> PersonalBoardBootstrap:
    workspace_id = workspace["workspace_id"]
    days = [selected_date + timedelta(days=i) for i in range(3)]
    timezone = ZoneInfo(workspace["timezone"])
    with session() as s:
        project_rows = s.execute(
            select(*_PROJECT_COLS).where(
                personal_board_projects.c.workspace_id == workspace_id,
                personal_board_projects.c.archived_at.is_(None),
            )
        ).all()
        folder_rows = s.execute(
            select(
                personal_board_folders.c.folder_id,
                personal_board_folders.c.name,
                personal_board_folders.c.kind,
                personal_board_folders.c.order_index,
            )
            .where(personal_board_folders.c.workspace_id == workspace_id)
            .order_by(personal_board_folders.c.order_index.asc())
        ).all()
        bucket_rows = s.execute(
            select(
                personal_board_backlog_buckets.c.bucket_id,
                personal_board_backlog_buckets.c.key,
                personal_board_backlog_buckets.c.label,
                personal_board_backlog_buckets.c.badge,
                personal_board_backlog_buckets.c.color,
                personal_board_backlog_buckets.c.order_index,
                personal_board_backlog_buckets.c.collapsed,
            )
            .where(personal_board_backlog_buckets.c.workspace_id == workspace_id)
            .order_by(personal_board_backlog_buckets.c.order_index.asc())
        ).all()
        backlog_task_rows = s.execute(
            select(personal_board_tasks)
            .where(
                personal_board_tasks.c.workspace_id == workspace_id,
                personal_board_tasks.c.status != "archived",
                personal_board_tasks.c.backlog_bucket_id.is_not(None),
            )
            .order_by(
                personal_board_tasks.c.order_index.asc(),
                personal_board_tasks.c.created_at.asc(),
            )
        ).all()
        integration_rows = s.execute(
            select(
                personal_board_integrations.c.integration_id,
                personal_board_integrations.c.kind,
                personal_board_integrations.c.label,
                personal_board_integrations.c.enabled,
                personal_board_integrations.c.has_notification,
                personal_board_integrations.c.order_index,
            )
            .where(personal_board_integrations.c.workspace_id == workspace_id)
            .order_by(personal_board_integrations.c.order_index.asc())
        ).all()
        pref = s.execute(
            select(personal_board_preferences).where(
                personal_board_preferences.c.workspace_id == workspace_id,
                personal_board_preferences.c.user_id == user_id,
            )
        ).first()

        projects = [_project_from_row(r) for r in project_rows]
        project_by_id = {p.project_id: p for p in projects}
        day_models = _assemble_days(s, workspace_id, days, timezone, project_by_id)

    folders = [_folder_from_row(row._mapping) for row in folder_rows]
    backlog_subtasks_by_task: dict[UUID, list[PersonalBoardSubtask]] = {}
    backlog_tasks = [
        _task_from_row(row._mapping, project_by_id, backlog_subtasks_by_task)
        for row in backlog_task_rows
    ]
    buckets = [
        PersonalBoardBacklogBucket(
            bucket_id=r.bucket_id,
            key=r.key,
            label=r.label,
            badge=r.badge,
            color=r.color,
            order_index=r.order_index,
            collapsed=r.collapsed,
            tasks=[task for task in backlog_tasks if task.backlog_bucket_id == r.bucket_id],
        )
        for r in bucket_rows
    ]
    integrations = [_integration_from_row(row._mapping) for row in integration_rows]

    return PersonalBoardBootstrap(
        node_id=workspace.get("node_id", ""),
        workspace_id=workspace_id,
        workspace_name=workspace["name"],
        timezone=workspace["timezone"],
        selected_date=selected_date,
        folders=folders,
        projects=projects,
        days=day_models,
        backlog_buckets=buckets,
        integrations=integrations,
        preferences=PersonalBoardPreferences(
            selected_date=pref._mapping["selected_date"] if pref else selected_date,
            right_panel=pref._mapping["right_panel"] if pref else "backlog",
            active_integration=pref._mapping["active_integration"] if pref else None,
            filter_mode=pref._mapping["filter_mode"] if pref else "all",
            sort_mode=pref._mapping["sort_mode"] if pref else "manual",
        ),
    )


def _assemble_days(
    s,
    workspace_id: UUID,
    days: list[date],
    timezone: ZoneInfo,
    project_by_id: dict[UUID, PersonalBoardProject],
) -> list[PersonalBoardDay]:
    """Build PersonalBoardDay rows for a contiguous date range.

    Shared per-day assembly used by both bootstrap() and list_days(). Queries
    day-scheduled tasks (+ subtasks), fixed events overlapping the range, and
    notes on those days, then groups them by local day."""
    range_start = datetime.combine(days[0], time.min, tzinfo=timezone)
    range_end = datetime.combine(days[-1] + timedelta(days=1), time.min, tzinfo=timezone)
    task_rows = s.execute(
        select(personal_board_tasks)
        .where(
            personal_board_tasks.c.workspace_id == workspace_id,
            personal_board_tasks.c.status != "archived",
            personal_board_tasks.c.scheduled_date.in_(days),
        )
        .order_by(
            personal_board_tasks.c.scheduled_date.asc().nulls_last(),
            personal_board_tasks.c.order_index.asc(),
            personal_board_tasks.c.created_at.asc(),
        )
    ).all()
    task_ids = [row._mapping["task_id"] for row in task_rows]
    subtask_rows = (
        s.execute(
            select(personal_board_subtasks)
            .where(personal_board_subtasks.c.task_id.in_(task_ids))
            .order_by(personal_board_subtasks.c.order_index.asc())
        ).all()
        if task_ids
        else []
    )
    event_rows = s.execute(
        select(personal_board_fixed_events)
        .where(
            personal_board_fixed_events.c.workspace_id == workspace_id,
            personal_board_fixed_events.c.starts_at < range_end,
            personal_board_fixed_events.c.ends_at > range_start,
        )
        .order_by(personal_board_fixed_events.c.starts_at.asc())
    ).all()
    note_rows = s.execute(
        select(personal_board_notes)
        .where(
            personal_board_notes.c.workspace_id == workspace_id,
            personal_board_notes.c.note_date.in_(days),
        )
        .order_by(personal_board_notes.c.note_date.asc(), personal_board_notes.c.order_index.asc())
    ).all()

    subtasks_by_task: dict[UUID, list[PersonalBoardSubtask]] = {}
    for row in subtask_rows:
        item = _subtask_from_row(row._mapping)
        subtasks_by_task.setdefault(item.task_id, []).append(item)

    tasks = [_task_from_row(row._mapping, project_by_id, subtasks_by_task) for row in task_rows]
    tasks_by_day = {day: [task for task in tasks if task.scheduled_date == day] for day in days}
    events = [_event_from_row(row._mapping, project_by_id) for row in event_rows]
    events_by_day: dict[date, list[PersonalBoardFixedEvent]] = {day: [] for day in days}
    for event in events:
        for event_day in event_local_days(event.starts_at, event.ends_at, timezone):
            if event_day in events_by_day:
                events_by_day[event_day].append(event)
    notes = [_note_from_row(row._mapping) for row in note_rows]
    notes_by_day = {day: [note for note in notes if note.note_date == day] for day in days}

    return [
        PersonalBoardDay(
            date=day,
            tasks=tasks_by_day[day],
            fixed_events=events_by_day[day],
            notes=notes_by_day[day],
        )
        for day in days
    ]


def _load_task(task_id: UUID) -> PersonalBoardTask:
    with session() as s:
        row = s.execute(
            select(personal_board_tasks).where(personal_board_tasks.c.task_id == task_id)
        ).one()
        project_row = None
        if row._mapping["project_id"] is not None:
            project_row = s.execute(
                select(*_PROJECT_COLS).where(
                    personal_board_projects.c.project_id == row._mapping["project_id"],
                    personal_board_projects.c.archived_at.is_(None),
                )
            ).first()
        subtask_rows = s.execute(
            select(personal_board_subtasks)
            .where(personal_board_subtasks.c.task_id == task_id)
            .order_by(personal_board_subtasks.c.order_index.asc())
        ).all()
    subtasks_by_task = {task_id: [_subtask_from_row(item._mapping) for item in subtask_rows]}
    projects = {}
    if project_row is not None:
        project = _project_from_row(project_row)
        projects[project.project_id] = project
    return _task_from_row(row._mapping, projects, subtasks_by_task)


def _load_event(event_id: UUID) -> PersonalBoardFixedEvent:
    with session() as s:
        row = s.execute(
            select(personal_board_fixed_events).where(
                personal_board_fixed_events.c.event_id == event_id
            )
        ).one()
        project_row = None
        if row._mapping["project_id"] is not None:
            project_row = s.execute(
                select(*_PROJECT_COLS).where(
                    personal_board_projects.c.project_id == row._mapping["project_id"],
                    personal_board_projects.c.archived_at.is_(None),
                )
            ).first()
    projects = {}
    if project_row is not None:
        project = _project_from_row(project_row)
        projects[project.project_id] = project
    return _event_from_row(row._mapping, projects)


def _load_note(note_id: UUID) -> PersonalBoardNote:
    with session() as s:
        row = s.execute(
            select(personal_board_notes).where(personal_board_notes.c.note_id == note_id)
        ).one()
    return _note_from_row(row._mapping)


def _load_subtask(subtask_id: UUID) -> PersonalBoardSubtask:
    with session() as s:
        row = s.execute(
            select(personal_board_subtasks).where(
                personal_board_subtasks.c.subtask_id == subtask_id
            )
        ).one()
    return _subtask_from_row(row._mapping)


def _load_objective_subtask(subtask_id: UUID) -> ObjectiveSubtask:
    with session() as s:
        row = s.execute(
            select(personal_objective_subtasks).where(
                personal_objective_subtasks.c.subtask_id == subtask_id
            )
        ).one()
    return _objective_subtask_from_row(row._mapping)


def _load_objective_comment(comment_id: UUID) -> ObjectiveComment:
    with session() as s:
        row = s.execute(
            select(
                personal_objective_comments.c.comment_id,
                personal_objective_comments.c.user_id,
                personal_objective_comments.c.body,
                personal_objective_comments.c.weekday,
                personal_objective_comments.c.created_at,
                users.c.display_name.label("author_name"),
            )
            .select_from(
                personal_objective_comments.join(
                    users,
                    personal_objective_comments.c.user_id == users.c.user_id,
                    isouter=True,
                )
            )
            .where(personal_objective_comments.c.comment_id == comment_id)
        ).one()
    return _objective_comment_from_row(row._mapping)


def _load_project(workspace_id: UUID, project_id: UUID) -> PersonalBoardProject:
    with session() as s:
        row = s.execute(
            select(*_PROJECT_COLS).where(
                personal_board_projects.c.project_id == project_id,
                personal_board_projects.c.workspace_id == workspace_id,
                personal_board_projects.c.archived_at.is_(None),
            )
        ).first()
    if row is None:
        raise LookupError("project not found")
    return _project_from_row(row)


def _load_backlog_bucket(workspace_id: UUID, bucket_id: UUID) -> PersonalBoardBacklogBucket:
    with session() as s:
        row = s.execute(
            select(
                personal_board_backlog_buckets.c.bucket_id,
                personal_board_backlog_buckets.c.key,
                personal_board_backlog_buckets.c.label,
                personal_board_backlog_buckets.c.badge,
                personal_board_backlog_buckets.c.color,
                personal_board_backlog_buckets.c.order_index,
                personal_board_backlog_buckets.c.collapsed,
            ).where(
                personal_board_backlog_buckets.c.bucket_id == bucket_id,
                personal_board_backlog_buckets.c.workspace_id == workspace_id,
            )
        ).first()
    if row is None:
        raise LookupError("bucket not found")
    return PersonalBoardBacklogBucket(
        bucket_id=row.bucket_id,
        key=row.key,
        label=row.label,
        badge=row.badge,
        color=row.color,
        order_index=row.order_index,
        collapsed=row.collapsed,
    )


def _validate_project(workspace_id: UUID, project_id: UUID | None) -> None:
    if project_id is None:
        return
    try:
        _load_project(workspace_id, project_id)
    except LookupError as e:
        raise ValueError("project not found") from e


def _validate_bucket(workspace_id: UUID, bucket_id: UUID | None) -> None:
    if bucket_id is None:
        return
    try:
        _load_backlog_bucket(workspace_id, bucket_id)
    except LookupError as e:
        raise ValueError("bucket not found") from e


def _validate_integration_kind(workspace_id: UUID, kind: str) -> None:
    with session() as s:
        exists = s.execute(
            select(personal_board_integrations.c.integration_id).where(
                personal_board_integrations.c.workspace_id == workspace_id,
                personal_board_integrations.c.kind == kind,
            )
        ).first()
    if exists is None:
        raise ValueError("integration not found")


def _next_folder_order(workspace_id: UUID) -> int:
    with session() as s:
        current = s.execute(
            select(func.max(personal_board_folders.c.order_index)).where(
                personal_board_folders.c.workspace_id == workspace_id
            )
        ).scalar()
    return 0 if current is None else int(current) + 1


def _next_task_order(
    workspace_id: UUID, scheduled_date: date | None, bucket_id: UUID | None
) -> int:
    with session() as s:
        where = [personal_board_tasks.c.workspace_id == workspace_id]
        if scheduled_date is not None:
            where.append(personal_board_tasks.c.scheduled_date == scheduled_date)
        else:
            where.append(personal_board_tasks.c.backlog_bucket_id == bucket_id)
        current = s.execute(
            select(func.max(personal_board_tasks.c.order_index)).where(and_(*where))
        ).scalar()
    return 0 if current is None else int(current) + 1


def _rebalance_task_order(
    s,
    workspace_id: UUID,
    user_id: UUID,
    scheduled_date: date | None,
    bucket_id: UUID | None,
    *,
    moving_task_id: UUID | None = None,
    insert_index: int | None = None,
    updated_at: datetime,
) -> None:
    if scheduled_date is None and bucket_id is None:
        return

    where = [
        personal_board_tasks.c.workspace_id == workspace_id,
        personal_board_tasks.c.user_id == user_id,
        personal_board_tasks.c.status != "archived",
    ]
    if scheduled_date is not None:
        where.append(personal_board_tasks.c.scheduled_date == scheduled_date)
    else:
        where.append(personal_board_tasks.c.backlog_bucket_id == bucket_id)

    ordered_ids = list(
        s.execute(
            select(personal_board_tasks.c.task_id)
            .where(and_(*where))
            .order_by(
                personal_board_tasks.c.order_index.asc(),
                personal_board_tasks.c.created_at.asc(),
                personal_board_tasks.c.task_id.asc(),
            )
        ).scalars()
    )
    task_ids = [item for item in ordered_ids if item != moving_task_id]
    if moving_task_id is not None:
        index = len(task_ids) if insert_index is None else min(insert_index, len(task_ids))
        task_ids.insert(index, moving_task_id)

    for index, item_id in enumerate(task_ids):
        s.execute(
            update(personal_board_tasks)
            .where(personal_board_tasks.c.task_id == item_id)
            .values(order_index=index, updated_at=updated_at)
        )


def rollover_overdue_tasks(workspace_id: UUID, user_id: UUID, today: date) -> int:
    """전날까지 끝내지 못한(미완료·open) 날짜 태스크를 오늘로 이월한다.

    - 대상: status='open' + 미완료 + 날짜 배정(scheduled_date < today).
    - 제외: 완료/보관, 백로그(날짜 없음), 캘린더 연동 태스크(team_event_id 또는
      source_kind='calendar' — 고정 일정이라 이월하면 일정과 어긋난다).
    - 오늘 기존 태스크 뒤에 붙이고 order_index를 재정렬한다. 멱등(이미 오늘이면 무동작).
    반환값: 이월한 태스크 수.
    """
    with session() as s:
        overdue = list(
            s.execute(
                select(personal_board_tasks.c.task_id)
                .where(
                    personal_board_tasks.c.workspace_id == workspace_id,
                    personal_board_tasks.c.user_id == user_id,
                    personal_board_tasks.c.status == "open",
                    personal_board_tasks.c.scheduled_date.isnot(None),
                    personal_board_tasks.c.scheduled_date < today,
                    personal_board_tasks.c.team_event_id.is_(None),
                    personal_board_tasks.c.source_kind != "calendar",
                )
                .order_by(
                    personal_board_tasks.c.scheduled_date.asc(),
                    personal_board_tasks.c.order_index.asc(),
                    personal_board_tasks.c.created_at.asc(),
                )
            ).scalars()
        )
        if not overdue:
            return 0
        now = datetime.now(UTC)
        base = (
            s.execute(
                select(func.coalesce(func.max(personal_board_tasks.c.order_index), -1)).where(
                    personal_board_tasks.c.workspace_id == workspace_id,
                    personal_board_tasks.c.user_id == user_id,
                    personal_board_tasks.c.scheduled_date == today,
                    personal_board_tasks.c.status != "archived",
                )
            ).scalar()
            or -1
        )
        for offset, task_id in enumerate(overdue):
            s.execute(
                update(personal_board_tasks)
                .where(personal_board_tasks.c.task_id == task_id)
                .values(scheduled_date=today, order_index=base + 1 + offset, updated_at=now)
            )
        _rebalance_task_order(s, workspace_id, user_id, today, None, updated_at=now)
        s.commit()
    return len(overdue)


def _next_note_order(workspace_id: UUID, note_date: date) -> int:
    with session() as s:
        current = s.execute(
            select(func.max(personal_board_notes.c.order_index)).where(
                personal_board_notes.c.workspace_id == workspace_id,
                personal_board_notes.c.note_date == note_date,
            )
        ).scalar()
    return 0 if current is None else int(current) + 1


def _next_subtask_order(task_id: UUID) -> int:
    with session() as s:
        current = s.execute(
            select(func.max(personal_board_subtasks.c.order_index)).where(
                personal_board_subtasks.c.task_id == task_id
            )
        ).scalar()
    return 0 if current is None else int(current) + 1


def _task_day_for_user(workspace_id: UUID, user_id: UUID, task_id: UUID) -> date | None:
    with session() as s:
        row = s.execute(
            select(personal_board_tasks.c.scheduled_date).where(
                personal_board_tasks.c.task_id == task_id,
                personal_board_tasks.c.workspace_id == workspace_id,
                personal_board_tasks.c.user_id == user_id,
                personal_board_tasks.c.status != "archived",
            )
        ).first()
    if row is None:
        raise LookupError("task not found")
    return row.scheduled_date


def _task_from_row(row, projects, subtasks_by_task) -> PersonalBoardTask:
    return PersonalBoardTask(
        task_id=row["task_id"],
        title=row["title"],
        status=row["status"],
        completed=row["status"] == "done",
        priority=row["priority"],
        scheduled_date=row["scheduled_date"],
        scheduled_time=row["scheduled_time"],
        scheduled_end_time=row["scheduled_end_time"],
        no_return=row["no_return"],
        due_date=row["due_date"],
        due_time=row["due_time"],
        backlog_bucket_id=row["backlog_bucket_id"],
        order_index=row["order_index"],
        source_kind=row["source_kind"],
        source_label=row["source_label"],
        team_event_id=row["team_event_id"],
        scope=row["scope"],
        company_project_id=row["company_project_id"],
        note=row["note"],
        project=projects.get(row["project_id"]),
        subtasks=subtasks_by_task.get(row["task_id"], []),
    )


def _folder_from_row(row) -> PersonalBoardFolder:
    return PersonalBoardFolder(
        folder_id=row["folder_id"],
        name=row["name"],
        kind=row["kind"],
        order_index=row["order_index"],
    )


def _integration_from_row(row) -> PersonalBoardIntegration:
    return PersonalBoardIntegration(
        integration_id=row["integration_id"],
        kind=row["kind"],
        label=row["label"],
        enabled=row["enabled"],
        has_notification=row["has_notification"],
        order_index=row["order_index"],
    )


def _subtask_from_row(row) -> PersonalBoardSubtask:
    return PersonalBoardSubtask(
        subtask_id=row["subtask_id"],
        task_id=row["task_id"],
        title=row["title"],
        completed=row["completed"],
        order_index=row["order_index"],
    )


def _event_from_row(row, projects) -> PersonalBoardFixedEvent:
    return PersonalBoardFixedEvent(
        event_id=row["event_id"],
        title=row["title"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        source_kind=row["source_kind"],
        source_label=row["source_label"],
        project=projects.get(row["project_id"]),
    )


def _note_from_row(row) -> PersonalBoardNote:
    return PersonalBoardNote(
        note_id=row["note_id"],
        note_date=row["note_date"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        order_index=row["order_index"],
        created_at=row["created_at"],
    )


def _daily_wiki_markdown(day: PersonalBoardDay) -> str | None:
    """Durable-knowledge projection of a board day for the LLM wiki.

    Ephemeral live-board data — 할 일(tasks) and 일정(calendar events) — is
    intentionally OMITTED. It lives in the personal board as the live source of
    truth and is reachable through the ``ticket_list`` / ``personal_schedule_list``
    tools, so distilling it into wiki claims only produced stale duplicates that
    later surfaced in ``wiki_search``/``wiki_ask`` (the 2026-07-14 board misroute:
    old journal todos answered "오늘 할 일" instead of the live board). Only freeform
    특이사항 notes are durable knowledge worth authoring.

    Returns None when the day has no notes; the caller then stores a source-only
    stub without wiki authoring, so no claims are created."""
    if not day.notes:
        return None
    lines = [f"# 개인 워크스페이스 {day.date.isoformat()}", "", "## 특이사항"]
    for note in day.notes:
        lines.extend([f"### {note.title}", f"- kind: {note.kind}", "", note.body, ""])
    return "\n".join(lines).strip() + "\n"
