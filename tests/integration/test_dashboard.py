"""Company dashboard API tests: CRUD, finance summary, weekly auto-link,
node guard."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from orthus import dashboard as d
from orthus.api.main import app
from orthus.connectors.nova import NovaMLClient
from orthus.settings import get_settings

client = TestClient(app)


def H(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


@pytest.fixture
def company_user(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    return user_id


def test_team_member_crud(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post("/dashboard/team-members", json={"name": "김대표", "title": "대표"}, headers=h)
    assert r.status_code == 201
    mid = r.json()["member_id"]

    r = client.get("/dashboard/team-members", headers=h)
    assert r.status_code == 200
    assert [m["name"] for m in r.json()] == ["김대표"]

    r = client.patch(
        f"/dashboard/team-members/{mid}", json={"name": "김대표", "title": "CEO"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["title"] == "CEO"

    r = client.delete(f"/dashboard/team-members/{mid}", headers=h)
    assert r.status_code == 204
    assert client.get("/dashboard/team-members", headers=h).json() == []


def test_finance_summary_balance(company_user: uuid.UUID) -> None:
    h = H(company_user)
    client.post(
        "/dashboard/finance/ledger",
        json={"entry_date": "2026-06-01", "entry_type": "revenue", "amount": 1000000},
        headers=h,
    )
    client.post(
        "/dashboard/finance/ledger",
        json={"entry_date": "2026-06-02", "entry_type": "expense", "amount": 300000},
        headers=h,
    )
    client.post(
        "/dashboard/finance/subscriptions",
        json={"name": "Notion", "amount": 12000, "billing_cycle": "monthly"},
        headers=h,
    )
    client.post(
        "/dashboard/finance/subscriptions",
        json={"name": "Figma", "amount": 120000, "billing_cycle": "yearly"},
        headers=h,
    )
    r = client.get("/dashboard/finance/summary", headers=h)
    assert r.status_code == 200
    s = r.json()
    assert s["total_revenue"] == 1000000
    assert s["total_expense"] == 300000
    assert s["balance"] == 700000
    # 12000 monthly + 120000/12 yearly = 22000
    assert s["monthly_subscription_cost"] == 22000
    assert s["active_subscription_count"] == 2
    # burn = subscriptions + api(0) = 22000; runway = 700000 / 22000 ≈ 31.8 months
    assert s["monthly_burn"] == 22000
    assert s["runway_months"] == 31.8


def test_api_key_stores_only_last4(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post(
        "/dashboard/finance/api-keys",
        json={"service_name": "OpenAI", "key_last4": "sk-supersecret-WXYZ"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["key_last4"] == "WXYZ"
    # full secret never round-trips
    assert "supersecret" not in str(body)


def test_weekly_plan_autolinks_to_same_week_retro(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "Nova"}, headers=h).json()["project_id"]

    # write the plan using a Wednesday; backend normalizes to that week's Sunday
    r = client.put(
        "/dashboard/weekly",
        json={
            "project_id": pid,
            "week_start": "2026-06-03",
            "plan_items": [{"id": "a", "text": "릴리스 준비", "done": False}],
            "retro_items": [],
        },
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["week_start"] == "2026-05-31"  # Sunday

    # reading any day in that week returns the same row → plan visible in retro view
    r = client.get(
        "/dashboard/weekly",
        params={"project_id": pid, "week_start": "2026-06-06"},  # Saturday, same Sun-week
        headers=h,
    )
    assert r.status_code == 200
    assert [p["text"] for p in r.json()["plan_items"]] == ["릴리스 준비"]


def test_calendar_event_time(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "스탠드업",
            "event_date": "2026-06-09",
            "start_time": "09:30",
            "end_time": "10:00",
            "all_day": False,
        },
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["start_time"].startswith("09:30")
    assert body["end_time"].startswith("10:00")
    assert body["all_day"] is False


def test_calendar_reflect_flag_controls_wiki_reflection(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 자동 저장(노션식)은 편집 중 reflect=false로 위키 반영을 건너뛰고, 닫을 때만
    # reflect=true로 1회 반영한다. reflect 플래그가 실제 background 반영을 제어하는지 확인.
    import orthus.api.routes.dashboard as dash_routes

    h = H(company_user)
    calls: list[str] = []

    def fake_reflect(user_id, ev, **kwargs):  # noqa: ANN001, ANN202
        calls.append(str(ev.event_id))

    monkeypatch.setattr(dash_routes, "reflect_calendar_event", fake_reflect)

    # 기본(reflect 미지정) → 반영 호출.
    r1 = client.post(
        "/dashboard/calendar/events",
        json={"title": "반영", "event_date": "2026-07-06"},
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    assert len(calls) == 1

    # reflect=false → 반영 스킵.
    r2 = client.post(
        "/dashboard/calendar/events?reflect=false",
        json={"title": "반영안함", "event_date": "2026-07-07"},
        headers=h,
    )
    assert r2.status_code == 201, r2.text
    assert len(calls) == 1  # 변화 없음
    eid = r2.json()["event_id"]

    # PATCH reflect=false → 반영 스킵.
    r3 = client.patch(
        f"/dashboard/calendar/events/{eid}?reflect=false",
        json={"title": "수정", "event_date": "2026-07-07"},
        headers=h,
    )
    assert r3.status_code == 200, r3.text
    assert len(calls) == 1

    # PATCH 기본 → 반영 호출(닫기 시 최종 반영 경로).
    r4 = client.patch(
        f"/dashboard/calendar/events/{eid}",
        json={"title": "수정2", "event_date": "2026-07-07"},
        headers=h,
    )
    assert r4.status_code == 200, r4.text
    assert len(calls) == 2


def test_calendar_create_idempotent_client_event_id(company_user: uuid.UUID) -> None:
    # 자동 저장은 keepalive/재시도/bfcache로 같은 새 일정을 여러 번 create할 수 있다.
    # 클라이언트가 넘긴 client_event_id로 create가 멱등(upsert)이어야 중복 행이 안 생긴다.
    h = H(company_user)
    cid = str(uuid.uuid4())
    r1 = client.post(
        f"/dashboard/calendar/events?client_event_id={cid}",
        json={"title": "멱등A", "event_date": "2026-09-01"},
        headers=h,
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["event_id"] == cid

    # 같은 client_event_id로 다시(제목 변경) → 중복 생성이 아니라 upsert.
    r2 = client.post(
        f"/dashboard/calendar/events?client_event_id={cid}",
        json={"title": "멱등B", "event_date": "2026-09-01"},
        headers=h,
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["event_id"] == cid
    assert r2.json()["title"] == "멱등B"

    lst = client.get("/dashboard/calendar?from=2026-09-01&to=2026-09-30", headers=h).json()
    rows = [e for e in lst if e["event_id"] == cid]
    assert len(rows) == 1
    assert rows[0]["title"] == "멱등B"

    # client_event_id 미지정 create는 서버가 uuid를 발급(기존 계약 무변경).
    r3 = client.post(
        "/dashboard/calendar/events",
        json={"title": "서버발급", "event_date": "2026-09-02"},
        headers=h,
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["event_id"] and r3.json()["event_id"] != cid


def test_calendar_event_no_return(company_user: uuid.UUID) -> None:
    # "복귀불가" is owned by the team calendar event (moved off the personal board):
    # create with it on, then PATCH it off, round-tripping through the API.
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "외근", "event_date": "2026-06-29", "no_return": True},
        headers=h,
    )
    assert r.status_code == 201, r.text
    event = r.json()
    assert event["no_return"] is True
    upd = client.patch(
        f"/dashboard/calendar/events/{event['event_id']}",
        json={"title": "외근", "event_date": "2026-06-29", "no_return": False},
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["no_return"] is False


def test_calendar_event_return_time(company_user: uuid.UUID) -> None:
    # "복귀 시간" — 복귀하는 경우의 복귀 시각. no_return과 상호 배타적이라,
    # no_return=true면 서버가 return_time을 비운다.
    h = H(company_user)
    # 1) 복귀 시간 지정 → 라운드트립.
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "외근", "event_date": "2026-07-10", "return_time": "14:00"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    event = r.json()
    assert event["return_time"].startswith("14:00")
    assert event["no_return"] is False
    eid = event["event_id"]

    # 2) no_return=true + return_time 동시 지정 → 서버가 return_time을 null로 정리.
    upd = client.patch(
        f"/dashboard/calendar/events/{eid}",
        json={
            "title": "외근",
            "event_date": "2026-07-10",
            "no_return": True,
            "return_time": "15:30",
        },
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["no_return"] is True
    assert body["return_time"] is None

    # 3) no_return 해제 + 새 복귀 시간 → 복귀 시간만 남는다.
    upd2 = client.patch(
        f"/dashboard/calendar/events/{eid}",
        json={
            "title": "외근",
            "event_date": "2026-07-10",
            "no_return": False,
            "return_time": "16:00",
        },
        headers=h,
    )
    assert upd2.status_code == 200, upd2.text
    body2 = upd2.json()
    assert body2["no_return"] is False
    assert body2["return_time"].startswith("16:00")


@pytest.mark.parametrize(
    "event_type",
    ["dev", "update"],
)
def test_calendar_event_type_options(company_user: uuid.UUID, event_type: str) -> None:
    # FE TYPE_OPTIONS / CalendarEventType union must all pass the DB CHECK
    # constraint (migration 0034). Regression for CheckViolation 500.
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "일정", "event_date": "2026-06-28", "event_type": event_type},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["event_type"] == event_type


def test_calendar_range_filter(company_user: uuid.UUID) -> None:
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={"title": "팀 미팅", "event_date": "2026-06-08", "event_type": "meeting"},
        headers=h,
    )
    client.post(
        "/dashboard/calendar/events",
        json={"title": "다음달", "event_date": "2026-07-08"},
        headers=h,
    )
    r = client.get(
        "/dashboard/calendar", params={"from": "2026-06-01", "to": "2026-06-30"}, headers=h
    )
    assert r.status_code == 200
    assert [e["title"] for e in r.json()] == ["팀 미팅"]


# ---------------- 반복 일정(루틴) ----------------
def test_calendar_recurring_weekly_expansion(company_user: uuid.UUID) -> None:
    # 매주 월·수 루틴: 마스터 1행이 조회 윈도 안의 회차로 펼쳐진다.
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "주간 회의",
            "event_date": "2026-07-06",  # 월
            "start_time": "10:00",
            "all_day": False,
            "repeat_freq": "weekly",
            "repeat_weekdays": [0, 2],  # 월·수
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["repeat_freq"] == "weekly"
    assert ev["repeat_weekdays"] == [0, 2]

    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-19"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == [
        "2026-07-06",
        "2026-07-08",
        "2026-07-13",
        "2026-07-15",
    ]
    # 모든 회차가 같은 마스터를 가리키고 series_start로 시리즈 시작일을 노출한다.
    assert {e["event_id"] for e in rows} == {ev["event_id"]}
    assert {e["series_start"] for e in rows} == {"2026-07-06"}
    assert all(e["start_time"].startswith("10:00") for e in rows)


def test_calendar_recurring_started_before_window(company_user: uuid.UUID) -> None:
    # 윈도 이전에 시작한 시리즈도 윈도 안 회차는 계속 나온다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={"title": "루틴", "event_date": "2026-06-01", "repeat_freq": "weekly"},
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-14"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06", "2026-07-13"]


def test_calendar_recurring_daily_until(company_user: uuid.UUID) -> None:
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={
            "title": "아침 점검",
            "event_date": "2026-07-01",
            "repeat_freq": "daily",
            "repeat_until": "2026-07-03",
        },
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-06-28", "to": "2026-07-10"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-01", "2026-07-02", "2026-07-03"]


def test_calendar_recurring_biweekly(company_user: uuid.UUID) -> None:
    # 격주: 시작일이 속한 주(월요일 기준)를 0주차로 짝수 주차만.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={"title": "격주 회고", "event_date": "2026-07-06", "repeat_freq": "biweekly"},
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06", "2026-07-20"]


def test_calendar_recurring_monthly_skips_missing_day(company_user: uuid.UUID) -> None:
    # 매월 31일 루틴은 31일이 없는 달(9월)을 건너뛴다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={"title": "월말 정산", "event_date": "2026-07-31", "repeat_freq": "monthly"},
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-10-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-31", "2026-08-31", "2026-10-31"]


def test_calendar_recurring_weekly_defaults_to_start_weekday(company_user: uuid.UUID) -> None:
    # 요일 미선택 매주 반복은 시작일 요일로 반복한다.
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "요일 기본", "event_date": "2026-07-06", "repeat_freq": "weekly"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["repeat_weekdays"] == [0]  # 월


def test_calendar_recurring_validation(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "잘못된 반복", "event_date": "2026-07-06", "repeat_freq": "yearly"},
        headers=h,
    )
    assert r.status_code == 422
    r = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "종료일이 시작 전",
            "event_date": "2026-07-06",
            "repeat_freq": "weekly",
            "repeat_until": "2026-06-30",
        },
        headers=h,
    )
    assert r.status_code == 422
    r = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "요일 범위 밖",
            "event_date": "2026-07-06",
            "repeat_freq": "weekly",
            "repeat_weekdays": [7],
        },
        headers=h,
    )
    assert r.status_code == 422


def test_calendar_recurring_update_and_clear(company_user: uuid.UUID) -> None:
    # 시리즈 편집은 마스터 1행을 고치고 모든 회차에 반영된다. 반복 해제도 가능.
    h = H(company_user)
    ev = client.post(
        "/dashboard/calendar/events",
        json={"title": "스탠드업", "event_date": "2026-07-06", "repeat_freq": "daily"},
        headers=h,
    ).json()
    upd = client.patch(
        f"/dashboard/calendar/events/{ev['event_id']}",
        json={
            "title": "데일리 스탠드업",
            "event_date": "2026-07-06",
            "repeat_freq": "daily",
            "repeat_until": "2026-07-08",
        },
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06", "2026-07-07", "2026-07-08"]
    assert {e["title"] for e in rows} == {"데일리 스탠드업"}

    # 반복 해제 → 단일 일정으로 돌아온다(반복 필드도 비워진다).
    upd = client.patch(
        f"/dashboard/calendar/events/{ev['event_id']}",
        json={"title": "데일리 스탠드업", "event_date": "2026-07-06"},
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06"]
    assert rows[0]["repeat_freq"] is None
    assert rows[0]["series_start"] is None


def test_calendar_recurring_multiday_span_shifts(company_user: uuid.UUID) -> None:
    # end_date가 있는 반복 일정은 회차마다 같은 기간으로 이동한다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={
            "title": "당직",
            "event_date": "2026-07-06",
            "end_date": "2026-07-07",
            "repeat_freq": "weekly",
        },
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-14"}, headers=h
    ).json()
    assert [(e["event_date"], e["end_date"]) for e in rows] == [
        ("2026-07-06", "2026-07-07"),
        ("2026-07-13", "2026-07-14"),
    ]


# ---------------- 추가 날짜(같은 일정을 다른 날짜에도) ----------------
def test_calendar_extra_dates_expansion(company_user: uuid.UUID) -> None:
    # 반복 없이 event_date + 추가 날짜 2개 → 마스터 1행이 세 회차로 펼쳐진다.
    h = H(company_user)
    r = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "촬영",
            "event_date": "2026-07-06",
            "extra_dates": ["2026-07-10", "2026-07-20"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["extra_dates"] == ["2026-07-10", "2026-07-20"]

    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06", "2026-07-10", "2026-07-20"]
    # 모든 회차가 같은 마스터를 가리키고 series_start로 원래 시작일을 노출한다
    # (FE가 어느 회차를 눌러도 마스터를 편집하도록).
    assert {e["event_id"] for e in rows} == {ev["event_id"]}
    assert {e["series_start"] for e in rows} == {"2026-07-06"}
    assert all(e["title"] == "촬영" for e in rows)


def test_calendar_extra_dates_out_of_window_master(company_user: uuid.UUID) -> None:
    # event_date가 윈도 밖이어도 추가 날짜가 윈도 안이면 그 회차는 나온다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={"title": "행사", "event_date": "2026-06-01", "extra_dates": ["2026-07-15"]},
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-15"]


def test_calendar_extra_dates_multiday_span(company_user: uuid.UUID) -> None:
    # end_date가 있으면 추가 날짜 회차도 같은 기간(일수)만큼 이어진다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={
            "title": "워크숍",
            "event_date": "2026-07-06",
            "end_date": "2026-07-07",
            "extra_dates": ["2026-07-20"],
        },
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [(e["event_date"], e["end_date"]) for e in rows] == [
        ("2026-07-06", "2026-07-07"),
        ("2026-07-20", "2026-07-21"),
    ]


def test_calendar_extra_dates_multiday_span_overlaps_window(company_user: uuid.UUID) -> None:
    # 다중일 일정에 추가 날짜가 붙어도, 마스터 회차의 기간이 윈도에 걸치면 시작일이
    # 윈도 밖이어도 나온다(기존 단일 일정 span-overlap 동작과 동일). 추가 날짜 회차도
    # 같은 규칙을 따른다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={
            "title": "장기행사",
            "event_date": "2026-06-29",  # 시작은 윈도(7월) 밖
            "end_date": "2026-07-02",  # 기간이 7/1~7/2로 윈도에 걸침
            "extra_dates": ["2026-07-30"],  # 추가 회차: 7/30~8/2, 7/30만 윈도 안
        },
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [(e["event_date"], e["end_date"]) for e in rows] == [
        ("2026-06-29", "2026-07-02"),
        ("2026-07-30", "2026-08-02"),
    ]


def test_calendar_extra_dates_no_window_includes_all(company_user: uuid.UUID) -> None:
    # 윈도 없이 조회하면(wiki backfill/agentic tool 경로) 명시적 추가 날짜는 반복의
    # +41일 기본 윈도로 잘리지 않고 전부 포함된다(event_date 전/후 모두).
    h = H(company_user)
    ev = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "전체노출",
            "event_date": "2026-07-20",
            "extra_dates": ["2026-07-01", "2026-09-01"],
        },
        headers=h,
    ).json()
    # d.list_calendar를 None 윈도로 직접 호출(라우트는 항상 윈도를 주지만, wiki
    # backfill/agentic tool은 None/None으로 부른다). 테스트 노드는 "company".
    from orthus import dashboard as d

    rows = d.list_calendar("company", None, None)
    got = sorted(e.event_date.isoformat() for e in rows if str(e.event_id) == ev["event_id"])
    assert got == ["2026-07-01", "2026-07-20", "2026-09-01"]


def test_calendar_extra_dates_with_recurrence_union(company_user: uuid.UUID) -> None:
    # 반복 회차 + 추가 날짜가 합쳐지고, 반복 회차와 겹치는 추가 날짜는 중복되지 않는다.
    h = H(company_user)
    client.post(
        "/dashboard/calendar/events",
        json={
            "title": "정기+추가",
            "event_date": "2026-07-06",  # 월, 매주 월
            "repeat_freq": "weekly",
            # 07-13은 반복 회차와 겹침(중복 제거), 07-09는 추가로만.
            "extra_dates": ["2026-07-09", "2026-07-13"],
        },
        headers=h,
    )
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-19"}, headers=h
    ).json()
    # 반복 회차 07-06·07-13(07-20은 윈도 밖) + 추가 07-09, 겹치는 07-13은 1회만.
    assert [e["event_date"] for e in rows] == [
        "2026-07-06",
        "2026-07-09",
        "2026-07-13",
    ]


def test_calendar_extra_dates_dedup_and_drop_event_date(company_user: uuid.UUID) -> None:
    # 시작일과 같은 추가 날짜, 중복 추가 날짜는 저장 시 정리된다.
    h = H(company_user)
    ev = client.post(
        "/dashboard/calendar/events",
        json={
            "title": "정리",
            "event_date": "2026-07-06",
            "extra_dates": ["2026-07-06", "2026-07-10", "2026-07-10"],
        },
        headers=h,
    ).json()
    assert ev["extra_dates"] == ["2026-07-10"]


def test_calendar_extra_dates_update_clears(company_user: uuid.UUID) -> None:
    # 수정 시 extra_dates를 비우면 추가 회차가 사라진다.
    h = H(company_user)
    ev = client.post(
        "/dashboard/calendar/events",
        json={"title": "임시", "event_date": "2026-07-06", "extra_dates": ["2026-07-10"]},
        headers=h,
    ).json()
    upd = client.patch(
        f"/dashboard/calendar/events/{ev['event_id']}",
        json={"title": "임시", "event_date": "2026-07-06", "extra_dates": []},
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["extra_dates"] == []
    rows = client.get(
        "/dashboard/calendar", params={"from": "2026-07-01", "to": "2026-07-31"}, headers=h
    ).json()
    assert [e["event_date"] for e in rows] == ["2026-07-06"]


def test_calendar_extra_dates_too_many_rejected(company_user: uuid.UUID) -> None:
    h = H(company_user)
    many = [f"2026-{7 + (i // 28):02d}-{1 + (i % 28):02d}" for i in range(101)]
    r = client.post(
        "/dashboard/calendar/events",
        json={"title": "과다", "event_date": "2026-06-01", "extra_dates": many},
        headers=h,
    )
    assert r.status_code == 422


def test_culture_upsert(company_user: uuid.UUID) -> None:
    h = H(company_user)
    assert client.get("/dashboard/culture", headers=h).json()["content"] == {}
    r = client.put(
        "/dashboard/culture",
        json={"content": {"wifi_ssid": "DEMO WORKSPACE 5G", "office_address": "어딘가구"}},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["content"]["wifi_ssid"] == "DEMO WORKSPACE 5G"
    # round-trips on a fresh GET
    assert (
        client.get("/dashboard/culture", headers=h).json()["content"]["office_address"] == "어딘가구"
    )


def _seed_notion_team_row(user_id: uuid.UUID, props: dict) -> None:
    from sqlalchemy import insert

    from orthus.db import session
    from orthus.tables import notion_rows

    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id="db-team",
                db_name="팀원",
                properties=props,
                scope="company",
                project="company",
                user_id=user_id,
            )
        )
        s.commit()


def test_notion_team_sync(company_user: uuid.UUID) -> None:
    h = H(company_user)
    _seed_notion_team_row(
        company_user,
        {
            "이름": "김대표",
            "이메일": "ys@acme.example",
            "전화번호": "010-1111-2222",
            "생일": "1995-03-01",
        },
    )
    _seed_notion_team_row(company_user, {"이름": "이개발", "연락처": "010-3333-4444"})

    r = client.post("/dashboard/team-members/sync-notion", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["created"] == 2
    assert body["updated"] == 0

    members = client.get("/dashboard/team-members", headers=h).json()
    names = sorted(m["name"] for m in members)
    assert names == ["김대표", "이개발"]
    ys = next(m for m in members if m["name"] == "김대표")
    assert ys["email"] == "ys@acme.example"
    assert ys["phone"] == "010-1111-2222"
    assert ys["birthday"] == "1995-03-01"

    # re-sync updates in place, no duplicates
    r2 = client.post("/dashboard/team-members/sync-notion", headers=h)
    assert r2.json()["updated"] == 2
    assert r2.json()["created"] == 0
    assert len(client.get("/dashboard/team-members", headers=h).json()) == 2


def test_meeting_notes_crud(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "Orbit"}, headers=h).json()[
        "project_id"
    ]
    mid = client.post("/dashboard/team-members", json={"name": "박기획"}, headers=h).json()[
        "member_id"
    ]
    r = client.post(
        "/dashboard/meeting-notes",
        json={
            "title": "RSA 미팅",
            "project_id": pid,
            "meeting_date": "2026-06-03",
            "attendee_ids": [mid],
            "body": "안건: 매물 연동\n결정: 다음주 재논의",
        },
        headers=h,
    )
    assert r.status_code == 201
    note = r.json()
    assert note["title"] == "RSA 미팅"
    assert note["project_id"] == pid
    assert note["attendee_ids"] == [mid]
    assert "매물 연동" in note["body"]

    rows = client.get("/dashboard/meeting-notes", headers=h).json()
    assert [n["title"] for n in rows] == ["RSA 미팅"]

    r = client.delete(f"/dashboard/meeting-notes/{note['note_id']}", headers=h)
    assert r.status_code == 204
    assert client.get("/dashboard/meeting-notes", headers=h).json() == []


def test_meeting_attachments(company_user: uuid.UUID) -> None:
    """PDF 등 미팅자료 첨부: media_store 실파일 저장 + 회의별 목록/삭제/카스케이드."""
    h = H(company_user)
    note = client.post(
        "/dashboard/meeting-notes",
        json={"title": "외부 미팅", "meeting_date": "2026-07-01", "meeting_kind": "external"},
        headers=h,
    ).json()
    nid = note["note_id"]
    assert note["attachments"] == []

    up = client.post(
        f"/dashboard/meeting-notes/{nid}/attachments",
        files={"file": ("agenda.pdf", b"%PDF-1.4 fake meeting material", "application/pdf")},
        headers=h,
    )
    assert up.status_code == 201, up.text
    att = up.json()
    assert att["filename"] == "agenda.pdf"
    assert att["mime_type"] == "application/pdf"
    assert att["size_bytes"] > 0
    assert att["url"].startswith("/dashboard/media/")

    # the note list projection carries the attachment
    rows = client.get("/dashboard/meeting-notes", headers=h).json()
    assert len(rows[0]["attachments"]) == 1
    assert rows[0]["attachments"][0]["attachment_id"] == att["attachment_id"]

    # uploading to a nonexistent note is 404 (no orphan file)
    missing = client.post(
        f"/dashboard/meeting-notes/{uuid.uuid4()}/attachments",
        files={"file": ("x.pdf", b"data", "application/pdf")},
        headers=h,
    )
    assert missing.status_code == 404

    # delete one attachment
    d = client.delete(
        f"/dashboard/meeting-notes/{nid}/attachments/{att['attachment_id']}",
        headers=h,
    )
    assert d.status_code == 204
    rows = client.get("/dashboard/meeting-notes", headers=h).json()
    assert rows[0]["attachments"] == []

    # deleting the note cascades any remaining attachment mappings
    client.post(
        f"/dashboard/meeting-notes/{nid}/attachments",
        files={"file": ("minutes.pdf", b"%PDF more", "application/pdf")},
        headers=h,
    )
    assert client.delete(f"/dashboard/meeting-notes/{nid}", headers=h).status_code == 204
    assert client.get("/dashboard/meeting-notes", headers=h).json() == []


def test_meeting_note_update_body_null_preserves(company_user: uuid.UUID) -> None:
    """유실 방지: body=None 은 '본문 변경 없음' — 필드만 고쳐도 본문이 날아가지 않는다.
    본문을 실제로 비우려면 명시적 빈 문자열을 보낸다."""
    h = H(company_user)
    note = client.post(
        "/dashboard/meeting-notes",
        json={"title": "본문 있는 회의", "meeting_date": "2026-07-02", "body": "중요한 결정사항"},
        headers=h,
    ).json()
    nid = note["note_id"]

    upd = client.patch(
        f"/dashboard/meeting-notes/{nid}",
        json={"title": "제목만 수정", "meeting_date": "2026-07-02", "body": None},
        headers=h,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["title"] == "제목만 수정"
    assert upd.json()["body"] == "중요한 결정사항"  # 본문 보존

    cleared = client.patch(
        f"/dashboard/meeting-notes/{nid}",
        json={"title": "제목만 수정", "meeting_date": "2026-07-02", "body": ""},
        headers=h,
    )
    assert cleared.status_code == 200
    assert cleared.json()["body"] == ""  # 명시적 비우기는 반영


def test_meeting_attachment_requires_operator(company_user: uuid.UUID) -> None:
    """media_store 실파일 write이므로 /dashboard/media와 동일하게 owner/admin 전용.
    session member는 첨부 업로드/삭제 403(비-매니저의 임의 파일 반입/삭제 차단)."""
    from orthus.api.deps import get_current_user
    from orthus.auth import AuthenticatedUser

    h = H(company_user)
    note = client.post(
        "/dashboard/meeting-notes",
        json={"title": "권한 회의", "meeting_date": "2026-07-03", "meeting_kind": "external"},
        headers=h,
    ).json()
    nid = note["note_id"]

    member = AuthenticatedUser(
        user_id=company_user,
        auth_mode="session",
        display_name="member",
        role="member",
        node_id="company",
    )
    app.dependency_overrides[get_current_user] = lambda: member
    try:
        up = client.post(
            f"/dashboard/meeting-notes/{nid}/attachments",
            files={"file": ("x.pdf", b"%PDF member", "application/pdf")},
            headers=h,
        )
        assert up.status_code == 403, up.text
        dele = client.delete(
            f"/dashboard/meeting-notes/{nid}/attachments/{uuid.uuid4()}",
            headers=h,
        )
        assert dele.status_code == 403, dele.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # owner/admin(데모)은 그대로 업로드된다.
    ok = client.post(
        f"/dashboard/meeting-notes/{nid}/attachments",
        files={"file": ("y.pdf", b"%PDF admin", "application/pdf")},
        headers=h,
    )
    assert ok.status_code == 201, ok.text


def test_meeting_attachment_delete_purges_disk_file(company_user: uuid.UUID) -> None:
    """명시적 첨부 삭제·회의 삭제 시 DB 매핑뿐 아니라 media_store 실파일도 제거."""
    from orthus import media as media_store

    h = H(company_user)
    note = client.post(
        "/dashboard/meeting-notes",
        json={"title": "파일삭제 회의", "meeting_date": "2026-07-04"},
        headers=h,
    ).json()
    nid = note["note_id"]

    att = client.post(
        f"/dashboard/meeting-notes/{nid}/attachments",
        files={"file": ("f.pdf", b"%PDF bytes", "application/pdf")},
        headers=h,
    ).json()
    media_name = att["url"].rsplit("/", 1)[-1]
    assert media_store.resolve_media(media_name)[0].is_file()

    # 첨부 삭제 → 실파일 제거
    assert (
        client.delete(
            f"/dashboard/meeting-notes/{nid}/attachments/{att['attachment_id']}",
            headers=h,
        ).status_code
        == 204
    )
    with pytest.raises(LookupError):
        media_store.resolve_media(media_name)

    # 회의 삭제 cascade도 남은 첨부 실파일을 제거
    att2 = client.post(
        f"/dashboard/meeting-notes/{nid}/attachments",
        files={"file": ("g.pdf", b"%PDF more", "application/pdf")},
        headers=h,
    ).json()
    media2 = att2["url"].rsplit("/", 1)[-1]
    assert media_store.resolve_media(media2)[0].is_file()
    assert client.delete(f"/dashboard/meeting-notes/{nid}", headers=h).status_code == 204
    with pytest.raises(LookupError):
        media_store.resolve_media(media2)


def test_infra_gpu_summary(company_user: uuid.UUID) -> None:
    h = H(company_user)
    client.post(
        "/dashboard/infra",
        json={"kind": "gpu", "name": "A100", "capacity": 4, "used": 3, "unit": "장"},
        headers=h,
    )
    client.post(
        "/dashboard/infra",
        json={"kind": "gpu", "name": "H200", "capacity": 8, "used": 0, "status": "reserved"},
        headers=h,
    )
    client.post(
        "/dashboard/infra",
        json={"kind": "storage", "name": "NAS", "capacity": 10, "unit": "TB"},
        headers=h,
    )
    s = client.get("/dashboard/infra/gpu-summary", headers=h).json()
    assert s["total"] == 12  # 4 + 8 (gpu only)
    assert s["used"] == 3
    assert s["available"] == 9

    rows = client.get("/dashboard/infra", headers=h).json()
    assert {r["name"] for r in rows} == {"A100", "H200", "NAS"}


# ---- Nova ML platform: VictoriaMetrics GPU sync + MLflow panel ----------
def _vm_vector(rows: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"status": "success", "data": {"resultType": "vector", "result": rows}}
    )


def _gpu_sample(node: str, gpu: str, value: str, *, model: str = "NVIDIA A100") -> dict:
    return {
        "metric": {"node": node, "Hostname": node, "gpu": gpu, "modelName": model},
        "value": [1781236178, value],
    }


def _vm_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("query", "")
    if "GPU_UTIL" in q:
        return _vm_vector([_gpu_sample("gpu-a100", "0", "42"), _gpu_sample("gpu-a100", "1", "3")])
    if "FB_USED" in q:
        return _vm_vector(
            [_gpu_sample("gpu-a100", "0", "30000"), _gpu_sample("gpu-a100", "1", "200")]
        )
    if "FB_FREE" in q:
        return _vm_vector(
            [_gpu_sample("gpu-a100", "0", "51000"), _gpu_sample("gpu-a100", "1", "80800")]
        )
    if "GPU_TEMP" in q:
        return _vm_vector([_gpu_sample("gpu-a100", "0", "61"), _gpu_sample("gpu-a100", "1", "38")])
    return httpx.Response(404)


def _vm_client(handler) -> NovaMLClient:
    return NovaMLClient(
        vm_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://vm")
    )


def test_sync_gpu_from_vm(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_vm_url", "http://vm")
    monkeypatch.setattr(d, "_nova_client", lambda: _vm_client(_vm_handler))
    h = H(company_user)

    r = client.post("/dashboard/infra/sync-gpu", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["used"] == 1  # only GPU0 (42%) is busy; GPU1 (3%) idle
    assert body["available"] == 1
    assert body["avg_util"] == 22  # round((42+3)/2)

    rows = client.get("/dashboard/infra", headers=h).json()
    gpu_rows = [x for x in rows if x["kind"] == "gpu"]
    assert len(gpu_rows) == 1
    assert gpu_rows[0]["name"] == "gpu-a100"
    assert gpu_rows[0]["capacity"] == 2
    assert gpu_rows[0]["used"] == 1

    summary = client.get("/dashboard/infra/gpu-summary", headers=h).json()
    assert summary["total"] == 2
    assert summary["used"] == 1
    assert summary["available"] == 1


def test_sync_gpu_vm_unreachable(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_vm_url", "http://vm")
    monkeypatch.setattr(d, "_nova_client", lambda: _vm_client(boom))
    r = client.post("/dashboard/infra/sync-gpu", headers=H(company_user))
    assert r.status_code == 422


def test_sync_gpu_falls_back_to_ssh(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_vm_url", "")  # VM unconfigured → SSH path
    called: dict[str, str] = {}

    def fake_ssh(node_id: str) -> d.GpuSyncResult:
        called["node_id"] = node_id
        return d.GpuSyncResult(host="a100", total=4, used=2, available=2, avg_util=50)

    monkeypatch.setattr(d, "sync_gpu_from_ssh", fake_ssh)
    r = client.post("/dashboard/infra/sync-gpu", headers=H(company_user))
    assert r.status_code == 200
    assert r.json()["host"] == "a100"
    assert called["node_id"] == "company"


def _storage_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("query", "")
    m = {"node": "bignas-v1", "mountpoint": "/share/CACHEDEV1_DATA", "fstype": "ext4"}
    if "size_bytes" in q:
        return _vm_vector([{"metric": m, "value": [1781236178, "100e12"]}])
    if "avail_bytes" in q:
        return _vm_vector([{"metric": m, "value": [1781236178, "40e12"]}])
    return httpx.Response(404)


def test_sync_storage_from_vm(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "_nova_client", lambda: _vm_client(_storage_handler))
    h = H(company_user)
    r = client.post("/dashboard/infra/sync-storage", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["nodes"] == 1

    rows = client.get("/dashboard/infra", headers=h).json()
    nas = [x for x in rows if x["kind"] == "storage" and x["name"] == "bignas-v1"]
    assert len(nas) == 1
    assert nas[0]["capacity"] == 100.0  # TB
    assert nas[0]["used"] == 60.0  # 100 - 40
    assert nas[0]["unit"] == "TB"


def test_sync_storage_no_metrics_422(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(d, "_nova_client", lambda: _vm_client(lambda req: _vm_vector([])))
    r = client.post("/dashboard/infra/sync-storage", headers=H(company_user))
    assert r.status_code == 422


def _mlflow_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/experiments/search"):
        return httpx.Response(200, json={"experiments": [{"experiment_id": "1", "name": "de-vfx"}]})
    if path.endswith("/runs/search"):
        return httpx.Response(
            200,
            json={
                "runs": [
                    {
                        "info": {
                            "run_id": "abc123",
                            "run_name": "run-latest",
                            "experiment_id": "1",
                            "status": "FINISHED",
                            "start_time": 1781236178000,
                            "end_time": 1781236200000,
                        },
                        "data": {
                            "metrics": [{"key": "vlm_score", "value": 90.0}],
                            "tags": [],
                        },
                    }
                ]
            },
        )
    return httpx.Response(404)


def _mlflow_client(handler) -> NovaMLClient:
    return NovaMLClient(
        mlflow_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mlflow")
    )


def test_ml_panel(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(settings, "nova_ml_grafana_url", "http://grafana")
    monkeypatch.setattr(d, "_nova_client", lambda: _mlflow_client(_mlflow_handler))

    r = client.get("/dashboard/infra/ml-panel", headers=H(company_user))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["mlflow_url"] == "http://mlflow"
    assert body["grafana_url"] == "http://grafana"
    assert body["error"] is None
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["run_name"] == "run-latest"
    assert run["experiment_name"] == "de-vfx"
    assert run["metrics"]["vlm_score"] == 90.0
    assert run["url"] == "http://mlflow/#/experiments/1/runs/abc123"


def test_ml_panel_unconfigured(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "")
    r = client.get("/dashboard/infra/ml-panel", headers=H(company_user))
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_ml_panel_unreachable(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(d, "_nova_client", lambda: _mlflow_client(boom))
    r = client.get("/dashboard/infra/ml-panel", headers=H(company_user))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["error"] is not None
    assert body["runs"] == []


def test_ml_panel_empty_experiments(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/experiments/search"):
            return httpx.Response(200, json={"experiments": []})
        return httpx.Response(404)

    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(d, "_nova_client", lambda: _mlflow_client(handler))
    r = client.get("/dashboard/infra/ml-panel", headers=H(company_user))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["error"] is None
    assert body["runs"] == []


def _grafana_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/health":
        return httpx.Response(200, json={"database": "ok"})
    if request.url.path == "/api/dashboards/uid/nova-gpu-kpi":
        return httpx.Response(200, json={"dashboard": {"uid": "nova-gpu-kpi"}})
    return httpx.Response(404)


def _combined_client(grafana_handler) -> NovaMLClient:
    return NovaMLClient(
        mlflow_client=httpx.Client(
            transport=httpx.MockTransport(_mlflow_handler), base_url="http://mlflow"
        ),
        grafana_client=httpx.Client(
            transport=httpx.MockTransport(grafana_handler), base_url="http://gf"
        ),
    )


def test_ml_panel_grafana_embed_when_reachable(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(settings, "nova_ml_grafana_url", "http://grafana")
    monkeypatch.setattr(d, "_nova_client", lambda: _combined_client(_grafana_handler))
    body = client.get("/dashboard/infra/ml-panel", headers=H(company_user)).json()
    assert body["grafana_ok"] is True
    assert body["grafana_embed_url"] == (
        "http://grafana/d/nova-gpu-kpi/?kiosk&theme=light&from=now-6h&to=now&refresh=30s"
    )


def test_ml_panel_grafana_down_degrades(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("grafana down")

    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(settings, "nova_ml_grafana_url", "http://grafana")
    monkeypatch.setattr(d, "_nova_client", lambda: _combined_client(down))
    r = client.get("/dashboard/infra/ml-panel", headers=H(company_user))
    assert r.status_code == 200  # page still renders
    body = r.json()
    assert body["grafana_ok"] is False
    assert body["grafana_embed_url"] == ""
    assert body["grafana_url"] == "http://grafana"  # link fallback still available


def test_ml_panel_grafana_discovers_dashboard_when_uid_missing(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={})
        if request.url.path.startswith("/api/dashboards/uid/"):
            return httpx.Response(404)  # configured uid gone
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[{"uid": "gpu-new", "title": "GPU board v9"}])
        return httpx.Response(404)

    settings = get_settings()
    monkeypatch.setattr(settings, "nova_ml_mlflow_url", "http://mlflow")
    monkeypatch.setattr(settings, "nova_ml_grafana_url", "http://grafana")
    monkeypatch.setattr(d, "_nova_client", lambda: _combined_client(handler))
    body = client.get("/dashboard/infra/ml-panel", headers=H(company_user)).json()
    assert body["grafana_ok"] is True
    assert "gpu-new" in body["grafana_embed_url"]


def test_infra_dashboard_kind_and_period(company_user: uuid.UUID) -> None:
    """kind='dashboard' rows (Grafana embeds) + the period field round-trip."""
    h = H(company_user)
    r = client.post(
        "/dashboard/infra",
        json={
            "kind": "dashboard",
            "name": "GPU KPI",
            "link": "http://100.64.0.1:30300/d/nova-gpu-kpi/x",
            "notes": "GPU 가동률",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "dashboard"

    r = client.post(
        "/dashboard/infra",
        json={"kind": "gpu", "name": "leased", "capacity": 8, "period": "2026-06 ~ 2026-12"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["period"] == "2026-06 ~ 2026-12"

    rows = client.get("/dashboard/infra", headers=h).json()
    dash = [x for x in rows if x["kind"] == "dashboard"]
    assert len(dash) == 1 and dash[0]["name"] == "GPU KPI"


def test_personal_node_returns_404(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    r = client.get("/dashboard/team-members", headers=H(user_id))
    assert r.status_code == 404


def test_partner_crud_with_contacts_and_filter(company_user: uuid.UUID) -> None:
    h = H(company_user)
    proj = client.post(
        "/dashboard/projects", json={"name": "아틀라스", "color": "#5d9bd8"}, headers=h
    ).json()["project_id"]
    other = client.post(
        "/dashboard/projects", json={"name": "NOVA", "color": "#9b68d6"}, headers=h
    ).json()["project_id"]

    # atlas partner tagged to the atlas project
    r = client.post(
        "/dashboard/partners",
        json={
            "name": "해오름기획",
            "org_type": "보조출연사",
            "address": "서울 어딘가구",
            "representative": "박도현",
            "project_ids": [proj],
            "field_tags": ["보조출연(드라마)"],
            "status": "협업중",
        },
        headers=h,
    )
    assert r.status_code == 201
    pid = r.json()["partner_id"]
    assert r.json()["contact_count"] == 0
    assert r.json()["project_ids"] == [proj]

    # nova partner that the project filter should exclude
    client.post(
        "/dashboard/partners",
        json={"name": "OpenAI", "org_type": "AI 파운데이션", "project_ids": [other]},
        headers=h,
    )

    assert len(client.get("/dashboard/partners", headers=h).json()) == 2
    filtered = client.get(f"/dashboard/partners?project_id={proj}", headers=h).json()
    assert [p["name"] for p in filtered] == ["해오름기획"]

    # nested contacts (primary listed first)
    c = client.post(
        f"/dashboard/partners/{pid}/contacts",
        json={"name": "박도현", "role": "대표", "phone": "010-0000-2222", "is_primary": True},
        headers=h,
    )
    assert c.status_code == 201
    cid = c.json()["contact_id"]
    client.post(
        f"/dashboard/partners/{pid}/contacts",
        json={"name": "김지선", "role": "지부장"},
        headers=h,
    )
    contacts = client.get(f"/dashboard/partners/{pid}/contacts", headers=h).json()
    assert [c["name"] for c in contacts] == ["박도현", "김지선"]

    partner = next(
        p for p in client.get("/dashboard/partners", headers=h).json() if p["partner_id"] == pid
    )
    assert partner["contact_count"] == 2

    r = client.patch(
        f"/dashboard/partners/{pid}",
        json={
            "name": "해오름기획",
            "org_type": "보조출연사",
            "status": "제휴완료",
            "project_ids": [proj],
        },
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "제휴완료"
    assert r.json()["contact_count"] == 2

    assert client.delete(f"/dashboard/partners/contacts/{cid}", headers=h).status_code == 204
    assert len(client.get(f"/dashboard/partners/{pid}/contacts", headers=h).json()) == 1
    assert client.delete(f"/dashboard/partners/{pid}", headers=h).status_code == 204
    remaining = client.get("/dashboard/partners", headers=h).json()
    assert [p["name"] for p in remaining] == ["OpenAI"]


def test_partner_contacts_require_existing_partner(company_user: uuid.UUID) -> None:
    h = H(company_user)
    missing = uuid.uuid4()
    assert client.get(f"/dashboard/partners/{missing}/contacts", headers=h).status_code == 404
    r = client.post(f"/dashboard/partners/{missing}/contacts", json={"name": "홍길동"}, headers=h)
    assert r.status_code == 404


def test_project_description_status_roundtrip(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post(
        "/dashboard/projects",
        json={"name": "대리AI", "color": "#5d9bd8", "description": "에이전트", "status": "진행중"},
        headers=h,
    ).json()["project_id"]
    got = next(
        p for p in client.get("/dashboard/projects", headers=h).json() if p["project_id"] == pid
    )
    assert got["description"] == "에이전트"
    assert got["status"] == "진행중"
    assert got["body"] is None
    detail = client.get(f"/dashboard/projects/{pid}", headers=h)
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == pid
    assert detail.json()["description"] == "에이전트"

    # 노션식 자유 본문(BlockNote 블록 JSON 문자열) 라운드트립
    block_json = '[{"type":"heading","content":[{"type":"text","text":"메모"}]}]'
    r = client.patch(
        f"/dashboard/projects/{pid}",
        json={**got, "body": block_json},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["body"] == block_json
    got2 = next(
        p for p in client.get("/dashboard/projects", headers=h).json() if p["project_id"] == pid
    )
    assert got2["body"] == block_json
    # 다른 필드만 바꾸는 PATCH는 body를 보존한다(요약/상태 편집 시 본문 유지)
    r = client.patch(
        f"/dashboard/projects/{pid}",
        json={**got2, "status": "완료"},
        headers=h,
    )
    assert r.json()["status"] == "완료"
    assert r.json()["body"] == block_json


def test_dashboard_page_crud(company_user: uuid.UUID) -> None:
    h = H(company_user)
    # 생성: 기본 제목
    r = client.post("/dashboard/pages", json={}, headers=h)
    assert r.status_code == 201
    page = r.json()
    pid = page["page_id"]
    assert page["title"] == "새 페이지"
    assert page["body"] is None

    # 제목/본문 부분 수정(PATCH) — 빠진 필드는 보존
    body_json = '[{"type":"paragraph","content":[{"type":"text","text":"중첩 메모"}]}]'
    r = client.patch(f"/dashboard/pages/{pid}", json={"title": "아이디어"}, headers=h)
    assert r.json()["title"] == "아이디어"
    r = client.patch(f"/dashboard/pages/{pid}", json={"body": body_json}, headers=h)
    assert r.json()["title"] == "아이디어"  # 보존
    assert r.json()["body"] == body_json

    # 단건 조회
    got = client.get(f"/dashboard/pages/{pid}", headers=h)
    assert got.status_code == 200
    assert got.json()["body"] == body_json

    assert got.json()["kind"] == "page"

    # 삭제 후 404
    assert client.delete(f"/dashboard/pages/{pid}", headers=h).status_code == 204
    assert client.get(f"/dashboard/pages/{pid}", headers=h).status_code == 404


def test_dashboard_memo_list(company_user: uuid.UUID) -> None:
    h = H(company_user)
    # 메모(kind=memo)와 일반 하위 페이지(kind=page)를 만든다
    memo = client.post(
        "/dashboard/pages", json={"title": "사업 아이디어", "kind": "memo"}, headers=h
    ).json()
    assert memo["kind"] == "memo"
    client.post("/dashboard/pages", json={"title": "하위", "kind": "page"}, headers=h)

    # 목록은 kind=memo 만 (기본값 memo)
    rows = client.get("/dashboard/pages", headers=h).json()
    ids = [r["page_id"] for r in rows]
    assert memo["page_id"] in ids
    assert all(r["kind"] == "memo" for r in rows)
    assert "updated_at" in rows[0]

    # kind=page 목록은 메모를 포함하지 않는다
    sub = client.get("/dashboard/pages?kind=page", headers=h).json()
    assert all(r["kind"] == "page" for r in sub)
    assert memo["page_id"] not in [r["page_id"] for r in sub]


def test_project_assignment_member_role(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "대리AI"}, headers=h).json()[
        "project_id"
    ]
    pid2 = client.post("/dashboard/projects", json={"name": "아틀라스"}, headers=h).json()[
        "project_id"
    ]
    mid = client.post("/dashboard/team-members", json={"name": "박기획"}, headers=h).json()[
        "member_id"
    ]

    # assign member to two projects with roles
    a = client.post(
        "/dashboard/assignments",
        json={"project_id": pid, "member_id": mid, "role": "PM"},
        headers=h,
    )
    assert a.status_code == 201
    aid = a.json()["assignment_id"]
    assert a.json()["role"] == "PM"
    assert a.json()["member_name"] == "박기획"
    client.post(
        "/dashboard/assignments",
        json={"project_id": pid2, "member_id": mid, "role": "개발"},
        headers=h,
    )

    # member-centric view
    by_member = client.get(f"/dashboard/assignments?member_id={mid}", headers=h).json()
    assert {r["project_name"] for r in by_member} == {"대리AI", "아틀라스"}

    # project-centric view
    by_project = client.get(f"/dashboard/assignments?project_id={pid}", headers=h).json()
    assert [r["role"] for r in by_project] == ["PM"]

    # re-assign same member/project updates role (idempotent upsert)
    again = client.post(
        "/dashboard/assignments",
        json={"project_id": pid, "member_id": mid, "role": "AX"},
        headers=h,
    )
    assert again.status_code == 201
    assert len(client.get(f"/dashboard/assignments?project_id={pid}", headers=h).json()) == 1
    assert again.json()["role"] == "AX"

    # patch role, then delete
    r = client.patch(f"/dashboard/assignments/{aid}", json={"role": "리드"}, headers=h)
    assert r.status_code == 200 and r.json()["role"] == "리드"
    assert client.delete(f"/dashboard/assignments/{aid}", headers=h).status_code == 204
    assert client.get(f"/dashboard/assignments?project_id={pid}", headers=h).json() == []


def test_meeting_kind_and_partner_filter(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "아틀라스"}, headers=h).json()[
        "project_id"
    ]
    partner = client.post("/dashboard/partners", json={"name": "브로아틀라스"}, headers=h).json()[
        "partner_id"
    ]

    client.post(
        "/dashboard/meeting-notes",
        json={"title": "내부 정기회의", "meeting_date": "2026-06-08", "meeting_kind": "internal"},
        headers=h,
    )
    ext = client.post(
        "/dashboard/meeting-notes",
        json={
            "title": "브로아틀라스 미팅",
            "meeting_date": "2026-06-09",
            "meeting_kind": "external",
            "project_id": pid,
            "partner_id": partner,
        },
        headers=h,
    )
    assert ext.status_code == 201
    assert ext.json()["meeting_kind"] == "external"
    assert ext.json()["partner_id"] == partner
    assert ext.json()["source"] == "manual"

    internal = client.get("/dashboard/meeting-notes?meeting_kind=internal", headers=h).json()
    assert [n["title"] for n in internal] == ["내부 정기회의"]
    external = client.get("/dashboard/meeting-notes?meeting_kind=external", headers=h).json()
    assert [n["title"] for n in external] == ["브로아틀라스 미팅"]
    # project filter
    by_proj = client.get(f"/dashboard/meeting-notes?project_id={pid}", headers=h).json()
    assert [n["title"] for n in by_proj] == ["브로아틀라스 미팅"]


def test_weekly_plan_auto_creates_internal_meeting(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "대리AI"}, headers=h).json()[
        "project_id"
    ]

    # saving a weekly plan auto-creates an internal meeting on that week's Sunday
    client.put(
        "/dashboard/weekly",
        json={
            "project_id": pid,
            "week_start": "2026-06-03",  # a Wednesday → normalized to 2026-05-31
            "plan_items": [{"id": "a", "text": "릴리스 준비", "done": False}],
            "retro_items": [{"id": "b", "text": "배포 자동화", "done": True}],
        },
        headers=h,
    )
    internal = client.get("/dashboard/meeting-notes?meeting_kind=internal", headers=h).json()
    auto = [n for n in internal if n["source"] == "weekly_plan"]
    assert len(auto) == 1
    assert auto[0]["meeting_date"] == "2026-05-31"  # Sunday
    assert "릴리스 준비" in (auto[0]["body"] or "")
    assert "배포 자동화" in (auto[0]["body"] or "")

    # re-saving the same week updates (no duplicate)
    saved = client.put(
        "/dashboard/weekly",
        json={
            "project_id": pid,
            "week_start": "2026-06-01",
            "plan_items": [{"id": "a", "text": "릴리스 + 문서", "done": False}],
            "retro_items": [],
        },
        headers=h,
    ).json()
    internal = client.get("/dashboard/meeting-notes?meeting_kind=internal", headers=h).json()
    auto = [n for n in internal if n["source"] == "weekly_plan"]
    assert len(auto) == 1
    assert "문서" in (auto[0]["body"] or "")

    # clearing both plan and retro removes the auto meeting; echo base_updated_at so the
    # destructive-empty guard (PR #190) treats this as an intentional clear, not a wipe.
    client.put(
        "/dashboard/weekly",
        json={
            "project_id": pid,
            "week_start": "2026-06-01",
            "plan_items": [],
            "retro_items": [],
            "base_updated_at": saved["updated_at"],
        },
        headers=h,
    )
    internal = client.get("/dashboard/meeting-notes?meeting_kind=internal", headers=h).json()
    assert [n for n in internal if n["source"] == "weekly_plan"] == []


def test_weekly_meeting_aggregates_all_projects(company_user: uuid.UUID) -> None:
    h = H(company_user)
    p1 = client.post("/dashboard/projects", json={"name": "대리AI"}, headers=h).json()["project_id"]
    p2 = client.post("/dashboard/projects", json={"name": "아틀라스"}, headers=h).json()["project_id"]

    client.put(
        "/dashboard/weekly",
        json={
            "project_id": p1,
            "week_start": "2026-06-08",
            "plan_items": [{"id": "a", "text": "에이전트 작업", "done": False}],
            "retro_items": [],
        },
        headers=h,
    )
    client.put(
        "/dashboard/weekly",
        json={
            "project_id": p2,
            "week_start": "2026-06-08",
            "plan_items": [{"id": "b", "text": "보조출연 컨택", "done": False}],
            "retro_items": [],
        },
        headers=h,
    )

    internal = client.get("/dashboard/meeting-notes?meeting_kind=internal", headers=h).json()
    auto = [n for n in internal if n["source"] == "weekly_plan"]
    # ONE meeting for the week, aggregating both projects
    assert len(auto) == 1
    m = auto[0]
    assert m["title"] == "6월 7일~13일 주간회의"
    assert m["project_id"] is None
    body = m["body"] or ""
    assert "## 대리AI" in body and "## 아틀라스" in body
    assert "에이전트 작업" in body and "보조출연 컨택" in body


def test_support_program_crud_and_project_filter(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pr = client.post("/dashboard/projects", json={"name": "NOVA"}, headers=h)
    assert pr.status_code == 201
    project_id = pr.json()["project_id"]

    r = client.post(
        "/dashboard/support-programs",
        json={
            "name": "초기창업패키지",
            "status": "발표준비",
            "project_id": project_id,
            "company": "아크메",
            "deadline": "2026-07-15",
        },
        headers=h,
    )
    assert r.status_code == 201
    created = r.json()
    pid = created["program_id"]
    assert created["project_name"] == "NOVA"

    # invalid status rejected
    r = client.post(
        "/dashboard/support-programs", json={"name": "x", "status": "없는상태"}, headers=h
    )
    assert r.status_code == 422

    # board move: status patch
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**created, "status": "서류제출완"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "서류제출완"

    # project filter
    r = client.get(f"/dashboard/support-programs?project_id={project_id}", headers=h)
    assert [i["program_id"] for i in r.json()] == [pid]
    r = client.get(f"/dashboard/support-programs?project_id={uuid.uuid4()}", headers=h)
    assert r.json() == []

    r = client.delete(f"/dashboard/support-programs/{pid}", headers=h)
    assert r.status_code == 204
    assert client.get("/dashboard/support-programs", headers=h).json() == []


def test_support_program_reorder_and_cancel_status(company_user: uuid.UUID) -> None:
    h = H(company_user)

    # 협약취소 상태가 허용된다
    r = client.post(
        "/dashboard/support-programs",
        json={"name": "협약 취소건", "status": "협약취소"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["status"] == "협약취소"

    # 합격 컬럼에 카드 3개
    ids = []
    for name in ("A", "B", "C"):
        rr = client.post(
            "/dashboard/support-programs",
            json={"name": name, "status": "합격"},
            headers=h,
        )
        ids.append(rr.json()["program_id"])

    # 드래그 재정렬: C, A, B 순서로 + sort_order 0..n 재부여
    reordered = [ids[2], ids[0], ids[1]]
    r = client.post(
        "/dashboard/support-programs/reorder",
        json={"status": "합격", "ordered_ids": reordered},
        headers=h,
    )
    assert r.status_code == 200
    rows = {i["program_id"]: i for i in r.json()}
    assert [rows[pid]["sort_order"] for pid in reordered] == [0, 1, 2]

    # 다른 컬럼 카드를 reorder로 합격 컬럼 맨 앞에 끌어오면 컬럼 이동 + 위치 지정
    rr = client.post(
        "/dashboard/support-programs",
        json={"name": "moved", "status": "시작 전"},
        headers=h,
    )
    moved = rr.json()["program_id"]
    r = client.post(
        "/dashboard/support-programs/reorder",
        json={"status": "합격", "ordered_ids": [moved, *reordered]},
        headers=h,
    )
    rows = {i["program_id"]: i for i in r.json()}
    assert rows[moved]["status"] == "합격"
    assert rows[moved]["sort_order"] == 0

    # 잘못된 상태는 거부
    r = client.post(
        "/dashboard/support-programs/reorder",
        json={"status": "없는상태", "ordered_ids": []},
        headers=h,
    )
    assert r.status_code == 422


def test_support_program_presentation_calendar_sync(company_user: uuid.UUID) -> None:
    h = H(company_user)
    owner = client.post(
        "/dashboard/team-members", json={"name": "박기획", "title": "개발"}, headers=h
    ).json()

    # default owner falls back to 박기획
    r = client.post(
        "/dashboard/support-programs",
        json={"name": "발표 동기화", "status": "발표준비"},
        headers=h,
    )
    assert r.status_code == 201
    created = r.json()
    assert created["owner_member_id"] == owner["member_id"]
    assert created["owner_name"] == "박기획"
    assert created["calendar_event_id"] is None
    pid = created["program_id"]

    # setting 발표일 auto-creates a team calendar event with the owner
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**created, "presentation_date": "2026-07-20"},
        headers=h,
    )
    assert r.status_code == 200
    updated = r.json()
    event_id = updated["calendar_event_id"]
    assert event_id is not None
    ev = client.get("/dashboard/calendar?from=2026-07-01&to=2026-07-31", headers=h).json()
    match = [e for e in ev if e["event_id"] == event_id]
    assert match and match[0]["title"] == "[발표] 발표 동기화"
    assert match[0]["event_date"] == "2026-07-20"
    assert match[0]["member_ids"] == [owner["member_id"]]

    # 발표일만 있으면 종일 일정으로 등록된다
    assert match[0]["all_day"] is True
    assert match[0]["start_time"] is None

    # 발표 시간을 넣으면 같은 이벤트가 시간 일정으로 갱신된다
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**updated, "presentation_time": "14:30"},
        headers=h,
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["presentation_time"] == "14:30:00"
    assert updated["calendar_event_id"] == event_id
    ev = client.get("/dashboard/calendar?from=2026-07-01&to=2026-07-31", headers=h).json()
    match = [e for e in ev if e["event_id"] == event_id]
    assert match and match[0]["all_day"] is False
    assert match[0]["start_time"] == "14:30:00"

    # 시간을 비우면 다시 종일 일정으로 돌아간다
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**updated, "presentation_time": None},
        headers=h,
    )
    updated = r.json()
    ev = client.get("/dashboard/calendar?from=2026-07-01&to=2026-07-31", headers=h).json()
    match = [e for e in ev if e["event_id"] == event_id]
    assert match and match[0]["all_day"] is True
    assert match[0]["start_time"] is None

    # changing 발표일 moves the same event
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**updated, "presentation_date": "2026-07-25"},
        headers=h,
    )
    assert r.json()["calendar_event_id"] == event_id

    # clearing 발표일 removes the event
    r = client.patch(
        f"/dashboard/support-programs/{pid}",
        json={**updated, "presentation_date": None},
        headers=h,
    )
    assert r.json()["calendar_event_id"] is None
    ev = client.get("/dashboard/calendar?from=2026-07-01&to=2026-07-31", headers=h).json()
    assert [e for e in ev if e["event_id"] == event_id] == []

    # deleting a program with 발표일 removes its event too
    r = client.post(
        "/dashboard/support-programs",
        json={"name": "삭제 동기화", "status": "발표준비", "presentation_date": "2026-08-01"},
        headers=h,
    )
    eid = r.json()["calendar_event_id"]
    assert eid is not None
    client.delete(f"/dashboard/support-programs/{r.json()['program_id']}", headers=h)
    ev = client.get("/dashboard/calendar?from=2026-08-01&to=2026-08-31", headers=h).json()
    assert [e for e in ev if e["event_id"] == eid] == []


def test_support_note_crud(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post(
        "/dashboard/support-notes",
        json={"kind": "site", "title": "기업마당", "url": "https://www.bizinfo.go.kr"},
        headers=h,
    )
    assert r.status_code == 201
    nid = r.json()["note_id"]

    assert (
        client.post(
            "/dashboard/support-notes", json={"kind": "bogus", "title": "x"}, headers=h
        ).status_code
        == 422
    )

    r = client.get("/dashboard/support-notes?kind=site", headers=h)
    assert [n["note_id"] for n in r.json()] == [nid]
    assert client.get("/dashboard/support-notes?kind=tip", headers=h).json() == []

    r = client.patch(
        f"/dashboard/support-notes/{nid}",
        json={"kind": "site", "title": "기업마당", "description": "정부 지원사업 통합 공고"},
        headers=h,
    )
    assert r.json()["description"] == "정부 지원사업 통합 공고"

    assert client.delete(f"/dashboard/support-notes/{nid}", headers=h).status_code == 204
    assert client.get("/dashboard/support-notes", headers=h).json() == []


# ---------------- 프로젝트 트래킹 (0094): 기간/오너/건강 + 활동 로그 ----------------
def test_project_tracking_fields_roundtrip_and_activity(company_user: uuid.UUID) -> None:
    h = H(company_user)
    r = client.post(
        "/dashboard/projects",
        json={"name": "추적테스트", "status": "진행중", "start_date": "2026-07-01"},
        headers=h,
    )
    assert r.status_code == 201
    pid = r.json()["project_id"]
    assert r.json()["start_date"] == "2026-07-01"
    assert r.json()["health"] is None

    r = client.patch(
        f"/dashboard/projects/{pid}",
        json={
            "name": "추적테스트",
            "status": "진행중",
            "start_date": "2026-07-01",
            "target_date": "2026-08-15",
            "health": "at_risk",
        },
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["target_date"] == "2026-08-15"
    assert body["health"] == "at_risk"
    assert body["updated_at"] is not None

    # 잘못된 health는 422
    r = client.patch(
        f"/dashboard/projects/{pid}",
        json={"name": "추적테스트", "health": "danger"},
        headers=h,
    )
    assert r.status_code == 422

    # 활동 피드: target_date + health 변경이 필드 단위로 남는다 (actor 포함).
    r = client.get(f"/dashboard/projects/{pid}/activity", headers=h)
    assert r.status_code == 200
    acts = r.json()
    fields = {(a["entity_type"], a["field"]) for a in acts}
    assert ("project", "health") in fields
    assert ("project", "target_date") in fields
    health_act = next(a for a in acts if a["field"] == "health")
    assert health_act["before"] is None
    assert health_act["after"] == "at_risk"
    assert health_act["actor_user_id"] == str(company_user)


def test_requirement_status_change_writes_activity(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "요구활동"}, headers=h).json()[
        "project_id"
    ]
    req = client.post(
        f"/dashboard/projects/{pid}/requirements",
        json={"kind": "goal", "text": "성능 목표 달성"},
        headers=h,
    ).json()
    r = client.patch(
        f"/dashboard/projects/{pid}/requirements/{req['requirement_id']}",
        json={"kind": "goal", "text": "성능 목표 달성", "status": "satisfied"},
        headers=h,
    )
    assert r.status_code == 200

    acts = client.get(f"/dashboard/projects/{pid}/activity", headers=h).json()
    kinds = [(a["entity_type"], a["action"]) for a in acts]
    assert ("requirement", "create") in kinds
    assert ("requirement", "status_change") in kinds
    sc = next(a for a in acts if a["action"] == "status_change")
    assert (sc["before"], sc["after"]) == ("open", "satisfied")


def test_board_row_create_delete_writes_activity(company_user: uuid.UUID) -> None:
    h = H(company_user)
    pid = client.post("/dashboard/projects", json={"name": "행활동"}, headers=h).json()[
        "project_id"
    ]
    board = client.post(f"/dashboard/projects/{pid}/board/ensure", headers=h).json()
    db_id = board["database_id"]
    title_prop = next(p["id"] for p in board["properties"] if p["type"] == "title")

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_prop: "촬영본 업로드"}},
        headers=h,
    ).json()
    r = client.delete(f"/dashboard/databases/{db_id}/rows/{row['row_id']}", headers=h)
    assert r.status_code == 204

    acts = client.get(f"/dashboard/projects/{pid}/activity", headers=h).json()
    row_acts = [a for a in acts if a["entity_type"] == "row"]
    assert {a["action"] for a in row_acts} == {"create", "delete"}
    assert all(a["after"] == "촬영본 업로드" for a in row_acts)


# ---------------- 청크 업로드 (대용량 동영상) ----------------
def test_chunked_media_upload_roundtrip(company_user: uuid.UUID, tmp_path, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "media_store_path", tmp_path)
    h = H(company_user)

    r = client.post(
        "/dashboard/media/uploads",
        json={"filename": "clip.mp4", "content_type": "video/mp4"},
        headers=h,
    )
    assert r.status_code == 200
    upload_id = r.json()["upload_id"]

    part1 = b"a" * 1024
    part2 = b"b" * 512
    r = client.put(f"/dashboard/media/uploads/{upload_id}/parts/0", content=part1, headers=h)
    assert r.status_code == 200 and r.json()["received"] == 1024
    # 순서 어긋난 파트는 409
    r = client.put(f"/dashboard/media/uploads/{upload_id}/parts/5", content=part2, headers=h)
    assert r.status_code == 409
    r = client.put(f"/dashboard/media/uploads/{upload_id}/parts/1", content=part2, headers=h)
    assert r.status_code == 200 and r.json()["received"] == 1536

    r = client.post(f"/dashboard/media/uploads/{upload_id}/complete", headers=h)
    assert r.status_code == 200
    asset = r.json()
    assert asset["size"] == 1536
    assert asset["content_type"] == "video/mp4"

    served = client.get(asset["url"], headers=h)
    assert served.status_code == 200
    assert served.content == part1 + part2

    # 취소는 멱등 (이미 완료된 세션에도 204)
    r = client.delete(f"/dashboard/media/uploads/{upload_id}", headers=h)
    assert r.status_code == 204
