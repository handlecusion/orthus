"""Personal board <-> team schedule bidirectional sync (plan: board-calendar link).

Covers the company-node + owner-scope path where personal_board_tasks and
team_calendar_events share one DB: email-based member resolution + user_id
backfill, task->event push (register/update), and event->task pull on bootstrap.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    auth_identities,
    team_calendar_events,
    team_members,
)

client = TestClient(app)

NODE = "company"
EMAIL = "2fe@acme.example"


@pytest.fixture
def company_owner(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Logged-in account on a company node with owner-scope board enabled, plus a
    team member whose comma-separated email list includes the account's login email."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    with session() as s:
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="dev",
                provider_subject=str(user_id),
                email=EMAIL,
                email_verified=True,
            )
        )
        s.execute(
            insert(team_members).values(
                member_id=uuid.uuid4(),
                node_id=NODE,
                name="박기획",
                email="2fe@acme.example, member1@example.com",
                color="#5d9bd8",
                sort_order=1,
                active=True,
            )
        )
        s.commit()
    return user_id


def _hdr(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def test_register_task_to_team_schedule_backfills_member_and_creates_event(
    company_owner: uuid.UUID,
) -> None:
    uid = company_owner
    # a dated task with a start time
    created = client.post(
        "/personal-board/tasks",
        json={"title": "기획 회의", "scheduled_date": "2026-06-05", "scheduled_time": "14:30"},
        headers=_hdr(uid),
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    assert created.json()["team_event_id"] is None

    # register onto the team schedule
    linked = client.post(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid))
    assert linked.status_code == 200, linked.text
    event_id = linked.json()["team_event_id"]
    assert event_id is not None

    with session() as s:
        # member link was backfilled from the login email
        member = s.execute(
            select(team_members.c.user_id, team_members.c.member_id).where(
                team_members.c.node_id == NODE
            )
        ).one()
        assert member.user_id == uid
        # the team calendar event mirrors the task (member, date, start time, link)
        ev = s.execute(
            select(
                team_calendar_events.c.title,
                team_calendar_events.c.event_date,
                team_calendar_events.c.start_time,
                team_calendar_events.c.member_ids,
                team_calendar_events.c.source,
                team_calendar_events.c.source_task_id,
            ).where(team_calendar_events.c.event_id == uuid.UUID(event_id))
        ).one()
    assert ev.title == "기획 회의"
    assert ev.event_date.isoformat() == "2026-06-05"
    assert ev.start_time.strftime("%H:%M") == "14:30"
    assert ev.member_ids == [str(member.member_id)]
    assert ev.source == "personal_board"
    assert ev.source_task_id == uuid.UUID(task_id)


def test_editing_linked_task_pushes_to_event(company_owner: uuid.UUID) -> None:
    uid = company_owner
    task_id = client.post(
        "/personal-board/tasks",
        json={"title": "초안", "scheduled_date": "2026-06-05", "scheduled_time": "09:00"},
        headers=_hdr(uid),
    ).json()["task_id"]
    event_id = client.post(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid)).json()[
        "team_event_id"
    ]

    client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"title": "최종본", "scheduled_time": "10:15"},
        headers=_hdr(uid),
    )
    with session() as s:
        ev = s.execute(
            select(team_calendar_events.c.title, team_calendar_events.c.start_time).where(
                team_calendar_events.c.event_id == uuid.UUID(event_id)
            )
        ).one()
    assert ev.title == "최종본"
    assert ev.start_time.strftime("%H:%M") == "10:15"


def test_board_edit_does_not_clobber_event_no_return(company_owner: uuid.UUID) -> None:
    # "복귀불가" now lives on the team calendar event, not the board. Editing the
    # linked board task must NOT overwrite the event's no_return (the board->event
    # push was decoupled from no_return when the toggle moved to the team schedule).
    uid = company_owner
    task_id = client.post(
        "/personal-board/tasks",
        json={"title": "외근", "scheduled_date": "2026-06-05", "scheduled_time": "09:00"},
        headers=_hdr(uid),
    ).json()["task_id"]
    event_id = client.post(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid)).json()[
        "team_event_id"
    ]
    # the board->event link no longer writes no_return, so the insert relies on the
    # DB server_default (false). Lock that contract.
    with session() as s:
        assert (
            s.execute(
                select(team_calendar_events.c.no_return).where(
                    team_calendar_events.c.event_id == uuid.UUID(event_id)
                )
            ).scalar_one()
            is False
        )
    # the team schedule turns on 복귀불가 for the event
    with session() as s:
        s.execute(
            update(team_calendar_events)
            .where(team_calendar_events.c.event_id == uuid.UUID(event_id))
            .values(no_return=True)
        )
        s.commit()
    # editing the board task pushes title/time to the event but must leave no_return
    client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"title": "외근(수정)", "scheduled_time": "10:15"},
        headers=_hdr(uid),
    )
    with session() as s:
        ev = s.execute(
            select(team_calendar_events.c.title, team_calendar_events.c.no_return).where(
                team_calendar_events.c.event_id == uuid.UUID(event_id)
            )
        ).one()
    assert ev.title == "외근(수정)"  # title still pushes
    assert ev.no_return is True  # no_return preserved (not clobbered to task's false)


def test_unlink_removes_event(company_owner: uuid.UUID) -> None:
    uid = company_owner
    task_id = client.post(
        "/personal-board/tasks",
        json={"title": "임시", "scheduled_date": "2026-06-05"},
        headers=_hdr(uid),
    ).json()["task_id"]
    event_id = client.post(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid)).json()[
        "team_event_id"
    ]

    out = client.delete(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid))
    assert out.status_code == 200, out.text
    assert out.json()["team_event_id"] is None
    with session() as s:
        gone = s.execute(
            select(team_calendar_events.c.event_id).where(
                team_calendar_events.c.event_id == uuid.UUID(event_id)
            )
        ).first()
    assert gone is None


def test_team_event_pulls_into_board_and_is_idempotent(company_owner: uuid.UUID) -> None:
    uid = company_owner
    # bootstrap once to backfill the member link, then read the member_id
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))
    with session() as s:
        member_id = (
            s.execute(select(team_members.c.member_id).where(team_members.c.node_id == NODE))
            .one()
            .member_id
        )
        event_id = uuid.uuid4()
        s.execute(
            insert(team_calendar_events).values(
                event_id=event_id,
                node_id=NODE,
                member_ids=[str(member_id)],
                title="전사 미팅",
                all_day=False,
                event_date="2026-06-05",
                start_time="11:00",
                event_type="meeting",
            )
        )
        s.commit()

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day = next(d for d in body["days"] if d["date"] == "2026-06-05")
    pulled = [t for t in day["tasks"] if t["team_event_id"] == str(event_id)]
    assert len(pulled) == 1
    assert pulled[0]["title"] == "전사 미팅"
    assert pulled[0]["scheduled_time"] == "11:00:00"
    assert pulled[0]["source_kind"] == "calendar"

    # second bootstrap must not duplicate the pulled task
    body2 = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day2 = next(d for d in body2["days"] if d["date"] == "2026-06-05")
    again = [t for t in day2["tasks"] if t["team_event_id"] == str(event_id)]
    assert len(again) == 1


def test_team_event_no_return_mirrors_into_board(company_owner: uuid.UUID) -> None:
    # The team event OWNS no_return; the pull mirrors it onto the board row and
    # re-syncs when the team schedule flips the flag (change-detection in
    # sync_team_events_to_board). The board no longer renders it, but the data
    # must stay consistent.
    uid = company_owner
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))
    with session() as s:
        member_id = (
            s.execute(select(team_members.c.member_id).where(team_members.c.node_id == NODE))
            .one()
            .member_id
        )
        event_id = uuid.uuid4()
        s.execute(
            insert(team_calendar_events).values(
                event_id=event_id,
                node_id=NODE,
                member_ids=[str(member_id)],
                title="외근",
                all_day=False,
                event_date="2026-06-05",
                start_time="11:00",
                event_type="event",
                no_return=True,
            )
        )
        s.commit()

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day = next(d for d in body["days"] if d["date"] == "2026-06-05")
    pulled = next(t for t in day["tasks"] if t["team_event_id"] == str(event_id))
    assert pulled["no_return"] is True

    # team schedule turns it off -> next pull re-syncs the board row
    with session() as s:
        s.execute(
            update(team_calendar_events)
            .where(team_calendar_events.c.event_id == event_id)
            .values(no_return=False)
        )
        s.commit()
    body2 = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day2 = next(d for d in body2["days"] if d["date"] == "2026-06-05")
    pulled2 = next(t for t in day2["tasks"] if t["team_event_id"] == str(event_id))
    assert pulled2["no_return"] is False


def test_pulled_event_shows_companions_or_solo(company_owner: uuid.UUID) -> None:
    """팀 일정이 개인 보드로 넘어올 때, 같이 가는 멤버가 있으면 동행자를, 혼자면
    '단독'을 source_label에 보여준다."""
    uid = company_owner
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))
    with session() as s:
        me = (
            s.execute(select(team_members.c.member_id).where(team_members.c.node_id == NODE))
            .one()
            .member_id
        )
        other = uuid.uuid4()
        s.execute(
            insert(team_members).values(
                member_id=other,
                node_id=NODE,
                name="이개발",
                email="csm@acme.example",
                color="#9b68d6",
                sort_order=2,
                active=True,
            )
        )
        solo_event = uuid.uuid4()
        duo_event = uuid.uuid4()
        s.execute(
            insert(team_calendar_events).values(
                event_id=solo_event,
                node_id=NODE,
                member_ids=[str(me)],
                title="단독 외근",
                all_day=False,
                event_date="2026-06-05",
                start_time="09:00",
                event_type="meeting",
            )
        )
        s.execute(
            insert(team_calendar_events).values(
                event_id=duo_event,
                node_id=NODE,
                member_ids=[str(me), str(other)],
                title="모자이크 미팅",
                all_day=False,
                event_date="2026-06-05",
                start_time="14:00",
                event_type="meeting",
            )
        )
        s.commit()

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day = next(d for d in body["days"] if d["date"] == "2026-06-05")
    solo = next(t for t in day["tasks"] if t["team_event_id"] == str(solo_event))
    duo = next(t for t in day["tasks"] if t["team_event_id"] == str(duo_event))
    assert solo["source_label"] == "팀 일정 · 단독"
    assert duo["source_label"] == "팀 일정 · 이개발"


def test_register_requires_dated_task(company_owner: uuid.UUID) -> None:
    uid = company_owner
    boot = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    bucket_id = boot["backlog_buckets"][0]["bucket_id"]
    task_id = client.post(
        "/personal-board/tasks",
        json={"title": "백로그", "backlog_bucket_id": bucket_id},
        headers=_hdr(uid),
    ).json()["task_id"]
    out = client.post(f"/personal-board/tasks/{task_id}/team-event", headers=_hdr(uid))
    assert out.status_code == 422


def test_cross_midnight_team_event_pulls_with_normalized_end_and_stays_editable(
    company_owner: uuid.UUID,
) -> None:
    """team_calendar_events has no end>start invariant, but the board enforces one.
    A cross-midnight event (23:00 start, 01:00 end) must mirror with a NULL
    scheduled_end_time so the pulled task does not get trapped — any later edit
    re-validates the scheduled-end invariant and would 422 if end<=start persisted."""
    uid = company_owner
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))
    with session() as s:
        member_id = (
            s.execute(select(team_members.c.member_id).where(team_members.c.node_id == NODE))
            .one()
            .member_id
        )
        event_id = uuid.uuid4()
        s.execute(
            insert(team_calendar_events).values(
                event_id=event_id,
                node_id=NODE,
                member_ids=[str(member_id)],
                title="야간 당직",
                all_day=False,
                event_date="2026-06-05",
                start_time="23:00",
                end_time="01:00",  # end < start (cross-midnight) — invalid for the board
                event_type="meeting",
            )
        )
        s.commit()

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    day = next(d for d in body["days"] if d["date"] == "2026-06-05")
    pulled = next(t for t in day["tasks"] if t["team_event_id"] == str(event_id))
    assert pulled["scheduled_time"] == "23:00:00"
    # end was dropped on the pull boundary, not copied verbatim.
    assert pulled["scheduled_end_time"] is None

    # The mirrored task is not trapped: an unrelated edit still succeeds.
    patched = client.patch(
        f"/personal-board/tasks/{pulled['task_id']}",
        json={"priority": "priority"},
        headers=_hdr(uid),
    )
    assert patched.status_code == 200
    assert patched.json()["priority"] == "priority"
