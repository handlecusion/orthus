"""Shared 비서 run-record + grounding helpers (prompt §6).

Every attempt — compiled, rejected, executed, or failed — is recorded as one
`query_runs` row. The validation gate stands between compile and execute; a
rejected query is never run. The structured(PG) backend
(`orthus.structured.query`) reuses these helpers so both record runs identically.
"""

from __future__ import annotations

from uuid import UUID

import sqlglot
from sqlalchemy import insert, update
from sqlglot import exp

from orthus.audit.redact import redact_pii_text
from orthus.db import session
from orthus.schemas.canonical import SCHEMA_VERSION, GroundingRef
from orthus.tables import query_runs


def insert_run(query_id: UUID, user_id: UUID, source_id: UUID | None, question: str) -> None:
    with session() as s:
        s.execute(
            insert(query_runs).values(
                query_id=query_id,
                user_id=user_id,
                source_id=source_id,
                nl_question=redact_pii_text(question),
                validation={},
                status="compiled",
                schema_version=SCHEMA_VERSION,
            )
        )
        s.commit()


def update_run(
    query_id: UUID,
    *,
    compiled_sql: str | None,
    validation: dict,
    status: str,
    result_meta: dict | None,
) -> None:
    with session() as s:
        s.execute(
            update(query_runs)
            .where(query_runs.c.query_id == query_id)
            .values(
                compiled_sql=redact_pii_text(compiled_sql) if compiled_sql is not None else None,
                validation=validation,
                status=status,
                result_meta=result_meta,
            )
        )
        s.commit()


def grounding_for(wiki_hits, final_sql: str | None, dialect: str) -> list[GroundingRef]:
    refs = [GroundingRef(kind="wiki_chunk", ref=str(h.chunk_id)) for h in wiki_hits]
    if final_sql:
        try:
            root = sqlglot.parse_one(final_sql, read=dialect)
            seen: set[str] = set()
            for tbl in root.find_all(exp.Table):
                if tbl.name and tbl.name not in seen:
                    seen.add(tbl.name)
                    refs.append(GroundingRef(kind="catalog_table", ref=tbl.name))
        except Exception:  # noqa: BLE001 — grounding is best-effort
            pass
    return refs
