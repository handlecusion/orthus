"""개인 보드 주 시작 = 일요일 헬퍼 의미 고정 (회사 weekly_entries와 동일)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

import orthus.personal_board as personal_board
from orthus.personal_board import _week_sunday


def test_week_sunday_returns_sunday_on_or_before() -> None:
    # 2026-06-04 is a Thursday → its week starts Sunday 2026-05-31.
    assert _week_sunday(date(2026, 6, 4)) == date(2026, 5, 31)
    # A Sunday maps to itself.
    assert _week_sunday(date(2026, 5, 31)) == date(2026, 5, 31)
    # A Saturday is the last day of the same Sunday-start week.
    assert _week_sunday(date(2026, 6, 6)) == date(2026, 5, 31)
    # A Monday belongs to the Sunday just before it.
    assert _week_sunday(date(2026, 6, 1)) == date(2026, 5, 31)


def test_weekly_plan_defaults_to_current_week(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid4()
    synced_weeks: list[date] = []

    class EmptyResult:
        def all(self):
            return []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    monkeypatch.setattr(
        personal_board,
        "ensure_workspace",
        lambda _user_id, _node_id: {"workspace_id": workspace_id, "timezone": "Asia/Seoul"},
    )
    monkeypatch.setattr(
        personal_board,
        "selected_date_or_today",
        lambda _selected_date, _timezone: date(2026, 7, 1),
    )
    monkeypatch.setattr(
        personal_board,
        "sync_company_plan_to_objectives",
        lambda _user_id, _node_id, target_week, email=None: synced_weeks.append(target_week),
    )
    monkeypatch.setattr(personal_board, "_projects_by_id", lambda _session, _workspace_id: {})
    monkeypatch.setattr(personal_board, "session", lambda: FakeSession())

    response = personal_board.list_weekly_plan(uuid4(), "personal-a")

    assert response.week_start == date(2026, 6, 28)
    assert synced_weeks == [date(2026, 6, 28)]
