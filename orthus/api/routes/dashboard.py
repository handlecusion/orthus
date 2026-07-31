"""Company dashboard routes: team, finance, weekly/monthly plans & retros,
team calendar, and culture. Company-node only; logic lives in orthus/dashboard.py."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from orthus.api.deps import get_current_user, get_user_id
from orthus.api.knowledge_token import (
    enforce_token_dashboard_write_rate_limit,
    get_session_user_or_knowledge_token,
    get_session_user_or_knowledge_write_token,
)
from orthus.auth import AuthenticatedUser, require_node_operator
from orthus import dashboard as d
from orthus import media as media_store
from orthus import notes as nt
from orthus import presence as pr
from orthus import project_database as pdb
from orthus import realtime
from orthus.dashboard_wiki import (
    reflect_calendar_event,
    reflect_meeting_note,
    reflect_partner_company,
    reflect_weekly,
)
from orthus.settings import get_settings

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _node_id() -> str:
    settings = get_settings()
    if settings.node_kind != "company":
        raise HTTPException(
            status_code=404, detail="company dashboard is only available on company nodes"
        )
    return settings.node_id


def _operator(current: AuthenticatedUser) -> str:
    """Company-node guard + owner/admin write authority (demo mode bypasses)."""
    node_id = _node_id()
    require_node_operator(current)
    return node_id


def _value_error(e: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(e))


def _not_found(e: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


# ---------------- Team members ----------------
@router.get("/team-members", response_model=list[d.TeamMember])
def list_team_members(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[d.TeamMember]:
    # Dual-auth so the owner's orthus-mcp/CLI (knowledge token) + Solar agentic
    # /ask can read the company team directory — company internal knowledge,
    # low-risk; also used to resolve member_ids when adding a team calendar event.
    return d.list_team_members(_node_id())


@router.post("/team-members", response_model=d.TeamMember, status_code=201)
def create_team_member(
    body: d.TeamMemberIn,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> d.TeamMember:
    # Dual-auth write: a `knowledge:write` token (owner's orthus-mcp/CLI) can add a
    # team member. require_node_operator (inside _operator) still gates session
    # callers to owner/admin and is a no-op for the owner-bound token.
    enforce_token_dashboard_write_rate_limit(current)
    node_id = _operator(current)
    try:
        return d.create_team_member(node_id, body, created_by=current.user_id)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/team-members/{member_id}", response_model=d.TeamMember)
def update_team_member(
    member_id: UUID, body: d.TeamMemberIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.TeamMember:
    node_id = _operator(current)
    try:
        return d.update_team_member(node_id, member_id, body, created_by=current.user_id)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/team-members/{member_id}", status_code=204)
def delete_team_member(
    member_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_team_member(node_id, member_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/team-members/sync-notion", response_model=d.TeamSyncResult)
def sync_team_members_from_notion(
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.TeamSyncResult:
    node_id = _operator(current)
    return d.sync_team_members_from_notion(node_id)


# --- 인재영입(recruiting) 후보 ---
@router.get("/recruiting-candidates", response_model=list[d.RecruitingCandidate])
def list_recruiting_candidates(
    user_id: UUID = Depends(get_user_id),
) -> list[d.RecruitingCandidate]:
    return d.list_recruiting_candidates(_node_id())


@router.post("/recruiting-candidates", response_model=d.RecruitingCandidate, status_code=201)
def create_recruiting_candidate(
    body: d.RecruitingCandidateIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.RecruitingCandidate:
    node_id = _operator(current)
    try:
        return d.create_recruiting_candidate(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/recruiting-candidates/{candidate_id}", response_model=d.RecruitingCandidate)
def update_recruiting_candidate(
    candidate_id: UUID,
    body: d.RecruitingCandidateIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.RecruitingCandidate:
    node_id = _operator(current)
    try:
        return d.update_recruiting_candidate(node_id, candidate_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/recruiting-candidates/{candidate_id}", status_code=204)
def delete_recruiting_candidate(
    candidate_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_recruiting_candidate(node_id, candidate_id)
    except LookupError as e:
        raise _not_found(e) from e


# --- 후보별 팀원 코멘트 ---
@router.get(
    "/recruiting-candidates/{candidate_id}/comments",
    response_model=list[d.RecruitingComment],
)
def list_recruiting_comments(
    candidate_id: UUID, user_id: UUID = Depends(get_user_id)
) -> list[d.RecruitingComment]:
    return d.list_recruiting_comments(_node_id(), candidate_id)


@router.post(
    "/recruiting-candidates/{candidate_id}/comments",
    response_model=d.RecruitingComment,
    status_code=201,
)
def create_recruiting_comment(
    candidate_id: UUID,
    body: d.RecruitingCommentIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.RecruitingComment:
    node_id = _operator(current)
    try:
        return d.create_recruiting_comment(node_id, candidate_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/recruiting-candidates/{candidate_id}/comments/{comment_id}", status_code=204)
def delete_recruiting_comment(
    candidate_id: UUID,
    comment_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> None:
    node_id = _operator(current)
    try:
        d.delete_recruiting_comment(node_id, comment_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Projects ----------------
@router.get("/projects", response_model=list[d.DashboardProject])
def list_projects(user_id: UUID = Depends(get_user_id)) -> list[d.DashboardProject]:
    return d.list_projects(_node_id())


# NOTE: /projects/{project_id}보다 먼저 선언해야 "progress"가 UUID로 파싱되지 않는다.
@router.get("/projects/progress", response_model=list[pdb.ProjectProgress])
def list_project_progress(user_id: UUID = Depends(get_user_id)) -> list[pdb.ProjectProgress]:
    """프로젝트별 진행률(소속 데이터베이스 status 집계). 하위 합산은 FE가 한다."""
    return pdb.progress_by_project(_node_id())


@router.get("/projects/requirements-summary", response_model=list[d.RequirementSummary])
def list_requirements_summary(user_id: UUID = Depends(get_user_id)) -> list[d.RequirementSummary]:
    """프로젝트별 SE 요구조건 충족 현황 (목록/하위 카드 배지용)."""
    return d.requirements_summary(_node_id())


@router.get("/projects/{project_id}", response_model=d.DashboardProject)
def get_project(project_id: UUID, user_id: UUID = Depends(get_user_id)) -> d.DashboardProject:
    try:
        return d.get_project(_node_id(), project_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/projects", response_model=d.DashboardProject, status_code=201)
def create_project(body: d.ProjectIn, user_id: UUID = Depends(get_user_id)) -> d.DashboardProject:
    try:
        return d.create_project(_node_id(), body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/projects/{project_id}", response_model=d.DashboardProject)
def update_project(
    project_id: UUID, body: d.ProjectIn, user_id: UUID = Depends(get_user_id)
) -> d.DashboardProject:
    try:
        return d.update_project(_node_id(), project_id, body, actor_user_id=user_id)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    try:
        d.delete_project(_node_id(), project_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- 프로젝트 SE 요구조건 (SRR 대장 + flow-down) ----------------
@router.get("/projects/{project_id}/requirements", response_model=list[d.ProjectRequirement])
def list_project_requirements(
    project_id: UUID, user_id: UUID = Depends(get_user_id)
) -> list[d.ProjectRequirement]:
    try:
        return d.list_requirements(_node_id(), project_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post(
    "/projects/{project_id}/requirements",
    response_model=d.ProjectRequirement,
    status_code=201,
)
def create_project_requirement(
    project_id: UUID, body: d.RequirementIn, user_id: UUID = Depends(get_user_id)
) -> d.ProjectRequirement:
    try:
        return d.create_requirement(_node_id(), project_id, body, actor_user_id=user_id)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.patch(
    "/projects/{project_id}/requirements/{requirement_id}",
    response_model=d.ProjectRequirement,
)
def update_project_requirement(
    project_id: UUID,
    requirement_id: UUID,
    body: d.RequirementIn,
    user_id: UUID = Depends(get_user_id),
) -> d.ProjectRequirement:
    try:
        return d.update_requirement(
            _node_id(), project_id, requirement_id, body, actor_user_id=user_id
        )
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/projects/{project_id}/requirements/{requirement_id}", status_code=204)
def delete_project_requirement(
    project_id: UUID, requirement_id: UUID, user_id: UUID = Depends(get_user_id)
) -> None:
    try:
        d.delete_requirement(_node_id(), project_id, requirement_id, actor_user_id=user_id)
    except LookupError as e:
        raise _not_found(e) from e


class FlowDownIn(BaseModel):
    requirement_ids: list[UUID]


@router.post(
    "/projects/{project_id}/requirements/flow-down",
    response_model=list[d.ProjectRequirement],
)
def flow_down_project_requirements(
    project_id: UUID, body: FlowDownIn, user_id: UUID = Depends(get_user_id)
) -> list[d.ProjectRequirement]:
    """상위 프로젝트 요구조건을 이 하위 프로젝트로 flow-down(복사+원본 링크)."""
    try:
        return d.flow_down_requirements(_node_id(), project_id, body.requirement_ids)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/projects/{project_id}/activity", response_model=list[d.ProjectActivityItem])
def list_project_activity(
    project_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    user_id: UUID = Depends(get_user_id),
) -> list[d.ProjectActivityItem]:
    """프로젝트 활동 피드(필드 변경·요구조건·배정·보드 행) 최신순."""
    return d.list_project_activity(_node_id(), project_id, limit=limit)


@router.get("/projects/{project_id}/board-tasks", response_model=list[d.ProjectBoardTask])
def list_project_board_tasks(
    project_id: UUID,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[d.ProjectBoardTask]:
    """팀원들이 회사 프로젝트 채널(내 보드)에 올린 회사 공개 업무를 회사 데이터로 조회.

    회사 구성원이면 누구나 읽을 수 있고(공개 범위 = 회사 구성원 전체), scope='company'
    행만 노출한다(개인 채널 업무는 비노출). 팀 캘린더와 같은 회사 내부 지식이라
    knowledge token(orthus ticket project-list)도 읽을 수 있다."""
    return d.list_project_board_tasks(_node_id(), project_id)


# ---------------- 프로젝트 인라인 데이터베이스 (노션식 표/칸반) ----------------
@router.get("/projects/{project_id}/databases", response_model=list[pdb.ProjectDatabase])
def list_project_databases(
    project_id: UUID,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[pdb.ProjectDatabase]:
    # Dual-auth read: `orthus ticket board`가 프로젝트 칸반(회사 내부 데이터)을 찾는다.
    return pdb.list_databases(_node_id(), project_id)


@router.post("/projects/{project_id}/board/ensure", response_model=pdb.ProjectDatabase)
def ensure_project_board(
    project_id: UUID, user_id: UUID = Depends(get_user_id)
) -> pdb.ProjectDatabase:
    """프로젝트 기본 칸반 보드 보장(멱등) — 보드 뷰가 있으면 그 데이터베이스 반환."""
    try:
        return pdb.ensure_board(_node_id(), project_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/databases", response_model=pdb.ProjectDatabaseBundle, status_code=201)
def create_database(
    body: pdb.DatabaseIn, user_id: UUID = Depends(get_user_id)
) -> pdb.ProjectDatabaseBundle:
    try:
        return pdb.create_database(_node_id(), body)
    except ValueError as e:
        raise _value_error(e) from e


@router.get("/databases/{database_id}", response_model=pdb.ProjectDatabaseBundle)
def get_database(
    database_id: UUID,
    body: Literal["full", "none"] = Query(default="full"),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> pdb.ProjectDatabaseBundle:
    """데이터베이스 + 전체 행 번들.

    기본은 행 body 포함(구 FE 호환 — 배포 skew 때 번들 body를 권위값으로 쓰는
    구 FE의 본문 유실 방지). `?body=none`은 신 FE 목록 렌더용 opt-in 생략."""
    try:
        return pdb.get_bundle(_node_id(), database_id, include_body=body != "none")
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/databases/{database_id}/meta", response_model=pdb.ProjectDatabaseMeta)
def get_database_meta(
    database_id: UUID, user_id: UUID = Depends(get_user_id)
) -> pdb.ProjectDatabaseMeta:
    """행 select 없는 경량 요약(제목/아이콘/프로젝트/행 수) — 번들 가드와 동일."""
    try:
        return pdb.get_meta(_node_id(), database_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/databases/{database_id}/rows/{row_id}", response_model=pdb.ProjectDatabaseRow)
def get_database_row(
    database_id: UUID, row_id: UUID, user_id: UUID = Depends(get_user_id)
) -> pdb.ProjectDatabaseRow:
    """단건 행 전체(body 포함) — 번들이 생략한 body의 지연 로드. 가드는 row PATCH와 동일."""
    try:
        return pdb.get_row(_node_id(), database_id, row_id)
    except LookupError as e:
        raise _not_found(e) from e


def _publish_project_board_ping(database_id: UUID) -> None:
    """프로젝트 칸반(인라인 DB) 변경을 SSE 구독자에게 내용 없는 핑으로 알린다.

    에이전트(orthus ticket)나 다른 브라우저가 행/스키마를 바꾸면 열려 있는 칸반이
    새로고침 없이 갱신된다. 회사 공유 데이터라 owner 필터 없이 node 전체에 흘리며,
    실제 데이터는 구독자 본인의 인증된 재조회로만 온다."""
    realtime.publish(
        {
            "type": "project_board_changed",
            "node_id": _node_id(),
            "database_id": str(database_id),
        }
    )


@router.patch("/databases/{database_id}", response_model=pdb.ProjectDatabase)
def update_database(
    database_id: UUID, body: pdb.DatabasePatch, user_id: UUID = Depends(get_user_id)
) -> pdb.ProjectDatabase:
    try:
        updated = pdb.update_database(_node_id(), database_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e
    _publish_project_board_ping(database_id)
    return updated


@router.delete("/databases/{database_id}", status_code=204)
def delete_database(database_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    try:
        pdb.delete_database(_node_id(), database_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post(
    "/databases/{database_id}/rows", response_model=pdb.ProjectDatabaseRow, status_code=201
)
def create_database_row(
    database_id: UUID,
    body: pdb.RowIn,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> pdb.ProjectDatabaseRow:
    # Dual-auth write: `orthus ticket board-add` — 팀 캘린더 write와 동일 신뢰 등급의
    # 회사 내부 데이터. 토큰은 rate limit, 행 delete/스키마 변경은 세션 전용 유지.
    enforce_token_dashboard_write_rate_limit(current)
    try:
        row = pdb.create_row(_node_id(), database_id, body)
    except LookupError as e:
        raise _not_found(e) from e
    d.log_row_activity(
        _node_id(), database_id, current.user_id, "create", row.row_id, props=row.props
    )
    _publish_project_board_ping(database_id)
    return row


@router.patch("/databases/{database_id}/rows/{row_id}", response_model=pdb.ProjectDatabaseRow)
def update_database_row(
    database_id: UUID,
    row_id: UUID,
    body: pdb.RowPatch,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> pdb.ProjectDatabaseRow:
    # Dual-auth write: `orthus ticket board-move` — props는 전체 교체 계약이라
    # CLI가 클라이언트에서 병합해 보낸다.
    enforce_token_dashboard_write_rate_limit(current)
    try:
        updated = pdb.update_row(_node_id(), database_id, row_id, body)
    except LookupError as e:
        raise _not_found(e) from e
    _publish_project_board_ping(database_id)
    return updated


@router.delete("/databases/{database_id}/rows/{row_id}", status_code=204)
def delete_database_row(
    database_id: UUID, row_id: UUID, user_id: UUID = Depends(get_user_id)
) -> None:
    props = d.row_props_for_log(_node_id(), database_id, row_id)
    try:
        pdb.delete_row(_node_id(), database_id, row_id)
    except LookupError as e:
        raise _not_found(e) from e
    d.log_row_activity(_node_id(), database_id, user_id, "delete", row_id, props=props)
    _publish_project_board_ping(database_id)


@router.post(
    "/databases/{database_id}/rows/{row_id}/files",
    response_model=pdb.ProjectDatabaseFileValue,
    status_code=201,
)
async def upload_database_file(
    database_id: UUID,
    row_id: UUID,
    file: UploadFile = File(...),
    user_id: UUID = Depends(get_user_id),
) -> pdb.ProjectDatabaseFileValue:
    data = await file.read(pdb.MAX_FILE_BYTES + 1)
    if len(data) > pdb.MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        return pdb.create_file(
            _node_id(),
            database_id,
            row_id,
            file.filename or "attachment",
            file.content_type,
            data,
            user_id,
        )
    except LookupError as e:
        raise _not_found(e) from e
    except ValueError as e:
        raise _value_error(e) from e


@router.get("/databases/{database_id}/files/{file_id}")
def get_database_file(
    database_id: UUID,
    file_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> Response:
    try:
        blob = pdb.get_file_blob(_node_id(), database_id, file_id)
    except LookupError as e:
        raise _not_found(e) from e
    disposition = f"inline; filename*=UTF-8''{quote(blob.filename)}"
    return Response(
        content=blob.data,
        media_type=blob.mime_type,
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/databases/{database_id}/files/{file_id}", status_code=204)
def delete_database_file(
    database_id: UUID,
    file_id: UUID,
    user_id: UUID = Depends(get_user_id),
) -> None:
    try:
        pdb.delete_file(_node_id(), database_id, file_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- 미디어 (이미지/동영상/파일 업로드 + 서빙) ----------------
@router.post("/media")
async def upload_media(
    file: UploadFile = File(...),
    current: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """노션식 본문/카드/파일속성 미디어 업로드. company-node owner/admin 전용.
    실파일로 저장하고 capability URL을 돌려준다(base64 미사용)."""
    _operator(current)
    data = await file.read()
    try:
        return media_store.store_upload(data, file.filename, file.content_type)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=413 if "large" in msg else 422, detail=msg) from e


class MediaChunkInitIn(BaseModel):
    filename: str | None = None
    content_type: str | None = None


@router.post("/media/uploads")
def init_media_upload(
    body: MediaChunkInitIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """대용량(동영상) 청크 업로드 세션 시작 → {upload_id}.

    단일 요청 업로드는 Cloudflare 요청 바디 상한(~100MB)과 RAM 적재에 걸린다.
    클라이언트가 파트를 순차 PUT하고 complete로 조립한다(`orthus/media.py`)."""
    _operator(current)
    return {"upload_id": media_store.chunk_init(body.filename, body.content_type)}


@router.put("/media/uploads/{upload_id}/parts/{index}")
async def put_media_upload_part(
    upload_id: str,
    index: int,
    request: Request,
    current: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    _operator(current)
    data = await request.body()
    try:
        received = media_store.chunk_append(upload_id, index, data)
    except LookupError as e:
        raise _not_found(e) from e
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=413 if "large" in msg else 409, detail=msg) from e
    return {"received": received}


@router.post("/media/uploads/{upload_id}/complete")
def complete_media_upload(
    upload_id: str,
    current: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    _operator(current)
    try:
        return media_store.chunk_complete(upload_id)
    except LookupError as e:
        raise _not_found(e) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/media/uploads/{upload_id}", status_code=204)
def abort_media_upload(
    upload_id: str,
    current: AuthenticatedUser = Depends(get_current_user),
) -> None:
    _operator(current)
    media_store.chunk_abort(upload_id)


@router.get("/media/{filename}")
def get_media(filename: str) -> FileResponse:
    """업로드 미디어 서빙. 파일명(unguessable uuid)이 capability라 별도 인증 없이
    `<img>`/`<video>`가 로드한다. company-node 가드만 둔다."""
    _node_id()
    try:
        path, ctype = media_store.resolve_media(filename)
    except LookupError as e:
        raise _not_found(e) from e
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    if not media_store.is_inline_type(ctype):
        headers["Content-Disposition"] = "attachment"
    return FileResponse(path, media_type=ctype, headers=headers)


# ---------------- Presence (실시간 협업: 누가 어디 작성 중) ----------------
@router.post("/presence/heartbeat", response_model=list[pr.Presence])
def presence_heartbeat(
    body: pr.PresenceHeartbeat,
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[pr.Presence]:
    name, color = d.member_identity(_node_id(), current.user_id)
    return pr.heartbeat(current.user_id, name, color, body)


@router.get("/stream")
async def stream(
    request: Request,
    current: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """계획·회고 변경을 즉시 푸시하는 SSE 스트림(6초 폴링 대체).

    인증은 세션 쿠키로 한다. EventSource는 커스텀 헤더는 못 보내지만 same-origin
    쿠키는 자동으로 싣는다(공개 호스트는 FE/API가 같은 origin). session mode에서는
    쿠키 없는/만료된 요청을 401로 막고, demo/dev mode는 데모 유저로 통과한다.
    이벤트는 내용 없이 변경 핑(node_id/project_id/week_start)만 담고, 실제 데이터는
    인증된 GET /weekly 재조회로 가져온다.
    """
    realtime.register_loop(asyncio.get_running_loop())
    node_id = _node_id()
    q = realtime.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive
                    continue
                if ev.get("node_id") and ev["node_id"] != node_id:
                    continue
                # 개인 보드 변경 핑은 그 보드의 owner에게만 흘린다 — 내용은 없지만
                # "누가 언제 개인 보드를 만졌다"는 메타데이터도 남에게 새지 않게.
                if ev.get("type") == "personal_board_changed" and ev.get("owner_id") != str(
                    current.user_id
                ):
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            realtime.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class LiveEditIn(BaseModel):
    kind: str
    project_id: str
    period_key: str
    # Plan/retro items relayed as-is (PlanItem objects). This path only fans out;
    # the durable shape is validated by the debounced save endpoints.
    plan: list[dict]
    retro: list[dict]
    editor_id: str


@router.post("/live-edit", status_code=204)
async def live_edit(
    body: LiveEditIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """협업 라이브 편집 relay: 입력 중인 내용을 즉시 다른 구독자에게 fan-out한다.

    DB 저장은 기존 디바운스 save가 맡고, 이 경로는 영속 없이 SSE로 중계만 한다 —
    재조회 왕복 없이 협업자 글자가 바로 보이게 하기 위함(Path A). 동일 origin 세션
    쿠키로 인증하고 node_id로 스코프한다(SSE gen이 node_id로 필터). 받은 쪽은 자기
    가 보낸 editor_id 에코를 무시하고, 현재 편집 중인 필드는 덮지 않는다.
    """
    realtime.register_loop(asyncio.get_running_loop())
    realtime.publish(
        {
            "type": "live_edit",
            "node_id": _node_id(),
            "kind": body.kind,
            "project_id": body.project_id,
            "period_key": body.period_key,
            "plan": body.plan,
            "retro": body.retro,
            "editor_id": body.editor_id,
        }
    )


# ---------------- Dashboard pages (노션식 페이지 / 자유 메모) ----------------
@router.get("/pages", response_model=list[d.DashboardPageSummary])
def list_pages(
    kind: str = Query(default="memo"),
    scope: Literal["all", "mine"] = Query(
        default="all"
    ),  # memo owner-scope, docs/personal-notes.md
    include_preview: bool = Query(default=False),
    user_id: UUID = Depends(get_user_id),
) -> list[d.DashboardPageSummary]:
    return d.list_pages(
        _node_id(),
        kind,
        owner_id=user_id,
        scope=scope,
        include_preview=include_preview,
    )


@router.post("/pages", response_model=d.DashboardPage, status_code=201)
def create_page(body: d.PageIn, user_id: UUID = Depends(get_user_id)) -> d.DashboardPage:
    # 생성자를 owner로 기록한다. subpage(kind='page')도 owner를 채우지만 접근 게이트는
    # 최상위 메모 단위이고 subpage는 node 스코프로 통과한다(orthus/notes.py::note_access).
    try:
        return d.create_page(_node_id(), body, owner_id=user_id)
    except d.PageIdCollision as e:
        # Do not reveal the owner or node that already holds this UUID.
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/pages/{page_id}", response_model=d.DashboardPage)
def get_page(page_id: UUID, user_id: UUID = Depends(get_user_id)) -> d.DashboardPage:
    node_id = _node_id()
    if not nt.can_read(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=404, detail="page not found")
    try:
        return d.get_page(node_id, page_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.patch("/pages/{page_id}", response_model=d.DashboardPage)
def update_page(
    page_id: UUID, body: d.PagePatch, user_id: UUID = Depends(get_user_id)
) -> d.DashboardPage:
    node_id = _node_id()
    access = nt.note_access(node_id, page_id, user_id)
    if not nt.can_read(access):
        raise HTTPException(status_code=404, detail="page not found")
    if not nt.can_write(access):
        raise HTTPException(status_code=403, detail="read-only share")
    try:
        return d.update_page(node_id, page_id, body)
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/pages/{page_id}", status_code=204)
def delete_page(page_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    node_id = _node_id()
    access = nt.note_access(node_id, page_id, user_id)
    if not nt.can_read(access):
        raise HTTPException(status_code=404, detail="page not found")
    if not nt.can_write(access):
        raise HTTPException(status_code=403, detail="read-only share")
    try:
        d.delete_page(node_id, page_id)
    except LookupError as e:
        raise _not_found(e) from e
    # 메모에 딸린 공유행/프로젝트 링크도 함께 정리(고아 방지).
    nt.cleanup_note(node_id, page_id)


# ---------------- Notes: 공유 + 얼라인 확인 + 프로젝트 링크 (docs/personal-notes.md) ----------------
@router.get("/notes/library", response_model=list[nt.NoteLibraryItem])
def list_note_library(
    scope: Literal["mine", "shared"] = Query(default="mine"),
    user_id: UUID = Depends(get_user_id),
) -> list[nt.NoteLibraryItem]:
    """메모 목록용 프로젝트·공유·preview bulk projection."""
    return nt.list_library_pages(_node_id(), user_id, scope=scope)


@router.get("/notes/shared", response_model=list[nt.SharedNoteSummary])
def list_shared_notes(user_id: UUID = Depends(get_user_id)) -> list[nt.SharedNoteSummary]:
    """나에게 공유된 메모 목록."""
    return nt.list_shared_pages(_node_id(), user_id)


@router.get("/notes/{page_id}/shares", response_model=list[nt.NoteShare])
def list_note_shares(page_id: UUID, user_id: UUID = Depends(get_user_id)) -> list[nt.NoteShare]:
    node_id = _node_id()
    if not nt.can_read(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=404, detail="note not found")
    return nt.list_shares(node_id, page_id)


@router.post("/notes/{page_id}/shares", response_model=list[nt.NoteShare], status_code=201)
def add_note_shares(
    page_id: UUID, body: nt.NoteShareIn, user_id: UUID = Depends(get_user_id)
) -> list[nt.NoteShare]:
    node_id = _node_id()
    if not nt.can_write(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=403, detail="only the owner can share")
    return nt.add_shares(node_id, page_id, user_id, body.shared_with, body.permission)


@router.delete("/notes/{page_id}/shares/{share_id}", status_code=204)
def delete_note_share(page_id: UUID, share_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    node_id = _node_id()
    if not nt.can_write(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=403, detail="only the owner can unshare")
    try:
        nt.delete_share(node_id, page_id, share_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/notes/{page_id}/acknowledge", status_code=204)
def acknowledge_note(page_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    """공유 수신자가 '얼라인 확인'을 남긴다."""
    node_id = _node_id()
    try:
        nt.acknowledge(node_id, page_id, user_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="not shared with you") from e


@router.get("/notes/{page_id}/projects", response_model=list[nt.NoteProjectLink])
def list_note_projects(
    page_id: UUID, user_id: UUID = Depends(get_user_id)
) -> list[nt.NoteProjectLink]:
    node_id = _node_id()
    if not nt.can_read(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=404, detail="note not found")
    return nt.list_project_links(node_id, page_id)


@router.put("/notes/{page_id}/projects", response_model=list[nt.NoteProjectLink])
def set_note_projects(
    page_id: UUID, body: nt.NoteProjectLinkIn, user_id: UUID = Depends(get_user_id)
) -> list[nt.NoteProjectLink]:
    node_id = _node_id()
    if not nt.can_write(nt.note_access(node_id, page_id, user_id)):
        raise HTTPException(status_code=403, detail="read-only share")
    return nt.set_project_links(node_id, page_id, body.project_ids)


@router.get("/projects/{project_id}/notes", response_model=list[d.DashboardPageSummary])
def list_project_notes(
    project_id: UUID, user_id: UUID = Depends(get_user_id)
) -> list[d.DashboardPageSummary]:
    """프로젝트에 링크된 메모 목록('관련 메모')."""
    return nt.list_notes_for_project(_node_id(), project_id, user_id)


# ---------------- Project role options (노션식 선택 속성: 추가/수정/삭제) ----------------
@router.get("/project-roles", response_model=list[d.ProjectRole])
def list_project_roles(user_id: UUID = Depends(get_user_id)) -> list[d.ProjectRole]:
    return d.list_project_roles(_node_id())


@router.post("/project-roles", response_model=d.ProjectRole, status_code=201)
def create_project_role(
    body: d.ProjectRoleIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.ProjectRole:
    node_id = _operator(current)
    try:
        return d.create_project_role(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/project-roles/{role_id}", response_model=d.ProjectRole)
def update_project_role(
    role_id: UUID,
    body: d.ProjectRolePatch,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.ProjectRole:
    node_id = _operator(current)
    try:
        return d.update_project_role(node_id, role_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/project-roles/{role_id}", status_code=204)
def delete_project_role(
    role_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_project_role(node_id, role_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Project assignments (member ↔ project ↔ role) ----------------
@router.get("/assignments", response_model=list[d.Assignment])
def list_assignments(
    project_id: UUID | None = Query(default=None),
    member_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.Assignment]:
    return d.list_assignments(_node_id(), project_id, member_id)


@router.post("/assignments", response_model=d.Assignment, status_code=201)
def create_assignment(
    body: d.AssignmentIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Assignment:
    node_id = _operator(current)
    try:
        return d.upsert_assignment(node_id, body)
    except LookupError as e:
        raise _not_found(e) from e


@router.patch("/assignments/{assignment_id}", response_model=d.Assignment)
def update_assignment(
    assignment_id: UUID,
    body: d.RoleUpdate,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.Assignment:
    node_id = _operator(current)
    try:
        return d.update_assignment(node_id, assignment_id, body)
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_assignment(
    assignment_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_assignment(node_id, assignment_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Weekly plans & retros ----------------
@router.get("/weekly", response_model=d.WeeklyEntry)
def get_weekly(
    project_id: UUID = Query(...),
    week_start: date = Query(...),
    user_id: UUID = Depends(get_user_id),
) -> d.WeeklyEntry:
    return d.get_weekly(_node_id(), project_id, week_start)


@router.put("/weekly", response_model=d.WeeklyEntry)
def put_weekly(
    body: d.WeeklyUpsert,
    background_tasks: BackgroundTasks,
    user_id: UUID = Depends(get_user_id),
) -> d.WeeklyEntry:
    try:
        entry = d.upsert_weekly(_node_id(), body)
    except d.EntryConflict as e:
        raise HTTPException(status_code=409, detail=jsonable_encoder(e.current)) from e
    background_tasks.add_task(reflect_weekly, user_id, entry)
    return entry


@router.get("/weekly/history", response_model=list[d.EntryHistory])
def get_weekly_history(
    project_id: UUID = Query(...),
    week_start: date = Query(...),
    user_id: UUID = Depends(get_user_id),
) -> list[d.EntryHistory]:
    return d.list_weekly_history(_node_id(), project_id, week_start)


@router.get("/weekly/list", response_model=list[d.PeriodSummary])
def list_weekly(
    project_id: UUID = Query(...), user_id: UUID = Depends(get_user_id)
) -> list[d.PeriodSummary]:
    return d.list_weekly(_node_id(), project_id)


# ---------------- Monthly plans & retros ----------------
@router.get("/monthly", response_model=d.MonthlyEntry)
def get_monthly(
    project_id: UUID = Query(...),
    month: date = Query(...),
    user_id: UUID = Depends(get_user_id),
) -> d.MonthlyEntry:
    return d.get_monthly(_node_id(), project_id, month)


@router.put("/monthly", response_model=d.MonthlyEntry)
def put_monthly(body: d.MonthlyUpsert, user_id: UUID = Depends(get_user_id)) -> d.MonthlyEntry:
    try:
        return d.upsert_monthly(_node_id(), body)
    except d.EntryConflict as e:
        raise HTTPException(status_code=409, detail=jsonable_encoder(e.current)) from e


@router.get("/monthly/history", response_model=list[d.EntryHistory])
def get_monthly_history(
    project_id: UUID = Query(...),
    month: date = Query(...),
    user_id: UUID = Depends(get_user_id),
) -> list[d.EntryHistory]:
    return d.list_monthly_history(_node_id(), project_id, month)


@router.get("/monthly/list", response_model=list[d.PeriodSummary])
def list_monthly(
    project_id: UUID = Query(...), user_id: UUID = Depends(get_user_id)
) -> list[d.PeriodSummary]:
    return d.list_monthly(_node_id(), project_id)


# ---------------- Team calendar ----------------
@router.get("/calendar", response_model=list[d.CalendarEvent])
def list_calendar(
    frm: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[d.CalendarEvent]:
    # Node-scoped team calendar (shared by all team members, not user-filtered).
    # Dual-auth so the owner's orthus-mcp (knowledge token) can read the shared
    # team schedule for schedule-aware reply drafting — company internal knowledge.
    return d.list_calendar(_node_id(), frm, to)


@router.get("/calendar/locations", response_model=list[str])
def list_calendar_locations(user_id: UUID = Depends(get_user_id)) -> list[str]:
    return d.list_calendar_locations(_node_id())


@router.post("/calendar/events", response_model=d.CalendarEvent, status_code=201)
def create_event(
    body: d.CalendarEventIn,
    background_tasks: BackgroundTasks,
    # FE 자동 저장(노션식)은 편집 중 자주 저장하되 위키 반영은 편집 종료(닫기) 시
    # 한 번만 하도록 reflect=false로 호출한다(반쯤 입력된 일정의 반복 인덱싱/LLM
    # authoring 비용 방지). 기본 true라 CLI/MCP·기존 호출은 무변경.
    reflect: bool = Query(default=True),
    # FE 자동 저장이 생성하는 멱등 event_id. keepalive/재시도/bfcache 재실행으로 같은
    # id가 여러 번 create돼도 서버가 upsert해 중복 행을 만들지 않는다(미지정 시 서버가
    # uuid4 발급 — 기존 계약 무변경).
    client_event_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> d.CalendarEvent:
    # Dual-auth write: a `knowledge:write` token (owner's orthus-mcp/CLI) can add a
    # shared team calendar event — company internal knowledge. Session callers are
    # unchanged; token writes are rate limited against agent spam.
    enforce_token_dashboard_write_rate_limit(current)
    node_id = _node_id()
    try:
        ev = d.create_event(node_id, current.user_id, body, client_event_id=client_event_id)
    except ValueError as e:
        raise _value_error(e) from e
    if reflect:
        background_tasks.add_task(reflect_calendar_event, current.user_id, ev)
    return ev


@router.patch("/calendar/events/{event_id}", response_model=d.CalendarEvent)
def update_event(
    event_id: UUID,
    body: d.CalendarEventIn,
    background_tasks: BackgroundTasks,
    # reflect=false면 위키 반영 스킵(자동 저장 중간 PATCH). 기본 true는 기존 계약.
    reflect: bool = Query(default=True),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> d.CalendarEvent:
    enforce_token_dashboard_write_rate_limit(current)
    try:
        ev = d.update_event(_node_id(), event_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e
    if reflect:
        background_tasks.add_task(reflect_calendar_event, current.user_id, ev)
    return ev


@router.delete("/calendar/events/{event_id}", status_code=204)
def delete_event(
    event_id: UUID,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> None:
    enforce_token_dashboard_write_rate_limit(current)
    try:
        d.delete_event(_node_id(), event_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Finance: summary ----------------
@router.get("/finance/summary", response_model=d.FinanceSummary)
def finance_summary(user_id: UUID = Depends(get_user_id)) -> d.FinanceSummary:
    return d.finance_summary(_node_id())


# ---------------- Finance: subscriptions ----------------
@router.get("/finance/subscriptions", response_model=list[d.Subscription])
def list_subscriptions(user_id: UUID = Depends(get_user_id)) -> list[d.Subscription]:
    return d.list_subscriptions(_node_id())


@router.post("/finance/subscriptions", response_model=d.Subscription, status_code=201)
def create_subscription(
    body: d.SubscriptionIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Subscription:
    node_id = _operator(current)
    try:
        return d.create_subscription(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/finance/subscriptions/{sub_id}", response_model=d.Subscription)
def update_subscription(
    sub_id: UUID, body: d.SubscriptionIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Subscription:
    node_id = _operator(current)
    try:
        return d.update_subscription(node_id, sub_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/finance/subscriptions/{sub_id}", status_code=204)
def delete_subscription(
    sub_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_subscription(node_id, sub_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Finance: API keys ----------------
@router.get("/finance/api-keys", response_model=list[d.ApiKey])
def list_api_keys(user_id: UUID = Depends(get_user_id)) -> list[d.ApiKey]:
    return d.list_api_keys(_node_id())


@router.post("/finance/api-keys", response_model=d.ApiKey, status_code=201)
def create_api_key(
    body: d.ApiKeyIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.ApiKey:
    node_id = _operator(current)
    try:
        return d.create_api_key(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/finance/api-keys/{key_id}", response_model=d.ApiKey)
def update_api_key(
    key_id: UUID, body: d.ApiKeyIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.ApiKey:
    node_id = _operator(current)
    try:
        return d.update_api_key(node_id, key_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/finance/api-keys/{key_id}", status_code=204)
def delete_api_key(key_id: UUID, current: AuthenticatedUser = Depends(get_current_user)) -> None:
    node_id = _operator(current)
    try:
        d.delete_api_key(node_id, key_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Finance: accounts ----------------
@router.get("/finance/accounts", response_model=list[d.Account])
def list_accounts(user_id: UUID = Depends(get_user_id)) -> list[d.Account]:
    return d.list_accounts(_node_id())


@router.post("/finance/accounts", response_model=d.Account, status_code=201)
def create_account(
    body: d.AccountIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Account:
    node_id = _operator(current)
    try:
        return d.create_account(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/finance/accounts/{account_id}", response_model=d.Account)
def update_account(
    account_id: UUID, body: d.AccountIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Account:
    node_id = _operator(current)
    try:
        return d.update_account(node_id, account_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/finance/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_account(node_id, account_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Finance: ledger ----------------
@router.get("/finance/ledger", response_model=list[d.LedgerEntry])
def list_ledger(user_id: UUID = Depends(get_user_id)) -> list[d.LedgerEntry]:
    return d.list_ledger(_node_id())


@router.post("/finance/ledger", response_model=d.LedgerEntry, status_code=201)
def create_ledger(
    body: d.LedgerIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.LedgerEntry:
    node_id = _operator(current)
    try:
        return d.create_ledger(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/finance/ledger/{ledger_id}", response_model=d.LedgerEntry)
def update_ledger(
    ledger_id: UUID, body: d.LedgerIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.LedgerEntry:
    node_id = _operator(current)
    try:
        return d.update_ledger(node_id, ledger_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/finance/ledger/{ledger_id}", status_code=204)
def delete_ledger(ledger_id: UUID, current: AuthenticatedUser = Depends(get_current_user)) -> None:
    node_id = _operator(current)
    try:
        d.delete_ledger(node_id, ledger_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Infra (GPU / 스토리지 / API 키) ----------------
@router.get("/infra", response_model=list[d.InfraResource])
def list_infra(user_id: UUID = Depends(get_user_id)) -> list[d.InfraResource]:
    return d.list_infra(_node_id())


@router.get("/infra/gpu-summary", response_model=d.GpuSummary)
def gpu_summary(user_id: UUID = Depends(get_user_id)) -> d.GpuSummary:
    return d.gpu_summary(_node_id())


@router.post("/infra/sync-gpu", response_model=d.GpuSyncResult)
def sync_gpu(current: AuthenticatedUser = Depends(get_current_user)) -> d.GpuSyncResult:
    node_id = _operator(current)
    try:
        return d.sync_gpu(node_id)
    except ValueError as e:
        raise _value_error(e) from e


@router.post("/infra/sync-storage", response_model=d.StorageSyncResult)
def sync_storage(current: AuthenticatedUser = Depends(get_current_user)) -> d.StorageSyncResult:
    node_id = _operator(current)
    try:
        return d.sync_storage_from_vm(node_id)
    except ValueError as e:
        raise _value_error(e) from e


@router.get("/infra/ml-panel", response_model=d.MlPanel)
def ml_panel(user_id: UUID = Depends(get_user_id)) -> d.MlPanel:
    _node_id()  # company-node guard; read needs login only (never raises upstream)
    return d.ml_panel()


@router.get("/infra/links", response_model=list[d.InfraResourceLink])
def list_infra_links(user_id: UUID = Depends(get_user_id)) -> list[d.InfraResourceLink]:
    return d.list_infra_links(_node_id())


@router.post("/infra/links", response_model=d.InfraResourceLink, status_code=201)
def add_infra_link(
    body: d.InfraResourceLinkIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.InfraResourceLink:
    node_id = _operator(current)
    return d.add_infra_link(node_id, body)


@router.delete("/infra/links/{link_id}", status_code=204)
def remove_infra_link(
    link_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.remove_infra_link(node_id, link_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/infra/providers", response_model=list[d.InfraProvider])
def list_providers(user_id: UUID = Depends(get_user_id)) -> list[d.InfraProvider]:
    return d.list_providers(_node_id())


@router.post("/infra/providers", response_model=d.InfraProvider, status_code=201)
def create_provider(
    body: d.InfraProviderIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.InfraProvider:
    node_id = _operator(current)
    try:
        return d.create_provider(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/infra/providers/{provider_id}", response_model=d.InfraProvider)
def update_provider(
    provider_id: UUID,
    body: d.InfraProviderIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.InfraProvider:
    node_id = _operator(current)
    try:
        return d.update_provider(node_id, provider_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/infra/providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_provider(node_id, provider_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/infra", response_model=d.InfraResource, status_code=201)
def create_infra(
    body: d.InfraResourceIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.InfraResource:
    node_id = _operator(current)
    try:
        return d.create_infra(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/infra/{resource_id}", response_model=d.InfraResource)
def update_infra(
    resource_id: UUID,
    body: d.InfraResourceIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.InfraResource:
    node_id = _operator(current)
    try:
        return d.update_infra(node_id, resource_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/infra/{resource_id}", status_code=204)
def delete_infra(resource_id: UUID, current: AuthenticatedUser = Depends(get_current_user)) -> None:
    node_id = _operator(current)
    try:
        d.delete_infra(node_id, resource_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Meeting notes (회의록) ----------------
@router.get("/meeting-notes", response_model=list[d.MeetingNote])
def list_meeting_notes(
    meeting_kind: str | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.MeetingNote]:
    return d.list_meeting_notes(_node_id(), meeting_kind, project_id)


@router.post("/meeting-notes", response_model=d.MeetingNote, status_code=201)
def create_meeting_note(
    body: d.MeetingNoteIn,
    background_tasks: BackgroundTasks,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.MeetingNote:
    node_id = _node_id()
    try:
        note = d.create_meeting_note(node_id, current.user_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    background_tasks.add_task(reflect_meeting_note, current.user_id, note)
    return note


@router.patch("/meeting-notes/{note_id}", response_model=d.MeetingNote)
def update_meeting_note(
    note_id: UUID,
    body: d.MeetingNoteIn,
    background_tasks: BackgroundTasks,
    user_id: UUID = Depends(get_user_id),
) -> d.MeetingNote:
    try:
        note = d.update_meeting_note(_node_id(), note_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e
    background_tasks.add_task(reflect_meeting_note, user_id, note)
    return note


@router.delete("/meeting-notes/{note_id}", status_code=204)
def delete_meeting_note(note_id: UUID, user_id: UUID = Depends(get_user_id)) -> None:
    try:
        d.delete_meeting_note(_node_id(), note_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Meeting attachments (PDF 등 미팅자료) ----------------
@router.post(
    "/meeting-notes/{note_id}/attachments",
    response_model=d.MeetingAttachment,
    status_code=201,
)
async def upload_meeting_attachment(
    note_id: UUID,
    file: UploadFile = File(...),
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.MeetingAttachment:
    """회의록에 파일(PDF 등)을 첨부한다. 바이트는 media_store 실파일에 저장하고
    회의↔첨부 매핑만 DB에 남긴다(본문 JSON에 base64로 박지 않음).

    media_store에 실파일 바이트를 쓰는 write이므로 `/dashboard/media` 업로드와
    동일하게 company-node owner/admin으로 가둔다(비-매니저의 임의 파일 반입·용량
    소진·capability URL 남발 차단)."""
    node_id = _operator(current)
    data = await file.read()
    try:
        return d.create_meeting_attachment(
            node_id, note_id, data, file.filename, file.content_type, current.user_id
        )
    except LookupError as e:
        raise _not_found(e) from e
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=413 if "large" in msg else 422, detail=msg) from e


@router.delete("/meeting-notes/{note_id}/attachments/{attachment_id}", status_code=204)
def delete_meeting_attachment(
    note_id: UUID,
    attachment_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> None:
    node_id = _operator(current)
    try:
        d.delete_meeting_attachment(node_id, note_id, attachment_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Culture ----------------
@router.get("/culture", response_model=d.CompanyCulture)
def get_culture(user_id: UUID = Depends(get_user_id)) -> d.CompanyCulture:
    return d.get_culture(_node_id())


@router.put("/culture", response_model=d.CompanyCulture)
def put_culture(
    body: d.CompanyCulture, current: AuthenticatedUser = Depends(get_current_user)
) -> d.CompanyCulture:
    node_id = _operator(current)
    return d.upsert_culture(node_id, body)


# ---------------- Partner companies ----------------
def _reflect_partner(
    node_id: str, user_id: UUID, partner: d.PartnerCompany, tasks: BackgroundTasks
) -> None:
    contacts = d.list_partner_contacts(node_id, partner.partner_id)
    tasks.add_task(reflect_partner_company, user_id, partner, contacts)


@router.get("/partners", response_model=list[d.PartnerCompany])
def list_partners(
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.PartnerCompany]:
    return d.list_partners(_node_id(), project_id)


@router.post("/partners", response_model=d.PartnerCompany, status_code=201)
def create_partner(
    body: d.PartnerCompanyIn,
    background_tasks: BackgroundTasks,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.PartnerCompany:
    node_id = _operator(current)
    try:
        partner = d.create_partner(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    _reflect_partner(node_id, current.user_id, partner, background_tasks)
    return partner


@router.patch("/partners/{partner_id}", response_model=d.PartnerCompany)
def update_partner(
    partner_id: UUID,
    body: d.PartnerCompanyIn,
    background_tasks: BackgroundTasks,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.PartnerCompany:
    node_id = _operator(current)
    try:
        partner = d.update_partner(node_id, partner_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e
    _reflect_partner(node_id, current.user_id, partner, background_tasks)
    return partner


@router.delete("/partners/{partner_id}", status_code=204)
def delete_partner(
    partner_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_partner(node_id, partner_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/partners/{partner_id}/contacts", response_model=list[d.PartnerContact])
def list_partner_contacts(
    partner_id: UUID, user_id: UUID = Depends(get_user_id)
) -> list[d.PartnerContact]:
    try:
        return d.list_partner_contacts(_node_id(), partner_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/partners/{partner_id}/contacts", response_model=d.PartnerContact, status_code=201)
def create_partner_contact(
    partner_id: UUID,
    body: d.PartnerContactIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.PartnerContact:
    node_id = _operator(current)
    try:
        return d.create_partner_contact(node_id, partner_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.patch("/partners/contacts/{contact_id}", response_model=d.PartnerContact)
def update_partner_contact(
    contact_id: UUID,
    body: d.PartnerContactIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.PartnerContact:
    node_id = _operator(current)
    try:
        return d.update_partner_contact(node_id, contact_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/partners/contacts/{contact_id}", status_code=204)
def delete_partner_contact(
    contact_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_partner_contact(node_id, contact_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Support programs (지원사업) ----------------
@router.get("/support-programs", response_model=list[d.SupportProgram])
def list_support_programs(
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.SupportProgram]:
    return d.list_support_programs(_node_id(), project_id)


@router.post("/support-programs", response_model=d.SupportProgram, status_code=201)
def create_support_program(
    body: d.SupportProgramIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.SupportProgram:
    node_id = _operator(current)
    try:
        return d.create_support_program(node_id, body, current.user_id)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/support-programs/{program_id}", response_model=d.SupportProgram)
def update_support_program(
    program_id: UUID,
    body: d.SupportProgramIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.SupportProgram:
    node_id = _operator(current)
    try:
        return d.update_support_program(node_id, program_id, body, current.user_id)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/support-programs/reorder", response_model=list[d.SupportProgram])
def reorder_support_programs(
    body: d.SupportReorderIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[d.SupportProgram]:
    node_id = _operator(current)
    try:
        return d.reorder_support_programs(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.delete("/support-programs/{program_id}", status_code=204)
def delete_support_program(
    program_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_support_program(node_id, program_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- Support notes (지원사업 꿀팁/검색 사이트) ----------------
@router.get("/support-notes", response_model=list[d.SupportNote])
def list_support_notes(
    kind: str | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.SupportNote]:
    return d.list_support_notes(_node_id(), kind)


@router.post("/support-notes", response_model=d.SupportNote, status_code=201)
def create_support_note(
    body: d.SupportNoteIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.SupportNote:
    node_id = _operator(current)
    try:
        return d.create_support_note(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e


@router.patch("/support-notes/{note_id}", response_model=d.SupportNote)
def update_support_note(
    note_id: UUID,
    body: d.SupportNoteIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.SupportNote:
    node_id = _operator(current)
    try:
        return d.update_support_note(node_id, note_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/support-notes/{note_id}", status_code=204)
def delete_support_note(
    note_id: UUID, current: AuthenticatedUser = Depends(get_current_user)
) -> None:
    node_id = _operator(current)
    try:
        d.delete_support_note(node_id, note_id)
    except LookupError as e:
        raise _not_found(e) from e


# ---------------- KPI (OKR + North Star) ----------------
@router.get("/kpis", response_model=list[d.Kpi])
def list_kpis(
    fiscal_year: int | None = Query(default=None),
    cadence: str | None = Query(default=None),
    quarter: int | None = Query(default=None),
    month: int | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    level: str | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.Kpi]:
    return d.list_kpis(
        _node_id(),
        fiscal_year=fiscal_year,
        cadence=cadence,
        quarter=quarter,
        month=month,
        project_id=project_id,
        level=level,
    )


@router.get("/kpis/tree", response_model=list[d.KpiTreeNode])
def kpi_tree(
    fiscal_year: int = Query(...),
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[d.KpiTreeNode]:
    return d.kpi_tree(_node_id(), fiscal_year=fiscal_year, project_id=project_id)


@router.get("/kpis/summary", response_model=d.KpiSummary)
def kpi_summary(
    fiscal_year: int = Query(...),
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> d.KpiSummary:
    return d.kpi_summary(_node_id(), fiscal_year, project_id)


# 정적 경로는 /kpis/{kpi_id}보다 먼저 선언(경로 매칭 안전).
@router.get("/kpis/confidence", response_model=list[d.KpiConfidence])
def list_kpi_confidence(
    fiscal_year: int = Query(...),
    user_id: UUID = Depends(get_user_id),
) -> list[d.KpiConfidence]:
    # 연도 배치(스파크라인용). 읽기라 다른 KPI read와 동일 게이트.
    return d.list_kpi_confidence(_node_id(), fiscal_year)


@router.get("/kpis/retro-card", response_model=d.KpiRetroCard)
def kpi_retro_card(
    mode: str = Query(...),
    ref: date = Query(...),
    project_id: UUID | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> d.KpiRetroCard:
    # 계획·회고 화면의 KPI 회고 카드 — 캘린더 수학(일요일 주/지난달/분기·연도
    # 경계)과 제안 점수 공식을 서버 한 곳에 고정한다.
    try:
        return d.kpi_retro_card(_node_id(), mode, ref, project_id)
    except ValueError as e:
        raise _value_error(e) from e


@router.post("/kpis/{kpi_id}/confidence", response_model=d.KpiConfidence, status_code=201)
def upsert_kpi_confidence(
    kpi_id: UUID,
    body: d.KpiConfidenceIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.KpiConfidence:
    # 주간 팀 리추얼 — plan PUT과 동일하게 인증 멤버 전원 허용(operator 아님).
    # 작성자 귀속은 서버가 user_id 링크로 resolve(클라이언트 불신, 미링크는 NULL).
    node_id = _node_id()
    author = d.resolve_member_id(node_id, current.user_id)
    try:
        return d.upsert_kpi_confidence(node_id, kpi_id, body, author)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/kpis/{kpi_id}/grade", response_model=d.Kpi)
def set_kpi_grade(
    kpi_id: UUID,
    body: d.KpiGradeIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.Kpi:
    # 채점도 회고 리추얼의 일부라 멤버 전원 허용(오너 결정 2026-07-12).
    # graded_by 서버 귀속으로 책임 추적, 재채점=덮어쓰기.
    node_id = _node_id()
    graded_by = d.resolve_member_id(node_id, current.user_id)
    try:
        return d.set_kpi_grade(node_id, kpi_id, body, graded_by)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/kpis/{kpi_id}/grade", response_model=d.Kpi)
def clear_kpi_grade(kpi_id: UUID, current: AuthenticatedUser = Depends(get_current_user)) -> d.Kpi:
    # 채점 말소는 파괴적 — KPI DELETE와 동급으로 operator 게이트.
    node_id = _operator(current)
    try:
        return d.clear_kpi_grade(node_id, kpi_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.post("/kpis", response_model=d.Kpi, status_code=201)
def create_kpi(body: d.KpiIn, current: AuthenticatedUser = Depends(get_current_user)) -> d.Kpi:
    node_id = _operator(current)
    try:
        return d.create_kpi(node_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.patch("/kpis/{kpi_id}", response_model=d.Kpi)
def update_kpi(
    kpi_id: UUID, body: d.KpiIn, current: AuthenticatedUser = Depends(get_current_user)
) -> d.Kpi:
    node_id = _operator(current)
    try:
        return d.update_kpi(node_id, kpi_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.delete("/kpis/{kpi_id}", status_code=204)
def delete_kpi(kpi_id: UUID, current: AuthenticatedUser = Depends(get_current_user)) -> None:
    node_id = _operator(current)
    try:
        d.delete_kpi(node_id, kpi_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/kpis/{kpi_id}/checkins", response_model=list[d.KpiCheckin])
def list_kpi_checkins(kpi_id: UUID, user_id: UUID = Depends(get_user_id)) -> list[d.KpiCheckin]:
    return d.list_kpi_checkins(_node_id(), kpi_id)


@router.post("/kpis/{kpi_id}/checkins", response_model=d.KpiCheckin, status_code=201)
def create_kpi_checkin(
    kpi_id: UUID,
    body: d.KpiCheckinIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> d.KpiCheckin:
    node_id = _operator(current)
    try:
        return d.create_kpi_checkin(node_id, kpi_id, body)
    except ValueError as e:
        raise _value_error(e) from e
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/kpis/{kpi_id}/linked", response_model=d.KpiLinked)
def kpi_linked(kpi_id: UUID, user_id: UUID = Depends(get_user_id)) -> d.KpiLinked:
    try:
        return d.kpi_linked(_node_id(), kpi_id)
    except LookupError as e:
        raise _not_found(e) from e


@router.get("/kpis/{kpi_id}/weekly-items", response_model=list[d.KpiWeeklyItem])
def kpi_weekly_items(kpi_id: UUID, user_id: UUID = Depends(get_user_id)) -> list[d.KpiWeeklyItem]:
    try:
        return d.kpi_weekly_items(_node_id(), kpi_id)
    except LookupError as e:
        raise _not_found(e) from e
