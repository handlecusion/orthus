"""M7: full-path + audit-span coverage across the whole product. The 비서 path is
now the structured(PG) backend (NL→SQL over `notion_rows`, P2.2a)."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import insert, select

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.models.adapters.mock import MockChat
from orthus.structured.query import query_structured
from orthus.tables import audit_log, notion_rows
from orthus.wiki import ask

# Scripted distill JSON so the editor save authors a wiki page with "연차/15일".
_DISTILL_JSON = json.dumps(
    {
        "summary": "연차 휴가는 입사 1년차부터 15일 부여.",
        "key_concepts": ["연차 휴가"],
        "terminology": ["연차"],
        "open_questions": [],
        "claims": [
            {
                "claim": "연차 휴가는 입사 1년차부터 15일이 부여된다.",
                "evidence": "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
                "confidence": "high",
                "page": {"slug": "연차-휴가", "title": "연차 휴가"},
                "conflicting": [],
            }
        ],
    }
)


def _seed_tickets(user_id) -> None:
    with session() as s:
        for status in ["진행중", "완료"]:
            s.execute(
                insert(notion_rows).values(
                    row_id=uuid.uuid4(),
                    db_id="db-tickets",
                    db_name="티켓",
                    properties={"상태": status},
                    scope="company",
                    owner_id=None,
                    user_id=user_id,
                )
            )
        s.commit()


def _nodes_for(correlation_id) -> dict[str, set[str]]:
    """node -> set of phases recorded under this correlation_id."""
    with session() as s:
        rows = s.execute(
            select(audit_log.c.node, audit_log.c.phase).where(
                audit_log.c.correlation_id == correlation_id
            )
        ).all()
    out: dict[str, set[str]] = {}
    for node, phase in rows:
        out.setdefault(node, set()).add(phase)
    return out


def _nodes_seen() -> set[str]:
    """All audit node names recorded so far (audit_log is cleaned per test)."""
    with session() as s:
        rows = s.execute(select(audit_log.c.node).distinct()).all()
    return {r[0] for r in rows}


def test_full_path_editor_save_to_wiki(user_id):
    # Save authors the doc into the wiki (T1: distill -> consolidate); ask then
    # grounds on the authored wiki page, not raw corpus chunks.
    save_editor_document(
        user_id,
        "휴가",
        [{}],
        "연차 휴가는 입사 1년차부터 15일이 부여됩니다.",
        chat_model=MockChat(default=_DISTILL_JSON),
    )
    out = ask(user_id, "연차 며칠?", k=3, chat_model=MockChat(default="15일"))
    assert out.sources and out.sources[0].page_slug
    assert "연차" in out.sources[0].excerpt or "15일" in out.sources[0].excerpt

    # The wiki-grounding path emits author/distill spans on save and
    # retrieve/answer spans on ask (audit_log is cleaned per test).
    seen = _nodes_seen()
    for node in ("wiki.author", "wiki.distill", "wiki.retrieve", "wiki.answer"):
        assert node in seen, f"missing audit span: {node}"


def test_assistant_executed_emits_full_audit_trace(user_id):
    _seed_tickets(user_id)
    sql = "SELECT count(*) AS n FROM notion_rows WHERE db_name = '티켓'"
    chat = MockChat(rules=[("티켓", '{"sql": "%s"}' % sql)])
    res = query_structured(user_id, "티켓 몇 개", chat_model=chat)
    assert res.status == "executed"

    # All pipeline steps share the run's correlation_id (= query_id), §4.3.
    nodes = _nodes_for(res.query_id)
    for node in ("assistant.run", "assistant.compile", "assistant.validate", "assistant.execute"):
        assert node in nodes, f"missing audit span: {node}"
        assert "enter" in nodes[node] and "exit" in nodes[node], f"{node} not enter+exit"


def test_assistant_rejected_has_no_execute_span(user_id):
    _seed_tickets(user_id)
    chat = MockChat(rules=[("티켓", '{"sql": "DELETE FROM notion_rows"}')])
    res = query_structured(user_id, "티켓 삭제", chat_model=chat)
    assert res.status == "rejected"
    assert res.validation.rejected_reason

    nodes = _nodes_for(res.query_id)
    assert "assistant.compile" in nodes
    assert "assistant.validate" in nodes
    assert "assistant.execute" not in nodes  # gate blocked execution


def test_explain_failure_is_rejected(pg):
    """5th reject class (spec §10 M6): a SQL that clears parse/statement/denylist/
    schema but cannot be EXPLAINed by the real DB must be rejected, never run.
    Exercised at the gate directly with a catalog that claims a table absent from
    the live DB (in the full pipeline, live introspection would strip it earlier)."""
    from orthus.assistant.validate import validate

    catalog = {"tables": {"ghost_table": {"columns": {"x": "integer"}}}}
    result, _sql = validate("SELECT x FROM ghost_table", "postgres", catalog, default_limit=1000)
    assert result.schema_ok is True  # catalog satisfied; the failure is EXPLAIN
    assert result.explain_ok is False
    assert result.rejected_reason.startswith("explain_failed")
    assert not result.passed


def test_five_reject_classes_all_blocked(user_id):
    _seed_tickets(user_id)
    bad_sqls = {
        "delete": "DELETE FROM notion_rows",
        "update": "UPDATE notion_rows SET db_name = 'x'",
        "multi": "SELECT db_name FROM notion_rows; DROP TABLE notion_rows",
        "hallucinated_col": "SELECT bogus_col FROM notion_rows",
        "unknown_table": "SELECT * FROM nonexistent_table",
    }
    for label, sql in bad_sqls.items():
        chat = MockChat(rules=[("티켓", '{"sql": "' + sql + '"}')])
        res = query_structured(user_id, f"티켓 {label}", chat_model=chat)
        assert res.status == "rejected", f"{label} should be rejected, got {res.status}"
        nodes = _nodes_for(res.query_id)
        assert "assistant.execute" not in nodes, f"{label} must not execute"
