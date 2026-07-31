"""Unit tests for the 비서 validation gate (prompt §6.3). No DB needed for
parse/statement/schema/limit checks — `explain_ok` is monkeypatched here; the
end-to-end EXPLAIN path is exercised in tests/integration/test_assistant.py."""

from __future__ import annotations

import pytest

from orthus.assistant import validate as validate_mod
from orthus.assistant.validate import validate

CATALOG = {
    "tables": {
        "employees": {
            "columns": {"id": "integer", "name": "text", "salary": "integer"},
            "description": None,
        }
    }
}


@pytest.fixture(autouse=True)
def _stub_explain(monkeypatch):
    """Stub the read-only engine so EXPLAIN succeeds without a real DB."""

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **kw):
            return None

    class _Engine:
        def connect(self):
            return _Conn()

    monkeypatch.setattr(validate_mod, "_ro_engine_for_dsn", lambda dsn: _Engine())
    # resolve_dsn would read env / settings; short-circuit it.
    monkeypatch.setattr(validate_mod, "resolve_dsn", lambda key: "stub-dsn")


def _validate(sql, **kw):
    return validate(sql, "postgres", CATALOG, default_limit=1000, **kw)


def test_happy_select_passes_and_injects_limit():
    result, final_sql = _validate("SELECT name FROM employees WHERE salary > 100")
    assert result.passed is True
    assert result.parsed is True
    assert result.statement_kind == "select"
    assert result.read_only_ok is True
    assert result.schema_ok is True
    assert result.explain_ok is True
    assert result.injected_limit == 1000
    assert "LIMIT 1000" in final_sql.upper()


def test_delete_rejected_non_select():
    result, _ = _validate("DELETE FROM employees")
    assert result.passed is False
    assert result.rejected_reason == "non_select_statement"


def test_update_rejected():
    result, _ = _validate("UPDATE employees SET salary = 0")
    assert result.passed is False
    assert result.rejected_reason == "non_select_statement"


def test_multiple_statements_rejected():
    result, _ = _validate("SELECT name FROM employees; DROP TABLE employees")
    assert result.passed is False
    assert result.rejected_reason == "multiple_statements"


def test_unknown_column_rejected():
    result, _ = _validate("SELECT bogus_col FROM employees")
    assert result.passed is False
    assert result.rejected_reason == "unknown_column:bogus_col"


def test_unknown_table_rejected():
    result, _ = _validate("SELECT * FROM nonexistent")
    assert result.passed is False
    assert result.rejected_reason == "unknown_table:nonexistent"


def test_dangerous_function_rejected():
    # pg_sleep has no FROM; either the column/table check or the function
    # denylist may trip first — either way it MUST be rejected and not pass.
    result, _ = _validate("SELECT pg_sleep(10)")
    assert result.passed is False
    assert result.rejected_reason is not None


def test_explicit_limit_not_overwritten():
    result, final_sql = _validate("SELECT name FROM employees LIMIT 5")
    assert result.passed is True
    assert result.injected_limit is None
    assert "LIMIT 5" in final_sql.upper()


def test_order_by_select_alias_passes():
    result, final_sql = _validate("SELECT name AS 이름 FROM employees ORDER BY 이름")
    assert result.passed is True
    assert result.schema_ok is True
    assert "ORDER BY" in final_sql.upper()


def test_union_select_passes_and_injects_limit():
    result, final_sql = _validate("SELECT name FROM employees UNION ALL SELECT name FROM employees")
    assert result.passed is True
    assert result.statement_kind == "select"
    assert result.schema_ok is True
    assert "UNION ALL" in final_sql.upper()
    assert "LIMIT 1000" in final_sql.upper()


def test_cte_embedded_dml_rejected():
    sql = "WITH x AS (DELETE FROM employees RETURNING id) SELECT * FROM x"
    result, _ = _validate(sql)
    assert result.passed is False
    assert result.rejected_reason == "non_select_statement"


def test_unparseable_rejected():
    result, _ = _validate("SELECT FROM WHERE )(")
    assert result.passed is False
    assert result.rejected_reason == "parse_failed"
