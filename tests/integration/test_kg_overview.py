"""전용 지식그래프 화면 — company_kg_overview 격리 + 엔드포인트 통합 (neo4j-test + PG).

overview 술어가 by-construction 옳은지 실 그래프에서 실증한다: company 집계는 **company
노드/엣지만** 세고 어떤 owner의 personal도 집계하지 않는다(strong form: overview.node_count +
Σowner_footprint == 전역 노드 수). 엔트리 라벨/모순 분리 + kg_query_runs 기록도 확인한다.
`make kg-test-up && make test`(:7688)에서 실행.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.kg.client import run_read
from orthus.kg.entities import persist_entities
from orthus.kg.gate import company_kg_overview, invalidate_overview_cache, owner_footprint
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import ExtractedEntity, WikiPage
from orthus.settings import get_settings
from orthus.tables import kg_query_runs, users
from orthus.wiki import store as wiki_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overview_cache():
    """집계는 60s TTL 캐시 — 테스트 간 결과가 새지 않게 앞뒤로 리셋(그래프 시드가 테스트마다
    다르므로 stale 값을 보면 카운트 단정이 무의미해진다)."""
    invalidate_overview_cache()
    yield
    invalidate_overview_cache()


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
def overview_graph(kg_env, user_id, monkeypatch):
    """company page-x/page-y(+backlink 엣지) + owner_a 개인 노트 2 + owner_b 개인 노트 1.

    company와 personal이 섞인 그래프라 company 집계가 personal을 새면 카운트가 어긋난다.
    양 flag on(owner-scope 노드/엣지가 실제로 투영되게)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()
    with session() as sess:
        sess.execute(insert(users).values(user_id=owner_b, display_name="Owner B"))
        sess.commit()
    # company 페이지 2개 + 상호 backlink(company 엣지 확보).
    _page(owner_a, "page-x", scope="company", owner_id=None, backlinks=["page-y"])
    _page(owner_a, "page-y", scope="company", owner_id=None, backlinks=["page-x"])
    _page(owner_a, "a-note-1", scope="personal", owner_id=owner_a, backlinks=["page-x"])
    _page(owner_a, "a-note-2", scope="personal", owner_id=owner_a, backlinks=["page-y"])
    _page(owner_b, "b-note-1", scope="personal", owner_id=owner_b, backlinks=["page-x"])
    run_rebuild()
    return {"owner_a": owner_a, "owner_b": owner_b}


def _global_node_count() -> int:
    recs = run_read("MATCH (n) WHERE n.scope IS NOT NULL RETURN count(n) AS c", {})
    return int(list(recs[0].values())[0]) if recs else 0


def test_overview_counts_only_company(overview_graph):
    """company 집계는 company 노드만 — personal은 절대 섞이지 않는다.

    strong form: overview.node_count + Σowner_footprint == scope 있는 전역 노드 수(분할).
    :KgMeta/:OutboxApplied 동기화 메타는 scope 속성이 없어 양쪽 카운트에서 자연 제외된다."""
    g = overview_graph
    ov = company_kg_overview()
    assert ov.status == "ready"
    assert ov.node_count > 0
    fp_a = owner_footprint(g["owner_a"])
    fp_b = owner_footprint(g["owner_b"])
    # 분할 증명 — company + 각 owner personal의 합이 scope 있는 전역 노드 수와 정확히 일치.
    assert ov.node_count + fp_a.node_count + fp_b.node_count == _global_node_count()
    # company 페이지가 by_label에 잡힌다(company 집계 성립).
    assert ov.by_label.get("WikiPage", 0) >= 2
    # company backlink 엣지가 잡힌다.
    assert ov.edge_count > 0


def test_overview_entity_count_matches_label(overview_graph):
    """entity_count는 by_label['Entity'] 파생값과 항상 일치한다(단일 출처)."""
    ov = company_kg_overview()
    assert ov.entity_count == ov.by_label.get("Entity", 0)


def test_overview_endpoint_and_query_run(overview_graph):
    """엔드포인트 200 + ready + Cache-Control + kg_query_runs에 template_name='kg_overview'
    row(params_redacted={}, user_id NULL — company 집계라 caller-bound 아님)."""
    g = overview_graph
    r = client.get("/kg/overview", headers={"X-User-Id": str(g["owner_a"])})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert set(body) == {
        "node_count",
        "edge_count",
        "by_label",
        "entity_count",
        "unresolved_conflicts",
        "resolved_conflicts",
        "status",
    }
    assert r.headers.get("cache-control") == "private, no-store"
    with session() as sess:
        rows = (
            sess.execute(
                select(kg_query_runs.c.params_redacted, kg_query_runs.c.user_id).where(
                    kg_query_runs.c.template_name == "kg_overview"
                )
            )
            .mappings()
            .all()
        )
    assert rows, "kg_overview run row not recorded"
    assert rows[-1]["params_redacted"] == {}
    assert rows[-1]["user_id"] is None


def test_overview_endpoint_disabled_when_kg_off(clean, user_id, monkeypatch):
    """flag off(기본)면 200 + status=disabled(측정 안 함, 500 아님). neo4j 불필요."""
    monkeypatch.setattr(get_settings(), "kg_enabled", False, raising=False)
    r = client.get("/kg/overview", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"


# --- 랜딩 개체 지도 (GET /kg/graph) ---------------------------------------------------


def test_entity_graph_endpoint_returns_entity_map(kg_env, user_id, monkeypatch):
    """한 페이지에 co-mention된 개체 2개 → 랜딩 개체 지도가 :Entity 노드 + RELATES_TO 엣지를
    돌려준다(company-scope, 검색 없이 바로 렌더 가능). owner-scope 무관(company_always)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    pid = wiki_store.write_page(
        WikiPage(slug="ent-map", title="개체 지도 페이지", definition="정의", overview="개요"),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )
    persist_entities(
        [ExtractedEntity(kind="person", name="Kim"), ExtractedEntity(kind="org", name="Acme")],
        page_id=pid,
        slug="ent-map",
        scope="company",
        user_id=user_id,
    )
    run_rebuild()
    r = client.get("/kg/graph", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["relation"] == "entity_overview"
    labels = {n["label"] for n in body["nodes"]}
    assert "Entity" in labels
    # 같은 페이지 co-mention → RELATES_TO 엣지 1쌍 이상. 전부 company scope(owner 미포함).
    assert any(e["rel"] == "RELATES_TO" for e in body["edges"])
    assert all(n["scope"] == "company" for n in body["nodes"])
    assert r.headers.get("cache-control") == "private, no-store"


def test_entity_graph_endpoint_disabled_when_kg_off(clean, user_id, monkeypatch):
    """flag off면 200 supported:false reason=kg_disabled(500 아님). neo4j 불필요."""
    monkeypatch.setattr(get_settings(), "kg_enabled", False, raising=False)
    r = client.get("/kg/graph", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    assert r.json()["supported"] is False and r.json()["reason"] == "kg_disabled"
