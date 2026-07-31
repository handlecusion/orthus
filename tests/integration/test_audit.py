import uuid

from sqlalchemy import select

from orthus.audit import audit, current_correlation_id
from orthus.db import session
from orthus.tables import audit_log


def _rows(correlation_id):
    with session() as s:
        return s.execute(
            select(audit_log.c.node, audit_log.c.phase, audit_log.c.output)
            .where(audit_log.c.correlation_id == correlation_id)
            .order_by(audit_log.c.audit_id)
        ).all()


def test_audit_enter_exit_pair_and_output_redacted(clean):
    cid = uuid.uuid4()
    with audit("test.node", correlation_id=cid) as span:
        assert current_correlation_id() == cid
        span.set_output({"email": "bob@x.com"})
    rows = _rows(cid)
    phases = [r.phase for r in rows]
    assert phases == ["enter", "exit"]
    exit_output = rows[1].output
    assert exit_output["email"].endswith("@x.com")
    assert "bob" not in exit_output["email"]


def test_audit_records_error_then_reraises(clean):
    cid = uuid.uuid4()
    try:
        with audit("test.boom", correlation_id=cid):
            raise ValueError("kaboom")
    except ValueError:
        pass
    rows = _rows(cid)
    phases = [r.phase for r in rows]
    assert phases == ["enter", "error"]


def test_nested_span_inherits_correlation_id(clean):
    cid = uuid.uuid4()
    with audit("outer", correlation_id=cid):
        with audit("inner") as inner:
            assert inner.correlation_id == cid
    rows = _rows(cid)
    assert {r.node for r in rows} == {"outer", "inner"}
