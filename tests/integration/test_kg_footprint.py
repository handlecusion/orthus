"""K7.3 — owner_footprint owner-격리 + 엔드포인트 통합 (neo4j-test + orthus_test PG).

footprint 술어가 by-construction 옳은지 실 그래프에서 실증한다: 한 owner의 footprint는
**자기 personal 노드/엣지만** 센다(타 owner 누출 0), null/비-owner는 0, 응답에 owner_id
부재 + 캐시 금지. `make kg-test-up && make test`(:7688)에서 실행.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.db import session
from orthus.kg.client import run_read
from orthus.kg.gate import owner_footprint
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import WikiPage
from orthus.settings import get_settings
from orthus.tables import users
from orthus.wiki import store as wiki_store

client = TestClient(app)


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


def _page(author, slug, *, scope, owner_id, backlinks=()):
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition="정의",
            overview="개요",
            backlinks=list(backlinks),
        ),
        user_id=author,
        scope=scope,
        owner_id=owner_id,
    )


@pytest.fixture
def footprint_graph(kg_env, user_id, monkeypatch):
    """company page-x/page-y + owner_a 개인 노트 2개(→page-x,page-y) + owner_b 개인 노트
    1개(→page-x). 비대칭(A=2, B=1)이라 누출이 생기면 카운트가 어긋난다. 양 flag on."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()
    # owner_b도 personal 데이터를 쓰므로 users에 실재해야 한다(embeddings FK).
    with session() as s:
        s.execute(insert(users).values(user_id=owner_b, display_name="Owner B"))
        s.commit()
    _page(owner_a, "page-x", scope="company", owner_id=None)
    _page(owner_a, "page-y", scope="company", owner_id=None)
    _page(owner_a, "a-note-1", scope="personal", owner_id=owner_a, backlinks=["page-x"])
    _page(owner_a, "a-note-2", scope="personal", owner_id=owner_a, backlinks=["page-y"])
    _page(owner_b, "b-note-1", scope="personal", owner_id=owner_b, backlinks=["page-x"])
    run_rebuild()
    return {"owner_a": owner_a, "owner_b": owner_b}


def _global_personal_node_count() -> int:
    recs = run_read("MATCH (n) WHERE n.scope = 'personal' RETURN count(n) AS c", {})
    return int(list(recs[0].values())[0]) if recs else 0


def test_k7_owner_footprint_counts_only_own(footprint_graph):
    g = footprint_graph
    fp_a = owner_footprint(g["owner_a"])
    fp_b = owner_footprint(g["owner_b"])
    # 각 owner는 자기 personal 노드를 본다(>0), 비대칭 보존(A가 B보다 많다).
    assert fp_a.node_count > 0 and fp_b.node_count > 0
    assert fp_a.node_count > fp_b.node_count
    # **누출 0의 강한 증명**: 전 owner footprint 합 == 전역 personal 노드 수(분할, 중복/오집계 없음).
    assert fp_a.node_count + fp_b.node_count == _global_personal_node_count()
    # personal backlink은 cross-scope 엣지(personal 끝점 owner) — 양 owner 모두 엣지>0.
    assert fp_a.edge_count > 0 and fp_b.edge_count > 0
    assert "WikiPage" in fp_a.by_label


def test_k7_owner_footprint_non_owner_zero(footprint_graph):
    """personal 데이터가 전혀 없는 caller는 빈 footprint(타인 personal 미집계)."""
    fp = owner_footprint(uuid.uuid4())
    assert fp.node_count == 0 and fp.edge_count == 0 and fp.by_label == {}


def test_k7_footprint_endpoint_session_caller_only(footprint_graph):
    """엔드포인트는 session user의 footprint만 — client가 caller/owner_id 쿼리를 끼워도
    무시된다(footprint 엔드포인트에 그런 파라미터가 없다, T1)."""
    g = footprint_graph
    fp_a = owner_footprint(g["owner_a"])
    r = client.get(
        "/wiki/kg/footprint",
        params={
            "caller": str(g["owner_b"]),
            "owner_id": str(g["owner_b"]),
            "user_id": str(g["owner_b"]),
        },
        headers={"X-User-Id": str(g["owner_a"])},
    )
    assert r.status_code == 200
    body = r.json()
    # owner_a의 footprint(주입한 owner_b 파라미터 무시).
    assert body["node_count"] == fp_a.node_count
    # T3/T6 — 응답에 owner_id 흔적 없음.
    assert "owner_id" not in body
    assert set(body) == {"node_count", "edge_count", "by_label", "status"}
    # K7.5 O1 — genuine 측정이므로 ready(0개여도 "측정함").
    assert body["status"] == "ready"
    # T4 — personal 데이터: 캐시 금지.
    assert r.headers.get("cache-control") == "private, no-store"


def test_k7_footprint_endpoint_rebuilding_status(footprint_graph):
    """K7.5 (O1/FE) — full rebuild 진행 중에는 half-projected footprint를 노출하지 않고
    status=rebuilding으로 보류 표시(counts 0). 엔드포인트가 per-request :KgMeta
    rebuild_in_progress를 본다."""
    from orthus.kg import store as kg_store
    from orthus.kg.client import kg_write_session

    g = footprint_graph
    with kg_write_session() as s:
        kg_store.set_meta(s, rebuild_in_progress=True)
    try:
        r = client.get("/wiki/kg/footprint", headers={"X-User-Id": str(g["owner_a"])})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rebuilding"
        assert body["node_count"] == 0 and body["edge_count"] == 0
    finally:
        with kg_write_session() as s:
            kg_store.set_meta(s, rebuild_in_progress=False)


def test_k7_footprint_endpoint_empty_when_owner_scope_off(kg_env, user_id):
    """owner-scope off(기본)면 200 + 빈 footprint(personal 미투영). fail-closed default.

    K7.5 O1 — 측정 자체를 안 한 fail-open empty이므로 status=unavailable(ready 아님)."""
    r = client.get("/wiki/kg/footprint", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    assert r.json() == {
        "node_count": 0,
        "edge_count": 0,
        "by_label": {},
        "status": "unavailable",
    }
