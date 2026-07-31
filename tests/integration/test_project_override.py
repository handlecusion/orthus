"""P2 acceptance: user-editable `project` via a db_name-level override.

Ingest assigns a DEFAULT project (id-set membership / db_name heuristic); a user
override (per db_name, in `project_overrides`) takes precedence and persists across
reseeds. Apply scope = structured(notion_rows) AND wiki/corpus (scope "B"):
documents carry source_db_name so wiki/corpus content is traceable to a db_name.

Covers migration 0007 (project_overrides + documents.source_db_name backfill),
override-precedence at ingest, set_project_override re-tag (structured + corpus +
1:1-traceable wiki), and the GET/PATCH /projects endpoints.

Postgres must be up on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text

from orthus.api.main import app
from orthus.connectors import NotionConnector, run_import
from orthus.connectors.notion_import import _build_project_resolver
from orthus.db import session
from orthus.documents import _row_uuid, save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.projects import set_project_override
from orthus.tables import (
    corpus_chunks,
    documents,
    embeddings,
    notion_rows,
    project_overrides,
    wiki_pages,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Migration 0007: project_overrides table + documents.source_db_name backfill
# ---------------------------------------------------------------------------


def test_migration_0007_columns_and_backfill(user_id) -> None:
    """project_overrides exists, documents.source_db_name exists, and the backfill
    UPDATE links a DB-row document to its db_name via source_external_id = row_id."""
    with session() as s:
        cols = {
            r[0]
            for r in s.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'source_db_name'"
                )
            ).all()
        }
        assert cols == {"source_db_name"}
        po_exists = s.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = 'project_overrides'")
        ).first()
        assert po_exists is not None

    # Simulate a pre-0007 DB-row document (source_db_name still NULL) + its
    # notion_rows row, then re-run the migration's backfill predicate.
    row_ext = str(uuid.uuid4())  # real Notion ids are UUIDs → join works
    row_id = _row_uuid(row_ext)
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="행",
                block_json=[],
                markdown="**상태**: 진행중",
                source="notion",
                source_external_id=row_ext,
                schema_version=1,
            )
        )
        s.execute(
            insert(notion_rows).values(
                row_id=row_id,
                db_id="db-x",
                db_name="협업업무표",
                properties={"상태": "진행중"},
                user_id=user_id,
            )
        )
        s.commit()
        s.execute(
            text(
                "UPDATE documents d SET source_db_name = nr.db_name "
                "FROM notion_rows nr WHERE d.source_external_id = nr.row_id::text"
            )
        )
        s.commit()
        got = s.execute(
            select(documents.c.source_db_name).where(documents.c.doc_id == doc_id)
        ).scalar()
    assert got == "협업업무표"


def test_migration_0007_roundtrip(user_id) -> None:
    """downgrade drops both objects; upgrade restores them. Run inline so we don't
    perturb the shared head migration the rest of the suite assumes."""
    with session() as s:
        # downgrade DDL
        s.execute(text("DROP INDEX IF EXISTS idx_documents_source_db_name"))
        s.execute(text("ALTER TABLE documents DROP COLUMN IF EXISTS source_db_name"))
        s.execute(text("DROP TABLE IF EXISTS project_overrides"))
        s.commit()
        gone = s.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='documents' AND column_name='source_db_name'"
            )
        ).first()
        assert gone is None
        # upgrade DDL (restore head so subsequent tests see the column/table)
        s.execute(
            text(
                "CREATE TABLE project_overrides ("
                "db_name TEXT PRIMARY KEY, "
                "project TEXT NOT NULL "
                "CHECK (project IN ('atlas','nova','orbit','company')), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        s.execute(text("ALTER TABLE documents ADD COLUMN source_db_name TEXT NULL"))
        s.execute(text("CREATE INDEX idx_documents_source_db_name ON documents(source_db_name)"))
        s.commit()
        back = s.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='documents' AND column_name='source_db_name'"
            )
        ).first()
    assert back is not None


# ---------------------------------------------------------------------------
# _build_project_resolver: override-by-db_name wins over id-set membership
# ---------------------------------------------------------------------------


def test_resolver_override_beats_id_set() -> None:
    atlas_ids = {"row-x"}
    nova_ids: set[str] = set()
    # db_name '티켓' overridden to 'nova', even though the page is in the atlas id-set.
    resolve = _build_project_resolver(atlas_ids, nova_ids, {"티켓": "nova"})
    assert resolve("row-x", "티켓") == "nova"  # override wins
    assert resolve("row-x", "다른DB") == "atlas"  # no override → id-set
    assert resolve("row-x", None) == "atlas"  # standalone page → no override


# ---------------------------------------------------------------------------
# Ingest with an override preset: a 티켓 row ingests as the override project
# ---------------------------------------------------------------------------

_DB_ID = "db-ticket-0001"
_ROW_ID = "row-ticket-1"  # synthetic id → atlas id-set membership (default)


def _row(row_id: str) -> dict:
    return {
        "object": "page",
        "id": row_id,
        "last_edited_time": "2024-04-01T10:00:00.000Z",
        "parent": {"type": "database_id", "database_id": _DB_ID},
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "행"}]},
            "상태": {"type": "status", "status": {"name": "진행중"}},
        },
    }


def _ingest_handler() -> Any:
    db_search = {
        "object": "list",
        "results": [{"object": "database", "id": _DB_ID, "title": [{"plain_text": "티켓"}]}],
        "has_more": False,
        "next_cursor": None,
    }
    page_search = {"object": "list", "results": [], "has_more": False, "next_cursor": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            body = request.read().decode() if request.content else ""
            if "database" in body:
                return httpx.Response(200, json=db_search)
            return httpx.Response(200, json=page_search)
        if path == f"/v1/databases/{_DB_ID}/query":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [_row(_ROW_ID)],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return httpx.Response(404, json={"message": f"not found: {path}"})

    return handler


def test_ingest_respects_override_preset(user_id) -> None:
    """With an override row db_name='티켓'→'nova' preset, a 티켓 row ingests as
    'nova' even though id-set membership (atlas) would say otherwise."""
    with session() as s:
        s.execute(insert(project_overrides).values(db_name="티켓", project="nova"))
        s.commit()

    # atlas id-set contains the row → default would be 'atlas'.
    resolver = _build_project_resolver({_ROW_ID}, set(), {"티켓": "nova"})
    client_http = httpx.Client(
        transport=httpx.MockTransport(_ingest_handler()),
        base_url="https://api.notion.com/v1",
    )
    connector = NotionConnector(
        "broad-token", client=client_http, min_interval=0, project_resolver=resolver
    )
    run_import(connector, user_id, since=None)

    with session() as s:
        proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == _row_uuid(_ROW_ID))
        ).scalar()
        # documents.source_db_name is stamped from the row's db_name.
        sdb = s.execute(
            select(documents.c.source_db_name).where(documents.c.source_external_id == _ROW_ID)
        ).scalar()
    assert proj == "nova"  # override wins over the atlas id-set
    assert sdb == "티켓"


# ---------------------------------------------------------------------------
# set_project_override: re-tag structured + corpus + persist
# ---------------------------------------------------------------------------


def _seed_db_row_doc(user_id, *, db_name, title, markdown, project) -> uuid.UUID:
    """Seed a notion_rows row + a matching documents row (source_db_name=db_name)
    + corpus chunk/embedding, mimicking a completed ingest."""
    row_ext = str(uuid.uuid4())
    row_id = _row_uuid(row_ext)
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=row_id,
                db_id=f"db-{db_name}",
                db_name=db_name,
                properties={"상태": "진행중"},
                project=project,
                user_id=user_id,
            )
        )
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json=[],
                markdown=markdown,
                source="notion",
                source_external_id=row_ext,
                source_db_name=db_name,
                schema_version=1,
                project=project,
            )
        )
        emb_id = uuid.uuid4()
        s.execute(
            insert(embeddings).values(
                embedding_id=emb_id,
                user_id=user_id,
                kind="corpus_chunk",
                ref_id=doc_id,
                vec=[0.1] * 1024,
                meta={},
                schema_version=1,
                model_version="mock",
                project=project,
            )
        )
        s.execute(
            insert(corpus_chunks).values(
                chunk_id=uuid.uuid4(),
                doc_id=doc_id,
                ordinal=0,
                content=markdown,
                embedding_id=emb_id,
                project=project,
            )
        )
        s.commit()
    return doc_id


def test_set_project_override_retags_structured_and_corpus(user_id) -> None:
    """set_project_override('협업업무표','company') flips notion_rows + documents +
    their corpus chunk/embedding, returns counts, and persists the override."""
    doc_id = _seed_db_row_doc(
        user_id,
        db_name="협업업무표",
        title="업무 A",
        markdown="업무 A 설명.",
        project="atlas",
    )

    counts = set_project_override("협업업무표", "company")
    assert counts["notion_rows"] == 1
    assert counts["documents"] == 1
    assert counts["corpus_chunks"] == 1
    assert counts["corpus_embeddings"] == 1

    with session() as s:
        nr_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.db_name == "협업업무표")
        ).scalar()
        doc_proj = s.execute(
            select(documents.c.project).where(documents.c.doc_id == doc_id)
        ).scalar()
        cc_proj = s.execute(
            select(corpus_chunks.c.project).where(corpus_chunks.c.doc_id == doc_id)
        ).scalar()
        emb_proj = s.execute(
            select(embeddings.c.project).where(
                embeddings.c.kind == "corpus_chunk", embeddings.c.ref_id == doc_id
            )
        ).scalar()
        ov = s.execute(
            select(project_overrides.c.project).where(project_overrides.c.db_name == "협업업무표")
        ).scalar()
    assert nr_proj == doc_proj == cc_proj == emb_proj == "company"
    assert ov == "company"  # persists for the next reseed


# ---------------------------------------------------------------------------
# Wiki re-tag: a 1:1-traceable wiki page flips project on override
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


def test_set_project_override_retags_traceable_wiki(user_id) -> None:
    """A wiki page authored from a SINGLE source doc with source_db_name=X (1:1
    traceable) flips project when X is overridden; its wiki_chunk embeddings too."""
    # Author a doc through the real wiki pipeline so source/page/links/embeddings
    # all exist, then mark the doc's source_db_name so it is traceable to '협업업무표'.
    topic = "프로젝트-협업-가이드"
    title = "협업 가이드"
    doc_id = save_editor_document(
        user_id,
        title,
        [{}],
        f"{topic}: 협업 가이드 본문.",
        chat_model=MockChat(
            default=_distill_json(
                f"{topic} 요약", f"{topic}: 협업.", page_slug=topic, page_title="협업"
            )
        ),
        scope="company",
        project="atlas",
    )
    with session() as s:
        s.execute(
            documents.update()
            .where(documents.c.doc_id == doc_id)
            .values(source_db_name="협업업무표")
        )
        s.commit()

    counts = set_project_override("협업업무표", "company")
    assert counts["wiki_pages"] >= 1  # at least the source page + the 1:1 concept page
    assert counts["wiki_embeddings"] >= 1

    with session() as s:
        # the concept page (kind='page') derived solely from this doc → re-tagged.
        page_proj = s.execute(
            select(wiki_pages.c.project).where(
                wiki_pages.c.kind == "page", wiki_pages.c.slug == topic
            )
        ).scalar()
        # its wiki_chunk embeddings follow.
        emb_projs = {
            r[0]
            for r in s.execute(
                select(embeddings.c.project).where(embeddings.c.kind == "wiki_chunk")
            ).all()
        }
    assert page_proj == "company"
    assert emb_projs == {"company"}


# ---------------------------------------------------------------------------
# Endpoints: GET /projects (groups + source flag + enum) and PATCH /projects
# ---------------------------------------------------------------------------


def test_get_and_patch_projects(user_id) -> None:
    _seed_db_row_doc(
        user_id,
        db_name="협업업무표",
        title="업무 B",
        markdown="업무 B.",
        project="atlas",
    )
    _seed_db_row_doc(
        user_id,
        db_name="티켓",
        title="티켓 1",
        markdown="티켓.",
        project="atlas",
    )

    # GET: lists db_name × project × count, all 'default' (no overrides yet).
    r = client.get("/projects")
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == ["atlas", "nova", "orbit", "company"]
    groups = {(g["db_name"], g["project"]): g for g in body["groups"]}
    assert groups[("협업업무표", "atlas")]["count"] == 1
    assert groups[("협업업무표", "atlas")]["source"] == "default"
    assert groups[("티켓", "atlas")]["source"] == "default"

    # PATCH: override 협업업무표 → company.
    p = client.patch("/projects", json={"db_name": "협업업무표", "project": "company"})
    assert p.status_code == 200
    assert p.json()["updated"]["notion_rows"] == 1

    # re-GET reflects the new project + 'override' source flag.
    r2 = client.get("/projects")
    groups2 = {(g["db_name"], g["project"]): g for g in r2.json()["groups"]}
    assert ("협업업무표", "company") in groups2
    assert groups2[("협업업무표", "company")]["source"] == "override"
    # 티켓 untouched.
    assert ("티켓", "atlas") in groups2
    assert groups2[("티켓", "atlas")]["source"] == "default"


def test_patch_projects_invalid_project_422(user_id) -> None:
    r = client.patch("/projects", json={"db_name": "티켓", "project": "bogus"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# clear_project_override: revert to auto-classification heuristic
# ---------------------------------------------------------------------------


def test_clear_project_override_reverts_to_auto(user_id) -> None:
    """Apply an override, then clear it.

    After clearing:
      - notion_rows.project flips back to the heuristic-derived value
        (resolve_project(db_name='협업업무표') → 'atlas', no nova keywords).
      - project_overrides row is absent.
      - documents / corpus_chunks / corpus_embeddings re-tagged to the auto value.
    """
    from orthus.projects import clear_project_override

    doc_id = _seed_db_row_doc(
        user_id,
        db_name="협업업무표",
        title="업무 clear",
        markdown="clear test.",
        project="atlas",
    )

    # Step 1: apply an override → flips to 'company'.
    set_project_override("협업업무표", "company")
    with session() as s:
        nr_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.db_name == "협업업무표")
        ).scalar()
        ov = s.execute(
            select(project_overrides.c.project).where(project_overrides.c.db_name == "협업업무표")
        ).scalar()
    assert nr_proj == "company"
    assert ov == "company"

    # Step 2: clear the override → reverts to heuristic ('atlas' for this db_name).
    resolved, counts = clear_project_override("협업업무표")
    assert resolved == "atlas"  # resolve_project(db_name='협업업무표') → atlas
    assert counts["notion_rows"] >= 1
    assert counts["documents"] >= 1

    with session() as s:
        nr_proj2 = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.db_name == "협업업무표")
        ).scalar()
        doc_proj2 = s.execute(
            select(documents.c.project).where(documents.c.doc_id == doc_id)
        ).scalar()
        cc_proj2 = s.execute(
            select(corpus_chunks.c.project).where(corpus_chunks.c.doc_id == doc_id)
        ).scalar()
        ov_gone = s.execute(
            select(project_overrides.c.db_name).where(project_overrides.c.db_name == "협업업무표")
        ).scalar()

    assert nr_proj2 == "atlas"
    assert doc_proj2 == "atlas"
    assert cc_proj2 == "atlas"
    assert ov_gone is None  # override row removed


def test_clear_project_override_endpoint(user_id) -> None:
    """PATCH /projects with project:null clears the override and returns source='auto'."""
    _seed_db_row_doc(
        user_id,
        db_name="협업업무표",
        title="업무 endpoint",
        markdown="endpoint clear test.",
        project="atlas",
    )

    # Set an override first via the endpoint.
    p = client.patch("/projects", json={"db_name": "협업업무표", "project": "company"})
    assert p.status_code == 200
    assert p.json()["source"] == "override"

    # Clear via PATCH with null.
    c = client.patch("/projects", json={"db_name": "협업업무표", "project": None})
    assert c.status_code == 200
    body = c.json()
    assert body["db_name"] == "협업업무표"
    assert body["project"] == "atlas"  # heuristic result
    assert body["source"] == "auto"

    # GET /projects reflects source='default' (no override in table).
    r = client.get("/projects")
    assert r.status_code == 200
    groups = {g["db_name"]: g for g in r.json()["groups"]}
    assert groups["협업업무표"]["source"] == "default"
