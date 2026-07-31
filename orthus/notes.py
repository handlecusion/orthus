"""개인 메모(personal notes) — 팀 공유 + 얼라인 확인 + 프로젝트 링크.

메모 본문/CRUD는 `orthus/dashboard.py`의 `dashboard_pages`(kind='memo')를 그대로
쓰고, 이 모듈은 그 위에 owner-scope 접근 판정과 공유/링크 부가 데이터를 얹는다.

접근 게이트는 최상위 메모(kind='memo') 단위다. 본문 안 중첩 subpage(kind='page')는
기존처럼 node 스코프이며 별도 게이트하지 않는다(부모 메모를 열 수 있어야만 도달).

회사 노드 전용(라우트가 `_node_id()`로 강제). 레거시 메모(owner_id IS NULL)는 회사
공용으로 취급해 누구나 열람/수정 가능(과거 동작 보존).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select

from orthus.dashboard import DashboardPageSummary, _page_preview, member_identity
from orthus.db import session
from orthus.tables import (
    dashboard_pages,
    dashboard_projects,
    note_project_links,
    note_shares,
    team_members,
)

VALID_PERMISSIONS = ("view", "edit")


# --------------------------------------------------------------------------
# 모델
# --------------------------------------------------------------------------
class NoteShare(BaseModel):
    share_id: UUID
    page_id: UUID
    shared_with: UUID
    shared_with_name: str
    shared_with_color: str | None = None
    permission: str = "view"
    created_by: UUID
    acknowledged_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None


class NoteShareIn(BaseModel):
    shared_with: list[UUID]
    permission: str = "view"


class NoteProjectLink(BaseModel):
    project_id: UUID
    name: str
    color: str | None = None


class NoteProjectLinkIn(BaseModel):
    project_ids: list[UUID]


class SharedNoteSummary(BaseModel):
    page_id: UUID
    title: str
    icon: str | None = None
    updated_at: datetime | None = None
    owner_id: UUID | None = None
    owner_name: str | None = None
    permission: str = "view"
    acknowledged_at: datetime | None = None


class NoteLibraryItem(BaseModel):
    """권한 확인이 끝난 메모 목록 한 행.

    `/notes`의 프로젝트 facet과 데이터베이스형 목록은 이 projection 하나만 읽는다.
    본문은 최대 4KB prefix에서 뽑은 짧은 preview만 포함하고, 프로젝트/공유 상태는
    선택된 page_id 집합을 대상으로 일괄 조회해 메모 수만큼 API를 반복하지 않는다.
    """

    page_id: UUID
    title: str
    icon: str | None = None
    preview: str | None = None
    updated_at: datetime | None = None
    owner_id: UUID | None = None
    owner_name: str | None = None
    access: Literal["owner", "edit", "view"]
    permission: Literal["edit", "view"] | None = None
    acknowledged_at: datetime | None = None
    projects: list[NoteProjectLink] = Field(default_factory=list)
    share_count: int = 0
    acknowledged_count: int = 0


# --------------------------------------------------------------------------
# 접근 판정
# --------------------------------------------------------------------------
def note_access(node_id: str, page_id: UUID, user_id: UUID) -> str:
    """메모에 대한 유저의 접근 권한: 'owner' | 'edit' | 'view' | 'none'.

    - 페이지 없음 → 'none'
    - subpage(kind != 'memo') → 'edit' (node 스코프, 게이트 안 함)
    - owner_id IS NULL (레거시 공용) → 'edit'
    - owner_id == user → 'owner'
    - 그 외 → note_shares.permission ('view'|'edit') 또는 'none'
    """
    with session() as s:
        row = s.execute(
            select(dashboard_pages.c.kind, dashboard_pages.c.owner_id).where(
                dashboard_pages.c.node_id == node_id,
                dashboard_pages.c.page_id == page_id,
            )
        ).first()
        if row is None:
            return "none"
        if row.kind != "memo":
            return "edit"
        if row.owner_id is None:
            return "edit"
        if row.owner_id == user_id:
            return "owner"
        share = s.execute(
            select(note_shares.c.permission).where(
                note_shares.c.page_id == page_id,
                note_shares.c.shared_with == user_id,
            )
        ).first()
    if share is None:
        return "none"
    return "edit" if share.permission == "edit" else "view"


def can_write(access: str) -> bool:
    return access in ("owner", "edit")


def can_read(access: str) -> bool:
    return access in ("owner", "edit", "view")


# --------------------------------------------------------------------------
# 공유
# --------------------------------------------------------------------------
def list_shares(node_id: str, page_id: UUID) -> list[NoteShare]:
    with session() as s:
        rows = s.execute(
            select(note_shares).where(
                note_shares.c.node_id == node_id,
                note_shares.c.page_id == page_id,
            )
        ).all()
    out: list[NoteShare] = []
    for r in rows:
        name, color = member_identity(node_id, r.shared_with)
        out.append(
            NoteShare(
                share_id=r.share_id,
                page_id=r.page_id,
                shared_with=r.shared_with,
                shared_with_name=name,
                shared_with_color=color,
                permission=r.permission,
                created_by=r.created_by,
                acknowledged_at=r.acknowledged_at,
                created_at=r.created_at,
            )
        )
    out.sort(key=lambda x: x.shared_with_name)
    return out


def add_shares(
    node_id: str,
    page_id: UUID,
    created_by: UUID,
    shared_with: list[UUID],
    permission: str = "view",
) -> list[NoteShare]:
    """대상별로 공유행을 upsert한다(권한만 갱신, ack는 보존)."""
    perm = permission if permission in VALID_PERMISSIONS else "view"
    with session() as s:
        for target in shared_with:
            if target == created_by:
                continue  # 자기 자신에게 공유 무의미
            existing = s.execute(
                select(note_shares.c.share_id).where(
                    note_shares.c.page_id == page_id,
                    note_shares.c.shared_with == target,
                )
            ).first()
            if existing is not None:
                s.execute(
                    note_shares.update()
                    .where(note_shares.c.share_id == existing.share_id)
                    .values(permission=perm)
                )
            else:
                s.execute(
                    note_shares.insert().values(
                        share_id=uuid.uuid4(),
                        node_id=node_id,
                        page_id=page_id,
                        shared_with=target,
                        permission=perm,
                        created_by=created_by,
                    )
                )
        s.commit()
    return list_shares(node_id, page_id)


def delete_share(node_id: str, page_id: UUID, share_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(note_shares).where(
                note_shares.c.node_id == node_id,
                note_shares.c.page_id == page_id,
                note_shares.c.share_id == share_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("share not found")


def acknowledge(node_id: str, page_id: UUID, user_id: UUID) -> None:
    """공유 수신자가 '얼라인 확인'을 남긴다."""
    with session() as s:
        result = s.execute(
            note_shares.update()
            .where(
                note_shares.c.node_id == node_id,
                note_shares.c.page_id == page_id,
                note_shares.c.shared_with == user_id,
            )
            .values(acknowledged_at=func.now())
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("share not found")


def list_shared_pages(node_id: str, user_id: UUID) -> list[SharedNoteSummary]:
    """나에게 공유된 메모 목록(공유자/권한/확인 상태 포함)."""
    j = note_shares.join(dashboard_pages, note_shares.c.page_id == dashboard_pages.c.page_id)
    with session() as s:
        rows = s.execute(
            select(
                dashboard_pages.c.page_id,
                dashboard_pages.c.title,
                dashboard_pages.c.icon,
                dashboard_pages.c.updated_at,
                dashboard_pages.c.owner_id,
                note_shares.c.permission,
                note_shares.c.acknowledged_at,
            )
            .select_from(j)
            .where(
                note_shares.c.node_id == node_id,
                note_shares.c.shared_with == user_id,
            )
            .order_by(dashboard_pages.c.updated_at.desc())
        ).all()
    out: list[SharedNoteSummary] = []
    for r in rows:
        owner_name = None
        if r.owner_id is not None:
            owner_name, _ = member_identity(node_id, r.owner_id)
        out.append(
            SharedNoteSummary(
                page_id=r.page_id,
                title=r.title,
                icon=r.icon,
                updated_at=r.updated_at,
                owner_id=r.owner_id,
                owner_name=owner_name,
                permission=r.permission,
                acknowledged_at=r.acknowledged_at,
            )
        )
    return out


def _project_links_by_page(
    s, node_id: str, page_ids: list[UUID]
) -> dict[UUID, list[NoteProjectLink]]:
    """선별된 메모 집합의 프로젝트를 한 번에 hydrate한다."""
    if not page_ids:
        return {}
    joined = note_project_links.join(
        dashboard_projects,
        and_(
            note_project_links.c.project_id == dashboard_projects.c.project_id,
            note_project_links.c.node_id == dashboard_projects.c.node_id,
        ),
    )
    rows = s.execute(
        select(
            note_project_links.c.page_id,
            dashboard_projects.c.project_id,
            dashboard_projects.c.name,
            dashboard_projects.c.color,
        )
        .select_from(joined)
        .where(
            note_project_links.c.node_id == node_id,
            note_project_links.c.page_id.in_(page_ids),
        )
        .order_by(dashboard_projects.c.name)
    ).all()
    grouped: dict[UUID, list[NoteProjectLink]] = defaultdict(list)
    for row in rows:
        grouped[row.page_id].append(
            NoteProjectLink(project_id=row.project_id, name=row.name, color=row.color)
        )
    return dict(grouped)


def _share_counts_by_page(s, node_id: str, page_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    if not page_ids:
        return {}
    rows = s.execute(
        select(
            note_shares.c.page_id,
            func.count(note_shares.c.share_id).label("share_count"),
            func.count(note_shares.c.acknowledged_at).label("acknowledged_count"),
        )
        .where(
            note_shares.c.node_id == node_id,
            note_shares.c.page_id.in_(page_ids),
        )
        .group_by(note_shares.c.page_id)
    ).all()
    return {row.page_id: (int(row.share_count), int(row.acknowledged_count)) for row in rows}


def _member_names_by_user(s, node_id: str, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = s.execute(
        select(team_members.c.user_id, team_members.c.name).where(
            team_members.c.node_id == node_id,
            team_members.c.user_id.in_(user_ids),
        )
    ).all()
    return {row.user_id: row.name for row in rows if row.user_id is not None}


def list_library_pages(
    node_id: str,
    user_id: UUID,
    *,
    scope: Literal["mine", "shared"] = "mine",
) -> list[NoteLibraryItem]:
    """Apple Notes형 목록을 위한 권한 안전 bulk projection.

    권한 predicate로 base page를 먼저 제한한 다음 그 page_id에 대해서만 프로젝트와
    공유 집계를 읽는다. 따라서 타인의 비공개 제목/preview/프로젝트 관계가 응답에
    섞이지 않고, 메모 수에 비례하는 API/DB N+1도 만들지 않는다.
    """
    body_prefix = func.left(dashboard_pages.c.body, 4000).label("body_prefix")

    with session() as s:
        if scope == "mine":
            rows = s.execute(
                select(
                    dashboard_pages.c.page_id,
                    dashboard_pages.c.title,
                    dashboard_pages.c.icon,
                    dashboard_pages.c.updated_at,
                    dashboard_pages.c.owner_id,
                    body_prefix,
                )
                .where(
                    dashboard_pages.c.node_id == node_id,
                    dashboard_pages.c.kind == "memo",
                    or_(
                        dashboard_pages.c.owner_id == user_id,
                        dashboard_pages.c.owner_id.is_(None),
                    ),
                )
                .order_by(dashboard_pages.c.updated_at.desc())
            ).all()
        else:
            joined = note_shares.join(
                dashboard_pages,
                and_(
                    note_shares.c.page_id == dashboard_pages.c.page_id,
                    note_shares.c.node_id == dashboard_pages.c.node_id,
                ),
            )
            rows = s.execute(
                select(
                    dashboard_pages.c.page_id,
                    dashboard_pages.c.title,
                    dashboard_pages.c.icon,
                    dashboard_pages.c.updated_at,
                    dashboard_pages.c.owner_id,
                    note_shares.c.permission,
                    note_shares.c.acknowledged_at,
                    body_prefix,
                )
                .select_from(joined)
                .where(
                    note_shares.c.node_id == node_id,
                    note_shares.c.shared_with == user_id,
                    dashboard_pages.c.kind == "memo",
                )
                .order_by(dashboard_pages.c.updated_at.desc())
            ).all()

        page_ids = [row.page_id for row in rows]
        projects = _project_links_by_page(s, node_id, page_ids)
        share_counts = _share_counts_by_page(s, node_id, page_ids) if scope == "mine" else {}
        owner_names = _member_names_by_user(
            s,
            node_id,
            {row.owner_id for row in rows if row.owner_id is not None},
        )

    items: list[NoteLibraryItem] = []
    for row in rows:
        share_count, acknowledged_count = share_counts.get(row.page_id, (0, 0))
        permission: Literal["edit", "view"] | None = None
        access: Literal["owner", "edit", "view"]
        if scope == "shared":
            # Legacy/manual rows have no DB CHECK constraint. Keep malformed
            # values fail-closed instead of letting one row 500 the whole list.
            permission = "edit" if row.permission == "edit" else "view"
            access = permission
        else:
            # owner_id NULL is the legacy company-public memo contract.
            access = "owner" if row.owner_id == user_id else "edit"
        items.append(
            NoteLibraryItem(
                page_id=row.page_id,
                title=row.title,
                icon=row.icon,
                preview=_page_preview(row.body_prefix),
                updated_at=row.updated_at,
                owner_id=row.owner_id,
                owner_name=owner_names.get(row.owner_id) if row.owner_id is not None else None,
                access=access,
                permission=permission,
                acknowledged_at=(row.acknowledged_at if scope == "shared" else None),
                projects=projects.get(row.page_id, []),
                share_count=share_count,
                acknowledged_count=acknowledged_count,
            )
        )
    return items


# --------------------------------------------------------------------------
# 프로젝트 링크
# --------------------------------------------------------------------------
def list_project_links(node_id: str, page_id: UUID) -> list[NoteProjectLink]:
    j = note_project_links.join(
        dashboard_projects,
        note_project_links.c.project_id == dashboard_projects.c.project_id,
    )
    with session() as s:
        rows = s.execute(
            select(
                dashboard_projects.c.project_id,
                dashboard_projects.c.name,
                dashboard_projects.c.color,
            )
            .select_from(j)
            .where(note_project_links.c.node_id == node_id, note_project_links.c.page_id == page_id)
            .order_by(dashboard_projects.c.name)
        ).all()
    return [NoteProjectLink(project_id=r.project_id, name=r.name, color=r.color) for r in rows]


def set_project_links(
    node_id: str, page_id: UUID, project_ids: list[UUID]
) -> list[NoteProjectLink]:
    """메모의 프로젝트 링크 집합을 통째로 교체한다(존재하는 프로젝트만)."""
    unique_ids = list(dict.fromkeys(project_ids))
    with session() as s:
        valid_rows = (
            s.execute(
                select(dashboard_projects.c.project_id).where(
                    dashboard_projects.c.node_id == node_id,
                    dashboard_projects.c.project_id.in_(unique_ids),
                )
            ).all()
            if unique_ids
            else []
        )
        valid = {r.project_id for r in valid_rows}
        s.execute(
            delete(note_project_links).where(
                note_project_links.c.node_id == node_id,
                note_project_links.c.page_id == page_id,
            )
        )
        for pid in unique_ids:
            if pid not in valid:
                continue
            s.execute(
                note_project_links.insert().values(
                    page_id=page_id,
                    project_id=pid,
                    node_id=node_id,
                )
            )
        s.commit()
    return list_project_links(node_id, page_id)


def cleanup_note(node_id: str, page_id: UUID) -> None:
    """메모 삭제 시 딸린 공유행/프로젝트 링크를 함께 제거한다(고아 방지)."""
    with session() as s:
        s.execute(
            delete(note_shares).where(
                note_shares.c.node_id == node_id, note_shares.c.page_id == page_id
            )
        )
        s.execute(
            delete(note_project_links).where(
                note_project_links.c.node_id == node_id,
                note_project_links.c.page_id == page_id,
            )
        )
        s.commit()


def list_notes_for_project(
    node_id: str, project_id: UUID, user_id: UUID
) -> list[DashboardPageSummary]:
    """프로젝트에 링크된 메모 중 현재 사용자가 볼 수 있는 목록.

    프로젝트 링크 자체가 비공개 메모의 제목을 새는 우회 경로가 되지 않도록 owner,
    legacy-public, explicit share 중 하나를 만족하는 최상위 메모만 반환한다.
    """
    j = note_project_links.join(
        dashboard_pages,
        and_(
            note_project_links.c.page_id == dashboard_pages.c.page_id,
            note_project_links.c.node_id == dashboard_pages.c.node_id,
        ),
    )
    shared = (
        select(note_shares.c.share_id)
        .where(
            note_shares.c.node_id == node_id,
            note_shares.c.page_id == dashboard_pages.c.page_id,
            note_shares.c.shared_with == user_id,
        )
        .exists()
    )
    with session() as s:
        rows = s.execute(
            select(
                dashboard_pages.c.page_id,
                dashboard_pages.c.title,
                dashboard_pages.c.icon,
                dashboard_pages.c.kind,
                dashboard_pages.c.updated_at,
                dashboard_pages.c.owner_id,
            )
            .select_from(j)
            .where(
                and_(
                    note_project_links.c.node_id == node_id,
                    note_project_links.c.project_id == project_id,
                    dashboard_pages.c.node_id == node_id,
                    dashboard_pages.c.kind == "memo",
                    or_(
                        dashboard_pages.c.owner_id == user_id,
                        dashboard_pages.c.owner_id.is_(None),
                        shared,
                    ),
                )
            )
            .order_by(dashboard_pages.c.updated_at.desc())
        ).all()
    return [
        DashboardPageSummary(
            page_id=r.page_id,
            title=r.title,
            icon=r.icon,
            kind=r.kind,
            updated_at=r.updated_at,
            owner_id=r.owner_id,
        )
        for r in rows
    ]
