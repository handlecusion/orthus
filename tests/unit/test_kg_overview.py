"""전용 지식그래프 화면 — company_kg_overview 단위 경계 테스트 (Neo4j 불필요).

overview도 footprint와 동형의 집계(scalar count)라 `_map_records` tripwire 밖이다 — 술어가
by-construction 옳아야 한다. 이 테스트들이 그걸 고정한다: company-only 술어 + `$caller`
미바인딩(타 owner personal 미집계), flag-off short-circuit(disabled), fail-open unavailable,
응답에 식별자 부재.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.kg import gate
from orthus.kg.gate import (
    _OVERVIEW_CONFLICTS_CYPHER,
    _OVERVIEW_EDGES_CYPHER,
    _OVERVIEW_NODES_CYPHER,
    company_kg_overview,
    invalidate_overview_cache,
)
from orthus.schemas.canonical import KgOverview

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overview_cache():
    """집계는 60s TTL 캐시라 테스트 간 결과가 샌다 — 각 테스트가 자기 monkeypatch 조건을
    실제로 측정하도록 앞뒤로 리셋한다(캐시 도입 전 동작과 동일하게)."""
    invalidate_overview_cache()
    yield
    invalidate_overview_cache()


@contextmanager
def _noop_audit(*_a, **_k):
    """audit() 대역 — 진짜 audit은 enter에서 DB에 span row를 쓰므로 CI fast 레인
    (DB 없음, unreachable DSN fail-closed)에서 예외가 나고 blanket except가 `unavailable`로
    삼킨다. ready 경로를 검증하는 순수 단위 테스트는 audit을 격리해야 한다."""
    yield SimpleNamespace(add_meta=lambda **_kw: None, correlation_id=uuid4())


def test_overview_predicate_is_company_only():
    """집계 Cypher는 company-only 술어(`scope='company'`)만 담고 `$caller`/personal을 절대
    바인딩하지 않는다 — 타 owner personal 노드/엣지가 회사 집계에 섞이면 안 된다."""
    for cypher in (_OVERVIEW_NODES_CYPHER, _OVERVIEW_EDGES_CYPHER, _OVERVIEW_CONFLICTS_CYPHER):
        assert "scope = 'company'" in cypher
        # caller-bound/personal 술어가 섞이면 company 집계 의미가 깨진다.
        assert "$caller" not in cypher
        assert "'personal'" not in cypher
        assert "owner_id" not in cypher


def test_overview_disabled_when_kg_off(monkeypatch):
    """flag-off는 Neo4j를 건드리지 않고 status=disabled(측정 안 함)."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: False)

    def _boom(*a, **k):  # Neo4j를 절대 호출하면 안 된다
        raise AssertionError("run_read must not be called when kg disabled")

    monkeypatch.setattr(gate, "run_read", _boom)
    ov = company_kg_overview()
    assert ov.status == "disabled"
    assert ov == KgOverview(status="disabled")


def test_overview_unavailable_when_kg_unavailable(monkeypatch):
    """flag on이지만 Neo4j 미가용이면 status=unavailable(빈 집계, 예외 미전파).

    audit도 격리한다 — 격리 없으면 DB-less 레인에서 audit enter가 먼저 터져 이 테스트가
    '의도한 kg_available 분기'가 아니라 audit 실패로 공허하게 통과한다."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: False)
    monkeypatch.setattr(gate, "audit", _noop_audit)
    monkeypatch.setattr(
        gate, "run_read", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no query"))
    )
    assert company_kg_overview() == KgOverview()  # default status="unavailable"


def test_overview_fail_open_on_driver_error(monkeypatch):
    """fail-open — Neo4j read 예외는 endpoint 500이 아니라 빈 unavailable로 수렴."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: True)
    monkeypatch.setattr(gate, "audit", _noop_audit)

    def _boom(*a, **k):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(gate, "run_read", _boom)
    ov = company_kg_overview()
    assert ov.status == "unavailable" and ov.node_count == 0


def test_overview_ready_aggregates_records(monkeypatch):
    """genuine 측정 성공은 status=ready — 노드 라벨/엣지/모순 record를 집계한다.

    run_read를 stub해 3개 쿼리(노드-by-label / 엣지-count / 모순-by-status)를 순서대로
    돌려준다. entity_count는 by_label['Entity'] 파생, 모순은 RESOLVED/UNRESOLVED로 분리."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: True)
    monkeypatch.setattr(gate, "audit", _noop_audit)  # DB-less fast 레인 격리

    class _Rec:
        def __init__(self, *vals):
            self._vals = list(vals)

        def values(self):
            return self._vals

    calls: list[str] = []

    def _fake_run_read(cypher, params):
        calls.append(cypher)
        if cypher is _OVERVIEW_NODES_CYPHER:
            return [_Rec("WikiPage", 5), _Rec("WikiClaim", 3), _Rec("Entity", 2)]
        if cypher is _OVERVIEW_EDGES_CYPHER:
            return [_Rec(12)]
        if cypher is _OVERVIEW_CONFLICTS_CYPHER:
            return [_Rec("UNRESOLVED", 4), _Rec("RESOLVED", 1)]
        raise AssertionError(f"unexpected cypher: {cypher}")

    monkeypatch.setattr(gate, "run_read", _fake_run_read)
    # kg_query_runs 기록은 best-effort — 단위 테스트에선 DB 없이 no-op으로 대체.
    monkeypatch.setattr(gate, "_record_overview_run_best_effort", lambda **k: None)

    ov = company_kg_overview()
    assert ov.status == "ready"
    assert ov.node_count == 10  # 5+3+2
    assert ov.by_label == {"WikiPage": 5, "WikiClaim": 3, "Entity": 2}
    assert ov.entity_count == 2
    assert ov.edge_count == 12
    assert ov.unresolved_conflicts == 4
    assert ov.resolved_conflicts == 1
    assert len(calls) == 3


def test_overview_status_field_has_disabled_state():
    """KgOverview는 footprint 3-state에 flag-off 배너용 disabled를 더한 4-state다."""
    schema = KgOverview.model_json_schema()
    literal = schema["properties"]["status"]["enum"]
    assert set(literal) == {"ready", "unavailable", "rebuilding", "disabled"}
    # 응답은 count류만 — slug/이름/owner_id 같은 식별자 필드가 없다.
    assert set(KgOverview.model_fields) == {
        "node_count",
        "edge_count",
        "by_label",
        "entity_count",
        "unresolved_conflicts",
        "resolved_conflicts",
        "status",
    }


def test_overview_endpoint_rebuilding_status(monkeypatch):
    """엔드포인트는 full rebuild 중 status=rebuilding으로 선판정(half-projected 보류) +
    Cache-Control private no-store. 인증은 footprint 등과 공유하는 dual-auth dependency."""
    import orthus.api.routes.kg as kg_route

    monkeypatch.setattr(kg_route, "kg_rebuild_in_progress", lambda: True)
    # 인증 통과용 X-User-Id(demo 모드) — rebuild 선판정이라 company_kg_overview는 미호출.
    r = client.get("/kg/overview", headers={"X-User-Id": "00000000-0000-0000-0000-000000000001"})
    assert r.status_code == 200
    assert r.json()["status"] == "rebuilding"
    assert r.headers.get("cache-control") == "private, no-store"


# --- TTL 캐시 (전체 스캔 비용 완화) ----------------------------------------------------


def _stub_run_read(monkeypatch, calls: list[str]):
    """3개 집계 쿼리를 stub하고 호출 Cypher를 기록한다."""

    class _Rec:
        def __init__(self, *vals):
            self._vals = list(vals)

        def values(self):
            return self._vals

    def _fake(cypher, params):
        calls.append(cypher)
        if cypher is _OVERVIEW_NODES_CYPHER:
            return [_Rec("WikiPage", 5)]
        if cypher is _OVERVIEW_EDGES_CYPHER:
            return [_Rec(9)]
        return []

    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: True)
    monkeypatch.setattr(gate, "run_read", _fake)
    monkeypatch.setattr(gate, "_record_overview_run_best_effort", lambda **k: None)
    monkeypatch.setattr(gate, "audit", _noop_audit)  # DB-less fast 레인 격리


def test_overview_caches_ready_result(monkeypatch):
    """성공 결과는 TTL 동안 캐시 — 2번째 호출은 Neo4j를 다시 스캔하지 않는다(전체 스캔이
    2s 타임아웃에 여유가 적어 빈도를 낮추는 게 캐시의 목적)."""
    calls: list[str] = []
    _stub_run_read(monkeypatch, calls)
    first = company_kg_overview()
    assert first.status == "ready" and first.node_count == 5
    n_after_first = len(calls)
    assert n_after_first == 3  # nodes/edges/conflicts
    second = company_kg_overview()
    assert second == first
    assert len(calls) == n_after_first  # 추가 왕복 0 — 캐시 히트


def test_overview_does_not_cache_failure(monkeypatch):
    """실패(unavailable)는 캐시하지 않는다 — Neo4j가 곧 복구돼도 TTL만큼 장애가 연장되면
    안 된다. 다음 호출이 즉시 재시도해 성공하면 ready."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: True)
    monkeypatch.setattr(gate, "kg_available", lambda: False)
    assert company_kg_overview().status == "unavailable"
    # 복구 후 다음 호출은 stale unavailable이 아니라 실측 ready여야 한다.
    calls: list[str] = []
    _stub_run_read(monkeypatch, calls)
    assert company_kg_overview().status == "ready"
    assert len(calls) == 3  # 캐시된 실패를 돌려주지 않고 실제로 재측정했다


def test_overview_cache_expires_after_ttl(monkeypatch):
    """TTL이 지나면 재측정한다(stale 상한 = _OVERVIEW_TTL_S)."""
    calls: list[str] = []
    _stub_run_read(monkeypatch, calls)
    base = 1000.0
    monkeypatch.setattr(gate.time, "monotonic", lambda: base)
    company_kg_overview()
    assert len(calls) == 3
    # TTL 직전 — 캐시 히트
    monkeypatch.setattr(gate.time, "monotonic", lambda: base + gate._OVERVIEW_TTL_S - 1)
    company_kg_overview()
    assert len(calls) == 3
    # TTL 경과 — 재측정
    monkeypatch.setattr(gate.time, "monotonic", lambda: base + gate._OVERVIEW_TTL_S + 1)
    company_kg_overview()
    assert len(calls) == 6


def test_overview_flag_off_not_cached(monkeypatch):
    """flag-off(disabled)는 캐시 대상이 아니다 — flag를 켜면 즉시 측정으로 넘어간다."""
    monkeypatch.setattr(gate, "kg_enabled", lambda: False)
    assert company_kg_overview().status == "disabled"
    calls: list[str] = []
    _stub_run_read(monkeypatch, calls)
    assert company_kg_overview().status == "ready"


# --- 랜딩 개체 지도 (entity_overview / GET /kg/graph) ---------------------------------


def test_entity_overview_template_is_company_only():
    """랜딩 개체 지도 Cypher는 company-only(RELATES_TO co-mention)이고 caller/personal을
    바인딩하지 않는다 + 상한은 별도 이름 `$overview_limit`(gate의 `$limit`과 충돌 금지)."""
    from orthus.kg.templates import _ENTITY_OVERVIEW

    assert "RELATES_TO" in _ENTITY_OVERVIEW
    assert "$overview_limit" in _ENTITY_OVERVIEW
    assert "$caller" not in _ENTITY_OVERVIEW
    assert "'personal'" not in _ENTITY_OVERVIEW
    assert "owner_id" not in _ENTITY_OVERVIEW


def test_entity_overview_registered_company_always():
    """entity_overview는 company_always(owner-scope 무관)이고 상한 setting_param을 가진다.
    입력 파라미터(slug/이름) 없음 — 무입력 company 전체 지도."""
    from orthus.kg.templates import TEMPLATES

    tpl = TEMPLATES["entity_overview"]
    assert tpl.predicate_kind == "company_always"
    assert tpl.bind_params == ()
    assert tpl.slug_resolution == ()
    assert ("overview_limit", "kg_overview_limit") in tpl.setting_params


def test_entity_overview_in_static_write_scan():
    """정적 write-keyword 검사 전수(all_cypher_variants)에 개체 지도 Cypher가 포함된다."""
    from orthus.kg.templates import _ENTITY_OVERVIEW, all_cypher_variants

    assert _ENTITY_OVERVIEW in all_cypher_variants()


def test_entity_graph_endpoint_disabled(monkeypatch):
    """flag-off는 GET /kg/graph를 200 supported:false reason=kg_disabled로(500 아님).
    Cache-Control private no-store."""
    import orthus.api.routes.kg as kg_route

    monkeypatch.setattr(kg_route.get_settings(), "kg_enabled", False, raising=False)
    r = client.get("/kg/graph", headers={"X-User-Id": "00000000-0000-0000-0000-000000000001"})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False and body["reason"] == "kg_disabled"
    assert r.headers.get("cache-control") == "private, no-store"


def test_entity_graph_endpoint_rebuilding(monkeypatch):
    """rebuild 중은 supported:false reason=kg_unavailable(half-projected 보류)."""
    import orthus.api.routes.kg as kg_route

    monkeypatch.setattr(kg_route.get_settings(), "kg_enabled", True, raising=False)
    monkeypatch.setattr(kg_route, "kg_rebuild_in_progress", lambda: True)
    r = client.get("/kg/graph", headers={"X-User-Id": "00000000-0000-0000-0000-000000000001"})
    assert r.status_code == 200
    assert r.json()["supported"] is False and r.json()["reason"] == "kg_unavailable"
