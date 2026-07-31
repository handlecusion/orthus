"""`_daily_wiki_markdown` — the durable-knowledge projection of a board day.

Regression for the 2026-07-14 board misroute: the daily journal used to distill
its `## 할 일`(tasks) and `## 일정`(events) into wiki claims, so an old journal
todo ("현장결제 공지는 미완료 할 일로 기록됐다") answered "오늘 할 일" instead of the
live board. Live-board data now stays OUT of the wiki; only 특이사항 notes author."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

from orthus.personal_board import _daily_wiki_markdown
from orthus.personal_board import (
    PersonalBoardDay,
    PersonalBoardFixedEvent,
    PersonalBoardNote,
    PersonalBoardTask,
)

DAY = date(2026, 7, 14)


def _task(title: str) -> PersonalBoardTask:
    return PersonalBoardTask(
        task_id=uuid.uuid4(),
        title=title,
        status="open",
        completed=False,
        priority="normal",
        scheduled_date=DAY,
        scheduled_time=time(11, 0),
        order_index=0,
        source_kind="manual",
    )


def _event(title: str) -> PersonalBoardFixedEvent:
    return PersonalBoardFixedEvent(
        event_id=uuid.uuid4(),
        title=title,
        starts_at=datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        source_kind="manual",
    )


def _note(title: str, body: str) -> PersonalBoardNote:
    return PersonalBoardNote(
        note_id=uuid.uuid4(),
        note_date=DAY,
        kind="note",
        title=title,
        body=body,
        order_index=0,
        created_at=datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
    )


def test_todos_and_events_never_reach_the_wiki():
    """A day with live-board tasks/events but no notes authors nothing — the todos
    must not become claims (returns None → caller skips wiki authoring)."""
    day = PersonalBoardDay(
        date=DAY,
        tasks=[_task("현장결제 공지"), _task("오늘자 클로드 결제하기")],
        fixed_events=[_event("서울대입구 셀링")],
        notes=[],
    )
    assert _daily_wiki_markdown(day) is None


def test_only_durable_notes_are_authored():
    """When durable 특이사항 notes exist, the wiki doc carries the notes and still
    excludes every task/event title and the 할 일/일정 headings."""
    day = PersonalBoardDay(
        date=DAY,
        tasks=[_task("현장결제 공지"), _task("오늘자 클로드 결제하기")],
        fixed_events=[_event("서울대입구 셀링")],
        notes=[_note("회고", "오늘 배포 회고: 롤백 절차 문서화 필요")],
    )
    md = _daily_wiki_markdown(day)
    assert md is not None
    assert "## 특이사항" in md and "회고" in md and "롤백 절차 문서화" in md
    # live-board data must not leak into the authored doc
    assert "## 할 일" not in md
    assert "## 일정" not in md
    assert "현장결제 공지" not in md
    assert "오늘자 클로드 결제하기" not in md
    assert "서울대입구 셀링" not in md
