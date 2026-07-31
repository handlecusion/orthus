"""The validation gate (prompt §6.3). Fail-closed: any exception → reject, never
execute. A compiled SQL must clear every check before the 비서 may run it.

Double defense: this application-level gate sits in front of a DB-level read-only
role. Even a bypass here cannot write, but the gate is the primary guard."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from orthus.audit import audit
from orthus.catalog import resolve_dsn
from orthus.db import get_ro_engine
from orthus.schemas.canonical import ValidationResult
from orthus.settings import get_settings
from sqlalchemy import text

# Functions that can read/write the filesystem, sleep, run side effects, or reach
# out of the DB. Case-insensitive match against any function name in the AST.
DANGEROUS_FUNCS: frozenset[str] = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "copy",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "set_config",
        "pg_logical_emit_message",
    }
)

# Any of these node types appearing anywhere in the AST means the statement is not
# a pure read. CTE-embedded DML (e.g. `WITH x AS (DELETE ... RETURNING ...)`) and
# statement-injection that survived parsing are both caught by walking the tree.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Command,
    exp.Set,
    exp.Use,
)


def _referenced_tables(root: exp.Expression) -> list[str]:
    return [t.name for t in root.find_all(exp.Table) if t.name]


def _table_columns(catalog: dict, table: str) -> dict[str, str]:
    return catalog.get("tables", {}).get(table, {}).get("columns", {})


def _check_schema(root: exp.Expression, catalog: dict) -> str | None:
    """Return a rejection reason, or None when all tables/columns resolve."""
    catalog_tables = catalog.get("tables", {})
    select_aliases = {
        expr.alias for expr in root.expressions if isinstance(expr, exp.Expression) and expr.alias
    }

    # Resolve table references. CTE names are local aliases, not real tables.
    cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    referenced: list[str] = []
    for tbl in root.find_all(exp.Table):
        name = tbl.name
        if not name or name in cte_names:
            continue
        referenced.append(name)
        if name not in catalog_tables:
            return f"unknown_table:{name}"

    referenced_cols = set(referenced)
    # Alias -> real table name, so qualified columns resolve to the right table.
    alias_to_table: dict[str, str] = {}
    for tbl in root.find_all(exp.Table):
        if tbl.name and tbl.name not in cte_names:
            alias_to_table[tbl.alias_or_name] = tbl.name

    for col in root.find_all(exp.Column):
        cname = col.name
        if not cname or cname == "*":
            continue
        qualifier = col.table  # alias or table the column is qualified with
        if qualifier:
            real = alias_to_table.get(qualifier)
            if real is None:
                # Qualified by a CTE alias — columns inside CTEs are derived;
                # we cannot validate against the live catalog, so skip.
                if qualifier in cte_names or qualifier in {
                    c.alias_or_name for c in root.find_all(exp.CTE)
                }:
                    continue
                return f"unknown_table:{qualifier}"
            if cname not in _table_columns(catalog, real):
                return f"unknown_column:{cname}"
        else:
            # Postgres permits SELECT output aliases in ORDER BY (and sqlglot
            # represents them as unqualified columns). The aliased expression
            # itself is still validated separately, so this does not permit
            # unknown base columns.
            if cname in select_aliases:
                continue
            # Unqualified: OK if it exists in ANY referenced real table.
            if not any(cname in _table_columns(catalog, t) for t in referenced_cols):
                # No referenced real tables (e.g. SELECT pg_sleep(1)) → can't
                # resolve a bare column either; treat as unknown.
                return f"unknown_column:{cname}"
    return None


def _is_read_query(root: exp.Expression) -> bool:
    return isinstance(root, (exp.Select, exp.SetOperation))


def _ro_engine_for_dsn(dsn: str):
    settings = get_settings()
    if dsn == settings.pg_dsn_readonly:
        return get_ro_engine()
    from sqlalchemy import create_engine

    return create_engine(
        dsn,
        pool_pre_ping=True,
        future=True,
        execution_options={"postgresql_readonly": True},
    )


def validate(
    sql: str,
    dialect: str,
    catalog: dict,
    *,
    default_limit: int,
    dsn_secret_key: str | None = None,
) -> tuple[ValidationResult, str]:
    """Run every gate check on `sql`. Returns the result and the (possibly
    LIMIT-injected) SQL. `rejected_reason` is None only if all checks passed."""
    result = ValidationResult()
    final_sql = sql

    with audit("assistant.validate") as span:
        # 1) parse
        try:
            statements = sqlglot.parse(sql, read=dialect)
        except Exception as e:  # noqa: BLE001 — fail closed
            result.rejected_reason = "parse_failed"
            span.add_meta(parsed=False, error=str(e))
            return result, final_sql
        statements = [s for s in statements if s is not None]
        if not statements:
            result.rejected_reason = "parse_failed"
            span.add_meta(parsed=False)
            return result, final_sql
        result.parsed = True

        # 2) multi-statement (covers `SELECT ...; DROP ...`)
        if len(statements) > 1:
            result.rejected_reason = "multiple_statements"
            span.add_meta(parsed=True, n_statements=len(statements))
            return result, final_sql

        root = statements[0]

        # 3) statement_kind: root must be a read-only query. SQL set operations
        # (`UNION`/`INTERSECT`/`EXCEPT`) are read-only when their children are
        # SELECTs; forbidden DML/DDL nodes are still caught by the AST walk below.
        if not _is_read_query(root):
            result.statement_kind = "other"
            result.rejected_reason = "non_select_statement"
            span.add_meta(parsed=True, statement_kind="other")
            return result, final_sql
        # Walk the whole AST: any DML/DDL node anywhere → reject.
        for node in root.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                result.statement_kind = "other"
                result.rejected_reason = "non_select_statement"
                span.add_meta(parsed=True, forbidden_node=type(node).__name__)
                return result, final_sql
        result.statement_kind = "select"

        # 4) read_only_ok: dangerous function denylist.
        for fn in root.find_all(exp.Func, exp.Anonymous):
            name = (fn.name or "").lower()
            if name in DANGEROUS_FUNCS:
                result.rejected_reason = f"denied_function:{name}"
                span.add_meta(denied_function=name)
                return result, final_sql
        result.read_only_ok = True

        # 5) schema_ok: tables/columns must exist in the catalog.
        schema_reason = _check_schema(root, catalog)
        if schema_reason is not None:
            result.rejected_reason = schema_reason
            span.add_meta(schema_ok=False, reason=schema_reason)
            return result, final_sql
        result.schema_ok = True

        # 6) limit injection.
        if root.args.get("limit") is None:
            root = root.limit(default_limit)
            result.injected_limit = default_limit
        final_sql = root.sql(dialect=dialect)

        # 7) explain_ok: EXPLAIN (no execution) over a read-only connection.
        try:
            dsn = resolve_dsn(dsn_secret_key) if dsn_secret_key else get_settings().pg_dsn_readonly
            engine = _ro_engine_for_dsn(dsn)
            with engine.connect() as conn:
                conn.execute(text(f"EXPLAIN {final_sql}"))
        except Exception as e:  # noqa: BLE001 — fail closed
            msg = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            result.rejected_reason = f"explain_failed:{msg}"
            span.add_meta(explain_ok=False, error=msg)
            return result, final_sql
        result.explain_ok = True

        span.add_meta(
            parsed=True,
            statement_kind=result.statement_kind,
            read_only_ok=result.read_only_ok,
            schema_ok=result.schema_ok,
            explain_ok=result.explain_ok,
            injected_limit=result.injected_limit,
        )
        return result, final_sql
