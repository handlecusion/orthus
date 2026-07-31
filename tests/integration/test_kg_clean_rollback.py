"""K7.5 (W4) — clean-rollback purges v2(owner-scope) 산출물.

PROVEN deactivation 절차 = **wipe-graph(DETACH DELETE all) → owner-scope flag OFF → v1 rebuild
from PG SoR**. fast rollback(flag off, rebuild 없음)은 personal 노드가 그래프에 잔존해 monitor
`residual_personal` 위반을 낸다 — clean-rollback만 owner-scope 산출물을 0으로 수렴시킨다.
durable invariant(미래 schema bump에도 유효): owner_id≠NULL 노드/엣지 0, scope='personal' 0,
ns_slug-only placeholder 0, company:-prefixed entity(=SoR 없음) 0, 멱등 + bare sync 무-resurrection.

neo4j-test(:7688) + orthus_test PG 필요(`make kg-test-up && make test`).
"""

from __future__ import annotations


import pytest

from orthus.kg.client import kg_write_session, run_read
from orthus.kg.rebuild import run_rebuild
from orthus.kg.sync import run_sync
from orthus.schemas.canonical import WikiPage
from orthus.settings import get_settings
from orthus.wiki import store as wiki_store


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev


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


def _count(cypher: str, params: dict | None = None) -> int:
    recs = run_read(cypher, params or {})
    return int(list(recs[0].values())[0]) if recs else 0


def test_k7_clean_rollback_purges_v2_placeholders(kg_env, user_id, monkeypatch):
    s = get_settings()
    owner_a = user_id
    # owner-scope ON으로 v2-shaped 그래프 생성: company page + personal note(→company edge, owner_id).
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    _page(owner_a, "co-page", scope="company", owner_id=None)
    _page(owner_a, "a-note", scope="personal", owner_id=owner_a, backlinks=["co-page"])
    run_rebuild()

    # 추가로 v2 junk를 수동 plant(미래 schema bump 시뮬): SoR 없는 company:-prefixed entity 노드.
    with kg_write_session() as ws:
        ws.run("CREATE (:Entity {entity_key:'company:person:ghost'})").consume()

    # dirty 상태 확인 — owner-scope 산출물이 그래프에 있다.
    assert _count("MATCH (n) WHERE n.scope = 'personal' RETURN count(n) AS c") >= 1
    assert _count("MATCH (n) WHERE n.owner_id IS NOT NULL RETURN count(n) AS c") >= 1
    assert (
        _count("MATCH (e:Entity) WHERE e.entity_key STARTS WITH 'company:' RETURN count(e) AS c")
        >= 1
    )

    # === 문서화된 clean-rollback 절차 ===
    # 1) wipe-graph: 모든 노드 DETACH DELETE(KgMeta 포함 — rebuild가 재생성).
    with kg_write_session() as ws:
        ws.run("MATCH (n) DETACH DELETE n").consume()
    # 2) owner-scope flag OFF.
    monkeypatch.setattr(s, "kg_owner_scope_enabled", False, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", False, raising=False)
    # 3) v1 rebuild from PG SoR(flag off → company-only 투영).
    run_rebuild()

    def _assert_clean():
        # durable invariant — owner-scope 산출물 0.
        assert _count("MATCH (n) WHERE n.owner_id IS NOT NULL RETURN count(n) AS c") == 0
        assert _count("MATCH ()-[r]->() WHERE r.owner_id IS NOT NULL RETURN count(r) AS c") == 0
        assert _count("MATCH (n) WHERE n.scope = 'personal' RETURN count(n) AS c") == 0
        # ns_slug-only placeholder(page_id NULL) 잔존 0 — co-page는 완전 resolve라 dangling 없음.
        assert _count("MATCH (p:WikiPage) WHERE p.page_id IS NULL RETURN count(p) AS c") == 0
        # company:-prefixed entity는 SoR가 없어(수동 plant만) wipe 후 재생성되지 않는다.
        assert (
            _count(
                "MATCH (e:Entity) WHERE e.entity_key STARTS WITH 'company:' RETURN count(e) AS c"
            )
            == 0
        )

    _assert_clean()

    # 멱등 — 한 번 더 rebuild해도 동일(잔존 0).
    run_rebuild()
    _assert_clean()

    # bare incremental sync도 owner-scope 산출물을 resurrect하지 않는다(음성단정).
    run_sync()
    _assert_clean()
