"""노션식 인라인 데이터베이스 (프로젝트/페이지 임베드).

회사 프로젝트를 노션 페이지처럼 만들기 위한 데이터 레이어다. 노션의 핵심 모델은
"칸반 보드 = 표 = 데이터베이스"이며 같은 행 컬렉션을 다르게 보는 것뿐이다. 그래서
하나의 모델(`project_databases` + `project_database_rows`)이 표 뷰와 보드(칸반) 뷰를
모두 표현한다.

- properties: 타입 있는 속성 정의 배열. 첫 title 속성이 행의 이름이다.
- views: 표(table) / 보드(board) 뷰. board는 select/status 속성으로 group_by 한다.
- rows.props: property id → 값 맵.

company-node 전용이며 라우트(orthus/api/routes/dashboard.py)는 thin하게 유지한다.
"""

from __future__ import annotations

import mimetypes
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Text, column, delete, func, select, values
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from orthus.db import session
from orthus.tables import (
    dashboard_projects,
    project_database_files,
    project_database_rows,
    project_databases,
)

# 지원하는 속성 타입. title은 행 이름(데이터베이스당 정확히 1개).
# created_time/last_edited_time은 행의 created_at/updated_at에서 파생(읽기 전용).
PROP_TYPES = {
    "title",
    "text",
    "number",
    "select",
    "status",
    "multi_select",
    "date",
    "checkbox",
    "person",
    "url",
    "files",
    "created_time",
    "last_edited_time",
}
SELECT_LIKE = {"select", "status"}
OPTION_TYPES = {"select", "status", "multi_select"}
VIEW_TYPES = {"table", "board"}
FILTER_OPS = {
    "contains",
    "is",
    "is_not",
    "is_empty",
    "is_not_empty",
    "checked",
    "unchecked",
}
SORT_DIRECTIONS = {"asc", "desc"}
# 노션 status 컬럼 그룹(보드 컬럼 정렬 순서). select에는 의미 없음.
STATUS_GROUPS = ("to_do", "in_progress", "complete")
MAX_FILE_BYTES = 50 * 1024 * 1024
_SKIP_PROP = object()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------
class SelectOption(BaseModel):
    id: str
    name: str
    color: str = "gray"
    # status 옵션의 컬럼 그룹(to_do|in_progress|complete). select/multi_select는 None.
    group: Optional[str] = None


class PropertyDef(BaseModel):
    id: str
    name: str
    type: str
    options: list[SelectOption] = Field(default_factory=list)


class ViewFilterDef(BaseModel):
    id: str
    prop_id: str
    op: str
    value: Any = None


class ViewSortDef(BaseModel):
    id: str
    prop_id: str
    direction: Literal["asc", "desc"] = "asc"


class ViewDef(BaseModel):
    id: str
    name: str
    type: str = "table"
    group_by: Optional[str] = None
    cover_property: Optional[str] = None
    hidden: list[str] = Field(default_factory=list)
    filters: list[ViewFilterDef] = Field(default_factory=list)
    sorts: list[ViewSortDef] = Field(default_factory=list)
    column_widths: dict[str, Any] = Field(default_factory=dict)


class ProjectDatabase(BaseModel):
    database_id: UUID
    project_id: Optional[UUID] = None
    title: str
    icon: Optional[str] = None
    properties: list[PropertyDef]
    views: list[ViewDef]


class ProjectDatabaseRow(BaseModel):
    row_id: UUID
    database_id: UUID
    props: dict[str, Any] = Field(default_factory=dict)
    sort_order: float = 0
    # 노션처럼 행 = 페이지: icon + cover + 본문(BlockNote JSON) + 시스템 시각.
    icon: Optional[str] = None
    cover: Optional[str] = None
    body: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProjectDatabaseBundle(BaseModel):
    database: ProjectDatabase
    # 기본은 body(BlockNote JSON) 포함. `?body=none` opt-in 목록 조회에서만 body=None
    # (구 FE가 번들 body를 권위값으로 쓰므로 생략을 기본값으로 하면 배포 skew 때
    # 본문이 유실된다) — 생략 시 단건 get_row로 지연 로드.
    rows: list[ProjectDatabaseRow]


class ProjectDatabaseMeta(BaseModel):
    """행 select 없이 쓰는 경량 요약 — 링크 행/브레드크럼용(제목 + 행 수)."""

    database_id: UUID
    project_id: Optional[UUID] = None
    title: str
    icon: Optional[str] = None
    row_count: int = 0


class ProjectDatabaseFileValue(BaseModel):
    id: str
    name: str
    type: Literal["image", "video", "file"]
    mime: Optional[str] = None
    size: Optional[int] = None
    url: Optional[str] = None
    dataUrl: Optional[str] = None


@dataclass(frozen=True)
class ProjectDatabaseFileBlob:
    file_id: UUID
    filename: str
    mime_type: str
    kind: str
    data: bytes


class DatabaseIn(BaseModel):
    project_id: Optional[UUID] = None
    title: str = "데이터베이스"
    icon: Optional[str] = None
    # 'table' | 'board' — 어느 뷰를 먼저 보여줄지(둘 다 생성된다).
    default_view: Literal["table", "board"] = "table"
    # 'basic' = 이름+상태, 'tasks' = 노션 협업업무표(상태 그룹/담당자/우선순위/소요시간/종료일).
    template: Literal["basic", "tasks"] = "basic"


class DatabasePatch(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    properties: Optional[list[PropertyDef]] = None
    views: Optional[list[ViewDef]] = None


class RowIn(BaseModel):
    props: dict[str, Any] = Field(default_factory=dict)
    sort_order: Optional[float] = None
    icon: Optional[str] = None
    cover: Optional[str] = None
    body: Optional[str] = None


class RowPatch(BaseModel):
    props: Optional[dict[str, Any]] = None
    sort_order: Optional[float] = None
    icon: Optional[str] = None
    # icon/body와 같은 규약: None=무변경, ""=커버 제거.
    cover: Optional[str] = None
    body: Optional[str] = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _db_out(row) -> ProjectDatabase:
    return ProjectDatabase(
        database_id=row.database_id,
        project_id=row.project_id,
        title=row.title,
        icon=row.icon,
        properties=[PropertyDef(**p) for p in (row.properties or [])],
        views=[ViewDef(**v) for v in (row.views or [])],
    )


def _row_out(row) -> ProjectDatabaseRow:
    return ProjectDatabaseRow(
        row_id=row.row_id,
        database_id=row.database_id,
        props=row.props or {},
        sort_order=row.sort_order or 0,
        icon=row.icon,
        cover=row.cover,
        body=row.body,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_properties(props: list[PropertyDef]) -> list[dict]:
    """속성 정의를 검증하고 직렬화한다. title 정확히 1개, 타입 allowlist, 옵션 id 보장."""
    if not props:
        raise ValueError("at least one property (title) required")
    titles = [p for p in props if p.type == "title"]
    if len(titles) != 1:
        raise ValueError("exactly one title property required")
    seen: set[str] = set()
    out: list[dict] = []
    for p in props:
        if p.type not in PROP_TYPES:
            raise ValueError(f"invalid property type {p.type!r}")
        pid = p.id or _short_id()
        if pid in seen:
            raise ValueError(f"duplicate property id {pid!r}")
        seen.add(pid)
        opts: list[dict] = []
        if p.type in OPTION_TYPES:
            for o in p.options:
                opt = {"id": o.id or _short_id(), "name": o.name, "color": o.color or "gray"}
                # status 옵션만 컬럼 그룹을 보존한다(보드 정렬용).
                if p.type == "status" and o.group in STATUS_GROUPS:
                    opt["group"] = o.group
                opts.append(opt)
        out.append({"id": pid, "name": p.name or "속성", "type": p.type, "options": opts})
    return out


COLUMN_WIDTH_MIN = 96
COLUMN_WIDTH_MAX = 720


def _normalize_column_widths(widths: dict[str, Any], prop_ids: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for prop_id, raw in (widths or {}).items():
        if prop_id not in prop_ids or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        width = int(raw)
        out[prop_id] = max(COLUMN_WIDTH_MIN, min(COLUMN_WIDTH_MAX, width))
    return out


def _validate_views(views: list[ViewDef], properties: list[dict]) -> list[dict]:
    if not views:
        raise ValueError("at least one view required")
    prop_ids = {p["id"] for p in properties}
    select_ids = {p["id"] for p in properties if p["type"] in SELECT_LIKE}
    file_ids = {p["id"] for p in properties if p["type"] == "files"}
    out: list[dict] = []
    for v in views:
        if v.type not in VIEW_TYPES:
            raise ValueError(f"invalid view type {v.type!r}")
        group_by = v.group_by
        if v.type == "board":
            # board는 select/status 기준으로 묶는다. 미지정이면 첫 select 속성으로.
            if group_by not in select_ids:
                group_by = next((p["id"] for p in properties if p["type"] in SELECT_LIKE), None)
        elif group_by is not None and group_by not in prop_ids:
            group_by = None
        cover_property = (
            v.cover_property if v.type == "board" and v.cover_property in file_ids else None
        )
        out.append(
            {
                "id": v.id or _short_id(),
                "name": v.name or ("보드" if v.type == "board" else "표"),
                "type": v.type,
                "group_by": group_by,
                "cover_property": cover_property,
                "hidden": [h for h in v.hidden if h in prop_ids],
                "filters": [
                    {"id": f.id or _short_id(), "prop_id": f.prop_id, "op": f.op, "value": f.value}
                    for f in v.filters
                    if f.prop_id in prop_ids and f.op in FILTER_OPS
                ],
                "sorts": [
                    {
                        "id": s.id or _short_id(),
                        "prop_id": s.prop_id,
                        "direction": s.direction if s.direction in SORT_DIRECTIONS else "asc",
                    }
                    for s in v.sorts
                    if s.prop_id in prop_ids
                ],
                "column_widths": _normalize_column_widths(v.column_widths, prop_ids),
            }
        )
    return out


def _view_defs_from_json(views: list[dict]) -> list[ViewDef]:
    return [ViewDef(**v) for v in views]


def _unique_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = item.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _normalize_number(value: Any) -> int | float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number.is_integer():
        return int(number)
    return number


def _normalize_file_values(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        name = str(item.get("name") or "첨부 파일").strip() or "첨부 파일"
        mime = item.get("mime") if isinstance(item.get("mime"), str) else None
        raw_type = item.get("type")
        kind = raw_type if raw_type in {"image", "video", "file"} else _file_kind(mime or "", name)
        raw_size = item.get("size")
        size: int | None = None
        if isinstance(raw_size, (int, float)) and not isinstance(raw_size, bool) and raw_size >= 0:
            size = int(raw_size)
        url = item.get("url") if isinstance(item.get("url"), str) else None
        data_url = item.get("dataUrl") if isinstance(item.get("dataUrl"), str) else None
        file_value: dict[str, Any] = {"id": raw_id.strip(), "name": name[:255], "type": kind}
        if mime is not None:
            file_value["mime"] = mime
        if size is not None:
            file_value["size"] = size
        if url is not None:
            file_value["url"] = url
        if data_url is not None:
            file_value["dataUrl"] = data_url
        out.append(file_value)
    return out


def _normalize_prop_value(prop: dict, value: Any) -> Any:
    prop_type = prop["type"]
    if prop_type in {"created_time", "last_edited_time"}:
        return _SKIP_PROP
    if value is None or value == "":
        if prop_type == "multi_select":
            return []
        if prop_type == "files":
            return []
        return None
    if prop_type in {"title", "text", "url"}:
        return str(value).strip() or None
    if prop_type == "number":
        return _normalize_number(value)
    if prop_type == "date":
        return value.strip() if isinstance(value, str) and value.strip() else None
    if prop_type == "checkbox":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        return None
    if prop_type == "person":
        return _unique_strings(value)
    if prop_type in SELECT_LIKE:
        valid = {str(o["id"]) for o in prop.get("options", [])}
        return str(value) if str(value) in valid else None
    if prop_type == "multi_select":
        valid = {str(o["id"]) for o in prop.get("options", [])}
        return [item for item in _unique_strings(value) if item in valid]
    if prop_type == "files":
        return _normalize_file_values(value)
    return value


def _normalize_row_props_for_schema(
    props: dict[str, Any], properties: list[dict]
) -> dict[str, Any]:
    raw = props or {}
    out: dict[str, Any] = {}
    for prop in properties:
        prop_id = prop["id"]
        if prop_id not in raw:
            continue
        value = _normalize_prop_value(prop, raw[prop_id])
        if value is _SKIP_PROP:
            continue
        out[prop_id] = value
    return out


def _pruned_props_for_schema(
    props: dict[str, Any], properties: list[dict]
) -> tuple[dict[str, Any], bool]:
    """Drop deleted properties and invalid option references from a row props map."""
    next_props = _normalize_row_props_for_schema(props, properties)
    return next_props, next_props != (props or {})


def _prune_rows_for_schema(s, node_id: str, database_id: UUID, properties: list[dict]) -> None:
    rows = s.execute(
        select(project_database_rows.c.row_id, project_database_rows.c.props).where(
            project_database_rows.c.node_id == node_id,
            project_database_rows.c.database_id == database_id,
        )
    ).all()
    for row in rows:
        props, changed = _pruned_props_for_schema(row.props or {}, properties)
        if not changed:
            continue
        s.execute(
            project_database_rows.update()
            .where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row.row_id,
            )
            .values(props=props, updated_at=func.now())
        )


def _stored_file_ids_in_props(database_id: UUID, props: dict[str, Any]) -> set[UUID]:
    ids: set[UUID] = set()
    file_path = f"/dashboard/databases/{database_id}/files/"
    for value in (props or {}).values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                continue
            try:
                file_id = uuid.UUID(raw_id)
            except ValueError:
                continue
            url = item.get("url")
            storage = item.get("storage")
            if storage == "database" or (isinstance(url, str) and file_path in url):
                ids.add(file_id)
    return ids


def _delete_unreferenced_database_files(s, node_id: str, database_id: UUID) -> None:
    referenced: set[UUID] = set()
    rows = s.execute(
        select(project_database_rows.c.props).where(
            project_database_rows.c.node_id == node_id,
            project_database_rows.c.database_id == database_id,
        )
    ).all()
    for row in rows:
        referenced.update(_stored_file_ids_in_props(database_id, row.props or {}))

    stmt = delete(project_database_files).where(
        project_database_files.c.node_id == node_id,
        project_database_files.c.database_id == database_id,
    )
    if referenced:
        stmt = stmt.where(project_database_files.c.file_id.not_in(referenced))
    s.execute(stmt)


def _props_without_file(props: dict[str, Any], file_id: UUID) -> tuple[dict[str, Any], bool]:
    fid = str(file_id)
    next_props = dict(props or {})
    changed = False
    for prop_id, value in list(next_props.items()):
        if not isinstance(value, list):
            continue
        filtered = [
            item
            for item in value
            if not (isinstance(item, dict) and str(item.get("id") or "") == fid)
        ]
        if filtered != value:
            next_props[prop_id] = filtered
            changed = True
    return next_props, changed


def _remove_file_refs_from_rows(s, node_id: str, database_id: UUID, file_id: UUID) -> None:
    rows = s.execute(
        select(project_database_rows.c.row_id, project_database_rows.c.props).where(
            project_database_rows.c.node_id == node_id,
            project_database_rows.c.database_id == database_id,
        )
    ).all()
    for row in rows:
        props, changed = _props_without_file(row.props or {}, file_id)
        if not changed:
            continue
        s.execute(
            project_database_rows.update()
            .where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row.row_id,
            )
            .values(props=props, updated_at=func.now())
        )


def _opt(name: str, color: str, group: Optional[str] = None) -> dict:
    o = {"id": _short_id(), "name": name, "color": color}
    if group:
        o["group"] = group
    return o


def _safe_filename(name: str) -> str:
    cleaned = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in '"\r\n')
    return (cleaned.strip() or "attachment")[:255]


def _file_kind(mime_type: str, filename: str) -> Literal["image", "video", "file"]:
    mime = (mime_type or "").lower()
    lower = filename.lower()
    if mime.startswith("image/") or lower.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
    ):
        return "image"
    if mime.startswith("video/") or lower.endswith((".mp4", ".mov", ".webm", ".m4v", ".avi")):
        return "video"
    return "file"


def _file_url(database_id: UUID, file_id: UUID) -> str:
    return f"/dashboard/databases/{database_id}/files/{file_id}"


def _seed_basic() -> tuple[list[dict], list[dict]]:
    """기본 데이터베이스: 이름(title) + 상태(status) + 표/보드 뷰."""
    title_id = _short_id()
    status_id = _short_id()
    properties = [
        {"id": title_id, "name": "이름", "type": "title", "options": []},
        {
            "id": status_id,
            "name": "상태",
            "type": "status",
            "options": [
                _opt("할 일", "gray", "to_do"),
                _opt("진행 중", "blue", "in_progress"),
                _opt("완료", "green", "complete"),
            ],
        },
    ]
    views = [
        {
            "id": _short_id(),
            "name": "표",
            "type": "table",
            "group_by": None,
            "cover_property": None,
            "hidden": [],
        },
        {
            "id": _short_id(),
            "name": "보드",
            "type": "board",
            "group_by": status_id,
            "cover_property": None,
            "hidden": [],
        },
    ]
    return properties, views


def _seed_tasks() -> tuple[list[dict], list[dict]]:
    """노션 '협업업무표' 100% 벤치마킹 시드.

    상태(status, 컬럼 그룹) + 담당자(person) + 우선순위/예상 소요 시간(select) +
    종료일(date) + 첨부(files) + 등록일(created_time). 보드는 상태로 group_by 한다.
    """
    title_id = _short_id()
    status_id = _short_id()
    attachment_id = _short_id()
    properties = [
        {"id": title_id, "name": "이름", "type": "title", "options": []},
        {
            "id": status_id,
            "name": "상태",
            "type": "status",
            "options": [
                _opt("시작 전", "gray", "to_do"),
                _opt("진행 중", "blue", "in_progress"),
                _opt("완료", "green", "complete"),
                _opt("보류", "brown", "to_do"),
            ],
        },
        {"id": _short_id(), "name": "담당자", "type": "person", "options": []},
        {
            "id": _short_id(),
            "name": "우선순위",
            "type": "select",
            "options": [
                _opt("높음", "pink"),
                _opt("핫픽스", "red"),
                _opt("보통", "yellow"),
                _opt("낮음", "green"),
                _opt("비상", "brown"),
            ],
        },
        {
            "id": _short_id(),
            "name": "예상 소요 시간",
            "type": "select",
            "options": [
                _opt("30분이하", "yellow"),
                _opt("30분~1시간", "purple"),
                _opt("2시간", "gray"),
                _opt("3시간", "pink"),
                _opt("4시간", "brown"),
                _opt("5시간 이상", "red"),
            ],
        },
        {"id": _short_id(), "name": "종료일", "type": "date", "options": []},
        {"id": attachment_id, "name": "첨부", "type": "files", "options": []},
        {"id": _short_id(), "name": "등록일", "type": "created_time", "options": []},
    ]
    views = [
        {
            "id": _short_id(),
            "name": "보드",
            "type": "board",
            "group_by": status_id,
            "cover_property": attachment_id,
            "hidden": [],
        },
        {
            "id": _short_id(),
            "name": "표",
            "type": "table",
            "group_by": None,
            "cover_property": None,
            "hidden": [],
        },
    ]
    return properties, views


# --------------------------------------------------------------------------
# database CRUD
# --------------------------------------------------------------------------
def create_database(node_id: str, body: DatabaseIn) -> ProjectDatabaseBundle:
    if body.template == "tasks":
        properties, views = _seed_tasks()  # 협업업무표: 보드가 첫 뷰
    else:
        properties, views = _seed_basic()
        if body.default_view == "board":
            views = [views[1], views[0]]  # 보드 뷰를 먼저
    database_id = uuid.uuid4()
    with session() as s:
        row = s.execute(
            project_databases.insert()
            .values(
                database_id=database_id,
                node_id=node_id,
                project_id=body.project_id,
                title=(body.title or "데이터베이스").strip() or "데이터베이스",
                icon=body.icon,
                properties=properties,
                views=views,
            )
            .returning(*_DB_COLS)
        ).one()
        s.commit()
    return ProjectDatabaseBundle(database=_db_out(row), rows=[])


_DB_COLS = (
    project_databases.c.database_id,
    project_databases.c.project_id,
    project_databases.c.title,
    project_databases.c.icon,
    project_databases.c.properties,
    project_databases.c.views,
)

_ROW_COLS = (
    project_database_rows.c.row_id,
    project_database_rows.c.database_id,
    project_database_rows.c.props,
    project_database_rows.c.sort_order,
    project_database_rows.c.icon,
    project_database_rows.c.cover,
    project_database_rows.c.body,
    project_database_rows.c.created_at,
    project_database_rows.c.updated_at,
)

# 번들 opt-in(`?body=none`) 목록용 — body를 select에서 빼 PG detoast 자체를 피한다
# (응답 body=None). 기본 번들은 여전히 _ROW_COLS 전체를 쓴다(구 FE 배포 skew 호환).
_ROW_LIST_COLS = tuple(c for c in _ROW_COLS if c is not project_database_rows.c.body)


def _row_list_out(row) -> ProjectDatabaseRow:
    return ProjectDatabaseRow(
        row_id=row.row_id,
        database_id=row.database_id,
        props=row.props or {},
        sort_order=row.sort_order or 0,
        icon=row.icon,
        cover=row.cover,
        body=None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_databases(node_id: str, project_id: UUID) -> list[ProjectDatabase]:
    with session() as s:
        rows = s.execute(
            select(*_DB_COLS).where(
                project_databases.c.node_id == node_id,
                project_databases.c.project_id == project_id,
            )
        ).all()
    return [_db_out(r) for r in rows]


def ensure_board(node_id: str, project_id: UUID) -> ProjectDatabase:
    """프로젝트(하위 포함)의 기본 칸반 보드를 보장한다 — 멱등.

    노션/Linear처럼 모든 프로젝트가 자기 업무 보드를 갖게 하는 진입점.
    이미 보드 뷰를 가진 소속 데이터베이스가 있으면 그걸 그대로 반환하고
    (첫 뷰가 board인 데이터베이스 우선), 없을 때만 tasks 템플릿으로
    '업무 보드'를 새로 만든다. 기존 보드(예: 아틀라스 협업업무표)를 대체하지 않는다.
    """
    with session() as s:
        exists = s.execute(
            select(dashboard_projects.c.project_id).where(
                dashboard_projects.c.node_id == node_id,
                dashboard_projects.c.project_id == project_id,
            )
        ).first()
        if exists is None:
            raise LookupError("project not found")
        rows = s.execute(
            select(*_DB_COLS)
            .where(
                project_databases.c.node_id == node_id,
                project_databases.c.project_id == project_id,
            )
            .order_by(project_databases.c.created_at)
        ).all()
    dbs = [_db_out(r) for r in rows]
    board = next((d for d in dbs if d.views and d.views[0].type == "board"), None) or next(
        (d for d in dbs if any(v.type == "board" for v in d.views)), None
    )
    if board is not None:
        return board
    bundle = create_database(
        node_id,
        DatabaseIn(project_id=project_id, title="업무 보드", template="tasks"),
    )
    return bundle.database


class ProjectProgress(BaseModel):
    """프로젝트 하나의 진행률 집계 — 소속 데이터베이스들의 status 속성 기준.

    각 데이터베이스의 첫 status 속성 옵션 그룹(to_do|in_progress|complete)으로
    행을 분류한다. status 값이 비어 있는 행은 to_do로 센다. status 속성이 없는
    데이터베이스는 집계 대상이 아니다(전부 결정론, LLM 0회).
    """

    project_id: UUID
    total: int = 0
    complete: int = 0
    in_progress: int = 0


def progress_by_project(node_id: str) -> list[ProjectProgress]:
    """노드의 모든 프로젝트-소속 데이터베이스를 훑어 프로젝트별 진행률을 집계한다.

    데이터베이스별 GROUP BY 루프(N+1) 대신 (database_id, status_prop_id) VALUES를
    조인해 상태값 분포를 쿼리 1회로 모은다. 응답 형태/의미는 동일하다.
    """
    with session() as s:
        dbs = s.execute(
            select(
                project_databases.c.database_id,
                project_databases.c.project_id,
                project_databases.c.properties,
            ).where(
                project_databases.c.node_id == node_id,
                project_databases.c.project_id.is_not(None),
            )
        ).all()
        agg: dict[UUID, ProjectProgress] = {}
        targets: list[tuple[UUID, str, dict[str, str]]] = []  # (db_id, status_id, option→group)
        for db in dbs:
            status_prop = next(
                (p for p in (db.properties or []) if p.get("type") == "status"), None
            )
            if status_prop is None:
                continue
            group_of = {
                str(o["id"]): (o.get("group") or "to_do") for o in status_prop.get("options", [])
            }
            targets.append((db.database_id, str(status_prop["id"]), group_of))
            agg.setdefault(db.project_id, ProjectProgress(project_id=db.project_id))
        if not targets:
            return list(agg.values())

        status_props = values(
            column("database_id", PG_UUID(as_uuid=True)),
            column("status_id", Text),
            name="status_props",
        ).data([(db_id, status_id) for db_id, status_id, _ in targets])
        status_value = project_database_rows.c.props[status_props.c.status_id].astext
        counts = s.execute(
            select(project_database_rows.c.database_id, status_value, func.count())
            .select_from(
                project_database_rows.join(
                    status_props,
                    status_props.c.database_id == project_database_rows.c.database_id,
                )
            )
            .group_by(project_database_rows.c.database_id, status_value)
        ).all()

    group_by_db = {db_id: group_of for db_id, _, group_of in targets}
    project_by_db = {db.database_id: db.project_id for db in dbs}
    for db_id, value, count in counts:
        prog = agg[project_by_db[db_id]]
        prog.total += count
        group = group_by_db[db_id].get(str(value)) if value is not None else "to_do"
        if group == "complete":
            prog.complete += count
        elif group == "in_progress":
            prog.in_progress += count
    return list(agg.values())


def get_bundle(
    node_id: str, database_id: UUID, *, include_body: bool = True
) -> ProjectDatabaseBundle:
    """데이터베이스 + 전체 행 번들.

    기본은 body 포함(구 FE 호환 — 배포 skew 동안 구 FE가 번들 body를 권위값으로
    쓴다). `include_body=False`는 신 FE의 목록 렌더용 opt-in으로, body를 select에서
    빼 PG detoast 자체를 피한다(응답 body=None, 단건 get_row로 지연 로드).
    """
    with session() as s:
        db = s.execute(
            select(*_DB_COLS).where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
        ).first()
        if db is None:
            raise LookupError("database not found")
        rows = s.execute(
            select(*(_ROW_COLS if include_body else _ROW_LIST_COLS))
            .where(project_database_rows.c.database_id == database_id)
            .order_by(project_database_rows.c.sort_order, project_database_rows.c.created_at)
        ).all()
    row_out = _row_out if include_body else _row_list_out
    return ProjectDatabaseBundle(database=_db_out(db), rows=[row_out(r) for r in rows])


def get_meta(node_id: str, database_id: UUID) -> ProjectDatabaseMeta:
    with session() as s:
        db = s.execute(
            select(
                project_databases.c.database_id,
                project_databases.c.project_id,
                project_databases.c.title,
                project_databases.c.icon,
            ).where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
        ).first()
        if db is None:
            raise LookupError("database not found")
        # 행 수는 번들 rows와 같은 where 조건으로 센다(길이 일치 보장).
        row_count = s.execute(
            select(func.count()).where(project_database_rows.c.database_id == database_id)
        ).scalar_one()
    return ProjectDatabaseMeta(
        database_id=db.database_id,
        project_id=db.project_id,
        title=db.title,
        icon=db.icon,
        row_count=row_count,
    )


def get_row(node_id: str, database_id: UUID, row_id: UUID) -> ProjectDatabaseRow:
    """단건 행 전체 조회(body 포함) — 번들에서 뺀 body의 지연 로드 경로."""
    with session() as s:
        row = s.execute(
            select(*_ROW_COLS).where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row_id,
            )
        ).first()
    if row is None:
        raise LookupError("row not found")
    return _row_out(row)


def update_database(node_id: str, database_id: UUID, patch: DatabasePatch) -> ProjectDatabase:
    values: dict = {}
    if patch.title is not None:
        values["title"] = patch.title.strip() or "데이터베이스"
    if patch.icon is not None:
        values["icon"] = patch.icon or None
    # properties/views는 함께 검증한다(view.group_by가 property를 참조).
    current_properties: Optional[list[dict]] = None
    current_views: Optional[list[dict]] = None
    if patch.properties is not None or patch.views is not None:
        with session() as s:
            cur = s.execute(
                select(project_databases.c.properties, project_databases.c.views).where(
                    project_databases.c.node_id == node_id,
                    project_databases.c.database_id == database_id,
                )
            ).first()
        if cur is None:
            raise LookupError("database not found")
        current_properties = cur.properties or []
        current_views = cur.views or []

    properties: Optional[list[dict]] = None
    if patch.properties is not None:
        properties = _validate_properties(patch.properties)
        values["properties"] = properties
    elif patch.views is not None:
        properties = current_properties or []

    if patch.properties is not None and patch.views is None:
        values["views"] = _validate_views(
            _view_defs_from_json(current_views or []), properties or []
        )
    if patch.views is not None:
        values["views"] = _validate_views(patch.views, properties)
    if not values:
        return get_bundle(node_id, database_id).database
    values["updated_at"] = func.now()
    with session() as s:
        row = s.execute(
            project_databases.update()
            .where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
            .values(**values)
            .returning(*_DB_COLS)
        ).first()
        if row is not None and properties is not None and patch.properties is not None:
            _prune_rows_for_schema(s, node_id, database_id, properties)
            _delete_unreferenced_database_files(s, node_id, database_id)
        s.commit()
    if row is None:
        raise LookupError("database not found")
    return _db_out(row)


def delete_database(node_id: str, database_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(project_databases).where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
        )
        s.execute(
            delete(project_database_rows).where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
            )
        )
        s.execute(
            delete(project_database_files).where(
                project_database_files.c.node_id == node_id,
                project_database_files.c.database_id == database_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("database not found")


# --------------------------------------------------------------------------
# row CRUD
# --------------------------------------------------------------------------
def _next_sort_order(s, database_id: UUID) -> float:
    cur = s.execute(
        select(func.max(project_database_rows.c.sort_order)).where(
            project_database_rows.c.database_id == database_id
        )
    ).scalar()
    return (cur or 0) + 1.0


def create_row(node_id: str, database_id: UUID, body: RowIn) -> ProjectDatabaseRow:
    with session() as s:
        # 데이터베이스 존재 + node 스코프 확인.
        owner = s.execute(
            select(project_databases.c.database_id, project_databases.c.properties).where(
                project_databases.c.node_id == node_id,
                project_databases.c.database_id == database_id,
            )
        ).first()
        if owner is None:
            raise LookupError("database not found")
        props = _normalize_row_props_for_schema(body.props or {}, owner.properties or [])
        sort_order = (
            body.sort_order if body.sort_order is not None else _next_sort_order(s, database_id)
        )
        row = s.execute(
            project_database_rows.insert()
            .values(
                row_id=uuid.uuid4(),
                node_id=node_id,
                database_id=database_id,
                props=props,
                sort_order=sort_order,
                icon=body.icon,
                cover=body.cover,
                body=body.body,
            )
            .returning(*_ROW_COLS)
        ).one()
        s.commit()
    return _row_out(row)


def update_row(
    node_id: str, database_id: UUID, row_id: UUID, patch: RowPatch
) -> ProjectDatabaseRow:
    values: dict = {}
    if patch.props is not None:
        values["props"] = patch.props
    if patch.sort_order is not None:
        values["sort_order"] = patch.sort_order
    if patch.icon is not None:
        values["icon"] = patch.icon or None
    if patch.cover is not None:
        values["cover"] = patch.cover or None
    if patch.body is not None:
        values["body"] = patch.body or None
    if not values:
        # no-op: 현재 값을 반환.
        with session() as s:
            row = s.execute(
                select(*_ROW_COLS).where(
                    project_database_rows.c.node_id == node_id,
                    project_database_rows.c.database_id == database_id,
                    project_database_rows.c.row_id == row_id,
                )
            ).first()
        if row is None:
            raise LookupError("row not found")
        return _row_out(row)
    values["updated_at"] = func.now()
    with session() as s:
        if patch.props is not None:
            owner = s.execute(
                select(project_databases.c.properties).where(
                    project_databases.c.node_id == node_id,
                    project_databases.c.database_id == database_id,
                )
            ).first()
            if owner is None:
                raise LookupError("database not found")
            values["props"] = _normalize_row_props_for_schema(
                patch.props or {}, owner.properties or []
            )
        row = s.execute(
            project_database_rows.update()
            .where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row_id,
            )
            .values(**values)
            .returning(*_ROW_COLS)
        ).first()
        if row is not None and patch.props is not None:
            _delete_unreferenced_database_files(s, node_id, database_id)
        s.commit()
    if row is None:
        raise LookupError("row not found")
    return _row_out(row)


def delete_row(node_id: str, database_id: UUID, row_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(project_database_rows).where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row_id,
            )
        )
        s.execute(
            delete(project_database_files).where(
                project_database_files.c.node_id == node_id,
                project_database_files.c.database_id == database_id,
                project_database_files.c.row_id == row_id,
            )
        )
        s.commit()
    if result.rowcount == 0:
        raise LookupError("row not found")


# --------------------------------------------------------------------------
# file attachments
# --------------------------------------------------------------------------
def create_file(
    node_id: str,
    database_id: UUID,
    row_id: UUID,
    filename: str,
    mime_type: str | None,
    data: bytes,
    created_by: UUID,
) -> ProjectDatabaseFileValue:
    safe_name = _safe_filename(filename)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file too large")
    guessed_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    kind = _file_kind(guessed_mime, safe_name)
    file_id = uuid.uuid4()
    with session() as s:
        owner = s.execute(
            select(project_database_rows.c.row_id).where(
                project_database_rows.c.node_id == node_id,
                project_database_rows.c.database_id == database_id,
                project_database_rows.c.row_id == row_id,
            )
        ).first()
        if owner is None:
            raise LookupError("row not found")
        s.execute(
            project_database_files.insert().values(
                file_id=file_id,
                node_id=node_id,
                database_id=database_id,
                row_id=row_id,
                filename=safe_name,
                mime_type=guessed_mime,
                kind=kind,
                size_bytes=len(data),
                data=data,
                created_by=created_by,
            )
        )
        s.commit()
    return ProjectDatabaseFileValue(
        id=str(file_id),
        name=safe_name,
        type=kind,
        mime=guessed_mime,
        size=len(data),
        url=_file_url(database_id, file_id),
        dataUrl=None,
    )


def get_file_blob(node_id: str, database_id: UUID, file_id: UUID) -> ProjectDatabaseFileBlob:
    with session() as s:
        row = s.execute(
            select(
                project_database_files.c.file_id,
                project_database_files.c.filename,
                project_database_files.c.mime_type,
                project_database_files.c.kind,
                project_database_files.c.data,
            ).where(
                project_database_files.c.node_id == node_id,
                project_database_files.c.database_id == database_id,
                project_database_files.c.file_id == file_id,
            )
        ).first()
    if row is None:
        raise LookupError("file not found")
    return ProjectDatabaseFileBlob(
        file_id=row.file_id,
        filename=row.filename,
        mime_type=row.mime_type or "application/octet-stream",
        kind=row.kind,
        data=bytes(row.data),
    )


def delete_file(node_id: str, database_id: UUID, file_id: UUID) -> None:
    with session() as s:
        result = s.execute(
            delete(project_database_files).where(
                project_database_files.c.node_id == node_id,
                project_database_files.c.database_id == database_id,
                project_database_files.c.file_id == file_id,
            )
        )
        if result.rowcount:
            _remove_file_refs_from_rows(s, node_id, database_id, file_id)
        s.commit()
    if result.rowcount == 0:
        raise LookupError("file not found")
