"""P2 acceptance: the `project` dimension (company → project → content).

Hierarchy: company (acme) → project (atlas | nova | orbit | company) →
content. The broad Notion workspace defaults to `atlas` for operational DBs,
while Nova, Orbit, and company-wide DBs are split out deterministically by
`resolve_project` (db_name match / Nova parent page).

Covers: resolve_project rule, migration 0006 defaults + Nova backfill, ingest
stamping, and project-scoped retrieval / structured query isolation.

Postgres must be up on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from sqlalchemy import insert, select, text

from orthus.connectors import NotionConnector, run_import
from orthus.connectors.project_map import (
    PROJECTS,
    NOVA_PARENT_PAGE_ID,
    resolve_project,
)
from orthus.db import session
from orthus.documents import _row_uuid, save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.structured.query import build_notion_catalog, query_structured
from orthus.tables import notion_rows
from orthus.wiki.retrieve import retrieve


# ---------------------------------------------------------------------------
# resolve_project (pure, no DB)
# ---------------------------------------------------------------------------


def test_resolve_project_nova_db_names() -> None:
    assert resolve_project(db_name="Nova 티켓") == "nova"
    assert resolve_project(db_name="Nova") == "nova"
    assert resolve_project(db_name="프로젝트 NOVA 백로그") == "nova"


def test_resolve_project_other_db_names_default_atlas() -> None:
    assert resolve_project(db_name="티켓") == "atlas"
    assert resolve_project(db_name="KPI") == "atlas"
    assert resolve_project(db_name=None) == "atlas"
    assert resolve_project(db_name="") == "atlas"


def test_resolve_project_company_common_db_names() -> None:
    assert resolve_project(db_name="팀원") == "company"
    assert resolve_project(db_name="참고 문서") == "company"
    assert resolve_project(db_name="AI관련툴") == "company"
    assert resolve_project(db_name="회사 미팅 기록") == "company"
    assert resolve_project(db_name="벤치마킹 회사 ") == "company"


def test_resolve_project_orbit_db_names() -> None:
    assert resolve_project(db_name="Orbit 매물") == "orbit"
    assert resolve_project(db_name="중앙센터 매물") == "orbit"
    assert resolve_project(db_name="원룸백과 리드") == "orbit"


def test_resolve_project_parent_chain_nova() -> None:
    assert resolve_project(db_name=None, parent_chain=[NOVA_PARENT_PAGE_ID]) == "nova"
    assert resolve_project(db_name=None, parent_chain=["some-other-page"]) == "atlas"


def test_project_enum_matches_check() -> None:
    assert PROJECTS == ("atlas", "nova", "orbit", "company")


# ---------------------------------------------------------------------------
# Migration 0006: columns, defaults, Nova backfill
# ---------------------------------------------------------------------------


def test_migration_0006_columns_default_atlas(user_id) -> None:
    """project column exists on the five tables and defaults to 'atlas' for rows
    inserted without an explicit project (mimicking a pre-0006 seeded row)."""
    row_id = uuid.uuid4()
    with session() as s:
        # Insert WITHOUT project → DB DEFAULT fills 'atlas'.
        s.execute(
            insert(notion_rows).values(
                row_id=row_id,
                db_id="db-x",
                db_name="티켓",
                properties={"a": "b"},
                user_id=user_id,
            )
        )
        s.commit()
        project = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == row_id)
        ).scalar()
    assert project == "atlas"

    with session() as s:
        cols = {
            r[0]
            for r in s.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE column_name = 'project' "
                    "AND table_name IN "
                    "('documents','corpus_chunks','embeddings','wiki_pages','notion_rows')"
                )
            ).all()
        }
    assert cols == {"documents", "corpus_chunks", "embeddings", "wiki_pages", "notion_rows"}


def test_migration_0006_backfills_nova_notion_rows(user_id) -> None:
    """The 0006 backfill UPDATE re-classifies Nova db_name rows to 'nova'. We
    simulate a pre-0006 row (explicit project='atlas') then run the same UPDATE
    predicate to assert it flips Nova rows only."""
    nova_id = uuid.uuid4()
    other_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=nova_id,
                db_id="db-s",
                db_name="Nova 백로그",
                properties={},
                project="atlas",
                user_id=user_id,
            )
        )
        s.execute(
            insert(notion_rows).values(
                row_id=other_id,
                db_id="db-o",
                db_name="티켓",
                properties={},
                project="atlas",
                user_id=user_id,
            )
        )
        s.commit()
        # Re-run the migration's backfill predicate.
        s.execute(
            text(
                "UPDATE notion_rows SET project='nova' "
                "WHERE db_name LIKE 'Nova%' OR db_name LIKE '%NOVA%'"
            )
        )
        s.commit()
        nova_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == nova_id)
        ).scalar()
        other_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == other_id)
        ).scalar()
    assert nova_proj == "nova"
    assert other_proj == "atlas"


# ---------------------------------------------------------------------------
# Ingest: project stamped on notion_rows via resolve_project
# ---------------------------------------------------------------------------

_NOVA_DB_ID = "db-nova-0001"
_CAST_DB_ID = "db-atlas-0001"


def _db_search_response() -> dict[str, Any]:
    return {
        "object": "list",
        "results": [
            {"object": "database", "id": _NOVA_DB_ID, "title": [{"plain_text": "Nova 티켓"}]},
            {"object": "database", "id": _CAST_DB_ID, "title": [{"plain_text": "티켓"}]},
        ],
        "has_more": False,
        "next_cursor": None,
    }


def _empty_page_search() -> dict[str, Any]:
    return {"object": "list", "results": [], "has_more": False, "next_cursor": None}


def _row(row_id: str, db_id: str) -> dict:
    return {
        "object": "page",
        "id": row_id,
        "last_edited_time": "2024-02-01T10:00:00.000Z",
        "parent": {"type": "database_id", "database_id": db_id},
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "행"}]},
            "상태": {"type": "status", "status": {"name": "진행중"}},
        },
    }


def _ingest_handler() -> Any:
    nova_row = "row-nova-1"
    cast_row = "row-cast-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            body = request.read().decode() if request.content else ""
            if "database" in body:
                return httpx.Response(200, json=_db_search_response())
            return httpx.Response(200, json=_empty_page_search())
        if path == f"/v1/databases/{_NOVA_DB_ID}/query":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [_row(nova_row, _NOVA_DB_ID)],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == f"/v1/databases/{_CAST_DB_ID}/query":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [_row(cast_row, _CAST_DB_ID)],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return httpx.Response(404, json={"message": f"not found: {path}"})

    return handler, nova_row, cast_row


def test_ingest_stamps_project_on_notion_rows(user_id) -> None:
    """A Nova DB row → notion_rows.project='nova'; a non-Nova row → 'atlas'."""
    handler, nova_row, cast_row = _ingest_handler()
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    connector = NotionConnector("test-token", client=client, min_interval=0)
    run_import(connector, user_id, since=None)

    with session() as s:
        nova_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == _row_uuid(nova_row))
        ).scalar()
        cast_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == _row_uuid(cast_row))
        ).scalar()
    assert nova_proj == "nova"
    assert cast_proj == "atlas"


# ---------------------------------------------------------------------------
# Structured query: project narrows the catalog + execution
# ---------------------------------------------------------------------------


def _seed_row(user_id, *, db_name, properties, project, scope="company") -> None:
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id=f"db-{db_name}",
                db_name=db_name,
                properties=properties,
                scope=scope,
                project=project,
                user_id=user_id,
            )
        )
        s.commit()


def test_structured_catalog_scoped_to_project(user_id) -> None:
    """build_notion_catalog(project='nova') surfaces only nova dbs; 'atlas'
    excludes nova; None sees both."""
    _seed_row(user_id, db_name="Nova 티켓", properties={"상태": "진행중"}, project="nova")
    _seed_row(user_id, db_name="티켓", properties={"상태": "완료"}, project="atlas")

    nova_cat = build_notion_catalog(user_id, scope="all", project="nova")
    assert "Nova 티켓" in nova_cat["tables"]["notion_rows"]["description"]
    assert "티켓 [" not in nova_cat["tables"]["notion_rows"]["description"].replace(
        "Nova 티켓", ""
    )

    cast_cat = build_notion_catalog(user_id, scope="all", project="atlas")
    desc_c = cast_cat["tables"]["notion_rows"]["description"]
    assert "Nova" not in desc_c
    assert "티켓" in desc_c

    all_cat = build_notion_catalog(user_id, scope="all", project=None)
    desc_all = all_cat["tables"]["notion_rows"]["description"]
    assert "Nova 티켓" in desc_all and "티켓 [" in desc_all


def test_structured_query_project_isolation(user_id) -> None:
    """query_structured(project='nova') counts only nova rows; the executor-level
    scope rewrite blocks cross-project leakage even if the SQL omits a project filter."""
    _seed_row(user_id, db_name="Nova 티켓", properties={"상태": "진행중"}, project="nova")
    _seed_row(user_id, db_name="Nova 티켓", properties={"상태": "진행중"}, project="nova")
    # A atlas row in a SAME-NAMED db: the rewrite must exclude it under project=nova.
    _seed_row(user_id, db_name="Nova 티켓", properties={"상태": "완료"}, project="atlas")

    sql = "SELECT count(*) AS n FROM notion_rows WHERE db_name = 'Nova 티켓'"
    chat = MockChat(rules=[("Nova", '{"sql": "%s"}' % sql)])
    res = query_structured(user_id, "Nova 티켓 개수", project="nova", chat_model=chat)
    assert res.status == "executed"
    # Only the 2 nova rows — the atlas row in the same db is filtered by the rewrite.
    assert res.rows[0][0] == 2


# ---------------------------------------------------------------------------
# Wiki retrieval: project narrows grounding
# ---------------------------------------------------------------------------


def _distill_json(summary: str, claim: str, *, page_slug: str, page_title: str) -> str:
    return json.dumps(
        {
            "summary": summary,
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": claim,
                    "evidence": claim,
                    "confidence": "high",
                    "page": {"slug": page_slug, "title": page_title},
                    "conflicting": [],
                }
            ],
        }
    )


def test_retrieve_filters_by_project(user_id) -> None:
    """retrieve(project='nova') returns only nova pages; 'atlas' excludes nova;
    None returns both."""
    topic_s = "프로젝트-노바-릴리스노트"
    topic_c = "프로젝트-아틀라스-온보딩"
    save_editor_document(
        user_id,
        "노바 릴리스",
        [{}],
        f"{topic_s}: 노바 v2 릴리스 노트.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic_s} 요약", f"{topic_s}: 노바 v2.", page_slug=topic_s, page_title="노바"
            )
        ),
        scope="company",
        project="nova",
    )
    save_editor_document(
        user_id,
        "아틀라스 온보딩",
        [{}],
        f"{topic_c}: 아틀라스 온보딩 가이드.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic_c} 요약", f"{topic_c}: 온보딩.", page_slug=topic_c, page_title="아틀라스"
            )
        ),
        scope="company",
        project="atlas",
    )

    nova_hits = retrieve(user_id, f"{topic_s} {topic_c}", k=10, project="nova")
    assert any(topic_s in h.page_slug for h in nova_hits)
    assert not any(topic_c in h.page_slug for h in nova_hits)

    cast_hits = retrieve(user_id, f"{topic_s} {topic_c}", k=10, project="atlas")
    assert any(topic_c in h.page_slug for h in cast_hits)
    assert not any(topic_s in h.page_slug for h in cast_hits)

    all_hits = retrieve(user_id, f"{topic_s} {topic_c}", k=10, project=None)
    slugs = {h.page_slug for h in all_hits}
    assert any(topic_s in s for s in slugs)
    assert any(topic_c in s for s in slugs)
