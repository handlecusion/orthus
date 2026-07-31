"""NL → SQL compilation (prompt §6.2). The LLM gets the catalog (tables/columns)
and the retrieved wiki chunks as grounding, and must emit a single read-only
SELECT as JSON `{"sql": "..."}` for the source's dialect."""

from __future__ import annotations

import json
from uuid import UUID

from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.schemas.canonical import ChunkHit, CompiledQuery

_SYSTEM = (
    "You are a careful SQL compiler. Translate the user's question into a SINGLE "
    "read-only {dialect} SELECT statement. Use ONLY the tables and columns listed "
    "in the schema. Never write INSERT/UPDATE/DELETE/DDL or multiple statements. "
    "When a column is JSONB (e.g. `properties`), read individual keys with the "
    "Postgres JSONB text operator: `properties->>'<key>'`. The keys you may use "
    "are listed in the table description. Filter rows with WHERE on real columns "
    "such as `db_name`, and aggregate over JSONB keys, e.g. "
    "SELECT properties->>'상태' AS status, count(*) FROM notion_rows "
    "WHERE db_name = '티켓' GROUP BY 1. "
    'Respond with JSON only: {{"sql": "<the SELECT>"}}.'
)


def _render_catalog(catalog: dict) -> str:
    lines: list[str] = []
    for name, info in catalog.get("tables", {}).items():
        cols = ", ".join(f"{c} {t}" for c, t in info.get("columns", {}).items())
        desc = info.get("description")
        suffix = f"  -- {desc}" if desc else ""
        lines.append(f"{name}({cols}){suffix}")
    return "\n".join(lines) if lines else "(no tables)"


def _render_grounding(wiki_hits: list[ChunkHit]) -> str:
    if not wiki_hits:
        return "(no wiki grounding)"
    return "\n".join(f"- {h.content}" for h in wiki_hits)


def compile_query(
    question: str,
    source_id: UUID,
    dialect: str,
    catalog: dict,
    wiki_hits: list[ChunkHit],
    chat_model: ChatModel,
) -> CompiledQuery:
    with audit("assistant.compile") as span:
        system = _SYSTEM.format(dialect=dialect)
        user = (
            f"Question: {question}\n\n"
            f"Schema:\n{_render_catalog(catalog)}\n\n"
            f"Business/wiki grounding:\n{_render_grounding(wiki_hits)}"
        )
        raw = chat_model.complete(system, user, json_only=True)
        try:
            sql = json.loads(raw)["sql"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ValueError(f"compile_output_invalid: {e}") from e
        span.add_meta(dialect=dialect, n_wiki=len(wiki_hits))
        span.set_output({"sql": sql})
        return CompiledQuery(question=question, source_id=source_id, dialect=dialect, sql=sql)
