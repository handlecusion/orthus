"""Reflect company dashboard records into the LLM wiki.

캘린더 이벤트 / 주간 계획·회고 / 회의록을 생성·수정할 때마다 markdown으로
렌더해 `upsert_source_document`에 태운다 — Notion/editor 문서가 쓰는 것과 동일한
멱등 corpus 색인 + wiki authoring 경로(AGENTS.md 원칙 #5/#6). 새 테이블/connector
없이 dashboard row를 `documents`(멱등) → corpus → LLM wiki로 흘린다.

company node 전용. 호출부(dashboard routes)는 이 함수들을 FastAPI BackgroundTask로
예약해 dashboard 저장 응답이 즉시 반환되게 한다(distill/consolidate는 off-request).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from orthus.dashboard import (
    CalendarEvent,
    MeetingNote,
    PartnerCompany,
    PartnerContact,
    PlanItem,
    WeeklyEntry,
)
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.models.base import ChatModel
from orthus.schemas.canonical import InternalDocument
from orthus.tables import dashboard_projects, team_members, weekly_entries

logger = logging.getLogger(__name__)

_SCOPE = "company"
_PROJECT = "company"


def _member_names(ids: list[UUID]) -> dict[str, str]:
    if not ids:
        return {}
    with session() as s:
        rows = s.execute(
            select(team_members.c.member_id, team_members.c.name).where(
                team_members.c.member_id.in_(ids)
            )
        ).all()
    return {str(r.member_id): r.name for r in rows}


def _project_names(project_ids: list[UUID]) -> list[str]:
    if not project_ids:
        return []
    with session() as s:
        rows = s.execute(
            select(dashboard_projects.c.project_id, dashboard_projects.c.name).where(
                dashboard_projects.c.project_id.in_(project_ids)
            )
        ).all()
    by_id = {str(r.project_id): r.name for r in rows}
    return [by_id[str(p)] for p in project_ids if str(p) in by_id]


def _project_name(project_id: UUID | None) -> str | None:
    if project_id is None:
        return None
    with session() as s:
        row = s.execute(
            select(dashboard_projects.c.name).where(dashboard_projects.c.project_id == project_id)
        ).first()
    return row.name if row else None


def _people(ids: list[UUID], names: dict[str, str]) -> str:
    if not ids:
        return "-"
    return ", ".join(names.get(str(i), str(i)) for i in ids)


def _items_md(items: list[PlanItem]) -> str:
    if not items:
        return "- (없음)"
    lines = []
    for it in items:
        group = f"[{it.group}] " if it.group else ""
        done = "[x] " if it.done else ""
        lines.append(f"- {done}{group}{it.text}")
    return "\n".join(lines)


# --- renderers: row -> (wiki title, markdown) --------------------------------
def render_calendar_event(ev: CalendarEvent) -> tuple[str, str]:
    names = _member_names(ev.member_ids)
    when = str(ev.event_date)
    if ev.end_date and ev.end_date != ev.event_date:
        when += f" ~ {ev.end_date}"
    if ev.all_day:
        when += " (종일)"
    elif ev.start_time:
        when += f" {ev.start_time}" + (f"–{ev.end_time}" if ev.end_time else "")
    lines = [
        f"# [일정] {ev.title}",
        "",
        f"- 일시: {when}",
        f"- 유형: {ev.event_type}",
    ]
    if ev.repeat_freq:
        freq_ko = {"daily": "매일", "weekly": "매주", "biweekly": "격주", "monthly": "매월"}
        weekday_ko = ["월", "화", "수", "목", "금", "토", "일"]
        rep = freq_ko.get(ev.repeat_freq, ev.repeat_freq)
        if ev.repeat_freq in ("weekly", "biweekly") and ev.repeat_weekdays:
            rep += " " + "·".join(weekday_ko[d] for d in ev.repeat_weekdays if 0 <= d <= 6)
        if ev.repeat_until:
            rep += f" (~{ev.repeat_until})"
        lines.append(f"- 반복: {rep}")
    if ev.location:
        lines.append(f"- 장소: {ev.location}")
    proj = _project_name(ev.project_id)
    if proj:
        lines.append(f"- 프로젝트: {proj}")
    lines.append(f"- 참석자: {_people(ev.member_ids, names)}")
    if ev.no_return:
        # "복귀불가" — 이 일정 뒤 담당자가 복귀하지 않음(팀 일정 소유 플래그). 회사 위키에서도 조회되게 남긴다.
        lines.append("- 복귀불가: 예")
    elif ev.return_time:
        # "복귀 시간" — 복귀하는 경우의 복귀 시각.
        lines.append(f"- 복귀 시간: {ev.return_time.strftime('%H:%M')}")
    if ev.description:
        lines += ["", ev.description]
    return f"[일정] {ev.title} ({ev.event_date})", "\n".join(lines)


def render_meeting_note(note: MeetingNote) -> tuple[str, str]:
    names = _member_names(note.attendee_ids)
    lines = [
        f"# [회의록] {note.title}",
        "",
        f"- 회의 일자: {note.meeting_date}",
        f"- 참석자: {_people(note.attendee_ids, names)}",
    ]
    proj = _project_name(note.project_id)
    if proj:
        lines.append(f"- 프로젝트: {proj}")
    if note.body:
        lines += ["", note.body]
    return f"[회의록] {note.title} ({note.meeting_date})", "\n".join(lines)


def render_weekly(entry: WeeklyEntry) -> tuple[str, str]:
    proj = _project_name(entry.project_id) or str(entry.project_id)
    title = f"[주간 회고] {proj} {entry.week_start}"
    lines = [
        f"# {title}",
        "",
        "## 계획",
        _items_md(entry.plan_items),
        "",
        "## 회고",
        _items_md(entry.retro_items),
    ]
    return title, "\n".join(lines)


# --- reflect: render + idempotent corpus index + wiki author -----------------
def _reflect(
    user_id: UUID,
    *,
    source: str,
    canonical: str,
    external_id: str,
    db_name: str,
    title: str,
    markdown: str,
    chat_model: ChatModel | None,
) -> None:
    doc = InternalDocument(
        title=title,
        markdown=markdown,
        source=source,
        source_external_id=external_id,
        source_canonical_id=canonical,
        source_db_name=db_name,
        source_last_edited_at=datetime.now(UTC),
        project=_PROJECT,
    )
    upsert_source_document(user_id, doc, chat_model=chat_model, scope=_SCOPE)


def reflect_calendar_event(
    user_id: UUID, ev: CalendarEvent, *, chat_model: ChatModel | None = None
) -> None:
    try:
        title, md = render_calendar_event(ev)
        _reflect(
            user_id,
            source="dashboard_calendar",
            canonical=f"dashboard:calendar:{ev.event_id}",
            external_id=str(ev.event_id),
            db_name="team_calendar_events",
            title=title,
            markdown=md,
            chat_model=chat_model,
        )
    except Exception:
        logger.exception("reflect calendar event %s into wiki failed", ev.event_id)


def reflect_meeting_note(
    user_id: UUID, note: MeetingNote, *, chat_model: ChatModel | None = None
) -> None:
    try:
        title, md = render_meeting_note(note)
        _reflect(
            user_id,
            source="dashboard_meeting",
            canonical=f"dashboard:meeting:{note.note_id}",
            external_id=str(note.note_id),
            db_name="meeting_notes",
            title=title,
            markdown=md,
            chat_model=chat_model,
        )
    except Exception:
        logger.exception("reflect meeting note %s into wiki failed", note.note_id)


def reflect_weekly(
    user_id: UUID, entry: WeeklyEntry, *, chat_model: ChatModel | None = None
) -> None:
    try:
        title, md = render_weekly(entry)
        _reflect(
            user_id,
            source="dashboard_weekly",
            canonical=f"dashboard:weekly:{entry.project_id}:{entry.week_start}",
            external_id=f"{entry.project_id}:{entry.week_start}",
            db_name="weekly_entries",
            title=title,
            markdown=md,
            chat_model=chat_model,
        )
    except Exception:
        logger.exception(
            "reflect weekly %s/%s into wiki failed", entry.project_id, entry.week_start
        )


def render_partner_company(
    partner: PartnerCompany, contacts: list[PartnerContact]
) -> tuple[str, str]:
    projects = _project_names(partner.project_ids)
    lines = [
        f"# [파트너사] {partner.name}",
        "",
        f"- 프로젝트: {', '.join(projects) if projects else '-'}",
        f"- 기관 형태: {partner.org_type or '-'}",
        f"- 대표: {partner.representative or '-'}",
        f"- 주소: {partner.address or '-'}",
        f"- 상태: {partner.status or '-'}",
    ]
    if partner.field_tags:
        lines.append(f"- 분야: {', '.join(partner.field_tags)}")
    if partner.memo:
        lines.append(f"- 메모: {partner.memo}")
    if partner.next_action:
        lines.append(f"- 다음 액션: {partner.next_action}")
    lines.append("")
    lines.append("## 인물")
    if contacts:
        for c in contacts:
            parts = [c.name]
            if c.role:
                parts.append(f"({c.role})")
            tail = " · ".join(x for x in [c.phone, c.email] if x)
            line = f"- {' '.join(parts)}"
            if tail:
                line += f" — {tail}"
            lines.append(line)
    else:
        lines.append("- (등록된 인물 없음)")
    return f"[파트너사] {partner.name}", "\n".join(lines)


def reflect_partner_company(
    user_id: UUID,
    partner: PartnerCompany,
    contacts: list[PartnerContact],
    *,
    chat_model: ChatModel | None = None,
) -> None:
    try:
        title, md = render_partner_company(partner, contacts)
        _reflect(
            user_id,
            source="dashboard_partner",
            canonical=f"dashboard:partner:{partner.partner_id}",
            external_id=str(partner.partner_id),
            db_name="partner_companies",
            title=title,
            markdown=md,
            chat_model=chat_model,
        )
    except Exception:
        logger.exception("reflect partner company %s into wiki failed", partner.partner_id)


def backfill_node_wiki(
    node_id: str, user_id: UUID, *, chat_model: ChatModel | None = None
) -> dict[str, int]:
    """One-off: reflect every existing calendar event / meeting note / weekly entry
    on this node into the wiki. Idempotent (canonical-id keyed), so re-running only
    refreshes. Use after first deploy to backfill rows created before reflection
    existed."""
    from orthus.dashboard import PlanItem as _PlanItem
    from orthus.dashboard import list_calendar, list_meeting_notes

    counts = {"calendar": 0, "meeting": 0, "weekly": 0}
    for ev in list_calendar(node_id, None, None):
        reflect_calendar_event(user_id, ev, chat_model=chat_model)
        counts["calendar"] += 1
    for note in list_meeting_notes(node_id):
        # Auto-synced plan·retro meetings duplicate the weekly entry, which is
        # already reflected below — only manual meetings carry new knowledge.
        if note.source != "manual":
            continue
        reflect_meeting_note(user_id, note, chat_model=chat_model)
        counts["meeting"] += 1
    with session() as s:
        rows = s.execute(
            select(
                weekly_entries.c.entry_id,
                weekly_entries.c.project_id,
                weekly_entries.c.week_start,
                weekly_entries.c.plan_items,
                weekly_entries.c.retro_items,
            ).where(weekly_entries.c.node_id == node_id)
        ).all()
    for r in rows:
        entry = WeeklyEntry(
            entry_id=r.entry_id,
            project_id=r.project_id,
            week_start=r.week_start,
            plan_items=[_PlanItem(**it) for it in (r.plan_items or [])],
            retro_items=[_PlanItem(**it) for it in (r.retro_items or [])],
        )
        reflect_weekly(user_id, entry, chat_model=chat_model)
        counts["weekly"] += 1
    return counts
