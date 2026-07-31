"""Document persistence. Single place that writes `documents` rows and then runs
the corpus pipeline, so editor saves and connector imports share one path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit import audit
from orthus.corpus import index_document
from orthus.db import session
from orthus.kg.outbox import enqueue_kg_event
from orthus.models.base import ChatModel
from orthus.schemas.canonical import SCHEMA_VERSION, InternalDocument
from orthus.structured.rows import structured_row_uuid
from orthus.tables import agent_work_items, documents, notion_rows, structured_rows

# Namespace for deriving a deterministic UUID PK from a Notion row id that is
# not itself a valid UUID (real Notion ids are UUIDs; tests use synthetic ids).
_NOTION_ROW_NS = uuid.UUID("6e0a5d3c-2b4f-4c1a-9e7d-8f1a2b3c4d5e")


def _row_uuid(row_id: str) -> UUID:
    """Deterministic UUID PK for a notion_rows row. Real Notion ids are UUIDs;
    parse them directly so re-imports map to the same PK. Synthetic ids fall
    back to a namespaced uuid5."""
    try:
        return uuid.UUID(row_id)
    except (ValueError, AttributeError):
        return uuid.uuid5(_NOTION_ROW_NS, row_id)


def save_editor_document(
    user_id: UUID,
    title: str,
    block_json: list[dict],
    markdown: str,
    doc_id: UUID | None = None,
    *,
    chat_model: ChatModel | None = None,
    scope: str = "company",
    project: str = "atlas",
) -> UUID:
    """Insert (doc_id=None) or update an editor-sourced document, then reindex
    and author it into the LLM wiki (T1 trigger, docs/llm-wiki.md §7).

    `scope` ('company' | 'personal') threads through the corpus + wiki write path;
    personal saves are owned by `user_id` (P2.1, docs/architecture-v2.md §2).
    `project` ('atlas' | 'nova' | 'orbit' | 'company') tags the content with its
    company→project bucket; editor saves default to the workspace project (P2)."""
    now = datetime.now(UTC)
    with session() as s:
        if doc_id is None:
            doc_id = uuid.uuid4()
            s.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=user_id,
                    title=title,
                    block_json=block_json,
                    markdown=markdown,
                    source="editor",
                    schema_version=SCHEMA_VERSION,
                    scope=scope,
                    project=project,
                )
            )
        else:
            s.execute(
                update(documents)
                .where(documents.c.doc_id == doc_id)
                .values(
                    title=title,
                    block_json=block_json,
                    markdown=markdown,
                    updated_at=now,
                )
            )
        s.commit()
    index_document(doc_id, user_id, markdown, scope=scope, project=project)
    _author_into_wiki(doc_id, user_id, chat_model, scope=scope, project=project)
    return doc_id


def create_agent_draft_document(
    user_id: UUID,
    title: str,
    block_json: list[dict],
    markdown: str,
    *,
    scope: str = "company",
    project: str = "company",
) -> UUID:
    """Create an editor-visible agent draft without corpus/wiki authoring.

    P3 draft review needs an editable row before approval. The normal editor save
    path intentionally authors into corpus/wiki, so draft creation uses this
    narrower write path until the reviewer explicitly saves/publishes later.
    """
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json=block_json,
                markdown=markdown,
                source="agent_draft",
                schema_version=SCHEMA_VERSION,
                scope=scope,
                project=project,
            )
        )
        s.commit()
    return doc_id


def save_agent_draft_document(
    user_id: UUID,
    doc_id: UUID,
    title: str,
    block_json: list[dict],
    markdown: str,
) -> UUID:
    """Update a reviewer-visible agent draft without corpus/wiki authoring."""
    now = datetime.now(UTC)
    with session() as s:
        result = s.execute(
            update(documents)
            .where(
                documents.c.doc_id == doc_id,
                documents.c.user_id == user_id,
                documents.c.source == "agent_draft",
            )
            .values(
                title=title,
                block_json=block_json,
                markdown=markdown,
                updated_at=now,
            )
        )
        if result.rowcount == 0:
            raise LookupError("agent draft document not found")
        s.commit()
    return doc_id


def publish_agent_draft_document(
    user_id: UUID,
    doc_id: UUID,
    title: str,
    block_json: list[dict],
    markdown: str,
    *,
    chat_model: ChatModel | None = None,
) -> UUID:
    """Promote an agent draft into the normal editor pipeline.

    Publishing is the explicit reviewer gate: the row flips from `agent_draft`
    to `editor`, then corpus indexing and wiki authoring run with the draft's
    original scope/project.
    """
    with audit("document.publish") as span:
        now = datetime.now(UTC)
        with session() as s:
            row = s.execute(
                select(documents.c.source, documents.c.scope, documents.c.project).where(
                    documents.c.doc_id == doc_id,
                    documents.c.user_id == user_id,
                )
            ).first()
            if row is None:
                raise LookupError("document not found")
            if row.source != "agent_draft":
                raise ValueError("document is not an agent draft")

            scope = row.scope
            project = row.project
            span.add_meta(
                doc_id=str(doc_id),
                user_id=str(user_id),
                source_from="agent_draft",
                source_to="editor",
                scope=scope,
                project=project,
            )
            s.execute(
                update(documents)
                .where(
                    documents.c.doc_id == doc_id,
                    documents.c.user_id == user_id,
                    documents.c.source == "agent_draft",
                )
                .values(
                    title=title,
                    block_json=block_json,
                    markdown=markdown,
                    source="editor",
                    updated_at=now,
                )
            )
            # K3 KG outbox — publish만(draft save 제외, P3.4b 경계 그대로).
            enqueue_kg_event(
                s, entity_kind="document", entity_id=doc_id, scope=scope, owner_id=user_id
            )
            s.commit()

        index_document(doc_id, user_id, markdown, scope=scope, project=project)
        _author_into_wiki(doc_id, user_id, chat_model, scope=scope, project=project)
        resolved = _mark_agent_draft_work_resolved(user_id, doc_id)
        span.add_meta(agent_work_items_resolved=resolved)

    return doc_id


def _mark_agent_draft_work_resolved(user_id: UUID, doc_id: UUID) -> int:
    now = datetime.now(UTC)
    draft_doc_id = agent_work_items.c.payload["draft_document"]["doc_id"].astext
    with session() as s:
        result = s.execute(
            update(agent_work_items)
            .where(
                agent_work_items.c.action_family == "document_draft",
                agent_work_items.c.created_by == user_id,
                agent_work_items.c.state == "draft_for_review",
                draft_doc_id == str(doc_id),
            )
            .values(state="resolved", updated_at=now)
        )
        s.commit()
    return result.rowcount or 0


def upsert_source_document(
    user_id: UUID,
    doc: InternalDocument,
    *,
    chat_model: ChatModel | None = None,
    scope: str = "company",
    source_account_id: UUID | None = None,
    defer_authoring: bool = False,
) -> tuple[UUID, bool]:
    """Idempotent upsert for connector imports.

    Default key: (source, source_account_id, source_external_id). When a connector
    provides `source_canonical_id`, that canonical id wins so multiple accounts
    that discover the same upstream object map to one document inside the same
    scope/owner boundary. Company canonical identity ignores importer user;
    personal canonical identity includes user_id.

    Returns (doc_id, changed). `changed` is False when an existing row was already
    up to date (same source_last_edited_at) — lets incremental sync skip reindexing.

    `defer_authoring=True` runs the row upsert + corpus indexing (embed) but SKIPS
    the inline wiki T1 trigger (distill + consolidate). The bulk-import parallel
    pipeline uses this so the slow per-doc distill can run concurrently and
    consolidate stays serial+ordered (notion_import.run_parallel_import)."""
    with session() as s:
        existing = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source_last_edited_at,
                documents.c.project,
            ).where(*source_document_identity_clauses(user_id, doc, scope, source_account_id))
        ).first()

        if existing is None:
            doc_id = uuid.uuid4()
            s.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=user_id,
                    title=doc.title,
                    block_json=doc.block_json,
                    markdown=doc.markdown,
                    source=doc.source,
                    source_account_id=source_account_id,
                    source_external_id=doc.source_external_id,
                    source_canonical_id=doc.source_canonical_id,
                    source_db_name=doc.source_db_name,
                    source_last_edited_at=doc.source_last_edited_at,
                    schema_version=SCHEMA_VERSION,
                    scope=scope,
                    project=doc.project,
                )
            )
            _upsert_notion_row(s, doc, user_id, scope)
            _upsert_structured_rows(s, doc_id, doc, user_id, scope, source_account_id)
            # K3 KG outbox — promote approve를 포함한 모든 company 문서 쓰기가
            # 이 함수의 자체 세션에서 commit된다(kg-implementation-spec §5.2 실측).
            enqueue_kg_event(
                s, entity_kind="document", entity_id=doc_id, scope=scope, owner_id=user_id
            )
            s.commit()
            index_document(doc_id, user_id, doc.markdown, scope=scope, project=doc.project)
            if not defer_authoring and doc.wiki_authoring:
                _author_into_wiki(doc_id, user_id, chat_model, scope=scope, project=doc.project)
            return doc_id, True

        doc_id, prev_edited, prev_project = existing
        unchanged = (
            doc.source_last_edited_at is not None
            and prev_edited is not None
            and doc.source_last_edited_at <= prev_edited
            and prev_project == doc.project
        )
        if unchanged:
            return doc_id, False

        s.execute(
            update(documents)
            .where(documents.c.doc_id == doc_id)
            .values(
                title=doc.title,
                block_json=doc.block_json,
                markdown=doc.markdown,
                source_canonical_id=doc.source_canonical_id,
                source_db_name=doc.source_db_name,
                source_last_edited_at=doc.source_last_edited_at,
                project=doc.project,
                updated_at=datetime.now(UTC),
            )
        )
        _upsert_notion_row(s, doc, user_id, scope)
        _upsert_structured_rows(s, doc_id, doc, user_id, scope, source_account_id)
        enqueue_kg_event(s, entity_kind="document", entity_id=doc_id, scope=scope, owner_id=user_id)
        s.commit()
        index_document(doc_id, user_id, doc.markdown, scope=scope, project=doc.project)
        if not defer_authoring and doc.wiki_authoring:
            _author_into_wiki(doc_id, user_id, chat_model, scope=scope, project=doc.project)
        return doc_id, True


def source_document_identity_clauses(
    user_id: UUID,
    doc: InternalDocument,
    scope: str,
    source_account_id: UUID | None,
) -> list[ColumnElement[bool]]:
    """Return SQL predicates for the connector document identity.

    Canonical ids are scoped to the node DB. Company data dedupes across importer
    users/accounts; personal data dedupes only within the importing owner. Without
    a canonical id, preserve account-scoped idempotency.
    """
    if doc.source_canonical_id:
        clauses: list[ColumnElement[bool]] = [
            documents.c.source == doc.source,
            documents.c.scope == scope,
            documents.c.source_canonical_id == doc.source_canonical_id,
        ]
        if scope == "personal":
            clauses.append(documents.c.user_id == user_id)
        return clauses
    return [
        documents.c.source == doc.source,
        documents.c.source_account_id.is_(None)
        if source_account_id is None
        else documents.c.source_account_id == source_account_id,
        documents.c.source_external_id == doc.source_external_id,
    ]


def _upsert_notion_row(s, doc: InternalDocument, user_id: UUID, scope: str) -> None:
    """Persist a structured DB row to `notion_rows` (single indexing path: editor
    saves carry no `structured`, so only Notion DB rows land here). Idempotent on
    `row_id` — re-import updates properties/updated_at (P2.2a)."""
    if not doc.structured:
        return
    st = doc.structured
    owner_id = user_id if scope == "personal" else None
    stmt = pg_insert(notion_rows).values(
        row_id=_row_uuid(st["row_id"]),
        db_id=st["db_id"],
        db_name=st["db_name"],
        properties=st.get("properties", {}),
        scope=scope,
        project=doc.project,
        owner_id=owner_id,
        user_id=user_id,
    )
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=[notion_rows.c.row_id],
            set_={
                "db_id": stmt.excluded.db_id,
                "db_name": stmt.excluded.db_name,
                "properties": stmt.excluded.properties,
                "scope": stmt.excluded.scope,
                "project": stmt.excluded.project,
                "owner_id": stmt.excluded.owner_id,
                "user_id": stmt.excluded.user_id,
                "updated_at": datetime.now(UTC),
            },
        )
    )


def _upsert_structured_rows(
    s,
    doc_id: UUID,
    doc: InternalDocument,
    user_id: UUID,
    scope: str,
    source_account_id: UUID | None,
) -> None:
    s.execute(delete(structured_rows).where(structured_rows.c.source_doc_id == doc_id))
    if not doc.structured_rows:
        return
    owner_id = user_id if scope == "personal" else None
    now = datetime.now(UTC)
    for raw in doc.structured_rows:
        if not isinstance(raw, dict):
            continue
        record_type = str(raw.get("record_type") or "").strip()
        record_key = str(raw.get("record_key") or raw.get("key") or "").strip()
        if not record_type or not record_key:
            continue
        source = str(raw.get("source") or doc.source).strip() or doc.source
        row_id = raw.get("row_id")
        try:
            row_uuid = uuid.UUID(str(row_id)) if row_id else None
        except (TypeError, ValueError):
            row_uuid = None
        if row_uuid is None:
            row_uuid = structured_row_uuid(source, record_type, doc_id, record_key)
        stmt = pg_insert(structured_rows).values(
            row_id=row_uuid,
            source=source,
            record_type=record_type,
            source_doc_id=doc_id,
            source_external_id=doc.source_external_id,
            source_account_id=source_account_id,
            record_key=record_key,
            properties=raw.get("properties") or {},
            evidence=raw.get("evidence"),
            confidence=str(raw.get("confidence") or "medium"),
            scope=scope,
            project=doc.project,
            owner_id=owner_id,
            user_id=user_id,
        )
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[structured_rows.c.row_id],
                set_={
                    "source_external_id": stmt.excluded.source_external_id,
                    "source_account_id": stmt.excluded.source_account_id,
                    "properties": stmt.excluded.properties,
                    "evidence": stmt.excluded.evidence,
                    "confidence": stmt.excluded.confidence,
                    "scope": stmt.excluded.scope,
                    "project": stmt.excluded.project,
                    "owner_id": stmt.excluded.owner_id,
                    "user_id": stmt.excluded.user_id,
                    "updated_at": now,
                },
            )
        )


def _author_into_wiki(
    doc_id: UUID,
    user_id: UUID,
    chat_model: ChatModel | None,
    *,
    scope: str = "company",
    project: str = "atlas",
) -> None:
    """T1 trigger: distill + consolidate the freshly-indexed doc into the wiki.
    Imported lazily so `orthus.documents` stays free of the orthus.wiki import chain.

    For personal scope the wiki pages are owned by `user_id`; company pages have
    no owner (owner_id=None). `project` tags the authored pages (P2)."""
    from orthus.wiki import author_from_document

    owner_id = user_id if scope == "personal" else None
    author_from_document(
        doc_id, user_id, chat_model=chat_model, scope=scope, owner_id=owner_id, project=project
    )
