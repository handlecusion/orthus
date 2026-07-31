"""P4.4 wiki-home Agent Work inbox summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.agentwork import create_assistant_command_work_item
from orthus.api.main import app
from orthus.db import session
from orthus.schemas.canonical import WikiPage, WikiTask
from orthus.settings import get_settings
from orthus.tables import promote_staging, users
from orthus.wiki import store as wiki_store
from orthus.wiki.gap import record_feedback

client = TestClient(app)


def _write_page(
    user_id: uuid.UUID, slug: str, *, scope: str, owner_id: uuid.UUID | None = None
) -> None:
    wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=slug,
            definition=f"{slug} definition",
            overview=f"{slug} overview",
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
        project="company",
    )


def _write_task(
    user_id: uuid.UUID,
    slug: str,
    *,
    created_at: datetime,
    scope: str,
    owner_id: uuid.UUID | None = None,
    resolved: bool = False,
) -> None:
    wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="open_question",
            description=f"Need source for {slug}",
            related=[],
            created_at=created_at,
            resolved=resolved,
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
        project="company",
    )


def _insert_pending_stage(user_id: uuid.UUID, *, title: str = "Personal note") -> uuid.UUID:
    stage_id = uuid.uuid4()
    now = datetime(2026, 6, 7, tzinfo=UTC)
    with session() as s:
        s.execute(
            insert(promote_staging).values(
                stage_id=stage_id,
                source_node_id="personal-a",
                source_doc_id=uuid.uuid4(),
                source_owner_id=user_id,
                source_scope="personal",
                source_title=title,
                sanitized_title=f"Sanitized {title}",
                sanitized_markdown="safe summary",
                source_meta={"target_project": "company"},
                status="pending",
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    return stage_id


def test_inbox_summary_company_buckets_counts_caps_and_links(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    base = datetime(2026, 6, 7, tzinfo=UTC)
    for index in range(6):
        _write_task(
            user_id,
            f"task-{index}",
            created_at=base + timedelta(minutes=index),
            scope="company",
        )
    _write_task(user_id, "task-resolved", created_at=base, scope="company", resolved=True)
    stage_id = _insert_pending_stage(user_id)
    gap_id = record_feedback(user_id, "회사 온보딩 문서 위치")
    assert gap_id is not None
    _write_page(user_id, "company-policy", scope="company")
    item = create_assistant_command_work_item(
        user_id,
        "문서 초안 작성해줘",
        actor_role="owner",
        context_wiki_slug="company-policy",
    )
    assert item is not None

    response = client.get("/agent-work/inbox-summary", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    body = response.json()
    assert body["node_kind"] == "company"
    buckets = body["buckets"]
    assert buckets["wiki_tasks"]["count"] == 6
    assert len(buckets["wiki_tasks"]["items"]) == 5
    assert buckets["wiki_tasks"]["items"][0]["slug"] == "task-5"
    assert buckets["wiki_tasks"]["items"][0]["href"] == "/wiki/tasks"
    assert buckets["promote_staging"]["supported"] is True
    assert buckets["promote_staging"]["count"] == 1
    assert buckets["promote_staging"]["items"][0]["stage_id"] == str(stage_id)
    assert buckets["promote_staging"]["items"][0]["href"] == "/promote"
    assert buckets["data_gaps"]["count"] == 1
    assert buckets["data_gaps"]["items"][0]["gap_id"] == str(gap_id)
    assert buckets["data_gaps"]["items"][0]["href"] == "/gaps"
    assert buckets["agent_work"]["count"] == 1
    assert buckets["agent_work"]["by_outcome"]["draft_for_review"] == 1
    assert sum(buckets["agent_work"]["by_outcome"].values()) == buckets["agent_work"]["count"]
    work = buckets["agent_work"]["items"][0]
    assert work["work_id"] == str(item.work_id)
    assert work["wiki_slugs"] == ["company-policy"]
    assert work["href"] == f"/agent-work?work_id={item.work_id}"


def test_inbox_summary_agent_work_counts_beyond_item_limit(clean, user_id, tmp_path, monkeypatch):
    """단일 SELECT(최신 5건) + 집계 COUNT로 바뀐 뒤에도 계약 유지: count는 open 전체
    총계, items는 updated_at 내림차순 최신 5건, by_outcome 합계 == count."""
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    items = []
    for index in range(7):
        item = create_assistant_command_work_item(
            user_id,
            f"문서 초안 작성해줘 {index}",
            actor_role="owner",
        )
        assert item is not None
        items.append(item)

    response = client.get("/agent-work/inbox-summary", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    bucket = response.json()["buckets"]["agent_work"]
    assert bucket["count"] == 7
    assert bucket["count_capped"] is False
    assert sum(bucket["by_outcome"].values()) == bucket["count"]
    assert len(bucket["items"]) == 5
    returned = [row["work_id"] for row in bucket["items"]]
    newest_first = [str(item.work_id) for item in reversed(items[-5:])]
    assert returned == newest_first


def test_inbox_summary_personal_clamps_owner_and_skips_promote(
    clean, user_id, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other User"))
        s.commit()
    base = datetime(2026, 6, 7, tzinfo=UTC)
    _write_task(user_id, "own-task", created_at=base, scope="personal", owner_id=user_id)
    _write_task(other_user, "other-task", created_at=base, scope="personal", owner_id=other_user)
    assert record_feedback(user_id, "내 Gmail 규칙") is not None
    assert record_feedback(other_user, "다른 사람 Gmail 규칙") is not None
    own_item = create_assistant_command_work_item(
        user_id,
        "문서 초안 작성해줘",
        actor_role="owner",
    )
    other_item = create_assistant_command_work_item(
        other_user,
        "문서 초안 작성해줘",
        actor_role="owner",
    )
    assert own_item is not None
    assert other_item is not None
    _insert_pending_stage(user_id)

    response = client.get("/agent-work/inbox-summary", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    buckets = response.json()["buckets"]
    assert buckets["promote_staging"] == {
        "count": 0,
        "supported": False,
        "count_capped": False,
        "items": [],
    }
    assert buckets["wiki_tasks"]["count"] == 1
    assert buckets["wiki_tasks"]["items"][0]["slug"] == "own-task"
    assert buckets["data_gaps"]["count"] == 1
    assert buckets["data_gaps"]["items"][0]["missing_topic"] == "내 Gmail 규칙"
    assert buckets["agent_work"]["count"] == 1
    assert buckets["agent_work"]["items"][0]["work_id"] == str(own_item.work_id)
