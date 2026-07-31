"""K7.5 (W3) — monitor boundary fail-closed exit + (M-r3) admin boundary-health readout.

owner-scope ON에서 경계 위반/검증불가는 비-0 종료(거짓 안심 금지). readout의 last_verified_at은
스케줄 monitor 실행 타임스탬프(`kg.boundary_health` audit) 기반이고, 기대 주기 내 미실행이면
healthy=stale(dead cadence가 healthy로 위장 불가). neo4j-test(:7688) + orthus_test PG 필요.
"""

from __future__ import annotations

import uuid

import pytest

from orthus.audit.logger import audit
from orthus.kg.client import close_kg_driver, kg_write_session
from orthus.kg.monitor import (
    EXIT_BOUNDARY_VIOLATION,
    EXIT_COULD_NOT_VERIFY,
    boundary_health_readout,
    exit_code,
    kg_monitor_summary,
    record_boundary_health,
)
from orthus.settings import get_settings


@pytest.fixture
def owner_scope_on(clean, kg_clean, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    return kg_clean


def test_k7_monitor_violation_exits_nonzero(owner_scope_on):
    """owner-scope ON + 경계 위반(scope='personal' AND owner_id IS NULL) → exit 3."""
    with kg_write_session() as ws:
        ws.run(
            "CREATE (:WikiPage {slug:'leak-x', scope:'personal'})"  # owner_id 누락 = B-shape 위반
        ).consume()
    summary = kg_monitor_summary()
    assert summary.boundary is not None
    assert summary.boundary.violations > 0
    assert exit_code(summary) == EXIT_BOUNDARY_VIOLATION


def test_k7_monitor_counter_failure_exits_nonzero(owner_scope_on):
    """owner-scope ON + Neo4j 미가용 → 경계 검증불가(verify_error) → exit 4(거짓 안심 금지)."""
    s = get_settings()
    good_uri = s.kg_uri
    close_kg_driver()
    monkeypatch_uri = "bolt://127.0.0.1:1"
    s.kg_uri = monkeypatch_uri
    try:
        summary = kg_monitor_summary()
    finally:
        close_kg_driver()
        s.kg_uri = good_uri
    assert summary.boundary is not None
    assert summary.boundary.verify_error is not None
    assert exit_code(summary) == EXIT_COULD_NOT_VERIFY


def _write_health_audit(*, healthy, **counters):
    """스케줄 monitor가 매 실행에 남기는 kg.boundary_health exit 행을 실제 audit 경로로 기록."""
    with audit("kg.boundary_health") as span:
        span.add_meta(healthy=healthy, **counters)


def test_k7_boundary_health_last_verified_from_schedule(clean):
    """(M-r3) readout의 last_verified_at = 스케줄 실행 타임스탬프. 기대 주기 내 미실행이면
    healthy=stale(dead cadence가 healthy로 위장 불가)."""
    # 1) 신선한 healthy 실행 → healthy=True, stale=False.
    _write_health_audit(healthy=True, personal_nodes_without_owner=0)
    fresh = boundary_health_readout(expected_cadence_seconds=3600)
    assert fresh.healthy is True
    assert fresh.stale_cadence is False
    assert fresh.last_verified_at is not None

    # 2) 같은(신선한) 행이라도 expected_cadence를 0으로 주면 age>0>0 → stale → healthy False
    #    (검증된 적 있어도 cadence가 dead면 healthy로 위장 불가).
    stale = boundary_health_readout(expected_cadence_seconds=0.0)
    assert stale.stale_cadence is True
    assert stale.healthy is False


def test_k7_boundary_health_no_schedule_is_stale(clean):
    """스케줄이 한 번도 안 돌았으면(audit 부재) stale + not healthy(검증된 적 없음)."""
    r = boundary_health_readout()
    assert r.stale_cadence is True
    assert r.healthy is False
    assert r.last_verified_at is None


def test_k7_record_boundary_health_roundtrip(owner_scope_on):
    """record_boundary_health → boundary_health_readout 라운드트립(counts-only)."""
    with kg_write_session() as ws:
        ws.run(
            "CREATE (:WikiPage {slug:'co-1', scope:'company', page_id:$p})", p=str(uuid.uuid4())
        ).consume()
    summary = kg_monitor_summary()
    record_boundary_health(summary)
    r = boundary_health_readout(expected_cadence_seconds=3600)
    assert r.last_verified_at is not None
    # counts-only — readout 필드는 카운터 + healthy/stale/last_verified_at만.
    assert set(type(r).model_fields) >= {
        "healthy",
        "stale_cadence",
        "last_verified_at",
        "personal_nodes_without_owner",
    }


def _health_audit_count() -> int:
    from sqlalchemy import func, select

    from orthus.db import session
    from orthus.tables import audit_log

    with session() as s:
        return int(
            s.execute(
                select(func.count())
                .select_from(audit_log)
                .where(audit_log.c.node == "kg.boundary_health", audit_log.c.phase == "exit")
            ).scalar_one()
        )


def test_k7_monitor_records_health_only_when_scheduled(owner_scope_on, monkeypatch):
    """(M-r3 fix) main()은 ORTHUS_KG_MONITOR_SCHEDULED=1일 때만 boundary-health audit를 쓴다 —
    수동 `make node-kg-monitor`가 staleness 시계를 리셋해 죽은 스케줄을 가리는 것을 막는다."""
    from orthus.kg.monitor import main

    before = _health_audit_count()
    # 수동 실행(플래그 없음) — 기록하지 않는다.
    monkeypatch.delenv("ORTHUS_KG_MONITOR_SCHEDULED", raising=False)
    main([])
    assert _health_audit_count() == before

    # 스케줄 실행(플래그 1) — 기록한다.
    monkeypatch.setenv("ORTHUS_KG_MONITOR_SCHEDULED", "1")
    main([])
    assert _health_audit_count() == before + 1
