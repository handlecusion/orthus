"""K7.2 Step 7 — boundary-matrix registry-completeness (neo4j 불필요).

no-RLS 경계가 자기 성장에 대해 self-defending이 되게 하는 단일 가드: 등록된 모든
템플릿이 predicate_kind로 분류되고, owner 템플릿은 byte-identical visibility_predicate
fragment를 모든 bound 노드/엣지에, company(framing A)는 scope='company'를, company_always
(K9 entity_neighbors/entity_mentions)·deferred는 owner predicate 부재 + code-guard로 커버됨을
단언한다. 신규 템플릿을 분류/술어 없이 추가하면 CI red.
"""

from __future__ import annotations

import pytest

from orthus.kg.templates import (
    TEMPLATES,
    ConflictsOfParams,
    EntityMentionsParams,
    EntityNeighborsParams,
    EntityOverviewParams,
    ExpandEntityParams,
    ExpandNodeParams,
    NeighborsParams,
    PageConflictsParams,
    PathBetweenParams,
    ProvenanceChainParams,
    _OWNER_SAMPLE_PARAMS,
)

_OWNER_FRAGMENT_TOKEN = ".scope = 'company' OR "  # visibility_predicate 변수-무관 토큰
_DEFERRED: set[str] = set()  # K9 — entity_mentions가 company_always로 재분류돼 deferred는 0
# K9 entity + E1b expand_entity + 랜딩 개체 지도 — owner-술어 면제 + wiring 허용.
_COMPANY_ALWAYS = {"entity_mentions", "entity_neighbors", "expand_entity", "entity_overview"}


@pytest.fixture
def owner_scope_on(monkeypatch):
    from orthus.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)
    return s


def _sample(name):
    return {
        "neighbors": NeighborsParams(slug="x", depth=1),
        "path_between": PathBetweenParams(slug_a="a", slug_b="b", max_hops=2),
        "path_between_company": PathBetweenParams(slug_a="a", slug_b="b", max_hops=2),
        "conflicts_of": ConflictsOfParams(slug="x"),
        "page_conflicts": PageConflictsParams(slug="x"),
        "provenance_chain": ProvenanceChainParams(slug="x"),
        "entity_mentions": EntityMentionsParams(name_norm="n"),
        "entity_neighbors": EntityNeighborsParams(slug="x"),
        "expand_node": ExpandNodeParams(label="WikiPage", id="x"),
        "expand_entity": ExpandEntityParams(entity_key="company:person:x"),
        "entity_overview": EntityOverviewParams(),
    }[name]


def test_every_template_classified_predicate_kind():
    valid = {"owner", "company", "company_always", "deferred"}
    for name, tmpl in TEMPLATES.items():
        assert tmpl.predicate_kind in valid, f"{name} not classified"


def test_partition_is_exhaustive_and_deferred_set_matches():
    deferred = {n for n, t in TEMPLATES.items() if t.predicate_kind == "deferred"}
    assert deferred == _DEFERRED, (
        "deferred set drift — 신규 deferred 템플릿이면 _DEFERRED와 boundary 처리(code-guard)"
        " 갱신 필요"
    )


def test_company_always_set_matches():
    """K9 — company_always 분류 drift 차단. 신규 company_always 템플릿이면 _COMPANY_ALWAYS와
    boundary 처리(gate stage-4 company-fork + no-$caller 단정)를 함께 갱신해야 한다."""
    company_always = {n for n, t in TEMPLATES.items() if t.predicate_kind == "company_always"}
    assert company_always == _COMPANY_ALWAYS, "company_always set drift"


def test_company_always_templates_have_no_owner_predicate(owner_scope_on):
    """R5/C-A — company_always Cypher는 owner-scope ON에서도 $caller·owner_id 참조가 없어야
    한다(구조적 personal-blind). gate가 company-fork로 $caller를 미바인딩하므로, Cypher가 owner
    변형을 골라 unbound-param이 되거나 미래 entity_key owner-prefix 누수가 benign으로 위장되는
    것을 빌드 타임에 차단한다."""
    for name in _COMPANY_ALWAYS:
        cypher = TEMPLATES[name].cypher_for(_sample(name), True)  # owner_scope=True 주입에도
        assert "$caller" not in cypher, f"{name}: company_always must not reference $caller"
        assert "owner_id" not in cypher, f"{name}: company_always must not reference owner_id"


def test_owner_templates_carry_predicate_on_every_bound_var(owner_scope_on):
    """owner 템플릿의 owner-variant는 모든 bound 노드 AND 엣지에 visibility_predicate를
    합성한다(B1). path 템플릿은 nodes(path)/relationships(path) all(), 비-path는 각
    bound var에 절(節)."""
    for name, tmpl in TEMPLATES.items():
        if tmpl.predicate_kind != "owner":
            continue
        cypher = tmpl.cypher_for(_sample(name))
        assert "$caller" in cypher, f"{name}: owner-variant missing $caller"
        assert _OWNER_FRAGMENT_TOKEN in cypher, f"{name}: missing visibility_predicate fragment"
        if "shortestPath" in cypher or "path =" in cypher:
            assert "nodes(path)" in cypher and "relationships(path)" in cypher, (
                f"{name}: path template must guard nodes AND relationships"
            )


def test_company_template_is_company_only_predicate(owner_scope_on):
    """framing A(company)는 scope='company'만 — owner_id/$caller 없음(비-owner 실행 안전)."""
    for name, tmpl in TEMPLATES.items():
        if tmpl.predicate_kind != "company":
            continue
        cypher = tmpl.cypher_for(_sample(name))
        assert "scope = 'company'" in cypher
        assert "owner_id" not in cypher and "$caller" not in cypher


def test_deferred_templates_have_no_owner_predicate(owner_scope_on):
    for name in _DEFERRED:
        cypher = TEMPLATES[name].cypher_for(_sample(name))
        assert "$caller" not in cypher and "owner_id" not in cypher


def test_owner_bind_covers_every_resolvable_slug_field():
    """flag-ON resolve 루프(gate.py)는 slug_resolution의 field마다 owner_bind[field]로
    resolve된 page_id를 바인딩한다. owner_bind가 한 field라도 빠지면 그 루프는 try 밖이라
    KeyError가 정규화되지 않고 endpoint 500으로 샌다(코드리뷰). 새 템플릿이 slug_resolution만
    선언하고 owner_bind를 빠뜨리면 여기서 CI red — 런타임 500 대신 빌드 타임 적발.

    company_always(K9 entity_neighbors)는 owner resolve 루프를 안 탄다(gate stage-4 company-fork)
    — slug_resolution은 flag-OFF company 선검증에만 쓰고 owner_bind=()가 정상이므로 면제한다."""
    for name, tmpl in TEMPLATES.items():
        if tmpl.predicate_kind == "company_always":
            continue
        resolvable = {field for field, _label in tmpl.slug_resolution}
        bound = {field for field, _id_param in tmpl.owner_bind}
        missing = resolvable - bound
        assert not missing, (
            f"{name}: slug_resolution field {sorted(missing)}에 대응하는 owner_bind 항목 없음 "
            "(gate flag-ON resolve 루프가 KeyError → 500)"
        )


def test_non_deferred_templates_have_owner_sample_params():
    """owner_variants_present(L3 fail-closed 가드)는 owner/company 템플릿마다
    _OWNER_SAMPLE_PARAMS.get(name)을 cypher_for에 넘긴다. 항목이 없으면 cypher_for(None)이
    호출돼 변형 검증이 거짓 통과하거나(예외 미발생) gate_check_error로 원인 불명 강제-off가
    된다(코드리뷰). 새 owner/company 템플릿이 sample을 빠뜨리면 여기서 CI red."""
    for name, tmpl in TEMPLATES.items():
        if tmpl.predicate_kind == "deferred":
            continue  # deferred는 owner_variants_present 술어 검사 대상 아님
        assert name in _OWNER_SAMPLE_PARAMS, (
            f"{name}: predicate_kind={tmpl.predicate_kind}인데 _OWNER_SAMPLE_PARAMS 누락 "
            "(owner_variants_present가 cypher_for(None) 호출)"
        )


def test_byte_identical_owner_fragment_across_owner_templates(owner_scope_on):
    """한 owner 템플릿만 손으로 약화시키는 drift 차단(§1.1) — node-var fragment 동일."""
    frag = "(_n.scope = 'company' OR _n.owner_id = $caller)"
    present = [
        name
        for name, t in TEMPLATES.items()
        if t.predicate_kind == "owner" and "nodes(path)" in t.cypher_for(_sample(name))
    ]
    assert present, "no path-style owner template found"
    for name in present:
        assert frag in TEMPLATES[name].cypher_for(_sample(name))
