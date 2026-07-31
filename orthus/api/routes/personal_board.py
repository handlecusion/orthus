"""Personal workspace board routes."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from orthus.api.deps import get_current_user, get_user_id
from orthus.api.knowledge_token import (
    enforce_token_dashboard_write_rate_limit,
    get_session_user_or_knowledge_token,
    get_session_user_or_knowledge_write_token,
)
from orthus.auth import AuthenticatedUser
from orthus.personal_board import (
    BacklogBucketPatch,
    DaysResponse,
    FixedEventCreate,
    FixedEventPatch,
    FolderCreate,
    NoteCreate,
    ObjectiveComment,
    ObjectiveCommentCreate,
    ObjectiveCreate,
    ObjectiveMoveDay,
    ObjectivePatch,
    ObjectiveSubtask,
    ObjectiveSubtaskCreate,
    ObjectiveSubtaskPatch,
    PreferencesPatch,
    PersonalBoardBootstrap,
    PersonalBoardBacklogBucket,
    PersonalBoardFixedEvent,
    PersonalBoardFolder,
    PersonalBoardNote,
    PersonalBoardPreferences,
    PersonalBoardProject,
    PersonalBoardSubtask,
    PersonalBoardTask,
    PersonalWeeklyObjective,
    ProjectCreate,
    ProjectPatch,
    SubtaskCreate,
    SubtaskPatch,
    TaskComment,
    TaskCommentCreate,
    TaskCreate,
    TaskPatch,
    WeeklyPlanResponse,
    WeeklyReviewResponse,
    board_notify_state,
    bootstrap,
    create_fixed_event,
    create_folder,
    create_note,
    create_objective,
    create_objective_comment,
    create_objective_subtask,
    create_project,
    create_subtask,
    create_task,
    create_task_comment,
    list_backlog_buckets,
    list_board_notifications,
    list_projects,
    list_task_comments,
    list_tasks,
    delete_objective,
    delete_project,
    delete_subtask,
    delete_task,
    duplicate_task,
    link_task_to_team_event,
    list_days,
    list_fixed_events,
    list_weekly_plan,
    move_objective_day,
    unlink_task_from_team_event,
    update_backlog_bucket,
    update_fixed_event,
    update_objective,
    update_objective_subtask,
    update_preferences,
    update_project,
    update_subtask,
    update_task,
    weekly_review,
)
from orthus.schemas.canonical import (
    PersonalBoardNotificationList,
    PersonalBoardNotifyState,
)
from orthus.settings import get_settings

router = APIRouter(prefix="/personal-board", tags=["personal-board"])


def _publish_board_ping(owner_id: UUID) -> None:
    """티켓 변경을 SSE 구독자에게 내용 없는 핑으로 알린다.

    에이전트(orthus ticket CLI/MCP)나 다른 기기가 만든 변경이 열려 있는 /board에
    새로고침 없이 반영되게 한다. 이벤트는 owner_id만 담고(내용 없음),
    /dashboard/stream이 owner가 다른 구독자에게는 전달하지 않는다 — 실제 데이터는
    구독자 본인의 인증된 재조회로만 온다."""
    from orthus import realtime

    realtime.publish(
        {
            "type": "personal_board_changed",
            "node_id": get_settings().node_id,
            "owner_id": str(owner_id),
        }
    )


def _node_id() -> str:
    """Resolve the node_id the personal board workspace lives under.

    Personal nodes always serve the board. On the company (central) node the
    board is only exposed when `ORTHUS_OWNER_SCOPE_ENABLED` is true (P8.5): every
    session user gets their own `(user_id, node_id)` workspace under the company
    node_id, fail-closed-isolated from other users by the workspace+user_id
    predicates on every read/write (docs/p8-central-consolidation.md §3/§6).
    Flag off keeps the central node board-free with the original 404.
    """
    settings = get_settings()
    if settings.node_kind == "personal":
        return settings.node_id
    if settings.node_kind == "company" and settings.owner_scope_enabled:
        return settings.node_id
    raise HTTPException(
        status_code=404, detail="personal board is only available on personal nodes"
    )


@router.get("/bootstrap", response_model=PersonalBoardBootstrap)
def get_personal_board(
    selected_date: date | None = Query(default=None, alias="date"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonalBoardBootstrap:
    return bootstrap(
        user.user_id, node_id=_node_id(), selected_date=selected_date, email=user.email
    )


@router.get("/tasks", response_model=list[PersonalBoardTask])
def get_tasks(
    status: str | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    id_prefix: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[PersonalBoardTask]:
    # Dual-auth: the owner's orthus-mcp/CLI (`orthus ticket list|show`) reads its
    # own tickets without the full bootstrap payload. Owner-scoped rows only.
    try:
        return list_tasks(
            current.user_id,
            _node_id(),
            status=status,
            project_id=project_id,
            id_prefix=id_prefix,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/notify-state", response_model=PersonalBoardNotifyState)
def get_board_notify_state(
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonalBoardNotifyState:
    """Cheap new-ticket signal for notifications (PR-N1).

    Mirrors GET /mail/notify-state: a read-only count/max over the caller's own
    board tickets — calendar-materialized auto-rows excluded — safe to poll
    frequently. Session auth only (no dual-auth token); never creates a
    workspace (zero-state when none exists).
    """

    return board_notify_state(user.user_id, _node_id())


@router.get("/notifications", response_model=PersonalBoardNotificationList)
def get_board_notifications(
    limit: int = Query(default=20, ge=1, le=50),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonalBoardNotificationList:
    """Recent non-calendar board tickets for the caller, newest first (read-only)."""

    return list_board_notifications(user.user_id, _node_id(), limit=limit)


@router.get("/projects", response_model=list[PersonalBoardProject])
def get_projects(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[PersonalBoardProject]:
    # Dual-auth: `orthus ticket` resolves `--project <name>` against the owner's
    # active channels; syncs assigned company channels first (CLI-only users
    # never hit the FE bootstrap that normally does this).
    return list_projects(current.user_id, _node_id(), email=current.email)


@router.get("/backlog-buckets", response_model=list[PersonalBoardBacklogBucket])
def get_backlog_buckets(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[PersonalBoardBacklogBucket]:
    # Dual-auth: `orthus ticket` resolves `--bucket <key>` to a bucket_id.
    return list_backlog_buckets(current.user_id, _node_id())


@router.post("/tasks", response_model=PersonalBoardTask, status_code=201)
def post_task(
    body: TaskCreate,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> PersonalBoardTask:
    # Dual-auth write: a `knowledge:write` token (owner's orthus-mcp/CLI) creates a
    # ticket on the owner's own board. Owner-bound token + owner-scoped rows; rate
    # limited like fixed-events. DELETE stays session-only (P3.3c: reversible only).
    enforce_token_dashboard_write_rate_limit(current)
    try:
        task = create_task(current.user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _publish_board_ping(current.user_id)
    return task


@router.patch("/tasks/{task_id}", response_model=PersonalBoardTask)
def patch_task(
    task_id: UUID,
    body: TaskPatch,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> PersonalBoardTask:
    enforce_token_dashboard_write_rate_limit(current)
    try:
        task = update_task(current.user_id, _node_id(), task_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    _publish_board_ping(current.user_id)
    return task


@router.post("/tasks/{task_id}/team-event", response_model=PersonalBoardTask)
def post_task_team_event(
    task_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonalBoardTask:
    """Register (or re-sync) a dated board task onto the team schedule."""
    try:
        return link_task_to_team_event(user.user_id, _node_id(), task_id, email=user.email)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/tasks/{task_id}/team-event", response_model=PersonalBoardTask)
def delete_task_team_event(
    task_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardTask:
    """Remove a board task from the team schedule (delete the mirrored event)."""
    try:
        return unlink_task_from_team_event(user_id, _node_id(), task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/projects", response_model=PersonalBoardProject, status_code=201)
def post_project(
    body: ProjectCreate,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardProject:
    try:
        return create_project(user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/projects/{project_id}", response_model=PersonalBoardProject)
def patch_project(
    project_id: UUID,
    body: ProjectPatch,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardProject:
    try:
        return update_project(user_id, _node_id(), project_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/projects/{project_id}", status_code=204)
def delete_project_route(
    project_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> None:
    try:
        delete_project(user_id, _node_id(), project_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/folders", response_model=PersonalBoardFolder, status_code=201)
def post_folder(
    body: FolderCreate,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardFolder:
    try:
        return create_folder(user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/backlog-buckets/{bucket_id}", response_model=PersonalBoardBacklogBucket)
def patch_backlog_bucket(
    bucket_id: UUID,
    body: BacklogBucketPatch,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardBacklogBucket:
    try:
        return update_backlog_bucket(user_id, _node_id(), bucket_id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.patch("/preferences", response_model=PersonalBoardPreferences)
def patch_preferences(
    body: PreferencesPatch,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardPreferences:
    try:
        return update_preferences(user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/tasks/{task_id}/comments", response_model=list[TaskComment])
def get_task_comments(
    task_id: UUID,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[TaskComment]:
    # Dual-auth: `orthus ticket show` reads the owner's own ticket comments.
    try:
        return list_task_comments(current.user_id, _node_id(), task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/tasks/{task_id}/comments", response_model=TaskComment, status_code=201)
def post_task_comment(
    task_id: UUID,
    body: TaskCommentCreate,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> TaskComment:
    # Dual-auth write: `orthus ticket comment` on the owner's own ticket; rate limited.
    enforce_token_dashboard_write_rate_limit(current)
    try:
        comment = create_task_comment(current.user_id, _node_id(), task_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    _publish_board_ping(current.user_id)
    return comment


@router.post("/tasks/{task_id}/subtasks", response_model=PersonalBoardSubtask, status_code=201)
def post_subtask(
    task_id: UUID,
    body: SubtaskCreate,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardSubtask:
    try:
        return create_subtask(user_id, _node_id(), task_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.patch("/subtasks/{subtask_id}", response_model=PersonalBoardSubtask)
def patch_subtask(
    subtask_id: UUID,
    body: SubtaskPatch,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardSubtask:
    try:
        return update_subtask(user_id, _node_id(), subtask_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/subtasks/{subtask_id}", status_code=204)
def remove_subtask(
    subtask_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> None:
    try:
        delete_subtask(user_id, _node_id(), subtask_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/fixed-events", response_model=list[PersonalBoardFixedEvent])
def get_fixed_events(
    frm: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[PersonalBoardFixedEvent]:
    # Dual-auth: the owner's orthus-mcp/CLI (knowledge token) lists its own
    # personal schedule to find an event_id before updating. Owner-scoped rows only.
    return list_fixed_events(current.user_id, _node_id(), frm, to)


@router.post("/fixed-events", response_model=PersonalBoardFixedEvent, status_code=201)
def post_fixed_event(
    body: FixedEventCreate,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> PersonalBoardFixedEvent:
    # Dual-auth write: a `knowledge:write` token (owner's orthus-mcp/CLI) can add a
    # personal schedule event. Owner-bound token + owner-scoped rows; rate limited.
    enforce_token_dashboard_write_rate_limit(current)
    try:
        return create_fixed_event(current.user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/fixed-events/{event_id}", response_model=PersonalBoardFixedEvent)
def patch_fixed_event(
    event_id: UUID,
    body: FixedEventPatch,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> PersonalBoardFixedEvent:
    enforce_token_dashboard_write_rate_limit(current)
    try:
        return update_fixed_event(current.user_id, _node_id(), event_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/notes", response_model=PersonalBoardNote, status_code=201)
def post_note(body: NoteCreate, user_id: UUID = Depends(get_user_id)) -> PersonalBoardNote:
    try:
        return create_note(user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/weekly-plan", response_model=WeeklyPlanResponse)
def get_weekly_plan(
    week_start: date | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> WeeklyPlanResponse:
    return list_weekly_plan(user.user_id, _node_id(), week_start, email=user.email)


@router.post("/weekly-plan/objectives", response_model=PersonalWeeklyObjective, status_code=201)
def post_objective(
    body: ObjectiveCreate,
    user_id: UUID = Depends(get_user_id),
) -> PersonalWeeklyObjective:
    try:
        return create_objective(user_id, _node_id(), body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/weekly-plan/objectives/{objective_id}", response_model=PersonalWeeklyObjective)
def patch_objective(
    objective_id: UUID,
    body: ObjectivePatch,
    user_id: UUID = Depends(get_user_id),
) -> PersonalWeeklyObjective:
    try:
        return update_objective(user_id, _node_id(), objective_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/weekly-plan/objectives/{objective_id}/move-day",
    response_model=PersonalWeeklyObjective,
)
def post_objective_move_day(
    objective_id: UUID,
    body: ObjectiveMoveDay,
    user_id: UUID = Depends(get_user_id),
) -> PersonalWeeklyObjective:
    try:
        return move_objective_day(user_id, _node_id(), objective_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/weekly-plan/objectives/{objective_id}", status_code=204)
def remove_objective(
    objective_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> None:
    try:
        delete_objective(user_id, _node_id(), objective_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/weekly-plan/objectives/{objective_id}/subtasks",
    response_model=ObjectiveSubtask,
    status_code=201,
)
def post_objective_subtask(
    objective_id: UUID,
    body: ObjectiveSubtaskCreate,
    user_id: UUID = Depends(get_user_id),
) -> ObjectiveSubtask:
    try:
        return create_objective_subtask(user_id, _node_id(), objective_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.patch(
    "/weekly-plan/objective-subtasks/{subtask_id}",
    response_model=ObjectiveSubtask,
)
def patch_objective_subtask(
    subtask_id: UUID,
    body: ObjectiveSubtaskPatch,
    user_id: UUID = Depends(get_user_id),
) -> ObjectiveSubtask:
    try:
        return update_objective_subtask(user_id, _node_id(), subtask_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/weekly-plan/objectives/{objective_id}/comments",
    response_model=ObjectiveComment,
    status_code=201,
)
def post_objective_comment(
    objective_id: UUID,
    body: ObjectiveCommentCreate,
    user_id: UUID = Depends(get_user_id),
) -> ObjectiveComment:
    try:
        return create_objective_comment(user_id, _node_id(), objective_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/weekly-review", response_model=WeeklyReviewResponse)
def get_weekly_review(
    week_start: date | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> WeeklyReviewResponse:
    return weekly_review(user_id, _node_id(), week_start)


@router.get("/days", response_model=DaysResponse)
def get_days(
    start: date = Query(...),
    days: int = Query(default=7),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DaysResponse:
    return list_days(user.user_id, _node_id(), start, days, email=user.email)


@router.post("/tasks/{task_id}/duplicate", response_model=PersonalBoardTask, status_code=201)
def post_task_duplicate(
    task_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> PersonalBoardTask:
    try:
        return duplicate_task(user_id, _node_id(), task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/tasks/{task_id}", status_code=204)
def remove_task(
    task_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> None:
    try:
        delete_task(user_id, _node_id(), task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
