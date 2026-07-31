"""K3 — transactional outbox 통합 회귀 (구현 명세 §5.6).

PG만 필요한 enqueue 회귀와 neo4j-test(:7688)가 필요한 worker 회귀를 나눈다.
핵심 고정 성질: enqueue 원자성(호출자 트랜잭션과 동일 운명), K7 이전 company
경계, `:OutboxApplied` 멱등 replay, dead-letter(5회), lease reclaim, Neo4j
다운 적체→drain(attempts 미증가), rebuild lock 대기, applied/마커 trim.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from orthus.db import session
from orthus.kg import store as kg_store
from orthus.kg.client import KgDisabled, KgUnavailable, close_kg_driver, kg_write_session
from orthus.kg.outbox import (
    KGOutboxWorker,
    drain,
    enqueue_kg_event,
    trim_applied,
)
from orthus.schemas.canonical import WikiClaim, WikiPage, WikiSource, WikiTask
from orthus.settings import get_settings
from orthus.tables import kg_outbox
from orthus.wiki import store as wiki_store
from orthus.wiki.consolidate import consolidate

_DEAD_URI = "bolt://127.0.0.1:1"


@pytest.fixture
def outbox_pg_env(clean, tmp_path):
    """PG만 쓰는 enqueue 테스트 환경 — enqueue는 Neo4j를 절대 건드리지 않는다
    (§2.6). kg_uri를 죽은 포트로 고정해 실수로라도 운영 포트(7687)를 보는 일이
    구조적으로 불가능하게 한다."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    settings.kg_enabled = True
    settings.kg_uri = _DEAD_URI
    settings.kg_password = "unused"
    close_kg_driver()
    yield
    close_kg_driver()
    settings.wiki_store_path = prev_root
    settings.kg_enabled = False
    settings.kg_uri = "bolt://127.0.0.1:7687"
    settings.kg_password = ""


@pytest.fixture
def kg_env(clean, kg_clean, tmp_path):
    """worker 테스트 환경 — neo4j-test + per-test wiki-store 격리
    (test_kg_projection.py의 kg_env와 동형; fixture 순서가 계약이다)."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


# --- seed helpers -----------------------------------------------------------------


def _write_claim(user_id, slug, *, conflicting=(), scope="company", owner_id=None):
    return wiki_store.write_claim(
        WikiClaim(
            slug=slug,
            claim=f"주장 {slug}",
            supporting=[],
            conflicting=list(conflicting),
            confidence="high",
            last_reviewed=date(2026, 6, 1),
            evidence="근거",
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _write_page(user_id, slug, *, scope="company", owner_id=None, backlinks=()):
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition="정의",
            overview="개요",
            backlinks=list(backlinks),
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _write_conflict_task(user_id, slug, related, *, resolved=False, scope="company", owner_id=None):
    return wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="conflict",
            description=f"충돌 {slug}",
            related=list(related),
            created_at=datetime.now(UTC),
            resolved=resolved,
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _outbox_rows() -> list:
    with session() as s:
        return s.execute(select(kg_outbox).order_by(kg_outbox.c.enqueued_at)).all()


def _set_all_status(status: str) -> None:
    with session() as s:
        s.execute(update(kg_outbox).values(status=status, lease_until=None))
        s.commit()


def _expire_leases() -> None:
    with session() as s:
        s.execute(update(kg_outbox).values(lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        s.commit()


def _insert_event(
    *, entity_kind="wiki_page", entity_id=None, op="upsert", status="pending", enqueued_at=None
) -> uuid.UUID:
    outbox_id = uuid.uuid4()
    values = {
        "outbox_id": outbox_id,
        "entity_kind": entity_kind,
        "entity_id": entity_id or uuid.uuid4(),
        "op": op,
        "status": status,
    }
    if enqueued_at is not None:
        values["enqueued_at"] = enqueued_at
    with session() as s:
        s.execute(insert(kg_outbox).values(**values))
        s.commit()
    return outbox_id


def _graph_snapshot(driver) -> tuple[list, list]:
    """KgMeta/OutboxApplied(동기화 메타) 제외 전수 snapshot — replay 멱등 검증용."""
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


def _node_slugs(driver, label: str) -> set[str]:
    with driver.session(database="neo4j") as s:
        return {r["slug"] for r in s.run(f"MATCH (n:{label}) RETURN n.slug AS slug")}


# --- enqueue (PG only) -------------------------------------------------------------


def test_outbox_enqueue_in_same_tx(outbox_pg_env):
    """호출자 트랜잭션과 원자적 — rollback이면 이벤트도 사라진다(§5.6)."""
    with session() as s:
        enqueue_kg_event(s, entity_kind="document", entity_id=uuid.uuid4(), scope="company")
        s.rollback()
    assert _outbox_rows() == []

    with session() as s:
        enqueue_kg_event(s, entity_kind="document", entity_id=uuid.uuid4(), scope="company")
        s.commit()
    rows = _outbox_rows()
    assert len(rows) == 1
    assert (rows[0].status, rows[0].op, rows[0].attempts) == ("pending", "upsert", 0)


def test_outbox_enqueue_company_only(outbox_pg_env, user_id):
    """personal 쓰기 → 이벤트 0건 — K7 이전 경계(projection 필터와 동일, §5.2)."""
    _write_page(user_id, "personal-page", scope="personal", owner_id=user_id)
    assert _outbox_rows() == []

    page_id = _write_page(user_id, "company-page")
    rows = _outbox_rows()
    assert [(r.entity_kind, r.entity_id) for r in rows] == [("wiki_page", page_id)]


def test_outbox_enqueue_noop_when_disabled(outbox_pg_env, user_id):
    """flag off 기간의 변경은 적재하지 않는다 — 켤 때 rebuild 1회가 운영 절차(§5.2)."""
    get_settings().kg_enabled = False
    _write_page(user_id, "company-page")
    assert _outbox_rows() == []


def test_document_upsert_enqueues_company_only(outbox_pg_env, user_id):
    """upsert_source_document hook — promote approve가 지나는 자체 commit 세션(§5.2)."""
    from orthus.documents import upsert_source_document
    from orthus.schemas.canonical import InternalDocument

    def _doc(external_id: str) -> InternalDocument:
        return InternalDocument(
            title="문서",
            block_json=[],
            markdown="본문",
            source="notion",
            source_external_id=external_id,
            project="atlas",
        )

    doc_id, _ = upsert_source_document(
        user_id, _doc("ext-1"), scope="company", defer_authoring=True
    )
    rows = _outbox_rows()
    assert [(r.entity_kind, r.entity_id) for r in rows] == [("document", doc_id)]

    upsert_source_document(user_id, _doc("ext-2"), scope="personal", defer_authoring=True)
    assert len(_outbox_rows()) == 1  # personal은 enqueue 없음


def test_worker_lease_reclaim(outbox_pg_env):
    """lease 동안 재claim 불가, 만료 후 재claim 가능(§5.6)."""
    outbox_id = _insert_event()
    worker = KGOutboxWorker(poll_seconds=0)

    first = worker.claim()
    assert [r.outbox_id for r in first] == [outbox_id]
    assert worker.claim() == []  # leased

    with session() as s:
        s.execute(
            update(kg_outbox)
            .where(kg_outbox.c.outbox_id == outbox_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        s.commit()
    reclaimed = worker.claim()
    assert [r.outbox_id for r in reclaimed] == [outbox_id]


def test_run_once_refuses_on_personal_node(outbox_pg_env):
    """projection 쓰기 경로는 company node 전용 — worker 핵심 경로에도
    require_company_node 가드(리뷰 교정; K2 rebuild/sync 선례 동형)."""
    get_settings().node_kind = "personal"
    with pytest.raises(KgDisabled):
        KGOutboxWorker(poll_seconds=0).run_once()
    get_settings().node_kind = "company"


def test_task_event_empty_related_noop(outbox_pg_env, user_id):
    """related가 빈 conflict task 이벤트는 no-op — 가드 없으면 _load_links가
    무필터 전사 링크를 로드한다(리뷰 교정)."""
    from orthus.kg import project

    _write_claim(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    task_page_id = _write_conflict_task(user_id, "conflict-empty", [])
    rows = project.load_one("wiki_page", task_page_id)
    assert rows.links == []
    assert rows.pages == []


def test_trim_unavailable_normalized(outbox_pg_env):
    """Neo4j 미가용 시 trim은 raw driver 예외가 아니라 KgUnavailable —
    CLI fail-loud 계약(§2.6)의 전제(리뷰 교정)."""
    with pytest.raises(KgUnavailable):
        trim_applied()


def test_normalize_unavailable_taxonomy():
    """TransientError(락 경합/deadlock)는 인프라성으로 정규화, 일반 예외는
    이벤트 실패로 남는다(리뷰 교정)."""
    from neo4j.exceptions import TransientError

    from orthus.kg.outbox import _normalize_unavailable

    assert isinstance(_normalize_unavailable(TransientError("deadlock")), KgUnavailable)
    assert isinstance(_normalize_unavailable(KgUnavailable("down")), KgUnavailable)
    assert _normalize_unavailable(RuntimeError("event bug")) is None


def test_worker_thread_start_conditions(outbox_pg_env):
    """lifespan 기동 조건(§5.3): kg_enabled + company node일 때만 thread 기동.

    uvicorn reload의 감시용 부모는 lifespan을 실행하지 않으므로 이 함수가
    serving 프로세스 한정 기동의 단일 지점이다(§12)."""
    from orthus.kg.outbox import start_outbox_worker_thread

    settings = get_settings()
    settings.kg_enabled = False
    assert start_outbox_worker_thread() is None

    settings.kg_enabled = True
    settings.node_kind = "personal"
    assert start_outbox_worker_thread() is None

    settings.node_kind = "company"
    handle = start_outbox_worker_thread()
    assert handle is not None
    thread, stop = handle
    assert thread.is_alive()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    # 코드리뷰 #6 — L3 force-disable이면 settings.kg_enabled=True여도 worker가 안 뜬다
    # (기동 판정이 kg_enabled()를 봐서 _kg_force_disabled를 AND). read+write 동시 차단.
    from orthus.kg.client import force_disable_kg, reset_kg_force_disabled

    try:
        force_disable_kg()
        assert start_outbox_worker_thread() is None
    finally:
        reset_kg_force_disabled()


# --- worker (neo4j-test) ------------------------------------------------------------


def test_worker_apply_idempotent_replay(kg_env, user_id):
    """같은 이벤트 2회 claim → 그래프 무변경 — `:OutboxApplied` 마커가 권위(§5.3).

    'worker 죽음(마커 생성 후 PG update 전)' 엣지(§5.5)와 동일 경로: PG 상태를
    pending으로 되돌려 재claim해도 마커 no-op → applied 갱신만 일어난다."""
    _write_claim(user_id, "claim-a")
    _write_page(user_id, "page-x", backlinks=["claim-a"])
    assert drain() > 0
    assert {r.status for r in _outbox_rows()} == {"applied"}
    before = _graph_snapshot(kg_env)

    _set_all_status("pending")  # 재claim 시뮬레이션 — 마커는 Neo4j에 남아 있다
    assert drain() > 0
    assert {r.status for r in _outbox_rows()} == {"applied"}
    assert _graph_snapshot(kg_env) == before


def test_outbox_marker_unique_constraint_enforced(kg_env, user_id):
    """`:OutboxApplied` UNIQUE가 테스트 그래프에도 부트되어 중복 마커 CREATE를
    DB-level로 막는다(conftest.kg_clean의 apply_schema) — 멱등이 app-level 가드
    단독이 아니라 graph constraint로도 보장됨을 고정.

    동시 double-apply(worker + CLI drain이 lease 만료 직후 겹침)에서 한쪽이
    이미 마커를 CREATE한 뒤 다른 쪽이 같은 outbox_id로 CREATE하면 ConstraintError가
    난다 — 이는 `_normalize_unavailable`이 처리 못 하는 일반 예외가 아니라 멱등
    위반 신호이며, MATCH guard가 정상 경로를 막아 실제론 도달하지 않는다."""
    import neo4j.exceptions

    page_id = _write_page(user_id, "page-marker")
    assert drain() > 0
    with kg_env.session(database="neo4j") as s:
        outbox_id = s.run("MATCH (o:OutboxApplied) RETURN o.outbox_id AS id LIMIT 1").single()["id"]
        with pytest.raises(neo4j.exceptions.ConstraintError):
            s.run(
                "CREATE (:OutboxApplied {outbox_id:$id, applied_at:datetime()})", id=outbox_id
            ).consume()
    assert page_id is not None  # seed sanity

    # 같은 이벤트 재claim → MATCH guard가 단축, 그래프 무변경(constraint와 무관하게 멱등).
    _set_all_status("pending")
    before = _graph_snapshot(kg_env)
    assert drain() > 0
    assert _graph_snapshot(kg_env) == before


def test_worker_dead_letter_after_5(kg_env, monkeypatch):
    """이벤트 실패 5회 → status='dead' + last_error 기록(§5.3).

    실패는 lease를 backoff로 유지하므로(리뷰 교정 — 즉시 5연속 소진 방지)
    같은 poll 사이클에서 재claim되지 않는다 — 각 시도 사이에 lease 만료를
    시뮬레이션한다. run_once는 성공만 집계하므로 항상 0을 반환한다."""
    _insert_event(entity_kind="wiki_page")

    def _boom(entity_kind, entity_id, *, scope_filter=None, cache=None):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr("orthus.kg.project.load_one", _boom)
    worker = KGOutboxWorker(poll_seconds=0)

    assert worker.run_once() == 0  # 실패는 applied로 집계되지 않는다(리뷰 교정)
    first = _outbox_rows()[0]
    assert (first.status, first.attempts) == ("pending", 1)
    assert first.lease_until is not None  # backoff lease 유지 — 즉시 재claim 불가
    assert worker.claim() == []

    for _ in range(4):
        _expire_leases()
        assert worker.run_once() == 0
    rows = _outbox_rows()
    assert len(rows) == 1
    assert rows[0].status == "dead"
    assert rows[0].attempts == 5
    assert "RuntimeError" in rows[0].last_error
    _expire_leases()
    assert worker.run_once() == 0  # dead는 더 이상 claim되지 않는다


def test_consolidate_to_graph_e2e(kg_env, user_id):
    """consolidate → drain → 그래프 반영 — 60초 SLA의 기계 검증판(§5.7: 측정
    구간은 consolidate PG commit → `:OutboxApplied` 생성)."""
    source = WikiSource(
        slug="source-e2e",
        title="source e2e",
        source_type="corpus_doc",
        source_ref=str(uuid.uuid4()),
        ingested_at=datetime.now(UTC),
        summary="요약",
        key_claims=["claim-e2e"],
    )
    claims = [
        WikiClaim(
            slug="claim-e2e",
            claim="e2e 주장",
            supporting=["source-e2e"],
            conflicting=[],
            confidence="high",
            last_reviewed=date(2026, 6, 1),
            evidence="근거",
        )
    ]
    committed_at = time.monotonic()
    consolidate(source, claims, user_id=user_id)
    pending = [r for r in _outbox_rows() if r.status == "pending"]
    assert pending, "consolidate가 outbox 이벤트를 enqueue해야 한다"

    assert drain() >= len(pending)
    elapsed = time.monotonic() - committed_at
    assert {r.status for r in _outbox_rows()} == {"applied"}
    assert "claim-e2e" in _node_slugs(kg_env, "WikiClaim")
    assert "source-e2e" in _node_slugs(kg_env, "WikiSource")
    with kg_env.session(database="neo4j") as s:
        markers = s.run("MATCH (o:OutboxApplied) RETURN count(o) AS c").single()["c"]
    assert markers == len(_outbox_rows())
    assert elapsed < 60, f"60초 SLA 위반: {elapsed:.1f}s"
    print(f"\n[K3 SLA evidence] consolidate commit → graph applied: {elapsed:.2f}s")


def test_neo4j_down_accumulates_then_drains(kg_env, user_id):
    """Neo4j 미가용 → 적체(attempts 미증가, claim 해제) → 복구 후 drain(§2.6).

    CI 공유 컨테이너를 멈추지 않고 죽은 URI 주입으로 시뮬레이션한다(2026-06-12
    결정). 실제 컨테이너 정지/재기동 사이클은 로컬 evidence로 별도 기록."""
    settings = get_settings()
    _write_claim(user_id, "claim-down")
    assert [r.status for r in _outbox_rows()] == ["pending"]

    live_uri = settings.kg_uri
    settings.kg_uri = _DEAD_URI
    close_kg_driver()
    worker = KGOutboxWorker(poll_seconds=0)
    assert worker.run_once() == 0
    rows = _outbox_rows()
    assert (rows[0].status, rows[0].attempts) == ("pending", 0)
    assert rows[0].lease_until is None  # claim 해제 — 재기동 즉시 drain 가능

    settings.kg_uri = live_uri
    close_kg_driver()
    assert drain() == 1
    assert [r.status for r in _outbox_rows()] == ["applied"]
    assert "claim-down" in _node_slugs(kg_env, "WikiClaim")


def test_k7_worker_holds_on_rebuild_in_progress(kg_env, user_id):
    """K7.5 — rebuild **boolean HOLD**(rebuild_in_progress=True) 동안 worker no-op 대기.

    attempts/상태 무변경(§4.7/§5.5). 구 clock-락(`rebuild_lock_until`) 시간 산술을
    boolean HOLD로 교체한 회귀(이전 `test_worker_waits_on_rebuild_lock` 대체)."""
    _write_claim(user_id, "claim-locked")
    with kg_write_session() as s:
        kg_store.set_meta(s, rebuild_in_progress=True)
    worker = KGOutboxWorker(poll_seconds=0)
    assert worker.run_once() == 0
    rows = _outbox_rows()
    assert (rows[0].status, rows[0].attempts, rows[0].lease_until) == ("pending", 0, None)

    # HOLD clear(완료/abort) 후 정상 drain.
    with kg_write_session() as s:
        kg_store.set_meta(s, rebuild_in_progress=False)
    assert drain() == 1
    assert [r.status for r in _outbox_rows()] == ["applied"]


def test_task_resolve_updates_conflict_edge_props(kg_env, user_id):
    """task 이벤트 → 노드 없이 CONFLICTS_WITH 엣지 속성 재SET(§5.4).

    WikiTask resolve(`PATCH /wiki/tasks`)도 `write_task→_persist`를 지나므로
    충돌 해소 상태 변경이 자동으로 그래프에 반영된다(§5.2 실측)."""
    _write_claim(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    _write_conflict_task(user_id, "conflict-ab", ["claim-a", "claim-b"], resolved=False)
    drain()

    def _edge_status():
        with kg_env.session(database="neo4j") as s:
            rec = s.run(
                "MATCH (:WikiClaim {slug:'claim-a'})-[k:CONFLICTS_WITH]-(:WikiClaim {slug:'claim-b'}) "
                "RETURN k.status AS status"
            ).single()
        return rec["status"] if rec else None

    assert _edge_status() == "UNRESOLVED"
    # task에는 그래프 노드가 없다(§4.2)
    with kg_env.session(database="neo4j") as s:
        task_nodes = s.run("MATCH (n {slug:'conflict-ab'}) RETURN count(n) AS c").single()["c"]
    assert task_nodes == 0

    _write_conflict_task(user_id, "conflict-ab", ["claim-a", "claim-b"], resolved=True)
    drain()
    assert _edge_status() == "RESOLVED"


def test_owner_scope_conflict_status_via_outbox_drain(kg_env, user_id, monkeypatch):
    """K8.6 (코드리뷰 test gap) — owner-scope personal conflict status가 **outbox 증분 경로**로도
    양방향 재투영된다. rebuild 경로(test_kg_boundary_matrix.test_k8_6_*)와 달리, personal task
    resolve가 enqueue→drain을 거쳐 `scope_filter_for_event('personal')→COMPANY_PLUS_OWNER` +
    owner-inclusive load_one으로 CONFLICTS_WITH status를 갱신함을 고정한다(증분 경로가 가장
    조용히 깨지기 쉬운 자리 — drain 테스트가 없었다)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    owner_a = user_id
    _write_claim(owner_a, "p-c1", conflicting=["p-c2"], scope="personal", owner_id=owner_a)
    _write_claim(owner_a, "p-c2", scope="personal", owner_id=owner_a)
    _write_conflict_task(
        owner_a, "p-task", ["p-c1", "p-c2"], resolved=False, scope="personal", owner_id=owner_a
    )
    drain()

    def _edge_status():
        with kg_env.session(database="neo4j") as sess:
            rec = sess.run(
                "MATCH (:WikiClaim {slug:'p-c1'})-[k:CONFLICTS_WITH]-(:WikiClaim {slug:'p-c2'}) "
                "RETURN k.status AS status"
            ).single()
        return rec["status"] if rec else None

    assert _edge_status() == "UNRESOLVED"
    # resolve → drain → RESOLVED: owner-ns conflict task가 증분 경로로 읽힌다(default 아님).
    _write_conflict_task(
        owner_a, "p-task", ["p-c1", "p-c2"], resolved=True, scope="personal", owner_id=owner_a
    )
    drain()
    assert _edge_status() == "RESOLVED"


def test_delete_event_detaches_node(kg_env, user_id):
    """op='delete' → DETACH DELETE (K3에 enqueue 원천은 없고 worker 지원만 — §5.2,
    첫 호출처는 K6 erasure). 없는 키는 no-op."""
    page_id = _write_page(user_id, "page-del")
    drain()
    assert "page-del" in _node_slugs(kg_env, "WikiPage")

    _insert_event(entity_kind="wiki_page", entity_id=page_id, op="delete")
    assert drain() == 1
    assert "page-del" not in _node_slugs(kg_env, "WikiPage")
    assert {r.status for r in _outbox_rows()} == {"applied"}


def test_trim_applied_retention(kg_env):
    """30일 경과 applied row(PG)+마커(Neo4j) trim — dead·최근 마커는 보존(§5.5/§4.6)."""
    old = datetime.now(UTC) - timedelta(days=40)
    old_applied = _insert_event(status="applied", enqueued_at=old)
    old_dead = _insert_event(status="dead", enqueued_at=old)
    fresh_applied = _insert_event(status="applied")
    with kg_env.session(database="neo4j") as s:
        s.run(
            "CREATE (:OutboxApplied {outbox_id:$a, applied_at:$old}), "
            "(:OutboxApplied {outbox_id:$b, applied_at:datetime()})",
            a=str(old_applied),
            b=str(fresh_applied),
            old=old,
        ).consume()

    report = trim_applied()
    assert report == {"pg_rows": 1, "markers": 1}
    remaining = {r.outbox_id for r in _outbox_rows()}
    assert remaining == {old_dead, fresh_applied}
    with kg_env.session(database="neo4j") as s:
        marker_ids = {r["id"] for r in s.run("MATCH (o:OutboxApplied) RETURN o.outbox_id AS id")}
    assert marker_ids == {str(fresh_applied)}


# --- K7.5 (W2 보강) — owner-scope row 라운드트립 + schema-version hold integration -------


def test_k7_outbox_row_carries_scope_owner(outbox_pg_env, user_id, monkeypatch):
    """enqueue → claim 라운드트립이 scope='personal' + owner_id를 보존한다(K7.1 unit 확인 후
    integration 고정). owner-scope ON일 때만 personal 이벤트가 적재된다."""
    monkeypatch.setattr("orthus.kg.outbox.kg_owner_scope_enabled", lambda: True)
    pid = uuid.uuid4()
    with session() as s:
        enqueue_kg_event(
            s, entity_kind="wiki_page", entity_id=pid, scope="personal", owner_id=user_id
        )
        s.commit()
    rows = _outbox_rows()
    assert len(rows) == 1
    assert rows[0].scope == "personal"
    assert rows[0].owner_id == user_id
    # company 이벤트는 owner_id NULL로 적재(대칭 확인).
    cid = uuid.uuid4()
    with session() as s:
        enqueue_kg_event(s, entity_kind="wiki_page", entity_id=cid, scope="company")
        s.commit()
    co = [r for r in _outbox_rows() if r.entity_id == cid][0]
    assert co.scope == "company" and co.owner_id is None


def test_outbox_holds_on_schema_version_mismatch(kg_env, user_id):
    """worker는 그래프 KgMeta.kg_schema_version이 코드(KG_SCHEMA_VERSION)와 다르면 hold —
    이벤트를 적용하지 않고 pending으로 release(v2 rebuild 전 v2-shape 노드 적재 차단)."""
    from orthus.kg.schema import KG_SCHEMA_VERSION

    _write_claim(user_id, "claim-ver")
    # 그래프 KgMeta를 mismatch 버전으로 — rebuild가 채운 정상 버전을 덮어쓴다.
    with kg_write_session() as s:
        kg_store.set_meta(s, kg_schema_version=KG_SCHEMA_VERSION + 999)
    worker = KGOutboxWorker(poll_seconds=0)
    assert worker.run_once() == 0  # hold — 적용 0
    rows = _outbox_rows()
    assert (rows[0].status, rows[0].attempts) == ("pending", 0)

    # 버전 정합 복원 → 정상 drain(같은 이벤트 — entity_kind 무관하게 hold가 풀린다).
    with kg_write_session() as s:
        kg_store.set_meta(s, kg_schema_version=KG_SCHEMA_VERSION)
    assert drain() == 1
    assert [r.status for r in _outbox_rows()] == ["applied"]
