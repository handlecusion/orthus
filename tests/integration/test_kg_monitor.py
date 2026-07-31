"""K6 PR4 — read-only KG 운영 요약 회귀 (구현 명세 §9.4).

PG 부분(outbox/query_runs)은 항상, Neo4j 부분(KgMeta/graph)은 가용할 때만
채운다(fail-open). 신규 public route 없음 — CLI/함수만.

neo4j-test(:7688) + orthus_test PG 필요(`make kg-test-up && make test`).
"""

from __future__ import annotations

import pytest

from orthus.kg.entities import persist_entities
from orthus.kg.monitor import kg_monitor_summary
from orthus.kg.rebuild import run_rebuild
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.schemas.canonical import ExtractedEntity, WikiPage
from orthus.settings import get_settings
from orthus.wiki import store as wiki_store


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev


def test_kg_monitor_summary_with_graph(kg_env, user_id):
    pid = wiki_store.write_page(
        WikiPage(slug="mon-a", title="mon-a", definition="d", overview="o"),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )
    persist_entities(
        [ExtractedEntity(kind="person", name="Kim")],
        page_id=pid,
        slug="mon-a",
        scope="company",
        user_id=user_id,
    )
    run_rebuild()

    summary = kg_monitor_summary()
    assert summary.kg_enabled is True
    assert summary.neo4j_available is True
    assert summary.graph is not None
    assert summary.graph.entity_count == 1
    assert summary.kg_schema_version == KG_SCHEMA_VERSION
    assert summary.last_rebuild_age_seconds is not None
    # write_page가 op='upsert' wiki_page 이벤트를 enqueue(drain 전이라 pending).
    assert summary.outbox.by_status.get("pending", 0) >= 1


def test_kg_monitor_pg_only_when_kg_off(clean):
    """KG off → graph None, neo4j_available False, PG 요약은 정상."""
    summary = kg_monitor_summary()
    assert summary.kg_enabled is False
    assert summary.neo4j_available is False
    assert summary.graph is None
    assert summary.outbox.by_status == {} or all(
        isinstance(v, int) for v in summary.outbox.by_status.values()
    )
    assert summary.query_runs.window_days == 7


def _mk_page(user_id, slug):
    return wiki_store.write_page(
        WikiPage(slug=slug, title=slug, definition="d", overview="o"),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )


def test_entity_junk_report(kg_env, user_id, monkeypatch):
    """K9 (R8) — junk-rate 측정 리포트: top-N entity-by-mention_count + hub 제외선(above_threshold)
    + 샘플 page + entity_neighbors hit rate(kg_query_runs). 운영자 튜닝 패스 done-criterion 도구."""
    from orthus.kg.gate import run_kg_template
    from orthus.kg.monitor import entity_junk_report

    pa = _mk_page(user_id, "jr-a")
    pb = _mk_page(user_id, "jr-b")
    # project:Orbit → jr-a, jr-b 양쪽(mention_count page-only=2). person:Kim → jr-a만(=1).
    for pid, slug in ((pa, "jr-a"), (pb, "jr-b")):
        persist_entities(
            [ExtractedEntity(kind="project", name="Orbit")],
            page_id=pid,
            slug=slug,
            scope="company",
            user_id=user_id,
        )
    persist_entities(
        [ExtractedEntity(kind="person", name="Kim")],
        page_id=pa,
        slug="jr-a",
        scope="company",
        user_id=user_id,
    )
    run_rebuild()
    # entity_neighbors 1회 실행(default threshold=40) → jr-a가 orbit으로 jr-b와 연결(nonempty)
    # → kg_query_runs 통계 채움.
    run_kg_template(user_id=user_id, template_name="entity_neighbors", params={"slug": "jr-a"})

    settings = get_settings()
    monkeypatch.setattr(settings, "kg_entity_hub_threshold", 2, raising=False)
    rep = entity_junk_report(top_n=10, sample_pages=3)
    assert rep.kg_available is True
    assert rep.hub_threshold == 2
    assert rep.entity_total == 2  # project:orbit + person:kim
    assert rep.hub_entities_excluded == 1  # orbit(mc=2) >= 2 → 제외, kim(mc=1)은 keep
    top = {e.name: e for e in rep.top_entities}
    assert top["Orbit"].mention_count == 2 and top["Orbit"].above_threshold is True
    assert top["Kim"].mention_count == 1 and top["Kim"].above_threshold is False
    assert top["Orbit"].sample_pages and set(top["Orbit"].sample_pages) <= {"jr-a", "jr-b"}
    # entity_neighbors hit rate — 1회 실행, 비명시 연결을 실제로 찾았다(nonempty).
    assert rep.neighbors_runs_total >= 1
    assert rep.neighbors_runs_nonempty >= 1


def test_entity_junk_report_fail_open_when_kg_off(clean):
    """KG off → graph 미가용이라 verify_error만, PG 통계(neighbors_runs)는 0. raise 금지."""
    from orthus.kg.monitor import entity_junk_report

    rep = entity_junk_report()
    assert rep.kg_available is False
    assert rep.verify_error is not None
    assert rep.top_entities == []
    assert rep.neighbors_runs_total == 0
