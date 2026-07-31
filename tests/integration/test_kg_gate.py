"""K4 — 읽기 게이트 + page graph API 통합 회귀 (구현 명세 §6.5).

structured 5-reject와 동형의 reject 회귀 세트가 핵심이다: 미등록 템플릿,
파라미터/깊이 초과, 미존재 slug, timeout, (write 키워드 정적 검사는
`tests/unit/test_kg_templates.py`). 모든 실행이 `kg_query_runs`에 남고
API는 fail-open(200 + supported:false)임을 함께 고정한다.

neo4j-test(:7688) + orthus_test PG에서 실행된다(`make kg-test-up && make test`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.kg import gate as kg_gate
from orthus.kg.client import close_kg_driver
from orthus.kg.entities import persist_entities
from orthus.kg.gate import KgQueryStatus, run_kg_template
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import (
    ExtractedEntity,
    WikiClaim,
    WikiPage,
    WikiSource,
    WikiTask,
)
from orthus.settings import get_settings
from orthus.tables import documents, kg_query_runs, structured_rows, wiki_pages
from orthus.wiki import store as wiki_store

client = TestClient(app)

BODY_MARKER = "KG_PII_BODY_MARKER_8f2c"


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """K2 `test_kg_projection.kg_env`와 동일 계약 — clean이 KG-off 기본값을
    깔고 kg_clean이 test 컨테이너 설정을 override하며, wiki-store frontmatter는
    per-test tmp root로 격리한다."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


# --- seed helpers (test_kg_projection 동형 — 자급 사본) ----------------------------


def _seed_doc(user_id, *, scope="company", project="atlas") -> uuid.UUID:
    doc_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title="문서",
                block_json={},
                markdown=f"{BODY_MARKER} 원본 본문",
                source="notion",
                schema_version=1,
                scope=scope,
                project=project,
            )
        )
        s.commit()
    return doc_id


def _seed_fact(user_id, *, source_doc_id=None, valid_until: str | None = None) -> uuid.UUID:
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
                scope="company",
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


def _write_page(user_id, slug, *, sources=(), backlinks=()):
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
        scope="company",
        owner_id=None,
    )


def _write_conflict_task(user_id, slug, related, *, resolved=False):
    return wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="conflict",
            description=f"{BODY_MARKER} 충돌 {slug}",
            related=list(related),
            created_at=datetime.now(UTC),
            resolved=resolved,
        ),
        user_id=user_id,
    )


def _seed_company(user_id) -> dict[str, uuid.UUID]:
    """doc ← source ← claim×2(conflict), page-x(→source, dangling backlink),
    page-y(→page-x backlink) — K4 템플릿 4종이 전부 닿는 최소 그래프."""
    doc_id = _seed_doc(user_id)
    _write_claim(user_id, "claim-a", supporting=["source-s"], conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    _write_source(user_id, "source-s", doc_id, key_claims=["claim-a"])
    _write_page(user_id, "page-x", sources=["source-s"], backlinks=["missing-page"])
    _write_page(user_id, "page-y", backlinks=["page-x"])
    _write_conflict_task(user_id, "task-conflict", ["claim-a", "claim-b"])
    return {"doc_id": doc_id}


def _insert_company_page_row(slug: str) -> None:
    """PG row만 필요한 테스트(404/가용성 분기)용 — wiki-store 미사용."""
    with session() as s:
        s.execute(
            insert(wiki_pages).values(
                page_id=uuid.uuid4(),
                slug=slug,
                kind="page",
                path=f"wiki/{slug}.md",
                title=slug,
                content_hash="x",
                schema_version=1,
                scope="company",
                project="company",
            )
        )
        s.commit()


def _runs() -> list:
    with session() as s:
        return list(s.execute(select(kg_query_runs)).mappings())


# --- reject 회귀 세트 (§6.5 #1-#4, #6) ----------------------------------------------


def test_gate_rejects_unknown_template(kg_env, user_id):
    result = run_kg_template(user_id=user_id, template_name="nope", params={})
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "unknown_template"
    runs = _runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "rejected"
    assert runs[0]["reject_reason"] == "unknown_template"


def test_gate_rejects_invalid_params(kg_env, user_id):
    result = run_kg_template(
        user_id=user_id, template_name="neighbors", params={"slug": "x", "depth": 3}
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "invalid_params:depth"
    assert _runs()[0]["reject_reason"] == "invalid_params:depth"


def test_gate_rejects_identical_slugs(kg_env, user_id):
    result = run_kg_template(
        user_id=user_id,
        template_name="path_between",
        params={"slug_a": "same", "slug_b": "same"},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "invalid_params:identical_slugs"


def test_gate_rejects_unknown_slug(kg_env, user_id):
    result = run_kg_template(
        user_id=user_id, template_name="neighbors", params={"slug": "ghost-slug"}
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "slug_not_found:ghost-slug"


def test_gate_rejects_when_disabled(clean, user_id):
    # clean 기본값이 KG-off — flag reject는 컨테이너 없이도 평가된다(1단계).
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "x"})
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "kg_disabled"
    assert _runs()[0]["status"] == "rejected"


def test_gate_unavailable_on_dead_uri(clean, user_id):
    settings = get_settings()
    _insert_company_page_row("alive-page")
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"
    settings.kg_password = "x"
    try:
        result = run_kg_template(
            user_id=user_id, template_name="neighbors", params={"slug": "alive-page"}
        )
    finally:
        close_kg_driver()
        settings.kg_enabled = False
        settings.kg_uri = "bolt://127.0.0.1:7687"
        settings.kg_password = ""
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "kg_unavailable"


def test_gate_timeout_recorded(kg_env, user_id):
    """§6.5 #4 — 양쪽에 dense mesh를 깐 비연결 shortestPath + timeout 1ms.

    transaction timeout은 서버측 강제다. BFS가 양쪽 mesh(각 300², 도합 18만
    rels)를 다 훑어야 비연결을 확정하므로 1ms 안에 끝날 수 없다 — planner
    cache가 따뜻해도 결정적으로 timeout한다."""
    _insert_company_page_row("hub-a")
    _insert_company_page_row("hub-b")
    with kg_env.session(database="neo4j") as s:
        for hub in ("hub-a", "hub-b"):
            s.run(
                "CREATE (h:WikiPage {slug:$hub, page_id:$hub}) "
                "WITH h UNWIND range(1, 300) AS i "
                "CREATE (x:WikiPage {slug:$hub+'-n'+toString(i), page_id:$hub+'-n'+toString(i)}) "
                "CREATE (h)-[:BACKLINK]->(x) "
                "WITH collect(x) AS xs UNWIND xs AS x1 UNWIND xs AS x2 "
                "CREATE (x1)-[:BACKLINK]->(x2)",
                hub=hub,
            ).consume()
    get_settings().kg_query_timeout_ms = 1
    result = run_kg_template(
        user_id=user_id,
        template_name="path_between",
        params={"slug_a": "hub-a", "slug_b": "hub-b"},
    )
    assert result.status is KgQueryStatus.TIMEOUT
    assert result.reject_reason == "timeout"
    assert _runs()[0]["status"] == "timeout"


def test_gate_timeout_mapping_deterministic(clean, user_id, monkeypatch):
    """driver timeout 예외 → TIMEOUT 정규화 — 서버 reaper 주기와 무관한 백스톱.

    예외 클래스/코드는 driver 버전 의존이라(구현 명세 §12) status code 기반
    `is_timeout_error` 정규화를 fake 예외로 직접 고정한다."""

    class _FakeTimeout(Exception):
        code = "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"

    _insert_company_page_row("alive-page")
    get_settings().kg_enabled = True
    monkeypatch.setattr("orthus.kg.gate.kg_available", lambda: True)

    def _raise(cypher, params):
        raise _FakeTimeout("transaction terminated")

    monkeypatch.setattr("orthus.kg.gate.run_read", _raise)
    result = run_kg_template(
        user_id=user_id, template_name="neighbors", params={"slug": "alive-page"}
    )
    get_settings().kg_enabled = False
    assert result.status is KgQueryStatus.TIMEOUT
    assert _runs()[0]["status"] == "timeout"


# --- 결과 매핑 규약 (§6.5 #7-#9) ------------------------------------------------------


def test_neighbors_graph_and_placeholder(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.OK
    by_id = {n.id: n for n in result.nodes}
    # 규약 3 — placeholder: materialized=false + id=slug (§6.5 #9).
    assert "missing-page" in by_id
    assert by_id["missing-page"].materialized is False
    # IN_PROJECT 제외 결정 — :Project 허브는 응답에 등장하지 않는다.
    assert not any(n.label == "Project" for n in result.nodes)
    assert not any(e.rel == "IN_PROJECT" for e in result.edges)
    rels = {e.rel for e in result.edges}
    assert "BACKLINK" in rels and "DERIVED_FROM" in rels

    deep = run_kg_template(
        user_id=user_id, template_name="neighbors", params={"slug": "page-x", "depth": 2}
    )
    assert deep.status is KgQueryStatus.OK
    assert len(deep.nodes) >= len(result.nodes)
    deep_labels = {n.label for n in deep.nodes}
    assert "Document" in deep_labels  # page→source→document 2-hop


def test_conflicts_of_temporal_serialized(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="conflicts_of", params={"slug": "claim-a"}
    )
    assert result.status is KgQueryStatus.OK
    edge = next(e for e in result.edges if e.rel == "CONFLICTS_WITH")
    # 규약 1 — temporal: driver DateTime이 ISO 문자열로 직렬화 (§6.5 #7).
    detected = edge.properties["detected_at"]
    assert isinstance(detected, str)
    datetime.fromisoformat(detected)
    assert edge.properties["status"] == "UNRESOLVED"


def test_conflicts_of_unmatched_omits_null_props(kg_env, user_id):
    """매칭 conflict task가 없으면 CONFLICTS_WITH는 `status`만 담는다 — Neo4j는
    null 속성을 저장하지 않으므로 projection이 None으로 쓴 `detected_at`/
    `conflict_reason`/`resolved_favoring_id` 키는 응답에서 **생략**된다(null이
    아니라 키 부재). FE sparse 계약(`KgGraphEdge` docstring, kg-model §2 wire
    format 주의)을 코드로 고정 — 소비자는 `.get()`을 써야 한다(리뷰 후속)."""
    # task 없이 conflicts 링크만 — claim-a ↔ claim-b, conflict WikiTask 미생성.
    _write_claim(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="conflicts_of", params={"slug": "claim-a"}
    )
    assert result.status is KgQueryStatus.OK
    edge = next(e for e in result.edges if e.rel == "CONFLICTS_WITH")
    assert edge.properties["status"] == "UNRESOLVED"
    # null 속성은 그래프에 존재하지 않아 응답 properties에서도 키가 없다.
    assert "detected_at" not in edge.properties
    assert "conflict_reason" not in edge.properties
    assert "resolved_favoring_id" not in edge.properties


def test_provenance_chain(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="provenance_chain", params={"slug": "claim-a"}
    )
    assert result.status is KgQueryStatus.OK
    labels = {n.label for n in result.nodes}
    assert labels == {"WikiClaim", "WikiSource", "Document"}
    assert {e.rel for e in result.edges} == {"SUPPORTS", "EXTRACTED_FROM"}


def test_path_between(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id,
        template_name="path_between",
        params={"slug_a": "page-y", "slug_b": "page-x"},
    )
    assert result.status is KgQueryStatus.OK
    assert {n.slug for n in result.nodes} == {"page-y", "page-x"}
    assert result.edges[0].rel == "BACKLINK"


def test_path_between_excludes_project_hub(kg_env, user_id):
    """지식 rel allowlist는 IN_PROJECT를 제외한다(K6 PR3 — missing_link 정합).

    같은 project(둘 다 기본 'atlas')의 두 page는 지식 link가 없으면 a→:Project→b
    허브 경로가 있어도 path_between이 잡지 않는다 → OK + 빈 결과(=비연결)."""
    _write_page(user_id, "solo-a")  # default project 'atlas', 링크 없음
    _write_page(user_id, "solo-b")
    run_rebuild()
    result = run_kg_template(
        user_id=user_id,
        template_name="path_between",
        params={"slug_a": "solo-a", "slug_b": "solo-b"},
    )
    assert result.status is KgQueryStatus.OK
    assert result.nodes == []
    assert result.edges == []


def test_expired_fact_excluded(kg_env, user_id):
    """규약 2 — TTL 필터는 게이트 결과 매퍼 단일 지점이다(§6.1, §6.5 #8).

    v1 템플릿 토폴로지에서 :StructuredFact는 leaf(doc 1-hop)라 WikiPage 시작
    depth≤2 탐색에 닿지 않는다 — 매퍼를 driver 실데이터 record로 직접 고정해
    전 템플릿·향후 템플릿(K6 entity_mentions)에 일괄 적용될 동작을 회귀로 둔다."""
    doc_id = _seed_doc(user_id)
    _seed_fact(user_id, source_doc_id=doc_id, valid_until="2020-01-01T00:00:00+00:00")
    fresh = _seed_fact(user_id, source_doc_id=doc_id, valid_until="2099-01-01T00:00:00+00:00")
    run_rebuild()
    with kg_env.session(database="neo4j") as s:
        records = list(
            s.run("MATCH (f:StructuredFact)-[r:EXTRACTED_FROM]->(d:Document) RETURN f, r, d")
        )
    nodes, edges, dropped = kg_gate._map_records(records, None)
    fact_ids = {n.id for n in nodes if n.label == "StructuredFact"}
    assert fact_ids == {str(fresh)}
    assert dropped["expired_facts"] == 1
    assert all(e.src == str(fresh) for e in edges if e.rel == "EXTRACTED_FROM")


# --- last_accessed_at (8단계) --------------------------------------------------------


def test_last_accessed_at_set_on_ok(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.OK
    with kg_env.session(database="neo4j") as s:
        value = s.run("MATCH (p:WikiPage {slug:'page-x'}) RETURN p.last_accessed_at AS t").single()[
            "t"
        ]
    assert value is not None


def test_last_accessed_at_best_effort(kg_env, user_id, monkeypatch):
    """관측 SET 실패는 swallow — 질의 결과에 영향 없음(kg-model §2 각주)."""
    _seed_company(user_id)
    run_rebuild()

    def _boom(page_ids):
        raise RuntimeError("write path down")

    monkeypatch.setattr("orthus.kg.store.touch_last_accessed", _boom)
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.OK
    assert result.nodes


# --- fail-open 견고성 (버그 리뷰 #9 — run_kg_template은 예외를 던지지 않는다) -------


def test_record_run_failure_preserves_result(kg_env, user_id, monkeypatch):
    """run 기록 실패는 best-effort — 이미 산출된 graph 결과 반환을 막지 않는다."""
    _seed_company(user_id)
    run_rebuild()

    def _boom(**kwargs):
        raise RuntimeError("kg_query_runs insert failed")

    monkeypatch.setattr("orthus.kg.gate._record_run", _boom)
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.OK
    assert result.nodes  # 결과 보존


def test_mapping_error_normalized_to_error_status(kg_env, user_id, monkeypatch):
    """그래프 결과 처리(_map_records) 예외는 driver와 구분된 mapping_error로 정규화 —
    endpoint 500 금지 + kg_query_runs 진단성 보존(매핑 결함이 driver 장애로 안 보임)."""
    _seed_company(user_id)
    run_rebuild()

    def _boom(records, caller):
        raise RuntimeError("mapping blew up")

    monkeypatch.setattr("orthus.kg.gate._map_records", _boom)
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.ERROR
    assert result.reject_reason == "mapping_error:RuntimeError"


def test_audit_infra_failure_fails_open(kg_env, user_id, monkeypatch):
    """audit 인프라의 실제 실패 모드(PG write)를 충실히 시뮬레이션 — `_write`(enter/exit
    span 기록)가 raise해도 최상위 try가 잡아 ERROR로 수렴, 예외 미전파(endpoint 500 금지).

    audit() 팩토리가 아니라 `orthus.audit.logger._write`를 mock한다 — 운영에서 PG가
    죽으면 audit `__enter__`의 `_write(enter)`가 raise하는 경로 그대로다."""
    _seed_company(user_id)
    run_rebuild()

    def _boom(*args, **kwargs):
        raise RuntimeError("audit_log insert failed")

    monkeypatch.setattr("orthus.audit.logger._write", _boom)
    result = run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    assert result.status is KgQueryStatus.ERROR
    assert result.reject_reason == "internal_error"


def test_endpoint_fail_open_on_mapping_error(kg_env, user_id, monkeypatch):
    """endpoint는 run_kg_template 내부 장애에도 500이 아니라 200 supported:false."""
    _seed_company(user_id)
    run_rebuild()

    def _boom(records, caller):
        raise RuntimeError("mapping blew up")

    monkeypatch.setattr("orthus.kg.gate._map_records", _boom)
    r = client.get("/wiki/pages/page-x/graph")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["reason"] == "kg_unavailable"


def test_limit_guard_zero_and_negative(kg_env, user_id, monkeypatch):
    """내부 호출자가 limit=0/음수/None을 줘도 서버 기본으로 수렴(음수 LIMIT 누출 차단).

    bind된 effective limit을 직접 캡처해 가드가 실제로 적용됐음을 고정한다 —
    status/nodes만으로는 가드 제거 회귀를 놓칠 수 있다(리뷰 후속)."""
    _seed_company(user_id)
    run_rebuild()
    captured: list[int] = []
    real_run_read = kg_gate.run_read

    def _spy(cypher, params):
        captured.append(params["limit"])
        return real_run_read(cypher, params)

    monkeypatch.setattr("orthus.kg.gate.run_read", _spy)
    cap = get_settings().kg_query_limit
    for bad in (0, -5, None):
        captured.clear()
        result = run_kg_template(
            user_id=user_id, template_name="neighbors", params={"slug": "page-x"}, limit=bad
        )
        assert result.status is KgQueryStatus.OK  # 음수 LIMIT이 driver_error로 새지 않음
        assert captured == [cap]  # 서버 기본으로 수렴
    # 양수이되 cap 미만은 그대로, cap 초과는 cap으로.
    for req, want in ((10, min(10, cap)), (cap + 100, cap)):
        captured.clear()
        run_kg_template(
            user_id=user_id, template_name="neighbors", params={"slug": "page-x"}, limit=req
        )
        assert captured == [want]


# --- kg_query_runs 기록/redaction ----------------------------------------------------


def test_kg_query_runs_params_redacted_and_correlated(kg_env, user_id):
    correlation_id = uuid.uuid4()
    result = run_kg_template(
        user_id=user_id,
        template_name="neighbors",
        params={"slug": "mail-bob@example.com"},
        correlation_id=correlation_id,
    )
    assert result.status is KgQueryStatus.REJECTED
    run = _runs()[0]
    assert run["correlation_id"] == correlation_id
    assert run["user_id"] == user_id
    # 파라미터/사유 양쪽 모두 redaction 통과 — raw email이 남지 않는다.
    assert "bob@example.com" not in str(run["params_redacted"])
    assert "bob@example.com" not in run["reject_reason"]
    assert run["reject_reason"].startswith("slug_not_found:")


def test_every_branch_records_a_run(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    run_kg_template(user_id=user_id, template_name="neighbors", params={"slug": "page-x"})
    run_kg_template(user_id=user_id, template_name="nope", params={})
    runs = _runs()
    assert {r["status"] for r in runs} == {"ok", "rejected"}
    ok = next(r for r in runs if r["status"] == "ok")
    assert ok["result_count"] >= 1
    assert ok["duration_ms"] is not None


# --- GET /wiki/pages/{slug:path}/graph (kg-model §4 응답 계약) -------------------------


def test_graph_endpoint_unsupported_when_disabled(clean, user_id):
    # flag off는 404보다 먼저 평가된다 — 존재하지 않는 slug여도 200 unsupported.
    r = client.get("/wiki/pages/whatever/graph", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["reason"] == "kg_disabled"
    assert body["nodes"] == [] and body["edges"] == []


def test_graph_endpoint_404_unknown_slug(kg_env):
    r = client.get("/wiki/pages/no-such-page/graph")
    assert r.status_code == 404


def test_graph_endpoint_returns_graph_no_body(kg_env, user_id):
    _seed_company(user_id)
    run_rebuild()
    r = client.get("/wiki/pages/page-x/graph", params={"depth": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    slugs = {n["slug"] for n in body["nodes"]}
    assert "page-x" in slugs and "missing-page" in slugs
    # 불변식 5 — 응답에 본문 없음(slug/title/메타만).
    assert BODY_MARKER not in r.text


def test_graph_endpoint_depth_validation(kg_env, user_id):
    _seed_company(user_id)
    r = client.get("/wiki/pages/page-x/graph", params={"depth": 3})
    assert r.status_code == 422


def test_graph_endpoint_unavailable_fail_open(clean, user_id):
    settings = get_settings()
    _insert_company_page_row("alive-page")
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"
    settings.kg_password = "x"
    try:
        r = client.get("/wiki/pages/alive-page/graph")
    finally:
        close_kg_driver()
        settings.kg_enabled = False
        settings.kg_uri = "bolt://127.0.0.1:7687"
        settings.kg_password = ""
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["reason"] == "kg_unavailable"


# --- K8 (모순 노출 / Phase B) — docs/kg-k8-plan.md ----------------------------------


def test_k8_conflicts_of_relaxed_returns_placeholder_counterpart(kg_env, user_id):
    """K8.1 (B1) — 반대편이 placeholder(dangling conflict dst)여도 conflicts_of가 반환한다.
    무라벨 `(o)` 완화 전엔 `(o:WikiClaim)`라 placeholder(:WikiPage)가 빠져 빈 결과였다."""
    _write_claim(user_id, "claim-x", conflicting=["ghost-claim"])
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="conflicts_of", params={"slug": "claim-x"}
    )
    assert result.status is KgQueryStatus.OK
    assert any(e.rel == "CONFLICTS_WITH" for e in result.edges)
    ghost = next((n for n in result.nodes if n.slug == "ghost-claim"), None)
    assert ghost is not None and ghost.materialized is False


def test_k8_page_conflicts_returns_conflict(kg_env, user_id):
    """K8.2 (B2/B3) — page → BACKLINK claim → CONFLICTS_WITH counterpart. 시작 page 노드와
    CONFLICTS_WITH 엣지가 함께 반환된다(패널 union + /ask conflict page-resolve가 공유)."""
    _seed_company(user_id)  # claim-a conflicting claim-b
    _write_page(user_id, "page-conf", backlinks=["claim-a"])
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="page_conflicts", params={"slug": "page-conf"}
    )
    assert result.status is KgQueryStatus.OK
    assert any(n.slug == "page-conf" for n in result.nodes)
    assert any(e.rel == "CONFLICTS_WITH" for e in result.edges)


def test_k8_page_conflicts_zero_returns_page_only(kg_env, user_id):
    """K8.2/K8.3 — 모순 0건이어도 OPTIONAL MATCH가 시작 page를 반환한다(zero-conflict가
    grounding 비어 demote되지 않게 — grounded "모순 없음" 답변의 전제)."""
    _write_claim(user_id, "claim-solo")
    _write_page(user_id, "page-solo", backlinks=["claim-solo"])
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="page_conflicts", params={"slug": "page-solo"}
    )
    assert result.status is KgQueryStatus.OK
    assert any(n.slug == "page-solo" for n in result.nodes)
    assert not any(e.rel == "CONFLICTS_WITH" for e in result.edges)


def test_k8_page_graph_endpoint_unions_conflicts(kg_env, user_id):
    """K8.2 (B2-a) — GET /wiki/pages/{slug}/graph가 neighbors에 page_conflicts를 union해
    CONFLICTS_WITH 엣지를 같은 패널 응답에 싣는다(별도 섹션 없음, 본문 없음 — 불변식 5)."""
    _seed_company(user_id)
    _write_page(user_id, "page-conf", backlinks=["claim-a"])
    run_rebuild()
    r = client.get("/wiki/pages/page-conf/graph", params={"depth": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert any(e["rel"] == "CONFLICTS_WITH" for e in body["edges"])
    # conflict_reason(=conflict task description, kg-model §2 edge property)은 정상 노출이다
    # (conflicts_of도 반환; task-write 시 redaction 통과). 노드 본문 부재(불변식 5)는 neighbors
    # 경로 test_graph_endpoint_returns_graph_no_body가 고정. 여기선 노드 props가 slug/title/메타만임을
    # 확인한다(claim/page/source 본문 키 부재). K9.2 additive wire 필드(mention_count/entity_kind,
    # 비-Entity 노드는 None)는 메타라 허용 — 본문 키(definition/overview/markdown)는 여전히 부재.
    for n in body["nodes"]:
        assert set(n) <= {
            "id",
            "label",
            "slug",
            "title",
            "materialized",
            "scope",
            "is_own_personal",
            "mention_count",
            "entity_kind",
        }


def test_k8_page_graph_skips_page_conflicts_when_no_claim_neighbor(kg_env, user_id):
    """코드리뷰 #7 — claim 이웃이 없고 truncate도 안 된 페이지는 두 번째 게이트 호출
    (page_conflicts)을 생략한다(불필요한 Neo4j 왕복/`kg_query_runs` row 제거). claim 있는
    페이지는 정상 호출한다. `kg_query_runs.template_name`으로 호출/생략을 단언한다."""
    _seed_company(user_id)
    _write_page(user_id, "lonely-page")  # backlink 없음 → claim 이웃 없음
    _write_page(user_id, "page-conf", backlinks=["claim-a"])  # claim-a는 _seed_company 충돌 claim
    run_rebuild()
    # (1) claim 없는 페이지: neighbors만 돌고 page_conflicts는 생략
    assert client.get("/wiki/pages/lonely-page/graph", params={"depth": 1}).status_code == 200
    runs = [row["template_name"] for row in _runs()]
    assert "neighbors" in runs
    assert "page_conflicts" not in runs
    # (2) claim 있는 페이지: page_conflicts 호출
    assert client.get("/wiki/pages/page-conf/graph", params={"depth": 1}).status_code == 200
    assert "page_conflicts" in [row["template_name"] for row in _runs()]


def test_k8_conflict_status_resolved_then_reopened(kg_env, user_id):
    """K8.4 — conflict task.resolved가 CONFLICTS_WITH status로 양방향 반영된다(resolve→RESOLVED,
    reopen→UNRESOLVED). rebuild가 status 수렴 권위(outbox도 같은 _build_conflict_index 경로)."""
    _write_claim(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    _write_conflict_task(user_id, "task-c", ["claim-a", "claim-b"], resolved=True)
    run_rebuild()
    r1 = run_kg_template(user_id=user_id, template_name="conflicts_of", params={"slug": "claim-a"})
    e1 = next(e for e in r1.edges if e.rel == "CONFLICTS_WITH")
    assert e1.properties["status"] == "RESOLVED"
    # reopen
    _write_conflict_task(user_id, "task-c", ["claim-a", "claim-b"], resolved=False)
    run_rebuild()
    r2 = run_kg_template(user_id=user_id, template_name="conflicts_of", params={"slug": "claim-a"})
    e2 = next(e for e in r2.edges if e.rel == "CONFLICTS_WITH")
    assert e2.properties["status"] == "UNRESOLVED"


def test_k8_merge_graph_union_overflow_sets_truncated():
    """K8.2 (코드리뷰 #7) — `_merge_graph`는 cross-call truncated의 단일 소유자다. 병합 노드
    수가 cap을 넘으면 overflow=True를 반환해 endpoint가 truncated에 OR한다 — 각 sub-call이
    개별적으로는 cap 이하여도 union이 넘으면 부분 결과임을 표시한다(false 'complete' 방지).
    dedup으로 겹치는 노드는 이중계수되지 않는다. Neo4j 불필요한 순수 단위."""
    from orthus.api.routes.wiki import _merge_graph
    from orthus.schemas.canonical import KgGraphNode

    def _n(i):
        return KgGraphNode(id=f"id-{i}", label="WikiPage", slug=f"s-{i}", materialized=True)

    # 각 sub-call(2개 노드)은 cap=3 이하지만 union(4)은 초과 → overflow.
    merged_nodes, _e, overflow = _merge_graph([_n(1), _n(2)], [], [_n(3), _n(4)], [], cap=3)
    assert len(merged_nodes) == 4 and overflow is True
    # union이 cap 이하면 overflow=False(과경고 금지).
    _mn, _e2, no_overflow = _merge_graph([_n(1), _n(2)], [], [_n(3)], [], cap=3)
    assert no_overflow is False
    # dedup — 같은 (label, scope, id)는 이중계수되지 않아 overflow로 오판하지 않는다.
    dn, _e3, dedup_overflow = _merge_graph([_n(1), _n(2)], [], [_n(1), _n(2)], [], cap=3)
    assert len(dn) == 2 and dedup_overflow is False


def test_k9_build_entity_teaser_suppresses_person():
    """K9.2 (D-TZ B) — 펼침-전 티저는 비-person 엔티티명만(person 억제 = person-forward 기본 뷰
    회피, §0 비목표). person-only 페이지는 page_count만(옵션 A fallback). 다리 없으면 None. 순수 단위."""
    from orthus.api.routes.wiki import _build_entity_teaser
    from orthus.schemas.canonical import KgGraphNode as N

    def _page(slug):
        return N(id=slug, label="WikiPage", slug=slug, materialized=True)

    def _ent(name, kind, mc):
        return N(id=f"e:{name}", label="Entity", title=name, entity_kind=kind, mention_count=mc)

    # 혼합 — 비-person(Orbit)만 노출, person(김대표) 억제. 희소 우선(mention_count 오름차순).
    nodes = [
        _page("page-a"),
        _ent("Orbit", "project", 2),
        _ent("Acme", "org", 9),
        _ent("김대표", "person", 1),
        _page("page-b"),
    ]
    t = _build_entity_teaser("page-a", nodes)
    assert t is not None
    assert t.page_count == 1  # start(page-a) 제외한 이웃 page 수
    assert "김대표" not in t.entity_names  # person 억제(D-TZ B 회귀)
    assert t.entity_names[0] == "Orbit"  # 희소(mc=2)가 Acme(mc=9)보다 앞
    assert t.person_only is False

    # person-only 페이지 — names 비고 page_count만(옵션 A).
    person_only = [_page("page-a"), _ent("김대표", "person", 1), _page("page-b")]
    t2 = _build_entity_teaser("page-a", person_only)
    assert t2 is not None and t2.entity_names == [] and t2.person_only is True

    # 이웃 page 없음 → 티저 None(calm 빈 상태 U3).
    assert _build_entity_teaser("page-a", [_page("page-a")]) is None


# --- K9 entity_neighbors (company_always — 비명시 cross-page 발견) -------------------


def _seed_entity_bridge(user_id) -> dict[str, uuid.UUID]:
    """page-a, page-b가 project:Orbit 엔티티를 공유 — entity_neighbors 다리의 최소 그래프.
    persist_entities를 page마다 호출해 같은 엔티티 mention을 양쪽에 단다(mention_count page-only=2)."""
    pa = _write_page(user_id, "page-a")
    pb = _write_page(user_id, "page-b")
    for pid, slug in ((pa, "page-a"), (pb, "page-b")):
        persist_entities(
            [ExtractedEntity(kind="project", name="Orbit")],
            page_id=pid,
            slug=slug,
            scope="company",
            user_id=user_id,
        )
    return {"page_a": pa, "page_b": pb}


def test_entity_neighbors_bridge(kg_env, user_id):
    """page-a가 공유 엔티티(Orbit)를 매개로 page-b와 엮인 다리를 반환한다 — 검색이 못 주는
    비명시 cross-page 연결. 매개 :Entity 노드(C-B) + MENTIONED_IN 다리 엣지(RETURN path 보존)."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="entity_neighbors", params={"slug": "page-a"}
    )
    assert result.status is KgQueryStatus.OK
    assert any(n.label == "Entity" for n in result.nodes)  # 매개 엔티티(다리, C-B)
    slugs = {n.slug for n in result.nodes if n.slug}
    assert "page-b" in slugs  # 공유 엔티티로 엮인 다른 페이지
    # 다리 엣지(MENTIONED_IN) 보존 — bare RETURN p,e,q면 엣지 소실(C-B/RETURN path).
    assert any(e.rel == "MENTIONED_IN" for e in result.edges)
    # R5 — company 경계: 모든 노드 scope=='company'(must-be-zero보다 강한 명시 단정).
    assert all(n.scope == "company" for n in result.nodes)
    assert result.boundary_tripwire_drops == 0


def test_entity_neighbors_params_logged_slug_only(kg_env, user_id):
    """PII — entity_neighbors는 slug-anchored라 kg_query_runs params가 slug만(이름 없음).
    /ask entity intent(K9.3a)의 name_norm 로그 drop과 달리 여기는 애초에 PII 무관."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    run_kg_template(user_id=user_id, template_name="entity_neighbors", params={"slug": "page-a"})
    runs = [r for r in _runs() if r["template_name"] == "entity_neighbors"]
    assert runs and set(runs[-1]["params_redacted"]) == {"slug"}


def test_entity_neighbors_hub_threshold_is_bound_param(kg_env, user_id, monkeypatch):
    """R9 — $hub_threshold가 bound $param이라 rebuild 없이 튜닝된다(C-E). 공유 엔티티의
    mention_count(page-only)=2: threshold=2면 2>=2로 다리 제외(빈 다리), threshold=3이면 포함.
    두 호출 사이 rebuild 없음 — threshold만 바뀌어 결과가 갈리는 게 핵심 단정."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    settings = get_settings()
    monkeypatch.setattr(settings, "kg_entity_hub_threshold", 2, raising=False)
    excluded = run_kg_template(
        user_id=user_id, template_name="entity_neighbors", params={"slug": "page-a"}
    )
    assert excluded.status is KgQueryStatus.OK
    assert "page-b" not in {n.slug for n in excluded.nodes if n.slug}  # hub 제외
    monkeypatch.setattr(settings, "kg_entity_hub_threshold", 3, raising=False)
    included = run_kg_template(
        user_id=user_id, template_name="entity_neighbors", params={"slug": "page-a"}
    )
    assert "page-b" in {n.slug for n in included.nodes if n.slug}  # 같은 그래프, threshold만 ↑


def test_entity_neighbors_served_under_owner_scope(kg_env, user_id, monkeypatch):
    """company_always — owner-scope ON에서도 서빙된다(deferred_template/owner_scope_required
    reject 아님). entity는 company-only data라 owner-fork 미진입($caller 미바인딩, 경계 누수 없음)."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    settings = get_settings()
    monkeypatch.setattr(settings, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(settings, "owner_scope_enabled", True, raising=False)
    result = run_kg_template(
        user_id=user_id, template_name="entity_neighbors", params={"slug": "page-a"}
    )
    assert result.status is KgQueryStatus.OK
    assert result.reject_reason is None
    assert "page-b" in {n.slug for n in result.nodes if n.slug}
    assert all(n.scope == "company" for n in result.nodes)


def test_entity_neighbors_unknown_slug_rejected(kg_env, user_id):
    """company 선검증 — 존재하지 않는 slug는 slug_not_found(company-only 경계, 그래프 스캔 차단)."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="entity_neighbors", params={"slug": "definitely-absent"}
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "slug_not_found:definitely-absent"


# --- K9.3a entity_mentions (/ask entity intent — entity-rooted star) -----------------


def test_entity_mentions_returns_star(kg_env, user_id):
    """K9.3a/b — name_norm으로 엔티티 중심 star(엔티티 + 언급 page들 + MENTIONED_IN 다리)를
    반환한다. page nodes는 /ask grounding 출처이자 K9.3b 시각 star의 spoke다. `RETURN e, m, n`의
    `m`이 명시 반환이라 MENTIONED_IN 다리가 보존된다(entity_neighbors의 RETURN path와 다른 이유)."""
    _seed_entity_bridge(user_id)  # project:Orbit → page-a, page-b가 공유
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="entity_mentions", params={"name_norm": "orbit"}
    )
    assert result.status is KgQueryStatus.OK
    assert any(n.label == "Entity" for n in result.nodes)  # 중심 엔티티
    slugs = {n.slug for n in result.nodes if n.slug}
    assert {"page-a", "page-b"} <= slugs  # 언급 page들(star spoke)
    assert any(e.rel == "MENTIONED_IN" for e in result.edges)  # 다리(m 명시 RETURN 보존)
    assert all(n.scope == "company" for n in result.nodes)
    assert result.boundary_tripwire_drops == 0


def test_entity_mentions_name_norm_dropped_from_log(kg_env, user_id):
    """K9.3a (U6) — name_norm은 사람/조직명(Direct PII)이라 kg_query_runs.params_redacted에서
    완전히 drop된다(log_drop_params). redact_pii는 이메일/전화만 지우고 이름은 남기므로, 키 자체를
    제거하는 게 유일한 안전책이다. slug-anchored entity_neighbors(slug만 기록)와 대비되는 핵심 회귀."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    run_kg_template(
        user_id=user_id, template_name="entity_mentions", params={"name_norm": "orbit"}
    )
    runs = [r for r in _runs() if r["template_name"] == "entity_mentions"]
    assert runs
    assert "name_norm" not in (runs[-1]["params_redacted"] or {})
    assert "orbit" not in str(runs[-1]["params_redacted"])


def test_entity_mentions_served_under_owner_scope(kg_env, user_id, monkeypatch):
    """company_always — owner-scope ON에서도 서빙된다(deferred_template reject 아님). entity는
    company-only data라 owner-fork 미진입($caller 미바인딩, 경계 누수 없음)."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    settings = get_settings()
    monkeypatch.setattr(settings, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(settings, "owner_scope_enabled", True, raising=False)
    result = run_kg_template(
        user_id=user_id, template_name="entity_mentions", params={"name_norm": "orbit"}
    )
    assert result.status is KgQueryStatus.OK
    assert result.reject_reason is None
    assert all(n.scope == "company" for n in result.nodes)


def test_entity_mentions_unknown_name_empty(kg_env, user_id):
    """존재하지 않는 name_norm → OK + 빈 결과(게이트 reject 아님 — name_norm은 slug 선검증 대상이
    아니다). /ask는 bind 단계에서 kg_entities 조회로 미존재를 걸러 호출 자체를 안 하지만, 게이트는
    빈 star를 정직히 돌려주고 호출자가 no_groundable_pages로 demote한다."""
    _seed_entity_bridge(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id, template_name="entity_mentions", params={"name_norm": "no-such-entity"}
    )
    assert result.status is KgQueryStatus.OK
    assert result.nodes == []


def test_graph_endpoint_entity_teaser_and_expand(kg_env, user_id):
    """K9.2 — 패널 그래프 endpoint: presence-gated entity 티저(기본 응답, person 억제 D-TZ B) +
    include=entity 펼침 시 entity 노드/엣지 합류. 기본(펼침 전)은 entity 노드 미합류(U2)."""
    from orthus.kg.client import close_kg_driver

    _seed_entity_bridge(user_id)  # page-a/page-b가 project:Orbit 공유
    run_rebuild()
    close_kg_driver()  # entities_present 캐시 무효화(이전 테스트의 빈-그래프 음성 캐시 제거)
    # 기본 — 티저는 실리되 entity 노드는 패널에 미합류(U2 펼침 전).
    r = client.get("/wiki/pages/page-a/graph", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    teaser = body["entity_teaser"]
    assert teaser is not None
    assert teaser["page_count"] == 1  # page-b
    assert teaser["entity_names"] == ["Orbit"]  # 비-person 다리(person 억제)
    assert teaser["person_only"] is False
    assert not any(n["label"] == "Entity" for n in body["nodes"])  # 펼침 전 미합류
    # 펼침 — include=entity면 entity 노드/엣지 합류 + wire 필드(mention_count/entity_kind/title).
    r2 = client.get(
        "/wiki/pages/page-a/graph",
        params={"include": "entity"},
        headers={"X-User-Id": str(user_id)},
    )
    b2 = r2.json()
    ent_nodes = [n for n in b2["nodes"] if n["label"] == "Entity"]
    assert ent_nodes
    e = ent_nodes[0]
    assert e["title"] == "Orbit" and e["entity_kind"] == "project" and e["mention_count"] == 2
    assert "page-b" in {n["slug"] for n in b2["nodes"] if n.get("slug")}
    assert any(edge["rel"] == "MENTIONED_IN" for edge in b2["edges"])
    assert b2["entity_teaser"] is not None  # 펼침 응답에도 티저 동봉


# --- E1b expand 게이트 reject 회귀 + happy-path + endpoint ------------------------------


def _page_id_of(slug: str, kind: str = "page") -> uuid.UUID:
    with session() as s:
        row = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == slug, wiki_pages.c.kind == kind)
        ).first()
    return row[0]


def test_expand_rejects_unknown_label(kg_env, user_id):
    """§8 #1 — Literal이 unknown label을 stage-3 invalid_params:label로 막는다. driver 미도달,
    kg_query_runs 1행."""
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "Nope", "id": str(uuid.uuid4())},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "invalid_params:label"
    assert len(_runs()) == 1


def test_expand_rejects_missing_id(kg_env, user_id):
    """§8 #2 — 존재하지 않는 UUID → entity_or_node_not_found(404 재료). kg_query_runs 1행."""
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(uuid.uuid4())},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "entity_or_node_not_found"
    assert len(_runs()) == 1


def test_expand_rejects_injection_string_id(kg_env, user_id):
    """§8 #3 — 비-UUID injection 문자열 → precheck UUID 파싱 실패 → entity_or_node_not_found
    (driver 미도달) + kg_query_runs에 write 흔적 없음(전부 rejected/read)."""
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "WikiPage", "id": "x') MATCH (n) DETACH DELETE n //"},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "entity_or_node_not_found"
    runs = _runs()
    assert all(r["status"] == "rejected" for r in runs)


def test_expand_entity_rejects_missing(kg_env, user_id):
    """존재하지 않는 entity_key → entity_or_node_not_found(company scope precheck 실패)."""
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_entity",
        params={"entity_key": "person:ghost"},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "entity_or_node_not_found"


def test_expand_entity_reject_drops_person_name_from_log(kg_env, user_id):
    """reject(entity_or_node_not_found) 경로도 성공 경로와 동일하게 log_drop_params를 적용해
    person-name entity_key를 kg_query_runs에서 완전히 drop한다. redact_pii는 한글 이름을 지우지
    못하므로 키 제거가 유일한 방어다 — erase된 Entity 노드 더블클릭 확장 not-found가 현실 트리거
    (person-entity redaction carve-out/K7.5 erasure 회귀)."""
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_entity",
        params={"entity_key": "person:김대표비밀"},
    )
    assert result.status is KgQueryStatus.REJECTED
    assert result.reject_reason == "entity_or_node_not_found"
    runs = [r for r in _runs() if r["template_name"] == "expand_entity"]
    assert runs
    assert "entity_key" not in (runs[-1]["params_redacted"] or {})
    assert "김대표비밀" not in str(runs[-1]["params_redacted"])


def test_expand_wikipage_1hop(kg_env, user_id):
    """§6.5 — WikiPage 노드 1-hop: page-x 이웃(source-s + dangling backlink) 반환, rel∈지식 rel."""
    _seed_company(user_id)
    run_rebuild()
    pid = _page_id_of("page-x")
    result = run_kg_template(
        user_id=user_id, template_name="expand_node", params={"label": "WikiPage", "id": str(pid)}
    )
    assert result.status is KgQueryStatus.OK
    assert result.nodes  # 최소 1-hop 이웃
    knowledge = {"SUPPORTS", "CONFLICTS_WITH", "BACKLINK", "DERIVED_FROM", "EXTRACTED_FROM"}
    assert all(e.rel in knowledge for e in result.edges)
    # 시작 노드 page-x가 path에 포함된다.
    assert str(pid) in {n.id for n in result.nodes}


def test_expand_document_1hop(kg_env, user_id):
    """Document 노드 1-hop: EXTRACTED_FROM 경유 source 반환."""
    ids = _seed_company(user_id)
    run_rebuild()
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "Document", "id": str(ids["doc_id"])},
    )
    assert result.status is KgQueryStatus.OK
    assert any(e.rel == "EXTRACTED_FROM" for e in result.edges)


def test_expand_entity_1hop(kg_env, user_id):
    """Entity 노드 1-hop(MENTIONED_IN) — endpoint가 namespaced id를 넘긴 뒤 gate가 unprefixed로
    PG 확인 + namespaced로 Cypher 바인딩. 언급 페이지 반환(company-only)."""
    from orthus.kg.client import close_kg_driver
    from orthus.kg.visibility import COMPANY_NS, entity_node_key_for

    _seed_entity_bridge(user_id)
    run_rebuild()
    close_kg_driver()
    node_id = entity_node_key_for(COMPANY_NS, "project:orbit")
    # endpoint는 namespaced node_id에서 접두를 떼어 넘기므로, gate 입력은 unprefixed.
    from orthus.kg.visibility import entity_key_from_node_id

    result = run_kg_template(
        user_id=user_id,
        template_name="expand_entity",
        params={"entity_key": entity_key_from_node_id(node_id)},
    )
    assert result.status is KgQueryStatus.OK
    slugs = {n.slug for n in result.nodes if n.slug}
    assert "page-a" in slugs and "page-b" in slugs
    assert any(e.rel == "MENTIONED_IN" for e in result.edges)


def test_expand_truncated(kg_env, user_id, monkeypatch):
    """§6.5 — kg_expand_limit=1로 monkeypatch → truncated=true(effective_limit로 clamp)."""
    _seed_company(user_id)
    run_rebuild()
    monkeypatch.setattr(get_settings(), "kg_query_limit", 1, raising=False)
    pid = _page_id_of("page-x")
    result = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
        limit=1,
    )
    assert result.status is KgQueryStatus.OK
    assert result.truncated is True


def test_expand_tripwire_zero(kg_env, user_id):
    """정상 확장에서 boundary_tripwire_drops==0(must-be-zero)."""
    _seed_company(user_id)
    run_rebuild()
    pid = _page_id_of("page-x")
    result = run_kg_template(
        user_id=user_id, template_name="expand_node", params={"label": "WikiPage", "id": str(pid)}
    )
    assert result.status is KgQueryStatus.OK
    assert result.boundary_tripwire_drops == 0


def test_graph_expand_endpoint_no_body(kg_env, user_id):
    """§6.6 — GET /wiki/graph/expand: 200, 불변식 5(본문 없음), Cache-Control private, no-store."""
    _seed_company(user_id)
    run_rebuild()
    pid = _page_id_of("page-x")
    r = client.get(
        "/wiki/graph/expand",
        params={"label": "WikiPage", "id": str(pid)},
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["nodes"]
    assert BODY_MARKER not in r.text  # 불변식 5
    assert r.headers["Cache-Control"] == "private, no-store"


def test_graph_expand_endpoint_404_unknown(kg_env, user_id):
    r = client.get(
        "/wiki/graph/expand",
        params={"label": "WikiPage", "id": str(uuid.uuid4())},
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 404


def test_graph_expand_endpoint_404_unknown_label(kg_env, user_id):
    r = client.get(
        "/wiki/graph/expand",
        params={"label": "Nope", "id": str(uuid.uuid4())},
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 404


def test_graph_expand_endpoint_disabled(clean, user_id):
    """flag off는 404보다 먼저 200 supported:false reason=kg_disabled."""
    r = client.get(
        "/wiki/graph/expand",
        params={"label": "WikiPage", "id": str(uuid.uuid4())},
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["reason"] == "kg_disabled"


def test_graph_expand_endpoint_unavailable_fail_open(clean, user_id):
    """dead URI → 200 supported:false(fail-open은 KG 가용성 한정)."""
    settings = get_settings()
    _insert_company_page_row("alive-page")
    pid = _page_id_of("alive-page")
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"
    settings.kg_password = "x"
    try:
        r = client.get(
            "/wiki/graph/expand",
            params={"label": "WikiPage", "id": str(pid)},
            headers={"X-User-Id": str(user_id)},
        )
    finally:
        close_kg_driver()
        settings.kg_enabled = False
        settings.kg_uri = "bolt://127.0.0.1:7687"
        settings.kg_password = ""
    assert r.status_code == 200
    assert r.json()["supported"] is False
