"""Agent-work checklist-badge notify signal (PR-N1).

`GET /agent-work/notify-state` mirrors the FE checklist badge semantics
(AppShell isChecklistBadgeItem): open agent-work items excluding `wiki_task`
source rows and `agent_task` (chat delegation) family rows, owner-scoped
fail-closed via `_agent_work_owner_predicate` — on a company node the caller
sees shared company rows plus their own owner-bound rows, never another
user's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.agentwork import agent_work_notify_state
from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import agent_work_items, users

client = TestClient(app)


def _other_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Other User"))
        s.commit()
    return uid


def _item(
    *,
    title: str,
    created_at: datetime,
    state: str = "draft_for_review",
    source_kind: str = "assistant_command",
    action_family: str = "document_draft",
    owner_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> uuid.UUID:
    settings = get_settings()
    work_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=owner_id,
                source_kind=source_kind,
                source_ref_id=str(work_id),
                action_family=action_family,
                title=title,
                payload={},
                state=state,
                # policy_outcome has its own CHECK set; the badge only reads
                # `state`, so keep it NULL for simplicity.
                policy_outcome=None,
                policy_reason="test",
                reason_codes=[],
                evidence=[],
                created_by=created_by,
                created_at=created_at,
            )
        )
        s.commit()
    return work_id


def _seed(user_id: uuid.UUID, other: uuid.UUID) -> None:
    t = lambda d: datetime(2026, 1, d, tzinfo=UTC)  # noqa: E731
    # Counted: shared company rows in badge states.
    _item(title="문서 초안 검토", created_at=t(1), created_by=user_id)
    _item(
        title="이메일 초안 검토",
        created_at=t(3),
        state="request_more_data",
        action_family="email_send",
        created_by=user_id,
    )
    # Counted: the caller's own owner-bound row.
    _item(title="내 개인 항목", created_at=t(4), owner_id=user_id, created_by=user_id)
    # Excluded: wiki_task source (badge exclusion).
    _item(title="위키 태스크", created_at=t(6), source_kind="wiki_task", created_by=user_id)
    # Excluded: agent_task family (chat delegation).
    _item(
        title="위임 태스크",
        created_at=t(7),
        source_kind="assistant_command",
        action_family="agent_task",
        created_by=user_id,
    )
    # Excluded: closed state (not in the FE OPEN_AGENT_WORK_STATES set).
    _item(title="완료된 항목", created_at=t(8), state="resolved", created_by=user_id)
    # Excluded: another user's owner-bound row (newest overall -> must be filtered).
    _item(title="남의 개인 항목", created_at=t(9), owner_id=other, created_by=other)


def test_notify_state_mirrors_checklist_badge(clean, user_id):
    other = _other_user()
    _seed(user_id, other)

    state = agent_work_notify_state(user_id)

    # 2 shared company rows + own owner-bound row; excludes wiki_task,
    # agent_task family, resolved, and the other user's owner-bound row.
    assert state.total == 3
    assert state.latest_at == datetime(2026, 1, 4, tzinfo=UTC)
    assert state.latest_title == "내 개인 항목"


def test_notify_state_owner_predicate_excludes_other_users_rows(clean, user_id):
    other = _other_user()
    _seed(user_id, other)

    state = agent_work_notify_state(other)

    # Other sees shared company rows + their own row — never the caller-bound one.
    assert state.total == 3
    assert state.latest_at == datetime(2026, 1, 9, tzinfo=UTC)
    assert state.latest_title == "남의 개인 항목"


def test_notify_state_counts_all_open_badge_states(clean, user_id):
    t = lambda d: datetime(2026, 2, d, tzinfo=UTC)  # noqa: E731
    for day, state in enumerate(
        (
            "pending",
            "classified",
            "auto_execute",
            "draft_for_review",
            "request_more_data",
            "rejected",
        ),
        start=1,
    ):
        _item(title=f"{state} 항목", created_at=t(day), state=state, created_by=user_id)
    _item(title="resolved 항목", created_at=t(20), state="resolved", created_by=user_id)

    state = agent_work_notify_state(user_id)

    assert state.total == 6
    assert state.latest_title == "rejected 항목"


def test_notify_state_empty(clean, user_id):
    state = agent_work_notify_state(user_id)

    assert state.total == 0
    assert state.latest_at is None
    assert state.latest_title is None


def test_notify_state_api(clean, user_id):
    other = _other_user()
    _seed(user_id, other)

    r = client.get("/agent-work/notify-state", headers={"X-User-Id": str(user_id)})

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["latest_title"] == "내 개인 항목"
