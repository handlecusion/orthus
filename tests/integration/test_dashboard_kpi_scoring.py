"""KPI OKR 채점 루프 회귀: 계획 항목 score(0~10) + 주간 신뢰도 + 공식 채점 + 회고 카드.

핵심 핀:
- score/grade 0은 유효값 — truthiness로 사라지면 안 된다(0-falsiness 회귀 가드).
- 신뢰도/채점은 KPI의 current_value/status(측정 경로)를 절대 건드리지 않는다.
- 채점은 final_progress(마감 진실)로만 흐르고 측정 progress는 불변.
- monthly 회고 카드는 조회 월의 지난달을 채점한다(1월 → 작년 12월 연도 경계 포함).
- score는 개인 보드 materialize로 유출되지 않는다(counts-only 경계).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from orthus import dashboard as d
from orthus.api.main import app
from orthus.api.routes import dashboard as dash_routes
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    dashboard_entry_history,
    dashboard_kpi_confidence,
    dashboard_projects,
    personal_weekly_objectives,
    team_members,
    users,
    weekly_entries,
)

client = TestClient(app)

_NODE = "company"


def H(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


@pytest.fixture
def company_user(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    return user_id


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


def _kpi(
    *,
    level: str = "key_result",
    cadence: str = "quarterly",
    fiscal_year: int = 2026,
    quarter: int | None = 2,
    month: int | None = None,
    project_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    status: str = "on_track",
    baseline: float | None = None,
    target: float | None = None,
    current_value: float | None = None,
    title: str = "KR",
) -> d.Kpi:
    return d.create_kpi(
        _NODE,
        d.KpiIn(
            parent_id=parent_id,
            level=level,
            cadence=cadence,
            fiscal_year=fiscal_year,
            quarter=quarter,
            month=month,
            title=title,
            project_id=project_id,
            status=status,
            baseline=baseline,
            target=target,
            current_value=current_value,
        ),
    )


def _role_override(user_id: uuid.UUID, role: str):
    return lambda: AuthenticatedUser(
        user_id=user_id,
        auth_mode="session",
        display_name="Member",
        role=role,
        node_id=_NODE,
    )


# --------------------------------------------------------------------------
# 계획 항목 달성 점수 (PlanItem.score)
# --------------------------------------------------------------------------
def test_plan_item_score_roundtrip(user_id):
    pid = _project()
    ws = date(2026, 6, 7)  # Sunday
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=ws,
            plan_items=[
                d.PlanItem(id="a", text="완전 미달", score=0),
                d.PlanItem(id="b", text="대부분", score=7),
                d.PlanItem(id="c", text="미채점"),
            ],
        ),
    )
    got = d.get_weekly(_NODE, pid, ws)
    assert [it.score for it in got.plan_items] == [0, 7, None]  # 0 ≠ None


def test_score_derives_done(user_id):
    """점수 기록 시 done=score>=7 파생(점검 체크박스 통합 규칙). 점수 없으면 유지."""
    pid = _project()
    ws = date(2026, 6, 7)
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=ws,
            plan_items=[
                d.PlanItem(id="a", text="7점", score=7, done=False),  # → True
                d.PlanItem(id="b", text="6점", score=6, done=True),  # → False (점수 우선)
                d.PlanItem(id="c", text="0점", score=0, done=True),  # → False (0 유효)
                d.PlanItem(id="d", text="10점", score=10),  # → True
                d.PlanItem(id="e", text="미채점 주중체크", done=True),  # 유지
            ],
        ),
    )
    got = d.get_weekly(_NODE, pid, ws)
    assert [(it.id, it.done) for it in got.plan_items] == [
        ("a", True),
        ("b", False),
        ("c", False),
        ("d", True),
        ("e", True),
    ]
    # monthly도 동일 규칙.
    d.upsert_monthly(
        _NODE,
        d.MonthlyUpsert(
            project_id=pid,
            month=date(2026, 6, 1),
            plan_items=[d.PlanItem(id="m1", text="8점", score=8, done=False)],
        ),
    )
    assert d.get_monthly(_NODE, pid, date(2026, 6, 1)).plan_items[0].done is True


def test_plan_item_score_validation_api(company_user):
    pid = _project()
    body = {
        "project_id": str(pid),
        "week_start": "2026-06-07",
        "plan_items": [{"id": "a", "text": "x", "score": 11}],
        "retro_items": [],
    }
    r = client.put("/dashboard/weekly", json=body, headers=H(company_user))
    assert r.status_code == 422
    body["plan_items"][0]["score"] = -1
    r = client.put("/dashboard/weekly", json=body, headers=H(company_user))
    assert r.status_code == 422


def test_legacy_row_without_score_loads_none(user_id):
    pid = _project()
    ws = date(2026, 6, 7)
    with session() as s:
        s.execute(
            insert(weekly_entries).values(
                entry_id=uuid.uuid4(),
                node_id=_NODE,
                project_id=pid,
                week_start=ws,
                plan_items=[{"id": "1", "text": "구 항목", "done": True}],
                retro_items=[],
            )
        )
        s.commit()
    got = d.get_weekly(_NODE, pid, ws)
    assert got.plan_items[0].score is None
    assert got.plan_items[0].done is True


def test_score_only_change_snapshots_history(user_id):
    pid = _project()
    ws = date(2026, 6, 7)
    saved = d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=ws,
            plan_items=[d.PlanItem(id="a", text="x")],
        ),
    )

    def _count() -> int:
        with session() as s:
            return s.execute(
                select(func.count())
                .select_from(dashboard_entry_history)
                .where(dashboard_entry_history.c.period_kind == "weekly")
            ).scalar_one()

    before = _count()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=ws,
            plan_items=[d.PlanItem(id="a", text="x", score=0)],  # score만 변경(0!)
            base_updated_at=saved.updated_at,
        ),
    )
    assert _count() == before + 1
    assert d.get_weekly(_NODE, pid, ws).plan_items[0].score == 0


def test_period_summary_scored_counts(user_id):
    pid = _project()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 6, 7),
            plan_items=[
                d.PlanItem(id="a", text="x", score=0),
                d.PlanItem(id="b", text="y", score=7),
                d.PlanItem(id="c", text="z"),
            ],
        ),
    )
    rows = d.list_weekly(_NODE, pid)
    assert rows[0].plan_scored == 2
    assert rows[0].plan_score_avg == 3.5  # (0+7)/2 — 0이 빠지면 7.0으로 회귀


def test_score_not_leaked_to_personal_board(user_id):
    pid = _project()
    ws = date(2026, 6, 7)
    member_uid = uuid.uuid4()
    mid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=member_uid, display_name="팀원"))
        s.execute(
            insert(team_members).values(
                member_id=mid, node_id=_NODE, user_id=member_uid, name="팀원"
            )
        )
        s.commit()
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=ws,
            plan_items=[d.PlanItem(id="a", text="할당 항목", assignee_member_id=str(mid), score=9)],
        ),
    )
    with session() as s:
        rows = s.execute(
            select(
                personal_weekly_objectives.c.title,
                personal_weekly_objectives.c.note,
            ).where(personal_weekly_objectives.c.source_plan_item_id == "a")
        ).all()
    assert len(rows) == 1  # 담당자 보드에 materialize됨
    assert rows[0].title == "할당 항목"
    assert not rows[0].note  # score가 어떤 형태로도 흘러가지 않는다

    # 읽기 방향도 counts-only(ItemProgress 스키마 고정).
    got = d.get_weekly(_NODE, pid, ws)
    prog = got.assignee_progress.get("a")
    assert prog is not None
    assert set(prog.model_dump().keys()) == {
        "has_objective",
        "subtasks_total",
        "subtasks_done",
        "completed",
    }


# --------------------------------------------------------------------------
# 주간 신뢰도 체크인
# --------------------------------------------------------------------------
def test_confidence_upsert_same_week(company_user):
    kpi = _kpi()
    h = H(company_user)
    # 수요일 날짜로 보내도 일요일로 정규화된다.
    r1 = client.post(
        f"/dashboard/kpis/{kpi.kpi_id}/confidence",
        json={"confidence": 4, "week_start": "2026-06-10"},
        headers=h,
    )
    assert r1.status_code == 201
    assert r1.json()["week_start"] == "2026-06-07"
    r2 = client.post(
        f"/dashboard/kpis/{kpi.kpi_id}/confidence",
        json={"confidence": 8, "week_start": "2026-06-08"},  # 같은 주(월요일)
        headers=h,
    )
    assert r2.status_code == 201
    with session() as s:
        rows = s.execute(
            select(dashboard_kpi_confidence.c.confidence).where(
                dashboard_kpi_confidence.c.kpi_id == kpi.kpi_id
            )
        ).all()
    assert [r.confidence for r in rows] == [8]  # upsert — 행 1개, 최신값


def test_confidence_bounds(company_user):
    kpi = _kpi()
    h = H(company_user)
    for bad in (0, 11):
        r = client.post(
            f"/dashboard/kpis/{kpi.kpi_id}/confidence",
            json={"confidence": bad},
            headers=h,
        )
        assert r.status_code == 422
    for ok in (1, 10):
        r = client.post(
            f"/dashboard/kpis/{kpi.kpi_id}/confidence",
            json={"confidence": ok, "week_start": f"2026-0{ok % 9 + 1}-01"},
            headers=h,
        )
        assert r.status_code == 201


def test_confidence_does_not_touch_kpi_metric(company_user):
    kpi = _kpi(baseline=0, target=100, current_value=40)
    h = H(company_user)
    r = client.post(f"/dashboard/kpis/{kpi.kpi_id}/confidence", json={"confidence": 3}, headers=h)
    assert r.status_code == 201
    after = [k for k in d.list_kpis(_NODE, fiscal_year=2026) if k.kpi_id == kpi.kpi_id][0]
    assert after.current_value == 40  # 측정 경로 불가침
    assert after.status == "on_track"
    assert after.progress == pytest.approx(0.4)


def test_confidence_batch_list_by_year(company_user):
    k26 = _kpi(fiscal_year=2026)
    k25 = _kpi(fiscal_year=2025, quarter=4)
    h = H(company_user)
    client.post(
        f"/dashboard/kpis/{k26.kpi_id}/confidence",
        json={"confidence": 6, "week_start": "2026-06-07"},
        headers=h,
    )
    client.post(
        f"/dashboard/kpis/{k25.kpi_id}/confidence",
        json={"confidence": 9, "week_start": "2025-11-02"},
        headers=h,
    )
    r = client.get("/dashboard/kpis/confidence?fiscal_year=2026", headers=h)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["kpi_id"] == str(k26.kpi_id)


def test_confidence_session_member_allowed(company_user, monkeypatch):
    kpi = _kpi()
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    app.dependency_overrides[dash_routes.get_current_user] = _role_override(company_user, "member")
    try:
        r = client.post(f"/dashboard/kpis/{kpi.kpi_id}/confidence", json={"confidence": 7})
    finally:
        app.dependency_overrides.pop(dash_routes.get_current_user, None)
    assert r.status_code == 201  # 신뢰도는 팀 리추얼 — 멤버 전원 허용


# --------------------------------------------------------------------------
# 공식 채점
# --------------------------------------------------------------------------
def test_grade_zero_valid_and_final_progress(company_user):
    kpi = _kpi(baseline=0, target=100, current_value=50)
    h = H(company_user)
    r = client.post(f"/dashboard/kpis/{kpi.kpi_id}/grade", json={"grade": 0}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["grade"] == 0  # 0 ≠ 미채점
    assert body["final_progress"] == 0.0  # 측정 0.5로 fallback하면 안 됨
    assert body["progress"] == pytest.approx(0.5)  # 측정 progress는 불변


def test_grade_validation_and_levels(company_user):
    h = H(company_user)
    obj = _kpi(level="objective", cadence="annual", quarter=None, title="연간 목표")
    r = client.post(f"/dashboard/kpis/{obj.kpi_id}/grade", json={"grade": 5}, headers=h)
    assert r.status_code == 422  # objective/north_star는 채점 불가(롤업 전용)
    kr = _kpi()
    for bad in (-1, 11):
        r = client.post(f"/dashboard/kpis/{kr.kpi_id}/grade", json={"grade": bad}, headers=h)
        assert r.status_code == 422


def test_regrade_overwrites_and_clear_requires_operator(company_user, monkeypatch):
    kpi = _kpi()
    h = H(company_user)
    r1 = client.post(
        f"/dashboard/kpis/{kpi.kpi_id}/grade",
        json={"grade": 4, "note": "첫 채점"},
        headers=h,
    )
    r2 = client.post(
        f"/dashboard/kpis/{kpi.kpi_id}/grade",
        json={"grade": 9, "note": "재채점"},
        headers=h,
    )
    assert r1.json()["grade"] == 4
    assert r2.json()["grade"] == 9
    assert r2.json()["grade_note"] == "재채점"

    # 말소는 session 비-operator 403, owner 200(demo는 _operator 우회라 session으로 고정).
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    app.dependency_overrides[dash_routes.get_current_user] = _role_override(company_user, "member")
    try:
        blocked = client.delete(f"/dashboard/kpis/{kpi.kpi_id}/grade")
    finally:
        app.dependency_overrides.pop(dash_routes.get_current_user, None)
    assert blocked.status_code == 403

    app.dependency_overrides[dash_routes.get_current_user] = _role_override(company_user, "owner")
    try:
        cleared = client.delete(f"/dashboard/kpis/{kpi.kpi_id}/grade")
    finally:
        app.dependency_overrides.pop(dash_routes.get_current_user, None)
    assert cleared.status_code == 200
    assert cleared.json()["grade"] is None


def test_grade_session_member_allowed(company_user, monkeypatch):
    kpi = _kpi()
    monkeypatch.setattr(get_settings(), "auth_mode", "session")
    app.dependency_overrides[dash_routes.get_current_user] = _role_override(company_user, "member")
    try:
        r = client.post(f"/dashboard/kpis/{kpi.kpi_id}/grade", json={"grade": 7})
    finally:
        app.dependency_overrides.pop(dash_routes.get_current_user, None)
    assert r.status_code == 200  # 채점도 회고 리추얼 — 멤버 전원(오너 결정)


def test_grade_rolls_up_tree_and_summary(company_user):
    obj = _kpi(level="objective", cadence="annual", quarter=None, title="목표")
    kr = _kpi(parent_id=obj.kpi_id, title="KR")
    t1 = _kpi(
        level="target",
        cadence="monthly",
        quarter=None,
        month=5,
        parent_id=kr.kpi_id,
        title="타깃1",
    )
    t2 = _kpi(
        level="target",
        cadence="monthly",
        quarter=None,
        month=6,
        parent_id=kr.kpi_id,
        title="타깃2",
    )
    h = H(company_user)
    client.post(f"/dashboard/kpis/{t1.kpi_id}/grade", json={"grade": 6}, headers=h)
    client.post(f"/dashboard/kpis/{t2.kpi_id}/grade", json={"grade": 8}, headers=h)

    tree = d.kpi_tree(_NODE, fiscal_year=2026)
    node_obj = next(n for n in tree if n.kpi_id == obj.kpi_id)
    node_kr = node_obj.children[0]
    assert node_kr.final_progress == pytest.approx(0.7)  # 자식 채점 평균(0.6, 0.8)
    assert node_kr.progress is None  # 측정 롤업은 progress 없음 그대로

    summary = d.kpi_summary(_NODE, 2026)
    assert summary.targets_graded == 2
    assert summary.targets_total == 2
    # avg는 final 기준: 타깃 0.6/0.8 + KR 롤업 0.7 → 0.7
    assert summary.avg_progress == pytest.approx(0.7)


# --------------------------------------------------------------------------
# 회고 카드
# --------------------------------------------------------------------------
def test_retro_card_weekly_sections(company_user):
    pid = _project()
    active = _kpi(quarter=2, project_id=pid, title="Q2 KR")
    _kpi(quarter=1, project_id=pid, title="Q1 KR")  # 다른 분기 — 제외
    _kpi(quarter=2, status="done", title="완료 KR")  # done — 제외
    annual = _kpi(cadence="annual", quarter=None, title="연간 KR")
    h = H(company_user)

    r = client.get(
        f"/dashboard/kpis/retro-card?mode=weekly&ref=2026-06-10&project_id={pid}",
        headers=h,
    )
    assert r.status_code == 200
    card = r.json()
    assert card["ref"] == "2026-06-07"  # 일요일 정규화
    ids = {it["kpi"]["kpi_id"] for it in card["confidence_krs"]}
    assert ids == {str(active.kpi_id), str(annual.kpi_id)}  # 회사 공통(NULL)도 포함
    assert card["unrecorded_kr_count"] == 2

    client.post(
        f"/dashboard/kpis/{active.kpi_id}/confidence",
        json={"confidence": 5, "week_start": "2026-06-07"},
        headers=h,
    )
    card2 = client.get(
        f"/dashboard/kpis/retro-card?mode=weekly&ref=2026-06-10&project_id={pid}",
        headers=h,
    ).json()
    rec = next(it for it in card2["confidence_krs"] if it["kpi"]["kpi_id"] == str(active.kpi_id))
    assert rec["this_week_recorded"] is True
    assert rec["confidence_series"] == [{"week_start": "2026-06-07", "confidence": 5}]
    assert card2["unrecorded_kr_count"] == 1


def test_retro_card_monthly_reviews_prev_month(company_user):
    may_t = _kpi(level="target", cadence="monthly", quarter=None, month=5, title="5월 타깃")
    _kpi(level="target", cadence="monthly", quarter=None, month=6, title="6월 타깃")
    h = H(company_user)
    card = client.get("/dashboard/kpis/retro-card?mode=monthly&ref=2026-06-01", headers=h).json()
    assert card["review_start"] == "2026-05-01"
    titles = [it["kpi"]["title"] for it in card["grade_targets"]]
    assert titles == ["5월 타깃"]
    assert card["grade_targets"][0]["kpi"]["kpi_id"] == str(may_t.kpi_id)
    assert card["grade_krs"] == []  # 5월은 분기 경계 아님


def test_retro_card_january_reviews_prior_december(company_user):
    dec_t = _kpi(level="target", cadence="monthly", quarter=None, month=12, title="12월 타깃")
    q4_kr = _kpi(quarter=4, title="Q4 KR")
    annual_kr = _kpi(cadence="annual", quarter=None, title="연간 KR")
    h = H(company_user)
    card = client.get("/dashboard/kpis/retro-card?mode=monthly&ref=2027-01-01", headers=h).json()
    # 1월 조회 → 작년 12월 리뷰(연도 경계) + Q4 KR + 연간 KR 채점.
    assert card["review_start"] == "2026-12-01"
    assert [it["kpi"]["kpi_id"] for it in card["grade_targets"]] == [str(dec_t.kpi_id)]
    kr_ids = {it["kpi"]["kpi_id"]: it["section"] for it in card["grade_krs"]}
    assert kr_ids == {
        str(q4_kr.kpi_id): "quarter_kr",
        str(annual_kr.kpi_id): "annual_kr",
    }


def test_retro_card_quarter_boundary_and_not(company_user):
    q1_kr = _kpi(quarter=1, title="Q1 KR")
    h = H(company_user)
    # 4월 조회 → 3월 리뷰 → Q1 KR 포함.
    april = client.get("/dashboard/kpis/retro-card?mode=monthly&ref=2026-04-01", headers=h).json()
    assert [it["kpi"]["kpi_id"] for it in april["grade_krs"]] == [str(q1_kr.kpi_id)]
    # 3월 조회 → 2월 리뷰 → KR 섹션 없음.
    march = client.get("/dashboard/kpis/retro-card?mode=monthly&ref=2026-03-01", headers=h).json()
    assert march["grade_krs"] == []


def test_retro_card_evidence_and_suggested(company_user):
    pid = _project()
    # 측정 없는 5월 타깃 — 근거는 연결 항목 점수만.
    t = _kpi(
        level="target",
        cadence="monthly",
        quarter=None,
        month=5,
        project_id=pid,
        title="5월 타깃",
    )
    d.upsert_weekly(
        _NODE,
        d.WeeklyUpsert(
            project_id=pid,
            week_start=date(2026, 5, 3),  # 5월 창 안 일요일
            plan_items=[
                d.PlanItem(id="a", text="x", kpi_id=str(t.kpi_id), score=0, done=False),
                d.PlanItem(id="b", text="y", kpi_id=str(t.kpi_id), score=10, done=True),
                d.PlanItem(id="c", text="z", score=5),  # 미태그 — 제외
            ],
        ),
    )
    h = H(company_user)
    card = client.get(
        f"/dashboard/kpis/retro-card?mode=monthly&ref=2026-06-01&project_id={pid}",
        headers=h,
    ).json()
    item = next(it for it in card["grade_targets"] if it["kpi"]["kpi_id"] == str(t.kpi_id))
    assert item["linked_total"] == 2
    assert item["linked_done"] == 1
    assert item["linked_scored"] == 2
    assert item["linked_score_avg"] == 5.0  # 0 포함 (0+10)/2
    assert item["suggested_grade"] == 5  # 측정/하위채점 없음 → 항목 점수

    # 측정이 있으면 측정이 우선(progress 0.0 → 제안 0, falsiness 핀).
    t2 = _kpi(
        level="target",
        cadence="monthly",
        quarter=None,
        month=5,
        project_id=pid,
        baseline=0,
        target=100,
        current_value=0,
        title="측정 타깃",
    )
    card2 = client.get(
        f"/dashboard/kpis/retro-card?mode=monthly&ref=2026-06-01&project_id={pid}",
        headers=h,
    ).json()
    item2 = next(it for it in card2["grade_targets"] if it["kpi"]["kpi_id"] == str(t2.kpi_id))
    assert item2["suggested_grade"] == 0


def test_retro_card_ungraded_past_count(company_user):
    h = H(company_user)
    _kpi(level="target", cadence="monthly", quarter=None, month=3, title="3월")
    graded = _kpi(level="target", cadence="monthly", quarter=None, month=4, title="4월")
    client.post(f"/dashboard/kpis/{graded.kpi_id}/grade", json={"grade": 8}, headers=h)
    _kpi(quarter=1, title="Q1 KR")  # 미채점 지난 분기 KR
    card = client.get("/dashboard/kpis/retro-card?mode=monthly&ref=2026-06-01", headers=h).json()
    # 5월 리뷰 기준 과거: 3월 타깃(미채점) + Q1 KR(미채점). 4월은 채점됨.
    assert card["ungraded_past_count"] == 2


def test_retro_card_invalid_mode(company_user):
    r = client.get("/dashboard/kpis/retro-card?mode=daily&ref=2026-06-01", headers=H(company_user))
    assert r.status_code == 422
