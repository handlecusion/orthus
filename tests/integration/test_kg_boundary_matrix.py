"""K7.2 Step 7 — owner-scope boundary matrix (neo4j-test + orthus_test PG).

Neo4j Community에 RLS가 없으므로 **이 매트릭스가 경계 그 자체**다(kg-k7-plan §0).
B1(owner predicate 노드+엣지), B5/B6(foreign≡absent 404 + start-node 가드),
B-admin(role 무바이패스), B-null-caller(null→company-only)를 실 그래프에서 실증한다.

fixture: company page-x/page-z + owner_a 개인 my-note(→page-x,page-z 백링크) +
owner_b 개인 their-note(→page-x). owner-scope flag on으로 rebuild → 개인 노드 투영.
`make kg-test-up && make test`(neo4j-test :7688)에서 실행된다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from sqlalchemy import insert, select

from orthus.db import session
from orthus.kg.gate import KgQueryStatus, run_kg_template
from orthus.kg.rebuild import run_rebuild
from orthus.schemas.canonical import WikiClaim, WikiPage, WikiTask
from orthus.settings import get_settings
from orthus.tables import kg_query_runs, users, wiki_pages
from orthus.wiki import store as wiki_store


def _page_id_of(slug: str, kind: str = "page") -> uuid.UUID:
    with session() as s:
        row = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == slug, wiki_pages.c.kind == kind)
        ).first()
    return row[0]


def _make_user() -> uuid.UUID:
    """실 user 행 생성 — write 경로(embeddings.user_id FK)를 타는 2번째 owner용. bare uuid는
    비-owner CALLER로만 쓸 수 있다(데이터를 쓰면 FK 위반)."""
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner B"))
        s.commit()
    return uid


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
def owner_graph(kg_env, user_id, monkeypatch):
    """company page-x/page-z + owner_a 개인 my-note(→page-x,page-z). owner_b/admin은
    **데이터를 쓰지 않는 비-owner caller**(bare uuid) — 경계 증명에는 foreign 노드 1개
    (owner_a의 my-note)와 그걸 못 보는 caller들이면 충분하다. owner의 '자기 개인 노트
    가시' 증명은 owner_a 케이스가 이미 한다."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    # 풀 스위트(neo4j-test를 장시간 wipe/rebuild로 혹사) 끝물에서 무거운 owner-variant 순회가
    # 기본 2s 테스트 타임아웃을 간헐적으로 넘겨 OK 경로가 TIMEOUT으로 깨졌다(코드리뷰 flakiness;
    # 단독 실행은 통과). 경계 검증의 목적은 정확성이므로 타임아웃 헤드룸을 넉넉히 준다 — 게이트
    # 로직/누출과 무관한 부하-타이밍 보정이다(owner-variant 쿼리 perf 측정은 K7.5 활성화 전 별도).
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()  # 비-owner caller (role 없음 — gate predicate는 role 무시, B-admin)
    admin = uuid.uuid4()  # 동일하게 비-owner caller
    _page(owner_a, "page-x", scope="company", owner_id=None)
    _page(owner_a, "page-z", scope="company", owner_id=None)
    _page(owner_a, "my-note", scope="personal", owner_id=owner_a, backlinks=["page-x", "page-z"])
    run_rebuild()
    return {"owner_a": owner_a, "owner_b": owner_b, "admin": admin}


def _neighbors(caller, slug, depth=1):
    return run_kg_template(
        user_id=caller, template_name="neighbors", params={"slug": slug, "depth": depth}
    )


def _slugs(result):
    return {n.slug for n in result.nodes}


# --- B1: owner predicate on nodes ----------------------------------------------


def test_k7_owner_sees_own_personal_neighbor(owner_graph):
    g = owner_graph
    r = _neighbors(g["owner_a"], "page-x")
    assert r.status is KgQueryStatus.OK
    assert "my-note" in _slugs(r)  # owner의 자기 개인 노트가 page-x 이웃으로 보임
    mine = next(n for n in r.nodes if n.slug == "my-note")
    assert mine.scope == "personal" and mine.is_own_personal is True
    px = next(n for n in r.nodes if n.slug == "page-x")
    assert px.scope == "company" and px.is_own_personal is False
    # wire에 owner_id 필드 자체가 없다(구조적).
    assert not hasattr(mine, "owner_id")


def test_k7_non_owner_no_foreign_personal_neighbor(owner_graph):
    """B1 — owner_b(비-owner)는 owner_a의 my-note를 절대 못 본다(company-only)."""
    g = owner_graph
    r = _neighbors(g["owner_b"], "page-x")
    assert r.status is KgQueryStatus.OK
    assert "my-note" not in _slugs(r)
    # 누설된 foreign personal 노드가 하나도 없어야 한다(전부 company).
    assert all(n.scope == "company" for n in r.nodes)
    assert all(not (n.scope == "personal" and not n.is_own_personal) for n in r.nodes)


def test_k7_admin_no_role_bypass(owner_graph):
    """B-admin — admin(role 없는 제3 유저)은 어떤 개인 노트도 못 본다(company-only)."""
    g = owner_graph
    r = _neighbors(g["admin"], "page-x")
    assert r.status is KgQueryStatus.OK
    assert all(n.scope == "company" for n in r.nodes)
    assert "my-note" not in _slugs(r) and "their-note" not in _slugs(r)


def test_k7_null_caller_sees_company_only(owner_graph):
    """B-null-caller — caller None(미인증/demo/collector)은 company-only."""
    r = _neighbors(None, "page-x")
    assert r.status is KgQueryStatus.OK
    assert all(n.scope == "company" for n in r.nodes)
    assert "my-note" not in _slugs(r) and "their-note" not in _slugs(r)


# --- B1 marquee: path leak dropped-not-masked ----------------------------------


def test_k7_path_leak_dropped_not_masked(owner_graph):
    """company A — personal X(owner) — company B: owner는 X 경유 경로를 받고,
    non-owner/admin/null은 X를 지나는 경로를 못 받는다(drop-not-leak)."""
    g = owner_graph
    own = run_kg_template(
        user_id=g["owner_a"],
        template_name="path_between",
        params={"slug_a": "page-x", "slug_b": "page-z"},
    )
    assert own.status is KgQueryStatus.OK
    assert "my-note" in _slugs(own)  # owner: 자기 개인 노드를 지나는 경로
    # 자기 개인 EDGE도 실제 순회됨(rel 절이 green-by-vacuity가 아님).
    assert any(e.scope == "personal" for e in own.edges)

    for caller in (g["owner_b"], g["admin"], None):
        r = run_kg_template(
            user_id=caller,
            template_name="path_between",
            params={"slug_a": "page-x", "slug_b": "page-z"},
        )
        assert r.status is KgQueryStatus.OK
        assert "my-note" not in _slugs(r)  # foreign personal hop 미노출
        assert all(n.scope == "company" for n in r.nodes)


# --- B5/B6: foreign start node 404 == absent -----------------------------------


def test_k7_foreign_start_node_404_equals_absent(owner_graph):
    """B5/B6 — non-owner가 foreign 개인 slug를 시작점으로 주면 absent와 동일한 404."""
    g = owner_graph
    foreign = _neighbors(g["owner_b"], "my-note")  # owner_a의 개인 노트
    absent = _neighbors(g["owner_b"], "no-such-slug")
    assert foreign.status is KgQueryStatus.REJECTED
    assert absent.status is KgQueryStatus.REJECTED
    # reject_reason 형태 동일(둘 다 slug_not_found:<caller 입력>) — 존재 오라클 아님.
    assert foreign.reject_reason == "slug_not_found:my-note"
    assert absent.reject_reason == "slug_not_found:no-such-slug"
    # owner 자신은 같은 slug를 정상 resolve.
    assert _neighbors(g["owner_a"], "my-note").status is KgQueryStatus.OK


def test_k7_foreign_probe_query_run_row_shape_equivalent(owner_graph):
    """B5 audit — foreign-exists probe와 truly-absent probe의 kg_query_runs 행이
    (caller 입력 slug 외) 동형(status/result_count). 감사 행이 존재 오라클이 아님."""
    g = owner_graph
    _neighbors(g["owner_b"], "my-note")
    _neighbors(g["owner_b"], "definitely-absent")
    with session() as s:
        rows = {
            r["reject_reason"]: r
            for r in s.execute(kg_query_runs.select()).mappings()
            if r["reject_reason"] and r["reject_reason"].startswith("slug_not_found")
        }
    foreign = rows["slug_not_found:my-note"]
    absent = rows["slug_not_found:definitely-absent"]
    assert foreign["status"] == absent["status"] == "rejected"
    assert foreign["result_count"] is None and absent["result_count"] is None


# --- company_always template (K9 — deferred→company_always 재분류) ---------------


def test_k7_entity_mentions_company_always_served_under_owner_scope(owner_graph):
    """K9 — entity_mentions가 deferred→company_always로 재분류돼(C-A/B3) owner-scope 활성에서도
    서빙된다(이전엔 deferred_template fail-closed). entity는 company-only projection이라
    gate stage-4 company-fork로 $caller 미바인딩 — 반환 노드는 전부 company(R5 경계, 누수 없음).
    매칭 entity가 없어 빈 결과여도 status OK(company_always는 reject 아님)."""
    r = run_kg_template(
        user_id=owner_graph["owner_a"],
        template_name="entity_mentions",
        params={"name_norm": "김대표"},
    )
    assert r.status is KgQueryStatus.OK
    assert r.reject_reason != "deferred_template"
    assert all(n.scope == "company" for n in r.nodes)
    assert r.boundary_tripwire_drops == 0


# --- resolve_slug personal-first (코드리뷰 #1) ----------------------------------


def test_k7_resolve_slug_prefers_caller_own_personal_over_company(kg_env, user_id):
    """코드리뷰 #1 — company와 caller 자기 personal이 같은 slug를 가지면 resolve_slug는
    자기 personal을 골라야 한다(personal-first). 이전엔 ORDER BY (owner_id==caller).desc()의
    Postgres NULLS FIRST 때문에 company row가 잘못 선택됐다(누출은 아니나 contract 위반 +
    L4 해시 누락)."""
    from orthus.kg.visibility import resolve_slug

    _page(user_id, "shared", scope="company", owner_id=None)
    _page(user_id, "shared", scope="personal", owner_id=user_id)
    own = resolve_slug("shared", user_id)
    assert own is not None and own.scope == "personal"  # 자기 personal 우선
    # caller None(B-null-caller)은 같은 slug라도 company만 본다.
    anon = resolve_slug("shared", None)
    assert anon is not None and anon.scope == "company"
    # 다른 owner(비-owner)는 자기 personal이 없으니 company로 fallback(cross-owner 격리).
    other = uuid.uuid4()
    foreign = resolve_slug("shared", other)
    assert foreign is not None and foreign.scope == "company"


def test_k7_graph_endpoint_start_node_owner_inclusive(kg_env, user_id, monkeypatch):
    """코드리뷰 #1 — owner-scope ON에서 graph endpoint의 404 게이트(`_graph_start_node_exists`)는
    게이트와 동일한 owner-inclusive resolve를 쓴다: company row 없는 caller 자기 personal page도
    존재로 인정한다(이전 company-only 체크가 K7.2 owner read 경로를 통째로 가렸다)."""
    from orthus.api.routes.wiki import _graph_start_node_exists

    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    _page(user_id, "mine-only", scope="personal", owner_id=user_id)
    assert (
        _graph_start_node_exists("mine-only", user_id) is True
    )  # 자기 personal → 존재(이전엔 404)
    assert _graph_start_node_exists("mine-only", uuid.uuid4()) is False  # 타 caller → 404(격리)
    assert _graph_start_node_exists("nope", user_id) is False  # 부재 → 404


# --- L4: personal slug hashed in query log -------------------------------------


def test_k7_personal_slug_not_leaked_on_partial_resolve_404(owner_graph):
    """L4 회귀(코드리뷰) — 다중 slug 템플릿에서 앞 slug가 자기 personal로 resolve된 뒤
    뒷 slug가 404나도, 앞 slug의 raw 개인 제목이 kg_query_runs에 새지 않는다. 루프 안
    404 early-return이 _stamp(hash된 log_params)를 거치는지 검증한다."""
    g = owner_graph
    r = run_kg_template(
        user_id=g["owner_a"],
        template_name="path_between",
        params={"slug_a": "my-note", "slug_b": "no-such-slug"},  # 자기 personal + absent
    )
    assert r.status is KgQueryStatus.REJECTED
    # reject_reason은 caller 입력 그대로(B5 등가) — 404난 뒷 slug.
    assert r.reject_reason == "slug_not_found:no-such-slug"
    with session() as s:
        rows = [
            row["params_redacted"]
            for row in s.execute(kg_query_runs.select()).mappings()
            if row["template_name"] == "path_between"
        ]
    assert rows, "path_between query run not recorded"
    # 어떤 기록에도 raw 'my-note'가 slug_a로 남지 않고, hash placeholder만 남는다.
    assert all(p.get("slug_a") != "my-note" for p in rows if isinstance(p, dict))
    assert any(
        isinstance(p, dict) and str(p.get("slug_a", "")).startswith("personal:") for p in rows
    )


def test_k7_personal_slug_hashed_in_query_runs(owner_graph):
    """L4 — owner가 자기 개인 slug로 질의하면 kg_query_runs.params에 raw 제목이 아니라
    hash가 남는다(admin-readable 로그에 개인 제목 비노출)."""
    g = owner_graph
    r = _neighbors(g["owner_a"], "my-note")
    assert r.status is KgQueryStatus.OK
    with session() as s:
        params_list = [
            row["params_redacted"]
            for row in s.execute(kg_query_runs.select()).mappings()
            if row["template_name"] == "neighbors"
        ]
    # 어떤 기록에도 raw 'my-note'가 slug 값으로 남지 않고, hash placeholder만.
    assert any(
        isinstance(p, dict) and str(p.get("slug", "")).startswith("personal:") for p in params_list
    )
    assert all(p.get("slug") != "my-note" for p in params_list if isinstance(p, dict))


# --- K7.4 two-framing end-to-end (실 Neo4j, owner_graph marquee) -----------------------
# owner_a의 personal my-note가 company page-x↔page-z를 잇는 유일 다리다. run_path_framings를
# 실 그래프로 돌려 (a) owner는 framing B(via_personal)로 자기 노트 경유 경로를 보고
# personal_dependent=True, (b) non-owner는 그 다리를 못 봐 demote(None), (c) owner_id가 wire에
# 절대 안 실림을 실증한다(mock이 아닌 실 게이트/Cypher 경로).

from orthus.kg.gate import run_path_framings  # noqa: E402


def test_k7_4_two_framing_owner_sees_personal_bridge(owner_graph):
    g = owner_graph
    out = run_path_framings(user_id=g["owner_a"], slug_a="page-x", slug_b="page-z", max_hops=4)
    assert out is not None  # owner는 다리를 본다
    framing_b = out.framings.framings[-1]
    assert framing_b.label == "via_personal"
    assert framing_b.personal_dependent is True  # my-note(personal) 경유
    assert framing_b.same_as_direct is False
    # spine = framing B(owner-visible) — 개인 다리 노드 포함
    assert "my-note" in out.spine.path_slugs
    mine = next(n for n in framing_b.nodes if n.slug == "my-note")
    assert mine.scope == "personal" and mine.is_own_personal is True
    # 구조적 — wire에 owner_id 필드 자체가 없다
    assert not hasattr(mine, "owner_id")
    # 회사 페이지는 company/비-own
    px = next(n for n in framing_b.nodes if n.slug == "page-x")
    assert px.scope == "company" and px.is_own_personal is False


def test_k7_4_two_framing_non_owner_cannot_see_bridge(owner_graph):
    # owner_b는 personal 페이지가 없어 short-circuit(framing A=company-only path만 확인).
    # page-x↔page-z는 company-only 경로가 없으므로(다리가 owner_a personal) → None demote.
    g = owner_graph
    out = run_path_framings(user_id=g["owner_b"], slug_a="page-x", slug_b="page-z", max_hops=4)
    assert out is None  # 비-owner에겐 다리가 안 보임(경계)
    # null caller(미인증)도 동일하게 못 본다
    assert run_path_framings(user_id=None, slug_a="page-x", slug_b="page-z", max_hops=4) is None


def test_k7_4_two_framing_all_company_short_circuit(kg_env, user_id, monkeypatch):
    # company co-a—co-b가 company-only 백링크로 직접 연결되고 caller가 personal 페이지 0개면
    # short-circuit: framing A 1회 + framing B 합성(same_as_direct=True, personal_dependent=False).
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    _page(user_id, "co-a", scope="company", owner_id=None, backlinks=["co-b"])
    _page(user_id, "co-b", scope="company", owner_id=None)
    run_rebuild()
    # bare caller(personal 페이지 0개) — short-circuit 경로
    bare = uuid.uuid4()
    out = run_path_framings(user_id=bare, slug_a="co-a", slug_b="co-b", max_hops=4)
    assert out is not None
    labels = [f.label for f in out.framings.framings]
    assert labels == ["direct_company", "via_personal"]
    framing_b = out.framings.framings[-1]
    assert framing_b.same_as_direct is True  # A==B(개인 hop 없음)
    assert framing_b.personal_dependent is False
    # spine 노드 전부 company(개인 노드 0)
    assert all(n.scope == "company" for n in out.spine.nodes)


# --- K8.6 owner-scope conflict status 분리 + B8 경계 (코드리뷰 테스트 gap) ----------


def _claim(author, slug, *, scope, owner_id, conflicting=()):
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
        user_id=author,
        scope=scope,
        owner_id=owner_id,
    )


def _conflict_task(author, slug, related, *, scope, owner_id, resolved):
    return wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="conflict",
            description=f"충돌 {slug}",
            related=list(related),
            created_at=datetime.now(UTC),
            resolved=resolved,
        ),
        user_id=author,
        scope=scope,
        owner_id=owner_id,
    )


def test_k8_6_owner_scope_personal_conflict_status_authoritative(kg_env, user_id, monkeypatch):
    """K8.6 (코드리뷰 test gap) — owner-scope에서 personal conflict task가 그 owner의
    namespace로 적재돼 CONFLICTS_WITH status를 권위적으로 정한다. task.resolved=True →
    status=RESOLVED여야 한다(owner-ns ingest/lookup이 깨지면 task를 못 읽어 default
    UNRESOLVED로 떨어지므로, RESOLVED 단언이 owner-ns 경로가 실제로 동작함을 입증한다)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    _claim(owner_a, "p-claim-1", scope="personal", owner_id=owner_a, conflicting=["p-claim-2"])
    _claim(owner_a, "p-claim-2", scope="personal", owner_id=owner_a)
    _conflict_task(
        owner_a,
        "p-task",
        ["p-claim-1", "p-claim-2"],
        scope="personal",
        owner_id=owner_a,
        resolved=True,
    )
    run_rebuild()
    r = run_kg_template(user_id=owner_a, template_name="conflicts_of", params={"slug": "p-claim-1"})
    assert r.status is KgQueryStatus.OK
    edge = next(e for e in r.edges if e.rel == "CONFLICTS_WITH")
    assert edge.scope == "personal"
    assert edge.properties["status"] == "RESOLVED"  # owner-ns conflict task가 읽힘(default 아님)


def test_k8_6_b8_non_owner_cannot_resolve_personal_conflict(kg_env, user_id, monkeypatch):
    """K8.6 B8 경계 — 비-owner는 타 owner의 personal claim을 conflict 시작점으로 resolve하지
    못한다(foreign≡absent slug_not_found). personal conflict 노드/엣지/status가 새지 않는다."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()  # 비-owner caller
    _claim(owner_a, "p-claim-1", scope="personal", owner_id=owner_a, conflicting=["p-claim-2"])
    _claim(owner_a, "p-claim-2", scope="personal", owner_id=owner_a)
    _conflict_task(
        owner_a,
        "p-task",
        ["p-claim-1", "p-claim-2"],
        scope="personal",
        owner_id=owner_a,
        resolved=False,
    )
    run_rebuild()
    r = run_kg_template(user_id=owner_b, template_name="conflicts_of", params={"slug": "p-claim-1"})
    assert r.status is KgQueryStatus.REJECTED
    assert r.reject_reason == "slug_not_found:p-claim-1"


# --- K8.2 page_conflicts owner-variant 경계 (코드리뷰 — 신규 누출 벡터 회귀) -----------
# page_conflicts는 K8이 추가한 유일한 새 owner-variant이고, 추가 bound 엣지 `bl`(BACKLINK) +
# 무라벨 counterpart `o`를 바인딩한다(개발검토 #3 unbound-edge 누출 벡터). 기존 page_conflicts
# 게이트 테스트는 전부 owner-scope OFF/단일-owner happy-path라, owner predicate가 빠져도 CI가
# 통과했다. 아래가 그 경계를 실 그래프로 고정한다(conflicts_of B8 테스트와 동형).


def test_k8_page_conflicts_owner_counterpart_dropped_for_non_owner(kg_env, user_id, monkeypatch):
    """K8.2 B7/B8 — company 페이지가 company claim을 백링크하고 그 claim이 owner_a의 personal
    claim과 CONFLICTS_WITH면: owner_a는 page_conflicts로 그 personal counterpart를 보고,
    비-owner는 절대 못 본다(drop, not mask). `visibility_predicate('o'/'k'/'bl')`가 하나라도
    빠지면 이 테스트가 깨진다 — 새 누출 벡터의 regression-lock."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()  # 비-owner caller
    # company 페이지 → company claim(백링크); owner_a personal claim이 그 company claim과 충돌.
    _claim(owner_a, "c-company", scope="company", owner_id=None)
    _claim(owner_a, "c-personal", scope="personal", owner_id=owner_a, conflicting=["c-company"])
    _page(owner_a, "team-retro", scope="company", owner_id=None, backlinks=["c-company"])
    run_rebuild()

    own = run_kg_template(
        user_id=owner_a, template_name="page_conflicts", params={"slug": "team-retro"}
    )
    assert own.status is KgQueryStatus.OK
    assert "c-personal" in _slugs(own)  # owner는 자기 personal counterpart를 본다
    assert any(e.rel == "CONFLICTS_WITH" for e in own.edges)
    mine = next(n for n in own.nodes if n.slug == "c-personal")
    assert mine.scope == "personal" and mine.is_own_personal is True

    other = run_kg_template(
        user_id=owner_b, template_name="page_conflicts", params={"slug": "team-retro"}
    )
    assert other.status is KgQueryStatus.OK
    assert "team-retro" in _slugs(other)  # 시작 page는 company라 보임
    assert "c-personal" not in _slugs(other)  # foreign personal counterpart 미노출(drop)
    # tripwire — wire에 foreign personal 노드/엣지가 하나도 없다(전부 company).
    assert all(n.scope == "company" for n in other.nodes)
    assert all(e.scope == "company" for e in other.edges)
    assert not any(e.rel == "CONFLICTS_WITH" for e in other.edges)


def test_k8_6_two_owners_same_pair_status_isolated(kg_env, user_id, monkeypatch):
    """K8.6 (코드리뷰 test gap) — 두 owner가 **같은 slug 쌍**으로 각자 personal conflict를
    가지면 status가 네임스페이스별로 분리된다: owner_a resolved=True→RESOLVED, owner_b
    resolved=False→UNRESOLVED, 서로의 엣지는 비노출. status 권위가 cross-owner로 새지 않음을
    적대적으로 고정한다(_build_conflict_index ns 키잉)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = _make_user()  # 실 user — 자기 personal claim을 write하므로(FK)
    for owner, resolved in ((owner_a, True), (owner_b, False)):
        _claim(owner, "p-c1", scope="personal", owner_id=owner, conflicting=["p-c2"])
        _claim(owner, "p-c2", scope="personal", owner_id=owner)
        _conflict_task(
            owner, "p-task", ["p-c1", "p-c2"], scope="personal", owner_id=owner, resolved=resolved
        )
    run_rebuild()

    ra = run_kg_template(user_id=owner_a, template_name="conflicts_of", params={"slug": "p-c1"})
    ea = next(e for e in ra.edges if e.rel == "CONFLICTS_WITH")
    assert ea.scope == "personal" and ea.properties["status"] == "RESOLVED"
    assert all(n.is_own_personal for n in ra.nodes if n.scope == "personal")

    rb = run_kg_template(user_id=owner_b, template_name="conflicts_of", params={"slug": "p-c1"})
    eb = next(e for e in rb.edges if e.rel == "CONFLICTS_WITH")
    assert eb.scope == "personal" and eb.properties["status"] == "UNRESOLVED"
    # owner_b는 owner_a의 엣지/노드를 절대 보지 않는다(자기 것만).
    assert all(n.is_own_personal for n in rb.nodes if n.scope == "personal")


def test_k8_b9_placeholder_counterpart_inherits_personal_scope(kg_env, user_id, monkeypatch):
    """K8 B9 — personal-source conflict의 placeholder counterpart(dangling dst)는 src
    네임스페이스(personal/owner)를 상속한다(company로 붕괴 금지). owner는 자기 placeholder를
    `scope='personal'`/`is_own_personal=True`로 보고, 비-owner는 시작 claim 자체가 foreign이라
    못 본다(emit_node funnel 권위 + gate predicate backstop)."""
    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "kg_query_timeout_ms", 15000, raising=False)
    owner_a = user_id
    owner_b = uuid.uuid4()
    _claim(owner_a, "p-claim", scope="personal", owner_id=owner_a, conflicting=["ghost-personal"])
    run_rebuild()
    own = run_kg_template(user_id=owner_a, template_name="conflicts_of", params={"slug": "p-claim"})
    assert own.status is KgQueryStatus.OK
    ghost = next((n for n in own.nodes if n.slug == "ghost-personal"), None)
    assert ghost is not None and ghost.materialized is False
    assert ghost.scope == "personal" and ghost.is_own_personal is True  # company로 안 붕괴
    # 비-owner는 시작 personal claim을 resolve 못 함 → placeholder도 당연히 도달 불가.
    other = run_kg_template(
        user_id=owner_b, template_name="conflicts_of", params={"slug": "p-claim"}
    )
    assert other.status is KgQueryStatus.REJECTED
    assert other.reject_reason == "slug_not_found:p-claim"


# --- E1b expand owner boundary --------------------------------------------------


def test_expand_owner_sees_own_personal(owner_graph):
    """owner가 자기 personal 노드를 시작점으로 expand하면 성공 + 자기 것 가시."""
    g = owner_graph
    pid = _page_id_of("my-note")
    r = run_kg_template(
        user_id=g["owner_a"],
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert r.status is KgQueryStatus.OK
    # 시작 personal 노드 + company 이웃(page-x/page-z)이 보인다.
    mine = next(n for n in r.nodes if n.id == str(pid))
    assert mine.scope == "personal" and mine.is_own_personal is True
    assert all(not (n.scope == "personal" and not n.is_own_personal) for n in r.nodes)


def test_expand_owner_boundary_404(owner_graph):
    """owner_b(비-owner)가 owner_a의 personal page_id로 expand → 404(부분 마스킹 없이).
    owner_a는 자기 personal expand OK."""
    g = owner_graph
    pid = _page_id_of("my-note")
    other = run_kg_template(
        user_id=g["owner_b"],
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert other.status is KgQueryStatus.REJECTED
    assert other.reject_reason == "entity_or_node_not_found"
    # owner_a 자기 것은 OK.
    own = run_kg_template(
        user_id=g["owner_a"],
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert own.status is KgQueryStatus.OK


def test_expand_company_node_owner_scope_on(owner_graph):
    """owner-scope ON에서 company page_id expand → OK(모든 caller). owner_a는 자기 personal 이웃도
    함께 본다(my-note가 page-x 백링크)."""
    g = owner_graph
    pid = _page_id_of("page-x")
    r = run_kg_template(
        user_id=g["owner_a"],
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert r.status is KgQueryStatus.OK
    assert "my-note" in {n.slug for n in r.nodes}  # owner 자기 personal 이웃 가시
    # 비-owner는 my-note를 못 본다.
    rb = run_kg_template(
        user_id=g["owner_b"],
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert rb.status is KgQueryStatus.OK
    assert "my-note" not in {n.slug for n in rb.nodes}
    assert all(n.scope == "company" for n in rb.nodes)


def test_expand_owner_scope_off_personal_404(kg_env, user_id, monkeypatch):
    """owner-scope OFF에서 personal page_id expand → company-only precheck → 404."""
    s = get_settings()
    # owner-scope OFF(kg_env 기본값). personal page를 하나 심는다.
    _page(user_id, "off-note", scope="personal", owner_id=user_id)
    run_rebuild()
    pid = _page_id_of("off-note")
    monkeypatch.setattr(s, "kg_owner_scope_enabled", False, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", False, raising=False)
    r = run_kg_template(
        user_id=user_id,
        template_name="expand_node",
        params={"label": "WikiPage", "id": str(pid)},
    )
    assert r.status is KgQueryStatus.REJECTED
    assert r.reject_reason == "entity_or_node_not_found"
