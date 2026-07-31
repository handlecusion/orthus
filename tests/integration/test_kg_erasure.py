"""K6 PR4 — erasure 전파 회귀 (구현 명세 §9.4/§9.5, operations.md §8.4).

person-entity orphan 처리가 핵심: 소스 page를 모두 지우면 :Entity 이름 노드까지
소멸해야 한다(page/doc 노드 detach만으로 불충분 — 이름 키 잔존). 지워진 page는
op='delete' outbox 이벤트(K6이 최초 호출처)로, orphan :Entity는 즉시 detach로.

neo4j-test(:7688) + orthus_test PG 필요(`make kg-test-up && make test`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from orthus.db import session
from orthus.kg.client import KgDisabled, close_kg_driver
from orthus.kg.entities import persist_entities
from orthus.kg.erase import erase_kg_for_pages
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import ExtractedEntity, WikiPage
from orthus.settings import get_settings
from orthus.tables import kg_entities, kg_entity_mentions, kg_outbox, wiki_pages
from orthus.wiki import store as wiki_store


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """neo4j-test + PG + tmp wiki-store. clean(KG-off 기본) → kg_clean(override)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev


def _seed_page(user_id, slug: str) -> uuid.UUID:
    return wiki_store.write_page(
        WikiPage(slug=slug, title=f"페이지 {slug}", definition="정의", overview="개요"),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )


def _ent(kind: str, name: str) -> ExtractedEntity:
    return ExtractedEntity(kind=kind, name=name)


def _count(driver, cypher: str, **p) -> int:
    with driver.session(database="neo4j") as s:
        return s.run(cypher, **p).single()["n"]


def test_erasure_detaches_graph_nodes(kg_env, user_id):
    """§9.5 — 사람을 mention하던 page를 모두 지우면 person :Entity 노드 소멸."""
    driver = kg_env
    pa = _seed_page(user_id, "erase-a")
    pb = _seed_page(user_id, "erase-b")
    persist_entities(
        [_ent("person", "Kim")], page_id=pa, slug="erase-a", scope="company", user_id=user_id
    )
    persist_entities(
        [_ent("person", "Kim")], page_id=pb, slug="erase-b", scope="company", user_id=user_id
    )
    run_rebuild()
    # K7 v2: 그래프 :Entity 노드 키는 company: 접두(PG kg_entities.entity_key는 unprefixed).
    kim = "MATCH (e:Entity {entity_key:'company:person:kim'}) RETURN count(e) AS n"
    assert _count(driver, kim) == 1
    # 회귀 가드: 모든 :Entity 키가 company: 접두여야 한다(접두 안 된 키 0 — D2).
    assert (
        _count(
            driver,
            "MATCH (e:Entity) WHERE NOT e.entity_key STARTS WITH 'company:' RETURN count(e) AS n",
        )
        == 0
    )
    assert _count(driver, "MATCH (:Entity)-[r:MENTIONED_IN]->(:WikiPage) RETURN count(r) AS n") == 2

    # 한 page만 erase → 다른 page에 surviving mention → orphan 아님, 노드 유지.
    r1 = erase_kg_for_pages([pa])
    assert r1.entities_deleted == 0
    assert _count(driver, kim) == 1

    # 마지막 mention page까지 erase → Kim orphan → PG row + :Entity 노드 소멸.
    r2 = erase_kg_for_pages([pb])
    assert r2.entities_deleted == 1
    assert r2.entity_keys == ["person:kim"]
    assert r2.graph_entities_detached == 1
    assert r2.neo4j_available is True
    assert _count(driver, kim) == 0
    # erase가 wiki page SoR(rows+markdown)까지 지운다(resurrection 차단, owner 결정 A).
    assert r1.wiki_pages_deleted == 1
    assert r2.wiki_pages_deleted == 1
    with session() as s:
        assert s.execute(select(kg_entity_mentions.c.mention_id)).first() is None
        assert s.execute(select(kg_entities.c.entity_id)).first() is None
        assert (
            s.execute(
                select(wiki_pages.c.page_id).where(wiki_pages.c.slug.in_(["erase-a", "erase-b"]))
            ).first()
            is None
        )
    # no-resurrection: PG SoR가 없으니 full rebuild가 노드를 되살리지 않는다.
    run_rebuild()
    assert _count(driver, kim) == 0
    assert _count(driver, "MATCH (p:WikiPage {slug:'erase-a'}) RETURN count(p) AS n") == 0
    assert _count(driver, "MATCH (p:WikiPage {slug:'erase-b'}) RETURN count(p) AS n") == 0


def test_erase_skips_personal_scope_page(kg_env, user_id):
    """company KG erasure는 owner-scope personal wiki page SoR을 건드리지 않는다(F1).

    company node에 공존하는 personal page id를 함께 넘겨도 그 SoR은 보존된다 —
    page_meta가 scope='company'/owner_id IS NULL로 한정되기 때문."""
    cpid = _seed_page(user_id, "co-keep")  # company
    ppid = wiki_store.write_page(
        WikiPage(slug="pers-keep", title="p", definition="d", overview="o"),
        user_id=user_id,
        scope="personal",
        owner_id=user_id,
    )
    run_rebuild()
    r = erase_kg_for_pages([cpid, ppid])
    assert r.wiki_pages_deleted == 1  # company page만
    assert r.page_delete_events == 1  # op='delete'도 company page에만(C6)
    with session() as s:
        assert (
            s.execute(select(wiki_pages.c.page_id).where(wiki_pages.c.page_id == cpid)).first()
            is None
        )
        assert (
            s.execute(select(wiki_pages.c.page_id).where(wiki_pages.c.page_id == ppid)).first()
            is not None  # personal page 보존
        )
        # op='delete'는 company page에만 enqueue(personal page_id로 cross-scope detach 금지)
        del_ids = set(
            s.execute(select(kg_outbox.c.entity_id).where(kg_outbox.c.op == "delete")).scalars()
        )
    assert cpid in del_ids
    assert ppid not in del_ids


def test_erasure_orphan_entity_recovered_by_rebuild_not_rerun(kg_env, user_id):
    """C1 — ⑤ Neo4j detach 누락 창(KG 미가용)에서 orphan :Entity가 잔존하고, erase
    재실행으로는 복구되지 않으며(mention이 이미 삭제돼 재감지 불가), rebuild가 복구한다."""
    driver = kg_env
    settings = get_settings()
    pa = _seed_page(user_id, "crash-a")
    persist_entities(
        [_ent("person", "Kim")], page_id=pa, slug="crash-a", scope="company", user_id=user_id
    )
    run_rebuild()
    # K7 v2: 그래프 :Entity 노드 키는 company: 접두(PG kg_entities.entity_key는 unprefixed).
    kim = "MATCH (e:Entity {entity_key:'company:person:kim'}) RETURN count(e) AS n"
    assert _count(driver, kim) == 1

    # KG 미가용(죽은 URI) 중 erase → 단계 ⑤ detach skip, PG entity row + wiki SoR는 삭제.
    good_uri = settings.kg_uri
    close_kg_driver()
    settings.kg_uri = "bolt://127.0.0.1:1"
    try:
        r = erase_kg_for_pages([pa])
    finally:
        close_kg_driver()
        settings.kg_uri = good_uri
    assert r.entities_deleted == 1
    assert r.graph_entities_detached is None  # detach 미수행(KG down)
    with session() as s:
        assert s.execute(select(kg_entities.c.entity_id)).first() is None  # PG row 삭제됨
    assert _count(driver, kim) == 1  # 그러나 orphan :Entity 노드는 그래프에 잔존

    # 재실행은 복구 못 함 — mention이 이미 삭제돼 affected가 비고 detach가 skip된다.
    r2 = erase_kg_for_pages([pa])
    assert r2.entities_deleted == 0
    assert _count(driver, kim) == 1  # 여전히 잔존

    # rebuild가 권위 복구 — kg_entities row 부재로 prune이 orphan :Entity를 detach.
    run_rebuild()
    assert _count(driver, kim) == 0


def test_erase_enqueues_page_delete_event(kg_env, user_id):
    """지워진 page는 op='delete' wiki_page 이벤트로 enqueue된다(K6 최초 호출처)."""
    pa = _seed_page(user_id, "erase-evt")
    run_rebuild()
    r = erase_kg_for_pages([pa])
    assert r.page_delete_events == 1
    with session() as s:
        deletes = s.execute(
            select(kg_outbox.c.entity_kind, kg_outbox.c.entity_id).where(kg_outbox.c.op == "delete")
        ).all()
    assert any(row.entity_kind == "wiki_page" and row.entity_id == pa for row in deletes)


def test_erase_refuses_on_personal_node(clean):
    """company-node 전용 — personal node에서는 KgDisabled(require_company_node)."""
    settings = get_settings()
    prev = settings.node_kind
    settings.node_kind = "personal"
    try:
        with pytest.raises(KgDisabled):
            erase_kg_for_pages([uuid.uuid4()])
    finally:
        settings.node_kind = prev


def test_erase_pg_cleanup_when_kg_off(clean, user_id, tmp_path):
    """KG off여도 PG SoR(identity row) 삭제는 수행된다(privacy 보증의 권위).

    Neo4j detach/enqueue는 skip(graph_entities_detached=None, page_delete_events는
    enqueue no-op이라 그래프 미반영)."""
    settings = get_settings()
    prev = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    try:
        # KG off에서는 persist가 fail-closed no-op이므로 SoR row를 직접 심는다.
        pid = uuid.uuid4()
        at = datetime.now(UTC)
        eid = uuid.uuid4()
        with session() as s:
            s.execute(
                kg_entities.insert().values(
                    entity_id=eid,
                    entity_key="person:kim",
                    entity_kind="person",
                    name_norm="kim",
                    display_name="Kim",
                    scope="company",
                    owner_id=None,
                    first_seen=at,
                    last_seen=at,
                    schema_version=1,
                    created_at=at,
                    updated_at=at,
                )
            )
            s.execute(
                kg_entity_mentions.insert().values(
                    mention_id=uuid.uuid4(),
                    entity_id=eid,
                    page_id=pid,
                    evidence_slug="off-a",
                    schema_version=1,
                    created_at=at,
                )
            )
            s.commit()

        report = erase_kg_for_pages([pid])
        assert report.kg_enabled is False
        assert report.entities_deleted == 1
        assert report.wiki_pages_deleted == 0  # page_id가 wiki_pages에 없음 → no-op
        assert report.graph_entities_detached is None  # KG off → 그래프 단계 skip
        with session() as s:
            assert s.execute(select(kg_entities.c.entity_id)).first() is None
            assert s.execute(select(kg_entity_mentions.c.mention_id)).first() is None
    finally:
        settings.wiki_store_path = prev
