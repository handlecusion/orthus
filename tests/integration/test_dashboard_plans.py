"""Server-side guard + history snapshot for weekly/monthly plan saves.

Covers Part B (destructive-empty optimistic-concurrency guard, 409 contract) and
Part E (dashboard_entry_history pre-overwrite snapshot). The bug: a blind
ON CONFLICT DO UPDATE wiped stored plans when a client that never loaded the row
sent empty arrays. The guard blocks that exact case while preserving intentional
clears (client echoes the loaded updated_at) and all non-empty edits.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, insert, select

from orthus import dashboard as d
from orthus.db import session
from orthus.tables import dashboard_entry_history, dashboard_projects

_NODE = "company"


def _project() -> uuid.UUID:
    pid = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=pid, node_id=_NODE, name=f"회사-{pid.hex[:8]}"
            )
        )
        s.commit()
    return pid


def _history_count(period_kind: str) -> int:
    with session() as s:
        return s.execute(
            select(func.count())
            .select_from(dashboard_entry_history)
            .where(dashboard_entry_history.c.period_kind == period_kind)
        ).scalar_one()


# --------------------------------------------------------------------------
# Part B — destructive-empty guard
# --------------------------------------------------------------------------
def test_empty_write_without_base_updated_at_is_blocked(user_id):
    pid = _project()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="채용 공고 마감")],
            retro_items=[d.PlanItem(id="2", text="배포 자동화 완료")],
        ),
    )

    # client that never loaded the row sends empty arrays + no base_updated_at
    with pytest.raises(d.EntryConflict) as ei:
        d.upsert_weekly(
            _NODE,
            d.WeeklyUpsert(project_id=pid, week_start=date(2026, 6, 8)),
        )
    # the conflict carries the unchanged current entry
    assert [it.text for it in ei.value.current.plan_items] == ["채용 공고 마감"]
    assert [it.text for it in ei.value.current.retro_items] == ["배포 자동화 완료"]

    # stored data preserved (not wiped)
    stored = d.get_weekly(_NODE, pid, date(2026, 6, 8))
    assert [it.text for it in stored.plan_items] == ["채용 공고 마감"]
    assert [it.text for it in stored.retro_items] == ["배포 자동화 완료"]


def test_intentional_clear_with_matching_base_updated_at_applies(user_id):
    pid = _project()
    saved = d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="채용 공고 마감")],
        ),
    )
    assert saved.updated_at is not None

    # client loaded the row → echoes its updated_at → guard passes → empty applied
    cleared = d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid, week_start=date(2026, 6, 8), base_updated_at=saved.updated_at
        ),
    )
    assert cleared.plan_items == []
    assert cleared.retro_items == []
    stored = d.get_weekly(_NODE, pid, date(2026, 6, 8))
    assert stored.plan_items == []


def test_normal_nonempty_edit_without_base_updated_at_applies(user_id):
    pid = _project()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="원안")],
        ),
    )
    # non-empty incoming always passes, even without base_updated_at (backward compat)
    updated = d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="수정안")],
        ),
    )
    assert [it.text for it in updated.plan_items] == ["수정안"]


def test_empty_write_on_empty_row_is_allowed(user_id):
    """Empty incoming over an empty/absent row is not destructive → allowed."""
    pid = _project()
    res = d.upsert_weekly(_NODE, d.WeeklyUpsert(project_id=pid, week_start=date(2026, 6, 8)))
    assert res.plan_items == []
    assert res.updated_at is not None


# --------------------------------------------------------------------------
# Part E — history snapshot
# --------------------------------------------------------------------------
def test_overwrite_snapshots_prior_content(user_id):
    pid = _project()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="원안")],
            retro_items=[d.PlanItem(id="2", text="회고")],
        ),
    )
    assert _history_count("weekly") == 0  # first insert: no prior content

    # a content-changing overwrite snapshots the PRIOR row
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 8),
            plan_items=[d.PlanItem(id="1", text="수정안")],
        ),
    )
    assert _history_count("weekly") == 1
    hist = d.list_weekly_history(_NODE, pid, date(2026, 6, 8))
    assert len(hist) == 1
    assert [it.text for it in hist[0].plan_items] == ["원안"]
    assert [it.text for it in hist[0].retro_items] == ["회고"]
    assert hist[0].prev_updated_at is not None


def test_idempotent_save_does_not_snapshot(user_id):
    pid = _project()
    plan = [d.PlanItem(id="1", text="원안")]
    d.upsert_weekly(
        _NODE, d.WeeklyUpsert(project_id=pid, week_start=date(2026, 6, 8), plan_items=plan)
    )
    # save identical content again — no content change, no snapshot noise
    d.upsert_weekly(
        _NODE, d.WeeklyUpsert(project_id=pid, week_start=date(2026, 6, 8), plan_items=plan)
    )
    assert _history_count("weekly") == 0


# --------------------------------------------------------------------------
# get_weekly returns updated_at
# --------------------------------------------------------------------------
def test_get_weekly_returns_updated_at(user_id):
    pid = _project()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid, week_start=date(2026, 6, 8), plan_items=[d.PlanItem(id="1", text="x")]
        ),
    )
    entry = d.get_weekly(_NODE, pid, date(2026, 6, 8))
    assert entry.updated_at is not None

    # missing row → updated_at is None
    other = d.get_weekly(_NODE, _project(), date(2026, 6, 8))
    assert other.updated_at is None


# --------------------------------------------------------------------------
# Monthly equivalent for the guard + snapshot
# --------------------------------------------------------------------------
def test_monthly_empty_write_without_base_is_blocked_and_snapshot(user_id):
    pid = _project()
    d.upsert_monthly(
        _NODE,
        d.MonthlyUpsert(
            project_id=pid,
            month=date(2026, 6, 1),
            plan_items=[d.PlanItem(id="1", text="이번 달 목표")],
        ),
    )

    with pytest.raises(d.EntryConflict):
        d.upsert_monthly(_NODE, d.MonthlyUpsert(project_id=pid, month=date(2026, 6, 1)))

    stored = d.get_monthly(_NODE, pid, date(2026, 6, 1))
    assert [it.text for it in stored.plan_items] == ["이번 달 목표"]

    # content-changing overwrite snapshots prior monthly content
    d.upsert_monthly(
        _NODE,
        d.MonthlyUpsert(
            project_id=pid,
            month=date(2026, 6, 1),
            plan_items=[d.PlanItem(id="1", text="수정 목표")],
        ),
    )
    assert _history_count("monthly") == 1
    hist = d.list_monthly_history(_NODE, pid, date(2026, 6, 1))
    assert [it.text for it in hist[0].plan_items] == ["이번 달 목표"]
