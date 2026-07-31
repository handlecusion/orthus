"""K2 — 결정론 projection 통합 회귀 (구현 명세 §4.10).

neo4j-test(:7688) + orthus_test PG에서 실행된다(`make kg-test-up && make test`).
핵심 고정 성질: rebuild 멱등(수용 기준 1), company scope PG↔Neo4j parity,
owner-scope row 미투영(boundary — 수용 기준 5 전반부), 그래프에 본문 부재(PII).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, insert, select, update

from orthus.db import get_engine, session
from orthus.kg import store as kg_store
from orthus.kg.client import KgDisabled
from orthus.kg.project import ScopeFilter, load_all, load_one
from orthus.kg.project import build as kg_build
from orthus.kg.rebuild import run_rebuild
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.kg.sync import run_sync
from orthus.projects import set_project_override
from orthus.schemas.canonical import WikiClaim, WikiPage, WikiSource, WikiTask
from orthus.settings import get_settings
from orthus.tables import documents, structured_rows, wiki_links, wiki_pages
from orthus.wiki import store as wiki_store

BODY_MARKER = "KG_PII_BODY_MARKER_8f2c"


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """KG 통합 테스트 환경 — 그래프/PG wipe에 더해 wiki-store frontmatter도
    per-test tmp root로 격리한다(`clean`은 디스크 wiki-store를 wipe하지 않아
    task frontmatter가 conflict index에 누적 오염될 수 있다).

    fixture 순서가 계약이다: `clean`이 먼저 KG-off 기본값을 깔고, `kg_clean`이
    그 위에 test 컨테이너 설정을 override한다(반대면 clean이 flag를 도로 끈다)."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


def _seed_doc(
    user_id, *, scope="company", project="atlas", title="문서", source_db_name=None
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json={},
                markdown=f"{BODY_MARKER} 원본 본문",
                source="notion",
                source_db_name=source_db_name,
                schema_version=1,
                scope=scope,
                project=project,
            )
        )
        s.commit()
    return doc_id


def _seed_fact(
    user_id,
    *,
    scope="company",
    owner_id=None,
    source_doc_id=None,
    valid_until: str | None = None,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    props = {"summary": f"{BODY_MARKER} fact"}
    if valid_until is not None:
        props["valid_until"] = valid_until
    with session() as s:
        s.execute(
            insert(structured_rows).values(
                row_id=row_id,
                source="slack",
                record_type="event",
                source_doc_id=source_doc_id,
                record_key=f"evt-{row_id}",
                properties=props,
                scope=scope,
                owner_id=owner_id,
                user_id=user_id,
            )
        )
        s.commit()
    return row_id


def _write_source(user_id, slug, doc_id, *, key_claims=()):
    return wiki_store.write_source(
        WikiSource(
            slug=slug,
            title=f"source {slug}",
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime.now(UTC),
            summary=f"{BODY_MARKER} 요약",
            key_claims=list(key_claims),
        ),
        user_id=user_id,
    )


def _write_claim(user_id, slug, *, supporting=(), conflicting=()):
    return wiki_store.write_claim(
        WikiClaim(
            slug=slug,
            claim=f"{BODY_MARKER} 주장 {slug}",
            supporting=list(supporting),
            conflicting=list(conflicting),
            confidence="high",
            last_reviewed=date(2026, 6, 1),
            evidence=f"{BODY_MARKER} 근거",
        ),
        user_id=user_id,
    )


def _write_page(user_id, slug, *, sources=(), backlinks=(), scope="company", owner_id=None):
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition=f"{BODY_MARKER} 정의",
            overview=f"{BODY_MARKER} 개요",
            sources=list(sources),
            backlinks=list(backlinks),
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _write_conflict_task(user_id, slug, related, *, resolved=False, created_at=None):
    return wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="conflict",
            description=f"충돌 {slug}",
            related=list(related),
            created_at=created_at or datetime.now(UTC),
            resolved=resolved,
        ),
        user_id=user_id,
    )


def _seed_company(user_id) -> dict[str, uuid.UUID]:
    """company scope 기본 그래프 재료: doc ← source ← claim×2(conflict),
    page(→source, → dangling backlink), fact(→doc)."""
    doc_id = _seed_doc(user_id)
    _write_claim(user_id, "claim-a", supporting=["source-s"], conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    _write_source(user_id, "source-s", doc_id, key_claims=["claim-a"])
    _write_page(user_id, "page-x", sources=["source-s"], backlinks=["missing-page"])
    fact_id = _seed_fact(user_id, source_doc_id=doc_id, valid_until="2026-12-31T00:00:00+00:00")
    return {"doc_id": doc_id, "fact_id": fact_id}


def _graph_snapshot(driver) -> tuple[list, list]:
    """KgMeta/OutboxApplied(동기화 메타 — 실행마다 바뀜)는 제외한 전수 snapshot."""
    with driver.session(database="neo4j") as s:
        nodes = sorted(
            (sorted(r["labels"]), sorted((k, str(v)) for k, v in r["props"].items()))
            for r in s.run(
                "MATCH (n) WHERE NOT n:KgMeta AND NOT n:OutboxApplied "
                "RETURN labels(n) AS labels, properties(n) AS props"
            )
        )
        edges = sorted(
            (
                r["rel"],
                str(r["src"]),
                str(r["dst"]),
                sorted((k, str(v)) for k, v in r["props"].items()),
            )
            for r in s.run(
                "MATCH (a)-[r]->(b) "
                "WHERE NOT a:KgMeta AND NOT a:OutboxApplied "
                "AND NOT b:KgMeta AND NOT b:OutboxApplied "
                "RETURN type(r) AS rel, properties(r) AS props, "
                "coalesce(a.page_id, a.doc_id, a.row_id, a.name, a.slug) AS src, "
                "coalesce(b.page_id, b.doc_id, b.row_id, b.name, b.slug) AS dst"
            )
        )
    return nodes, edges


def _count(driver, cypher: str, **params) -> int:
    with driver.session(database="neo4j") as s:
        return s.run(cypher, **params).single()["n"]


def test_rebuild_idempotent_two_runs(kg_env, user_id):
    _seed_company(user_id)
    _write_conflict_task(user_id, "task-conflict", ["claim-a", "claim-b"])
    run_rebuild()
    first = _graph_snapshot(kg_env)
    run_rebuild()
    second = _graph_snapshot(kg_env)
    assert first == second
    assert first[0], "snapshot must not be empty"


def test_parity_counts_company_scope(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()

    with session() as s:

        def sql_count(table, *conds) -> int:
            return s.execute(select(func.count()).select_from(table).where(*conds)).scalar_one()

        for kind, label in (("page", "WikiPage"), ("claim", "WikiClaim"), ("source", "WikiSource")):
            expected = sql_count(
                wiki_pages, wiki_pages.c.scope == "company", wiki_pages.c.kind == kind
            )
            got = _count(
                kg_env, f"MATCH (n:{label}) WHERE n.page_id IS NOT NULL RETURN count(n) AS n"
            )
            assert got == expected, f"{label} parity"

        assert _count(kg_env, "MATCH (n:Document) RETURN count(n) AS n") == sql_count(
            documents, documents.c.scope == "company"
        )
        assert _count(kg_env, "MATCH (n:StructuredFact) RETURN count(n) AS n") == sql_count(
            structured_rows, structured_rows.c.scope == "company"
        )

        for rel_sor, rel_kg in (
            ("supports", "SUPPORTS"),
            ("conflicts", "CONFLICTS_WITH"),
            ("backlink", "BACKLINK"),
            ("derived_from", "DERIVED_FROM"),
        ):
            expected = s.execute(
                select(func.count())
                .select_from(
                    wiki_links.join(wiki_pages, wiki_pages.c.page_id == wiki_links.c.src_page_id)
                )
                .where(
                    wiki_pages.c.scope == "company",
                    wiki_pages.c.kind.in_(("page", "claim", "source")),
                    wiki_links.c.rel == rel_sor,
                )
            ).scalar_one()
            got = _count(kg_env, f"MATCH ()-[r:{rel_kg}]->() RETURN count(r) AS n")
            assert got == expected, f"{rel_kg} parity"

    # provenance: corpus_doc source 1 + fact 1, IN_PROJECT: page 1 + document 1.
    assert _count(kg_env, "MATCH ()-[r:EXTRACTED_FROM]->() RETURN count(r) AS n") == 2
    assert _count(kg_env, "MATCH ()-[r:IN_PROJECT]->() RETURN count(r) AS n") == 2
    # TTL 속성이 datetime으로 투영됐는지(파싱 성공 케이스).
    assert (
        _count(
            kg_env,
            "MATCH (f:StructuredFact) WHERE f.valid_until IS NOT NULL RETURN count(f) AS n",
        )
        == 1
    )


def test_owner_scope_rows_not_projected(kg_env, user_id):
    _seed_company(user_id)
    owner = user_id
    personal_page = _write_page(user_id, "personal-page", scope="personal", owner_id=owner)
    personal_doc = _seed_doc(user_id, scope="personal")
    personal_fact = _seed_fact(user_id, scope="personal", owner_id=owner)

    # SELECT 필터 자체가 boundary다(§4.2) — load 단계에서 이미 제외.
    rows = load_all(scope_filter=ScopeFilter.COMPANY)
    loaded_pages = {p.page_id for p in rows.pages}
    assert personal_page not in loaded_pages
    assert personal_doc not in {d.doc_id for d in rows.docs}
    assert personal_fact not in {f.row_id for f in rows.facts}
    # K7 v2: slug_map은 (namespace, slug) 키 — COMPANY 필터엔 company 네임스페이스만.
    assert ("company", "personal-page") not in rows.slug_map
    assert not any(slug == "personal-page" for _, slug in rows.slug_map)

    run_rebuild()
    for key in (personal_page, personal_doc, personal_fact):
        n = _count(
            kg_env,
            "MATCH (n) WHERE n.page_id = $k OR n.doc_id = $k OR n.row_id = $k RETURN count(n) AS n",
            k=str(key),
        )
        assert n == 0, f"owner-scope row {key} leaked into graph"
    assert _count(kg_env, "MATCH (n {slug:'personal-page'}) RETURN count(n) AS n") == 0


def test_owner_filter_company_doc_owner_id_null(kg_env, user_id):
    """리뷰 #1 회귀 — owner-inclusive 필터에서도 company Document는 owner_id NULL.
    documents.user_id는 NOT NULL이라 무조건 owner_id로 라벨링하면 company 문서가
    owner를 갖게 돼 'company→owner_id NULL' 불변이 깨진다(CASE로 personal만 owner)."""
    _seed_company(user_id)
    personal_doc = _seed_doc(user_id, scope="personal")

    rows = load_all(scope_filter=ScopeFilter.COMPANY_PLUS_OWNER)
    by_id = {d.doc_id: d for d in rows.docs}
    # personal doc은 owner=user_id, company doc은 owner=None.
    assert by_id[personal_doc].scope == "personal"
    assert by_id[personal_doc].owner_id == user_id
    company_docs = [d for d in rows.docs if d.scope == "company"]
    assert company_docs, "company docs must be present"
    assert all(d.owner_id is None for d in company_docs)

    # build 노드도 company doc은 owner_id None을 SET한다.
    plan = kg_build(rows)
    company_doc_ids = {str(d.doc_id) for d in company_docs}
    for node in plan.nodes:
        if node.label.value == "Document" and node.merge_value in company_doc_ids:
            assert node.props["owner_id"] is None


def test_conflict_edge_props_from_task(kg_env, user_id):
    # 4조합: 매칭 task 없음 / unresolved / resolved / 같은 쌍 복수 task(최신 우선).
    _write_claim(user_id, "c-none-1", conflicting=["c-none-2"])
    _write_claim(user_id, "c-none-2")
    _write_claim(user_id, "c-open-1", conflicting=["c-open-2"])
    _write_claim(user_id, "c-open-2")
    _write_claim(user_id, "c-done-1", conflicting=["c-done-2"])
    _write_claim(user_id, "c-done-2")
    _write_claim(user_id, "c-multi-1", conflicting=["c-multi-2"])
    _write_claim(user_id, "c-multi-2")

    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    _write_conflict_task(user_id, "t-open", ["c-open-1", "c-open-2"], created_at=t0)
    _write_conflict_task(user_id, "t-done", ["c-done-1", "c-done-2"], resolved=True, created_at=t0)
    _write_conflict_task(user_id, "t-multi-old", ["c-multi-1", "c-multi-2"], created_at=t0)
    _write_conflict_task(
        user_id,
        "t-multi-new",
        ["c-multi-1", "c-multi-2"],
        resolved=True,
        created_at=t0 + timedelta(days=1),
    )
    run_rebuild()

    def edge_props(slug: str) -> dict:
        with kg_env.session(database="neo4j") as s:
            return s.run(
                "MATCH (:WikiClaim {slug:$slug})-[r:CONFLICTS_WITH]-() "
                "RETURN properties(r) AS p LIMIT 1",
                slug=slug,
            ).single()["p"]

    none_props = edge_props("c-none-1")
    assert none_props["status"] == "UNRESOLVED"
    assert "detected_at" not in none_props and "conflict_reason" not in none_props

    open_props = edge_props("c-open-1")
    assert open_props["status"] == "UNRESOLVED"
    assert open_props["conflict_reason"] == "충돌 t-open"
    assert open_props["detected_at"] is not None

    assert edge_props("c-done-1")["status"] == "RESOLVED"
    # 같은 쌍 복수 task → created_at 최신(t-multi-new, resolved) 우선(§4.3).
    multi_props = edge_props("c-multi-1")
    assert multi_props["status"] == "RESOLVED"
    assert multi_props["conflict_reason"] == "충돌 t-multi-new"
    # resolved_favoring_id는 v1 항상 null → Neo4j에서는 속성 부재(kg-model §2).
    assert "resolved_favoring_id" not in multi_props


def test_placeholder_promotion_no_duplicate(kg_env, user_id):
    _write_page(user_id, "page-x", backlinks=["missing-page"])
    run_rebuild()
    assert (
        _count(
            kg_env,
            "MATCH (n:WikiPage {slug:'missing-page'}) WHERE n.page_id IS NULL "
            "AND n.materialized = false RETURN count(n) AS n",
        )
        == 1
    )

    _write_page(user_id, "missing-page")  # 실체 생성 → 다음 sync에서 승격(§4.4)
    result = run_sync()
    assert result.status == "ok"
    with kg_env.session(database="neo4j") as s:
        records = s.run(
            "MATCH (n:WikiPage {slug:'missing-page'}) "
            "RETURN n.page_id AS page_id, n.materialized AS materialized"
        ).data()
    assert len(records) == 1, "placeholder/실체 중복 공존 금지(§4.4)"
    assert records[0]["page_id"] is not None
    assert records[0]["materialized"] is True
    # 기존 backlink 엣지가 승격된 노드에 그대로 붙어 있다.
    assert (
        _count(
            kg_env,
            "MATCH (:WikiPage {slug:'page-x'})-[r:BACKLINK]->"
            "(:WikiPage {slug:'missing-page'}) RETURN count(r) AS n",
        )
        == 1
    )


def test_prune_removes_deleted_rows(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        s.run("CREATE (:OutboxApplied {outbox_id:'evt-1', applied_at: datetime()})").consume()

    with session() as s:
        claim_b = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == "claim-b")
        ).scalar_one()
        # row 삭제 시뮬레이션 — claim-b 자신과 claim-b를 가리키던 링크까지 SoR에서 제거.
        s.execute(
            delete(wiki_links).where(
                (wiki_links.c.src_page_id == claim_b) | (wiki_links.c.dst_slug == "claim-b")
            )
        )
        s.execute(delete(wiki_pages).where(wiki_pages.c.page_id == claim_b))
        s.commit()

    run_rebuild()
    assert (
        _count(kg_env, "MATCH (n:WikiClaim {page_id:$k}) RETURN count(n) AS n", k=str(claim_b)) == 0
    )
    # 삭제 노드와 그 엣지가 사라지고, 잔존 placeholder도 생기지 않는다.
    assert _count(kg_env, "MATCH ()-[r:CONFLICTS_WITH]->() RETURN count(r) AS n") == 0
    assert _count(kg_env, "MATCH (n:WikiPage {slug:'claim-b'}) RETURN count(n) AS n") == 0
    # 동기화 메타는 prune 대상이 아니다(§4.6 4단계).
    assert _count(kg_env, "MATCH (m:KgMeta) RETURN count(m) AS n") == 1
    assert _count(kg_env, "MATCH (o:OutboxApplied) RETURN count(o) AS n") == 1


def test_sync_watermark_overlap(kg_env, user_id):
    _write_page(user_id, "page-x")
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        meta = kg_store.get_meta(s)
    assert meta is not None and meta.last_sync_at is not None

    # watermark *직전*에 commit된 row(시계 차 시뮬레이션) — OVERLAP 60초가 흡수(§4.7).
    _write_page(user_id, "page-late")
    with session() as s:
        s.execute(
            update(wiki_pages)
            .where(wiki_pages.c.slug == "page-late")
            .values(updated_at=meta.last_sync_at - timedelta(seconds=30))
        )
        s.commit()

    result = run_sync()
    assert result.status == "ok"
    assert (
        _count(
            kg_env,
            "MATCH (n:WikiPage {slug:'page-late'}) WHERE n.page_id IS NOT NULL "
            "RETURN count(n) AS n",
        )
        == 1
    )


def test_sync_refuses_on_schema_version_mismatch(kg_env, user_id):
    _write_page(user_id, "page-x")
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        kg_store.set_meta(s, kg_schema_version=KG_SCHEMA_VERSION + 999)

    _write_page(user_id, "page-after-bump")
    result = run_sync()
    assert result.status == "rebuild_required"
    # 그래프 무변경 — 새 page가 들어가지 않았다.
    assert _count(kg_env, "MATCH (n:WikiPage {slug:'page-after-bump'}) RETURN count(n) AS n") == 0


def test_sync_requires_initial_rebuild(kg_env, user_id):
    """빈 그래프에서 sync 호출 → rebuild required(§4.9)."""
    _write_page(user_id, "page-x")
    result = run_sync()
    assert result.status == "rebuild_required"


def test_k7_sync_skips_while_rebuild_in_progress(kg_env, user_id):
    """K7.5 — boolean HOLD(rebuild_in_progress=True) 동안 sync는 locked.

    구 clock-락(`rebuild_lock_until` 시간 산술) 대체 회귀
    (이전 `test_sync_skips_while_rebuild_lock_active`)."""
    _write_page(user_id, "page-x")
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        kg_store.set_meta(s, rebuild_in_progress=True)
    result = run_sync()
    assert result.status == "locked"


def test_k7_rebuild_clears_in_progress_and_records_seconds(kg_env, user_id):
    """K7.5 — 정상 rebuild는 완료 시 rebuild_in_progress=False로 clear하고
    last_rebuild_seconds를 기록한다(HOLD가 stuck-true로 남지 않음)."""
    _write_page(user_id, "page-x")
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        meta = kg_store.get_meta(s)
    assert meta is not None
    assert meta.rebuild_in_progress is False
    assert meta.rebuild_started_at is None
    assert meta.last_rebuild_seconds is not None and meta.last_rebuild_seconds >= 0


def test_k7_rebuild_abort_clears_in_progress(kg_env, user_id, monkeypatch):
    """K7.5 — rebuild가 도중 예외로 abort해도 HOLD를 clear한다(stuck-true 방지).

    projection 단계에서 예외를 주입해 set True → abort → clear 경로를 고정한다."""
    _write_page(user_id, "page-x")

    def _boom(*_a, **_k):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr("orthus.kg.project.load_all", _boom)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        run_rebuild()
    # 주입 abort가 정상 흐름을 끊어, _write_page의 corpus/outbox write 커넥션이 미커밋
    # idle-in-transaction으로 풀에 남으면, 다음 테스트의 공유-DB cleanup이
    # `DELETE FROM users`에서 embeddings_user_id_fkey로 깨지고 116개가 cascade ERROR가 된다
    # (flaky, 풀 커넥션 타이밍 의존). 엔진 풀을 dispose해 누수 커넥션을 떨어내 격리를 보장한다.
    get_engine().dispose()
    with kg_env.session(database="neo4j") as s:
        meta = kg_store.get_meta(s)
    # set True 후 abort에서 clear됨 — sync/worker가 영구 locked되지 않는다.
    assert meta is not None
    assert meta.rebuild_in_progress is False
    assert meta.rebuild_started_at is None


def test_no_body_text_in_graph(kg_env, user_id):
    _seed_company(user_id)
    _write_conflict_task(user_id, "task-conflict", ["claim-a", "claim-b"])
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        leaked = s.run(
            "MATCH (n) UNWIND keys(n) AS k WITH n, k "
            "WHERE toString(n[k]) CONTAINS $marker "
            "RETURN labels(n) AS labels, k AS key",
            marker=BODY_MARKER,
        ).data()
        leaked += s.run(
            "MATCH ()-[r]->() UNWIND keys(r) AS k WITH r, k "
            "WHERE toString(r[k]) CONTAINS $marker "
            "RETURN type(r) AS rel, k AS key",
            marker=BODY_MARKER,
        ).data()
    assert leaked == [], f"본문 텍스트가 그래프 속성에 유출됨: {leaked}"


def test_prune_removes_stale_node_after_kind_change(kg_env, user_id):
    """같은 page_id의 kind가 바뀌면(SoR 정체성은 (slug,scope,owner) —
    `_upsert_page_row`가 kind를 UPDATE한다) 구 라벨 노드를 rebuild prune이
    지워야 한다 — 라벨별 ExpectedKeys 회귀(K2 리뷰 교정, 2026-06-12)."""
    _write_claim(user_id, "flip-me")
    run_rebuild()
    with session() as s:
        page_id = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == "flip-me")
        ).scalar_one()
        s.execute(
            update(wiki_pages)
            .where(wiki_pages.c.page_id == page_id)
            .values(kind="page", title="페이지 flip-me", updated_at=func.now())
        )
        s.commit()

    run_rebuild()
    assert (
        _count(kg_env, "MATCH (n:WikiClaim {page_id:$k}) RETURN count(n) AS n", k=str(page_id)) == 0
    ), "kind-flip 후 구 라벨 노드가 prune을 통과하면 안 된다"
    assert (
        _count(kg_env, "MATCH (n:WikiPage {page_id:$k}) RETURN count(n) AS n", k=str(page_id)) == 1
    )


def test_kg_refuses_on_personal_node(clean, monkeypatch):
    """projection 쓰기 경로는 company node 전용 — personal node env에서 돌면
    personal DB 기준 prune이 공유 Neo4j의 company 그래프를 삭제할 수 있다.
    셸 가드(sync_cycle.sh)와 별개로 코드 레벨 fail-closed를 고정한다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "kg_enabled", True)
    with pytest.raises(KgDisabled):
        run_rebuild()
    with pytest.raises(KgDisabled):
        run_sync()


def test_sync_picks_up_project_retag(kg_env, user_id):
    """/projects 재태깅이 updated_at을 갱신해 kg-sync가 변경을 본다는 회귀 —
    bump가 빠지면 sync는 ok를 보고하면서 그래프는 옛 project를 가리킨다."""
    _seed_doc(user_id, source_db_name="retag-db")
    run_rebuild()
    assert _count(kg_env, "MATCH (d:Document {project:'atlas'}) RETURN count(d) AS n") == 1

    set_project_override("retag-db", "nova")
    result = run_sync()
    assert result.status == "ok"
    assert _count(kg_env, "MATCH (d:Document {project:'nova'}) RETURN count(d) AS n") == 1
    assert (
        _count(
            kg_env,
            "MATCH (:Document {project:'nova'})-[r:IN_PROJECT]->(:Project {name:'nova'}) "
            "RETURN count(r) AS n",
        )
        == 1
    )


def test_rebuild_clears_stale_source_front_props(kg_env, user_id):
    """frontmatter가 사라진 :WikiSource의 source_type/source_ref가 rebuild에서
    제거된다 — 키 생략이 아니라 None 포함이어야 Neo4j `SET +=`가 수렴한다."""
    doc_id = _seed_doc(user_id)
    _write_source(user_id, "source-gone", doc_id)
    run_rebuild()
    assert (
        _count(
            kg_env,
            "MATCH (s:WikiSource {slug:'source-gone'}) "
            "WHERE s.source_ref IS NOT NULL RETURN count(s) AS n",
        )
        == 1
    )

    md = next(Path(get_settings().wiki_store_path).rglob("source-gone.md"))
    md.unlink()
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        props = s.run(
            "MATCH (s:WikiSource {slug:'source-gone'}) RETURN properties(s) AS p"
        ).single()["p"]
    assert "source_type" not in props and "source_ref" not in props
    # provenance 엣지도 expected에서 빠져 prune된다.
    assert _count(kg_env, "MATCH ()-[r:EXTRACTED_FROM]->() RETURN count(r) AS n") == 0


def test_load_one_single_row_plan(kg_env, user_id):
    """K3 outbox 단일 이벤트 재투영 경로 — batch와 같은 build()를 지난다(§5.4)."""
    ids = _seed_company(user_id)
    with session() as s:
        page_id = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == "page-x")
        ).scalar_one()

    plan = kg_build(load_one("wiki_page", page_id))
    real_pages = [n for n in plan.nodes if n.merge_key == "page_id"]
    assert [n.merge_value for n in real_pages] == [str(page_id)]
    rels = {e.rel.value for e in plan.edges}
    assert "DERIVED_FROM" in rels  # page → source
    assert "BACKLINK" in rels  # page → dangling placeholder

    # owner-scope/미존재 row는 빈 plan으로 수렴 — K3 worker no-op 계약.
    empty = kg_build(load_one("document", uuid.uuid4()))
    assert [n for n in empty.nodes if n.merge_key == "doc_id"] == []
    assert ids["doc_id"] is not None
