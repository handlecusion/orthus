"""K7.5 (W1) — owner-scope KG erasure (그래프 표시 제거; 실제 PG 삭제는 P8 위임 — Q2).

erase_kg_for_owner는 `scope='personal' AND owner_id=$owner_id` 노드/엣지만 DETACH/DELETE
한다(양방향 격리: A를 지워도 B 불변, company 끝점 노드/엔티티 불변). PG SoR을 안 지우므로
재 rebuild는 노드를 되살리고(pg_copies_pending=True 정직), P8 PG-delete를 시뮬한 fixture만
no-resurrection을 보인다. content-blind(카운트만, slug/title 미read), copy는 ETA/jargon 없음.

neo4j-test(:7688) + orthus_test PG 필요(`make kg-test-up && make test`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert

from orthus.db import session
from orthus.kg import gate
from orthus.kg.client import kg_write_session, run_read
from orthus.kg.erase import (
    _WHAT_REMAINS,
    _erase_owner_graph,
    erase_kg_for_owner,
    erase_kg_for_owners,
    main,
)
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import OwnerErasureReport, WikiPage
from orthus.settings import get_settings
from orthus.tables import users
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


@pytest.fixture
def two_owner_graph(kg_env, user_id, monkeypatch):
    """company page-x/page-y + owner_a 개인 노트 2개(→page-x,page-y) + owner_b 개인 노트
    1개(→page-x). 비대칭(A=2, B=1)이라 cross-detach가 생기면 카운트가 어긋난다. 양 flag on."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()
    with session() as sess:
        sess.execute(insert(users).values(user_id=owner_b, display_name="Owner B"))
        sess.commit()
    _page(owner_a, "page-x", scope="company", owner_id=None)
    _page(owner_a, "page-y", scope="company", owner_id=None)
    _page(owner_a, "a-note-1", scope="personal", owner_id=owner_a, backlinks=["page-x"])
    _page(owner_a, "a-note-2", scope="personal", owner_id=owner_a, backlinks=["page-y"])
    _page(owner_b, "b-note-1", scope="personal", owner_id=owner_b, backlinks=["page-x"])
    run_rebuild()
    return {"owner_a": owner_a, "owner_b": owner_b}


def _personal_node_count(owner_id) -> int:
    recs = run_read(
        "MATCH (n) WHERE n.scope = 'personal' AND n.owner_id = $o RETURN count(n) AS c",
        {"o": str(owner_id)},
    )
    return int(list(recs[0].values())[0]) if recs else 0


def _company_node_count() -> int:
    recs = run_read("MATCH (n) WHERE n.scope = 'company' RETURN count(n) AS c", {})
    return int(list(recs[0].values())[0]) if recs else 0


def _entity_count() -> int:
    recs = run_read("MATCH (e:Entity) RETURN count(e) AS c", {})
    return int(list(recs[0].values())[0]) if recs else 0


def test_k7_owner_erasure_detach_delete(two_owner_graph):
    """양방향 격리 + mixed-endpoint — A의 owner-scope 노드/엣지만 detach. B·company 불변."""
    g = two_owner_graph
    a, b = g["owner_a"], g["owner_b"]
    before_a = _personal_node_count(a)
    before_b = _personal_node_count(b)
    company_before = _company_node_count()
    entity_before = _entity_count()
    assert before_a >= 2 and before_b >= 1  # 비대칭 시드

    # mixed-endpoint defense-in-depth: 양 끝점이 company인데 엣지 자체가 A-owned인 personal
    # 엣지를 수동 생성(현 projection엔 없지만 명시적 sweep가 잡아야 한다). B-owned 엣지도 하나.
    with kg_write_session() as ws:
        ws.run(
            "MATCH (x:WikiPage {slug:'page-x'}), (y:WikiPage {slug:'page-y'}) "
            "CREATE (x)-[:RELATES_TO {scope:'personal', owner_id:$a}]->(y) "
            "CREATE (x)-[:RELATES_TO {scope:'personal', owner_id:$b}]->(y)",
            a=str(a),
            b=str(b),
        ).consume()

    report = erase_kg_for_owner(a, actor_user_id=a)

    assert report.removed_from_graph is True
    assert report.neo4j_available is True
    assert report.nodes_detached == before_a
    assert report.pg_copies_pending is True  # Q2 — PG 삭제는 P8
    # A의 personal 노드/엣지 0, B 불변, company 노드·엔티티 불변.
    assert _personal_node_count(a) == 0
    assert _personal_node_count(b) == before_b
    assert _company_node_count() == company_before
    assert _entity_count() == entity_before
    # mixed-endpoint: A-owned company-끝점 엣지는 sweep로 제거, B-owned는 생존.
    a_mixed = run_read(
        "MATCH (:WikiPage {slug:'page-x'})-[r:RELATES_TO]->(:WikiPage {slug:'page-y'}) "
        "WHERE r.owner_id = $a RETURN count(r) AS c",
        {"a": str(a)},
    )
    b_mixed = run_read(
        "MATCH (:WikiPage {slug:'page-x'})-[r:RELATES_TO]->(:WikiPage {slug:'page-y'}) "
        "WHERE r.owner_id = $b RETURN count(r) AS c",
        {"b": str(b)},
    )
    assert int(list(a_mixed[0].values())[0]) == 0
    assert int(list(b_mixed[0].values())[0]) == 1


def test_k7_back_to_back_owner_erase_isolated(two_owner_graph):
    """A erase 후 B 불변(cross-detach 없음), 이어 B erase하면 B도 0."""
    g = two_owner_graph
    a, b = g["owner_a"], g["owner_b"]
    before_b = _personal_node_count(b)

    erase_kg_for_owner(a, actor_user_id=a)
    assert _personal_node_count(a) == 0
    assert _personal_node_count(b) == before_b  # A erase가 B를 안 건드림

    erase_kg_for_owner(b, actor_user_id=b)
    assert _personal_node_count(b) == 0


def test_k7_erased_owner_rows_not_reprojected_on_rebuild(two_owner_graph):
    """P8 PG-delete를 시뮬한 fixture만 no-resurrection을 보인다(P8 조건부 계약).

    K7.5 erase는 PG SoR을 안 지우므로 그것만으론 rebuild가 노드를 되살린다(정직).
    PG owner-A SoR을 지운 뒤 rebuild해야 owner-A personal 노드가 0으로 수렴한다."""
    g = two_owner_graph
    a = g["owner_a"]
    erase_kg_for_owner(a, actor_user_id=a)
    assert _personal_node_count(a) == 0

    # PG SoR이 남아 있으면 rebuild가 A의 personal 노드를 되살린다(resurrection — 정직).
    run_rebuild()
    assert _personal_node_count(a) >= 2

    # P8 PG-delete 시뮬: owner-A personal wiki page SoR(rows+markdown) 삭제 후 rebuild.
    for slug in ("a-note-1", "a-note-2"):
        wiki_store.delete_item("page", slug, scope="personal", owner_id=a)
    run_rebuild()
    assert _personal_node_count(a) == 0


def test_k7_erase_noop_records_before_after(two_owner_graph):
    """이미 비워진 owner를 다시 erase하면 nodes=0, before/after 모두 0(status=ready)."""
    g = two_owner_graph
    a = g["owner_a"]
    erase_kg_for_owner(a, actor_user_id=a)
    report = erase_kg_for_owner(a, actor_user_id=a)
    assert report.nodes_detached == 0
    assert report.footprint_before.status == "ready"
    assert report.footprint_before.node_count == 0
    assert report.footprint_after.status == "ready"
    assert report.footprint_after.node_count == 0


def test_k7_owner_erase_content_blind(two_owner_graph, monkeypatch):
    """content-blind — erase 경로가 target에 대해 resolve_slug/neighbors(제목 노출 read)를
    호출하지 않는다. 외부 owner 대상에도 0 content read로 동작한다(owner_id/카운트만)."""
    g = two_owner_graph
    a = g["owner_a"]

    def _boom(*args, **kwargs):
        raise AssertionError("content read (resolve_slug/neighbors) must not run during erase")

    monkeypatch.setattr(gate, "resolve_slug", _boom, raising=False)
    monkeypatch.setattr(gate, "run_kg_template", _boom, raising=False)
    report = erase_kg_for_owner(a, actor_user_id=uuid.uuid4())  # admin(다른 actor) 대상 owner a
    assert report.removed_from_graph is True
    assert _personal_node_count(a) == 0


def test_k7_erasure_report_after_count_status_gated(kg_env, user_id, monkeypatch):
    """(O-r1/W2) Neo4j 미가용이면 footprint_after는 status!=ready(0을 성공으로 안 봄),
    removed_from_graph=False. `_erase_owner_graph` 코어가 status를 게이트한다."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    # Neo4j 미가용 시뮬 — gate와 erase 양쪽 kg_available을 False로.
    monkeypatch.setattr(gate, "kg_available", lambda: False)
    monkeypatch.setattr("orthus.kg.erase.kg_available", lambda: False)
    report = _erase_owner_graph(user_id, actor_user_id=user_id)
    assert report.removed_from_graph is False
    assert report.neo4j_available is False
    assert report.footprint_after.status == "unavailable"
    assert report.footprint_after.node_count == 0


def test_k7_erasure_copy_no_jargon_no_eta():
    """(M-r1) 정규 erasure 문구 — '확정 일자 없음'(SLA 날짜 약속 아님), '공유 그래프' jargon 없음."""
    text = _WHAT_REMAINS
    assert "확정 일자는 없습니다" in text
    assert "no committed date" in text.lower()
    # jargon/누출 금지
    assert "공유 그래프" not in text
    assert "shared graph" not in text.lower()
    # 구체적 날짜(예: 2026-..., YYYY-MM-DD)를 약속하지 않는다.
    import re

    assert re.search(r"\d{4}-\d{2}-\d{2}", text) is None


def test_k7_erase_cli_warns_pg_not_deleted(two_owner_graph, capsys):
    """(P-r1) confirm 실행 전 PG-first WARNING이 stderr에 출력된다."""
    g = two_owner_graph
    a = str(g["owner_a"])
    rc = main(["--owner", a, "--confirm", a])
    assert rc == 0
    err = capsys.readouterr().err
    assert "그래프 표시만 제거" in err
    assert "PG-delete-first" in err


def test_k7_erase_cli_owner_mismatch_refused(two_owner_graph, capsys):
    """--confirm이 owner-id와 불일치하면 비가역 실행 거부(abort, exit 2)."""
    g = two_owner_graph
    a = str(g["owner_a"])
    rc = main(["--owner", a, "--confirm", str(uuid.uuid4())])
    assert rc == 2
    # 실제 삭제는 일어나지 않았다.
    assert _personal_node_count(g["owner_a"]) >= 2


def test_k7_erase_cli_dry_run_no_delete(two_owner_graph, capsys):
    """--dry-run은 footprint만 보고 삭제 0."""
    g = two_owner_graph
    a = str(g["owner_a"])
    before = _personal_node_count(g["owner_a"])
    rc = main(["--owner", a, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert _personal_node_count(g["owner_a"]) == before  # 미삭제


def test_k7_batch_owner_erase_single_hold(two_owner_graph):
    """(M-r2) 배치 — N owner를 단일 HOLD에서 순차 erase, counts-only 통합 artifact."""
    g = two_owner_graph
    a, b = g["owner_a"], g["owner_b"]
    reports = erase_kg_for_owners([a, b], actor_user_id=a)
    assert len(reports) == 2
    assert all(isinstance(r, OwnerErasureReport) for r in reports)
    assert _personal_node_count(a) == 0
    assert _personal_node_count(b) == 0
    # closeout artifact는 카운트만 — owner_id 외 식별 내용 없음(2-state report).
    assert all(r.pg_copies_pending is True for r in reports)
