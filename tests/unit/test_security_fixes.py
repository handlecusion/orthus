"""Unit tests for S2 + S3 security fixes.

S2: PII in nl_question / compiled_sql is redacted before persisting to query_runs.
S3: scope rewrite fail-closed — any exception from _inject_scope_filter rejects
    the query (status="rejected", rejected_reason starts with "scope_rewrite_failed")
    and does NOT fall through to execute_readonly.
"""

from __future__ import annotations

import uuid

from orthus.assistant import pipeline as pipeline_mod
from orthus.structured import query as query_mod
from orthus.structured.query import query_structured


# ---------------------------------------------------------------------------
# S2 — PII at rest: pipeline.insert_run / update_run call redact_pii_text
# ---------------------------------------------------------------------------


def test_insert_run_calls_redact_pii_text_on_question(monkeypatch):
    """insert_run must pass the question through redact_pii_text before INSERT."""
    calls: list[str] = []
    original = pipeline_mod.redact_pii_text

    def spy(s: str) -> str:
        calls.append(s)
        return original(s)

    monkeypatch.setattr(pipeline_mod, "redact_pii_text", spy)

    # Patch session so no real DB needed.
    class _FakeConn:
        def execute(self, *a, **kw):
            pass

        def commit(self):
            pass

    from contextlib import contextmanager

    @contextmanager
    def _fake_session():
        yield _FakeConn()

    monkeypatch.setattr(pipeline_mod, "session", _fake_session)

    pipeline_mod.insert_run(uuid.uuid4(), uuid.uuid4(), None, "call bob@secret.org")

    assert any("bob@secret.org" in c for c in calls), (
        "redact_pii_text was not called with the nl_question"
    )


def test_update_run_calls_redact_pii_text_on_sql(monkeypatch):
    """update_run must pass compiled_sql through redact_pii_text before UPDATE."""
    calls: list[str] = []
    original = pipeline_mod.redact_pii_text

    def spy(s: str) -> str:
        calls.append(s)
        return original(s)

    monkeypatch.setattr(pipeline_mod, "redact_pii_text", spy)

    class _FakeConn:
        def execute(self, *a, **kw):
            pass

        def commit(self):
            pass

    from contextlib import contextmanager

    @contextmanager
    def _fake_session():
        yield _FakeConn()

    monkeypatch.setattr(pipeline_mod, "session", _fake_session)

    raw_sql = "SELECT * FROM t WHERE email = 'alice@corp.com'"
    pipeline_mod.update_run(
        uuid.uuid4(),
        compiled_sql=raw_sql,
        validation={},
        status="executed",
        result_meta=None,
    )

    assert any("alice@corp.com" in c for c in calls), (
        "redact_pii_text was not called with compiled_sql"
    )


# ---------------------------------------------------------------------------
# S3 — scope rewrite fail-closed
# ---------------------------------------------------------------------------


def test_scope_rewrite_failure_rejects_not_executes(monkeypatch):
    """If _inject_scope_filter raises, the query is rejected and execute_readonly
    is never called."""
    uid = uuid.uuid4()

    from orthus.schemas.canonical import CompiledQuery, ValidationResult

    fake_compiled = CompiledQuery(
        question="count rows",
        source_id=None,
        dialect="postgres",
        sql="SELECT count(*) FROM notion_rows",
    )
    fake_validation = ValidationResult(
        parsed=True,
        statement_kind="select",
        schema_ok=True,
        read_only_ok=True,
        explain_ok=True,
        injected_limit=1000,
    )

    monkeypatch.setattr(query_mod, "build_notion_catalog", lambda *a, **kw: {})
    monkeypatch.setattr(query_mod, "insert_run", lambda *a, **kw: None)
    monkeypatch.setattr(query_mod, "compile_query", lambda *a, **kw: fake_compiled)
    monkeypatch.setattr(
        query_mod, "validate", lambda *a, **kw: (fake_validation, fake_compiled.sql)
    )
    monkeypatch.setattr(query_mod, "grounding_for", lambda *a, **kw: [])
    # The model resolver is task-aware now (docs/model-orchestration.md); compile is
    # stubbed above, so the model itself is never used — this just neuters resolution.
    monkeypatch.setattr(query_mod, "get_chat_model_for", lambda task: None)

    update_calls: list[dict] = []

    def spy_update(query_id, *, compiled_sql, validation, status, result_meta):
        update_calls.append({"status": status, "validation": validation})

    monkeypatch.setattr(query_mod, "update_run", spy_update)

    execute_calls: list = []
    monkeypatch.setattr(query_mod, "execute_readonly", lambda *a, **kw: execute_calls.append(1))

    monkeypatch.setattr(
        query_mod,
        "_inject_scope_filter",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("synthetic rewrite failure")),
    )

    result = query_structured(uid, "count rows")

    assert result.status == "rejected"
    assert result.validation.rejected_reason is not None
    assert result.validation.rejected_reason.startswith("scope_rewrite_failed:")
    assert execute_calls == []
    assert len(update_calls) == 1
    assert update_calls[0]["status"] == "rejected"
    assert update_calls[0]["validation"]["rejected_reason"].startswith("scope_rewrite_failed:")
