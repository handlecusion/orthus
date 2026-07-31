"""K4 — 템플릿 registry 정적 회귀 (구현 명세 §6.2/§6.5 #5·#6).

read-only 게이트의 "write Cypher reject"는 런타임 검사가 아니라 구조다:
(a) 게이트 공개 표면에 raw Cypher 입력 경로가 없고, (b) 등록 Cypher 전수가
read-only 절만 포함함을 정적으로 고정한다. Neo4j Community에 DB 레벨
read-only 계정이 없는 만큼(kg-model §4) 이 회귀가 그 역할을 보완한다.
"""

from __future__ import annotations

import inspect
import re

import pytest
from pydantic import ValidationError

from orthus.kg.gate import run_kg_template
from orthus.kg.templates import (
    TEMPLATES,
    NeighborsParams,
    PathBetweenParams,
    all_cypher_variants,
)

# 금지 키워드는 단어 경계로 매칭한다 — `shortestPath` 같은 함수명이 오탐되지
# 않는 이유(§6.2). CALL은 프로시저 호출(쓰기 우회 가능) 차단이다.
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|CALL|LOAD|FOREACH)\b",
    re.IGNORECASE,
)


def test_registered_cypher_has_no_write_keywords():
    variants = all_cypher_variants()
    assert (
        len(variants) >= 8
    )  # neighbors 2 + path_between 3 + conflicts + provenance + entity_mentions
    for cypher in variants:
        match = _WRITE_KEYWORDS.search(cypher)
        assert match is None, f"write keyword {match.group(0)!r} in: {cypher}"


def test_shortest_path_not_false_positive():
    # 단어 경계 매칭의 회귀 — 함수명 안의 키워드 조각은 매칭되지 않아야 한다.
    assert _WRITE_KEYWORDS.search("MATCH path = shortestPath((a)-[*..4]-(b)) RETURN path") is None


def test_gate_public_surface_has_no_raw_cypher_param():
    """게이트 공개 표면에 Cypher 문자열 파라미터가 없음을 타입 수준에서 고정(§6.2)."""
    params = inspect.signature(run_kg_template).parameters
    assert set(params) == {"user_id", "template_name", "params", "correlation_id", "limit"}
    for name in params:
        assert "cypher" not in name.lower()
        assert "raw" not in name.lower()


def test_all_templates_resolve_to_precompiled_strings():
    """cypher_for는 사전 컴파일 문자열 선택만 한다 — 값 보간이 없으면 어떤
    파라미터 조합이든 변형 집합 안의 문자열이 그대로 나온다."""
    variants = set(all_cypher_variants())
    for depth in (1, 2):
        cypher = TEMPLATES["neighbors"].cypher_for(NeighborsParams(slug="x", depth=depth))
        assert cypher in variants
        assert "$slug" in cypher and "$limit" in cypher
    for hops in (2, 3, 4):
        cypher = TEMPLATES["path_between"].cypher_for(
            PathBetweenParams(slug_a="a", slug_b="b", max_hops=hops)
        )
        assert cypher in variants
    for name in ("conflicts_of", "provenance_chain"):
        template = TEMPLATES[name]
        cypher = template.cypher_for(template.params_model(slug="x"))
        assert cypher in variants


def test_neighbors_excludes_in_project_hub_rel():
    """:Project 허브 도배 방지 — neighbors 기본 탐색에서 IN_PROJECT 제외
    (K4 실측 결정, kg-model §4). project 귀속은 노드 props로 노출된다."""
    for depth in (1, 2):
        cypher = TEMPLATES["neighbors"].cypher_for(NeighborsParams(slug="x", depth=depth))
        assert "IN_PROJECT" not in cypher
        assert "BACKLINK" in cypher  # rel allowlist가 비어있지 않음


def test_depth_and_hops_literal_bounds():
    with pytest.raises(ValidationError):
        NeighborsParams(slug="x", depth=3)
    with pytest.raises(ValidationError):
        NeighborsParams(slug="", depth=1)
    with pytest.raises(ValidationError):
        PathBetweenParams(slug_a="a", slug_b="b", max_hops=5)


def test_path_between_rejects_identical_slugs():
    with pytest.raises(ValidationError) as exc_info:
        PathBetweenParams(slug_a="same", slug_b="same")
    assert "identical_slugs" in str(exc_info.value)


def test_k7_template_resolution_declaration_matches_slug_params():
    """K7.2 — slug_resolution(field,label)/bind_params/owner_bind 선언이 실제 모델
    필드와 일치(단일 출처). 비-`:WikiPage` 시작노드도 label로 선언 가능."""
    valid_labels = {"WikiPage", "WikiClaim", "WikiSource", "Document", "StructuredFact"}
    for template in TEMPLATES.values():
        fields = template.params_model.model_fields
        for field, label in template.slug_resolution:
            assert field in fields, f"{template.name}: {field} not a model field"
            assert label in valid_labels, f"{template.name}: bad resolve label {label}"
        for bind_field in template.bind_params:
            assert bind_field in fields
        # owner_bind는 resolvable field에서 id-param으로 매핑한다.
        resolvable = {f for f, _ in template.slug_resolution}
        for resolved_field, id_param in template.owner_bind:
            assert resolved_field in resolvable
            assert id_param.startswith("page_id")


# --- K7.2 owner-variant Cypher --------------------------------------------------


def _enable_owner_scope(monkeypatch):
    from orthus.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)


def test_owner_variant_predicate_on_nodes_and_edges(monkeypatch):
    """flag-on owner-variant는 모든 path 노드 AND 모든 path 엣지에 visibility_predicate를
    합성하고(B1), 시작노드를 slug 아닌 page_id로 바인딩한다(B4)."""
    _enable_owner_scope(monkeypatch)
    nb = TEMPLATES["neighbors"].cypher_for(NeighborsParams(slug="x", depth=1))
    assert "all(_n IN nodes(path) WHERE (_n.scope = 'company' OR _n.owner_id = $caller))" in nb
    assert (
        "all(_r IN relationships(path) WHERE (_r.scope = 'company' OR _r.owner_id = $caller))" in nb
    )
    assert "$page_id" in nb and "{slug:$slug}" not in nb

    pb = TEMPLATES["path_between"].cypher_for(PathBetweenParams(slug_a="a", slug_b="b", max_hops=2))
    assert "$page_id_a" in pb and "$page_id_b" in pb
    assert "_r.owner_id = $caller" in pb  # rel clause present (not green-by-vacuity)


def test_framing_a_company_only_predicate(monkeypatch):
    """framing A(path_between_company)는 scope='company' 술어만 — owner_id=$caller 없음
    (비-owner/admin도 실행하므로 절대 personal 누수 불가)."""
    _enable_owner_scope(monkeypatch)
    fa = TEMPLATES["path_between_company"].cypher_for(
        PathBetweenParams(slug_a="a", slug_b="b", max_hops=2)
    )
    assert "all(_n IN nodes(path) WHERE _n.scope = 'company')" in fa
    assert "all(_r IN relationships(path) WHERE _r.scope = 'company')" in fa
    assert "owner_id" not in fa and "$caller" not in fa


def test_cypher_for_explicit_owner_scope_overrides_flag(monkeypatch):
    """코드리뷰 #5 — 게이트가 주입한 owner_scope가 flag 재독을 이긴다(TOCTOU 차단).
    flag와 반대값을 주입하면 변형이 인자대로 선택돼 bind/cypher 불일치가 불가능하다."""
    # flag OFF여도 owner_scope=True 주입 → owner 변형(page_id/$caller).
    nb_owner = TEMPLATES["neighbors"].cypher_for(NeighborsParams(slug="x", depth=1), True)
    assert "$page_id" in nb_owner and "$caller" in nb_owner and "{slug:$slug}" not in nb_owner
    # flag ON이어도 owner_scope=False 주입 → v1(slug, 술어 없음).
    _enable_owner_scope(monkeypatch)
    nb_v1 = TEMPLATES["neighbors"].cypher_for(NeighborsParams(slug="x", depth=1), False)
    assert "{slug:$slug}" in nb_v1 and "$caller" not in nb_v1


def test_entity_mentions_company_always_no_owner_predicate(monkeypatch):
    """K9 — entity_mentions는 deferred→company_always로 재분류됐다(C-A/B3). flag-on이어도 owner
    predicate 없음(company-only data, 구조적 no-op). /ask entity intent(K9.3a) wiring 허용."""
    _enable_owner_scope(monkeypatch)
    from orthus.kg.templates import EntityMentionsParams

    em = TEMPLATES["entity_mentions"].cypher_for(EntityMentionsParams(name_norm="n"))
    assert "$caller" not in em and "owner_id" not in em
    assert TEMPLATES["entity_mentions"].predicate_kind == "company_always"


def test_entity_neighbors_cypher_shape(monkeypatch):
    """K9 — entity_neighbors(패널, page-rooted)는 RETURN path(C-B 다리 엣지 보존) + $slug 시작 +
    bound $hub_threshold(R9) + 사전 컴파일 변형. company_always라 flag-on에서도 owner 변형 없음."""
    from orthus.kg.templates import EntityNeighborsParams

    tmpl = TEMPLATES["entity_neighbors"]
    cypher = tmpl.cypher_for(EntityNeighborsParams(slug="page-x"))
    assert cypher in set(all_cypher_variants())
    assert "RETURN path" in cypher  # bare RETURN p,e,q는 MENTIONED_IN 엣지를 잃는다(C-B)
    assert "{slug:$slug}" in cypher and "$hub_threshold" in cypher and "$limit" in cypher
    assert "$caller" not in cypher and "owner_id" not in cypher
    assert tmpl.predicate_kind == "company_always"
    # $hub_threshold는 settings 주입 선언(Cypher 상수 아님 — rebuild 없이 튜닝, R9).
    assert tmpl.setting_params == (("hub_threshold", "kg_entity_hub_threshold"),)
    # flag-on에서도 동일 문자열(company-only data, owner_scope 무시).
    _enable_owner_scope(monkeypatch)
    assert tmpl.cypher_for(EntityNeighborsParams(slug="page-x"), True) == cypher


def test_owner_variants_present_true_under_flag(monkeypatch):
    """inter-PR fail-closed 가드(§6/L3)의 판정: flag-on이면 owner-bearing 템플릿 전수가
    canonical fragment를 담는다."""
    from orthus.kg.templates import owner_variants_present

    _enable_owner_scope(monkeypatch)
    assert owner_variants_present() is True


def test_owner_variants_present_false_if_path_template_misses_relationships(monkeypatch):
    """코드리뷰 #4 — 런타임 L3 가드는 노드 술어 존재만으로 통과하면 안 된다. path 템플릿이
    nodes(path)만 가드하고 relationships(path)를 빠뜨리면(B1 rel-clause green-by-vacuity)
    owner_variants_present()가 False를 반환해 KG가 fail-closed 강제-off되어야 한다."""
    import dataclasses

    from orthus.kg.templates import TEMPLATES, owner_variants_present

    _enable_owner_scope(monkeypatch)
    # neighbors의 owner-variant가 nodes(path)만 가드하고 relationships(path)를 누락한 변형.
    node_only = (
        "MATCH path = (p:WikiPage {page_id:$page_id})-[:X*1..1]-(n) "
        "WHERE all(_n IN nodes(path) WHERE (_n.scope = 'company' OR _n.owner_id = $caller)) "
        "RETURN path LIMIT $limit"
    )
    bad = dataclasses.replace(TEMPLATES["neighbors"], cypher_for=lambda params: node_only)
    monkeypatch.setitem(TEMPLATES, "neighbors", bad)
    assert owner_variants_present() is False


def test_predicate_fragment_byte_identical_across_owner_templates(monkeypatch):
    """visibility_predicate fragment가 owner 템플릿 전반에 byte-identical(§1.1) —
    한 템플릿만 손으로 약화시키는 drift 차단."""
    _enable_owner_scope(monkeypatch)
    frag = "(_n.scope = 'company' OR _n.owner_id = $caller)"
    for name in ("neighbors", "path_between"):
        sample = (
            NeighborsParams(slug="x", depth=2)
            if name == "neighbors"
            else PathBetweenParams(slug_a="a", slug_b="b", max_hops=4)
        )
        assert frag in TEMPLATES[name].cypher_for(sample)
