"""K7.3 — owner_footprint 단위 경계 테스트 (Neo4j 불필요).

footprint는 집계(scalar count)라 `_map_records`의 wire tripwire 보호 밖이다 — 술어가
by-construction 옳아야 하고(kg-k7.3-plan §5/T1·T2), 이 테스트들이 그걸 고정한다:
own-personal-only 술어 존재, null/flag-off short-circuit, 응답에 owner_id 부재.
"""

from __future__ import annotations

import uuid

from orthus.kg import gate
from orthus.kg.gate import (
    _FOOTPRINT_EDGES_CYPHER,
    _FOOTPRINT_NODES_CYPHER,
    owner_footprint,
)
from orthus.schemas.canonical import OwnerFootprint


def test_k7_owner_footprint_predicate_present():
    """node/edge 카운트 Cypher가 own-personal-only 술어(`scope='personal' AND
    owner_id=$caller`)를 담는다 — read 술어(`company OR ...`)가 **아니다**(T2)."""
    for cypher in (_FOOTPRINT_NODES_CYPHER, _FOOTPRINT_EDGES_CYPHER):
        assert "scope = 'personal'" in cypher
        assert "owner_id = $caller" in cypher
        # read 술어(company OR owner)가 섞이면 footprint가 company까지 세어 의미가 깨진다.
        assert "scope = 'company'" not in cypher
        # 값은 driver 바인딩($caller)으로만 — f-string 보간 금지(게이트 불변식).
        assert "$caller" in cypher


def test_k7_owner_footprint_null_caller_zero(monkeypatch):
    """B-null-caller — caller=None은 Neo4j를 건드리지 않고 빈 footprint. owner-scope가
    켜져 있어도 마찬가지(company에는 owner footprint가 없다)."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_owner_scope_enabled", lambda: True)

    def _boom(*a, **k):  # Neo4j를 절대 호출하면 안 된다
        raise AssertionError("run_read must not be called for null caller")

    monkeypatch.setattr(gate, "run_read", _boom)
    fp = owner_footprint(None)
    assert fp == OwnerFootprint()
    assert fp.node_count == 0 and fp.edge_count == 0 and fp.by_label == {}


def test_k7_owner_footprint_empty_when_owner_scope_off(monkeypatch):
    """owner-scope OFF면 personal 노드가 애초에 투영되지 않으므로 빈 footprint —
    Neo4j 왕복도 하지 않는다(short-circuit)."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_owner_scope_enabled", lambda: False)
    monkeypatch.setattr(
        gate, "run_read", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no query"))
    )
    assert owner_footprint(uuid.uuid4()) == OwnerFootprint()


def test_k7_owner_footprint_empty_when_kg_disabled(monkeypatch):
    monkeypatch.setattr(gate, "kg_enabled", lambda: False)
    monkeypatch.setattr(gate, "kg_owner_scope_enabled", lambda: True)
    assert owner_footprint(uuid.uuid4()) == OwnerFootprint()


def test_k7_footprint_response_has_no_owner_id():
    """T3/T6 — OwnerFootprint는 owner_id를 절대 노출하지 않는다(카운트만 + status)."""
    assert "owner_id" not in OwnerFootprint.model_fields
    assert set(OwnerFootprint.model_fields) == {
        "node_count",
        "edge_count",
        "by_label",
        "status",  # K7.5 O1 — fail-open empty vs genuine-0 구분
    }


def test_k7_footprint_status_unavailable_not_zero(monkeypatch):
    """K7.5 (O1) — fail-open empty footprint는 status=unavailable이지 ready가 아니다.

    flag-off/null-caller/driver-error/owner-scope-off는 모두 측정 자체를 못 한 것이므로
    `(status=ready, count=0)` 거짓성공으로 렌더되면 안 된다(default가 unavailable)."""
    # null caller
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_owner_scope_enabled", lambda: True)
    assert owner_footprint(None).status == "unavailable"
    # kg disabled
    monkeypatch.setattr(gate, "kg_enabled", lambda: False)
    assert owner_footprint(uuid.uuid4()).status == "unavailable"
    # driver error (fail-open)
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: True)
    monkeypatch.setattr(
        gate, "run_read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    fp = owner_footprint(uuid.uuid4())
    assert fp.status == "unavailable" and fp.node_count == 0


def test_k7_owner_footprint_fail_open_on_driver_error(monkeypatch):
    """fail-open — Neo4j read 예외는 endpoint 500이 아니라 빈 footprint로 수렴."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_owner_scope_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(gate, "run_read", _boom)
    assert owner_footprint(uuid.uuid4()) == OwnerFootprint()
