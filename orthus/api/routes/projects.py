"""Project assignment console (P2, docs/p2-fe-and-sources.md).

GET /projects lists each Notion db_name with its current project, row count, and
whether the project is a user OVERRIDE or the ingest DEFAULT. PATCH /projects sets
an override for a db_name and re-tags the existing content (structured + 1:1
wiki/corpus), persisting across reseeds."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from orthus.api.deps import get_user_id
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.auth import AuthenticatedUser
from orthus.connectors.project_map import PROJECTS
from orthus.db import session
from orthus.projects import clear_project_override, set_project_override
from orthus.tables import notion_rows, project_overrides

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectGroup(BaseModel):
    db_name: str
    project: str
    count: int
    source: str  # 'override' | 'default'


class ProjectsOut(BaseModel):
    projects: list[str]  # the valid enum values
    groups: list[ProjectGroup]


class ProjectPatchIn(BaseModel):
    db_name: str
    project: Optional[str] = None


class ProjectPatchOut(BaseModel):
    db_name: str
    project: str
    source: str  # 'override' | 'auto'
    updated: dict[str, int]


@router.get("", response_model=ProjectsOut)
def list_projects(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> ProjectsOut:
    # Dual-auth so the owner's orthus-mcp (knowledge token) + Solar agentic /ask
    # can read the project directory — company internal knowledge, low-risk.
    with session() as s:
        rows = s.execute(
            select(
                notion_rows.c.db_name,
                notion_rows.c.project,
                func.count().label("count"),
            )
            .group_by(notion_rows.c.db_name, notion_rows.c.project)
            .order_by(notion_rows.c.db_name, notion_rows.c.project)
        ).all()
        overridden = {r[0] for r in s.execute(select(project_overrides.c.db_name)).all()}
    groups = [
        ProjectGroup(
            db_name=db_name,
            project=project,
            count=count,
            source="override" if db_name in overridden else "default",
        )
        for db_name, project, count in rows
    ]
    return ProjectsOut(projects=list(PROJECTS), groups=groups)


@router.patch("", response_model=ProjectPatchOut)
def patch_project(body: ProjectPatchIn, user_id: UUID = Depends(get_user_id)) -> ProjectPatchOut:
    if body.project is None:
        # Clear override → revert to auto-classification heuristic.
        resolved, counts = clear_project_override(body.db_name)
        return ProjectPatchOut(
            db_name=body.db_name,
            project=resolved,
            source="auto",
            updated=counts,
        )
    if body.project not in PROJECTS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid project {body.project!r}; must be one of {list(PROJECTS)}",
        )
    counts = set_project_override(body.db_name, body.project)
    return ProjectPatchOut(
        db_name=body.db_name, project=body.project, source="override", updated=counts
    )
