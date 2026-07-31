"""5-reject regression for the structured(PG) backend (P2.2a, the product's
safety core). The 비서 NL→SQL validation gate is reused as-is, now targeting our
own JSONB row store `notion_rows` instead of an external DSN.

A `MockChat` scripts the compiled SQL so the validation gate, EXPLAIN, and
execution all run against the real `notion_rows` schema. DELETE / UPDATE / multi-
statement / unknown-table / unknown-column are ALL rejected, never executed, and
each attempt is recorded in `query_runs`. This MUST stay green — non-negotiable."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, select

from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.structured.query import query_structured
from orthus.tables import notion_rows, query_runs


def _seed_rows(user_id) -> None:
    with session() as s:
        for i, status in enumerate(["진행중", "완료", "진행중"]):
            s.execute(
                insert(notion_rows).values(
                    row_id=uuid.uuid4(),
                    db_id="db-tickets",
                    db_name="티켓",
                    properties={"상태": status, "담당자": f"u{i}"},
                    scope="company",
                    owner_id=None,
                    user_id=user_id,
                )
            )
        s.commit()


def _run_status(query_id) -> str:
    with session() as s:
        return s.execute(
            select(query_runs.c.status).where(query_runs.c.query_id == query_id)
        ).scalar_one()


def test_happy_jsonb_aggregate_executes(user_id):
    _seed_rows(user_id)
    sql = (
        "SELECT properties->>'상태' AS status, count(*) AS n "
        "FROM notion_rows WHERE db_name = '티켓' GROUP BY 1"
    )
    chat = MockChat(rules=[("count", '{"sql": "%s"}' % sql)])
    result = query_structured(user_id, "티켓 상태별 개수 count", chat_model=chat)

    assert result.status == "executed"
    assert result.validation.passed is True
    assert result.validation.explain_ok is True
    assert result.source_id is None
    assert set(result.columns) == {"status", "n"}
    # 진행중=2, 완료=1
    counts = {row[0]: row[1] for row in result.rows}
    assert counts == {"진행중": 2, "완료": 1}
    assert any(g.kind == "catalog_table" and g.ref == "notion_rows" for g in result.grounding)
    assert _run_status(result.query_id) == "executed"


@pytest.mark.parametrize(
    "bad_sql, expected_reason",
    [
        ("DELETE FROM notion_rows", "non_select_statement"),
        ("UPDATE notion_rows SET db_name = 'x'", "non_select_statement"),
        (
            "SELECT db_name FROM notion_rows; DROP TABLE notion_rows",
            "multiple_statements",
        ),
        ("SELECT bogus_col FROM notion_rows", "unknown_column:bogus_col"),
        ("SELECT db_name FROM nonexistent_table", "unknown_table:nonexistent_table"),
    ],
)
def test_reject_classes_not_executed_and_recorded(user_id, bad_sql, expected_reason):
    _seed_rows(user_id)
    # needle "x" matches every question below; script the malicious SQL.
    chat = MockChat(rules=[("x", '{"sql": "%s"}' % bad_sql)])
    result = query_structured(user_id, "x", chat_model=chat)

    assert result.status == "rejected"
    assert result.validation.passed is False
    assert result.validation.rejected_reason == expected_reason
    assert result.rows == []
    assert result.row_count is None
    # recorded with status rejected, never executed.
    assert _run_status(result.query_id) == "rejected"
    with session() as s:
        row = s.execute(
            select(
                query_runs.c.status,
                query_runs.c.source_id,
                query_runs.c.validation,
            ).where(query_runs.c.query_id == result.query_id)
        ).one()
    assert row.status == "rejected"
    assert row.source_id is None  # structured runs carry no external source_id
    assert row.validation["rejected_reason"] == expected_reason
