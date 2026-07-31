"""E1b — expand 템플릿 단위 검사 (neo4j 불필요).

expand_node(owner 5라벨)/expand_entity(company_always)의 사전 컴파일 Cypher shape,
merge key, owner-variant predicate, entity_key roundtrip, write-keyword 부재를 고정한다.
"""

from __future__ import annotations

from orthus.kg.templates import (
    TEMPLATES,
    ExpandEntityParams,
    ExpandNodeParams,
    _EXPAND_MERGE_KEY,
    all_cypher_variants,
)
from orthus.kg.visibility import COMPANY_NS, entity_key_from_node_id, entity_node_key_for


def _enable_owner_scope(monkeypatch):
    from orthus.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", True, raising=False)
    monkeypatch.setattr(s, "owner_scope_enabled", True, raising=False)


def test_expand_node_flag_off_cypher_per_label(monkeypatch):
    """5개 라벨 flag-off Cypher: merge key로 MATCH + _KNOWLEDGE_RELS*1..1 + RETURN path LIMIT
    $limit + IN_PROJECT 부재 + owner 술어 부재($caller 없음)."""
    from orthus.settings import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "kg_owner_scope_enabled", False, raising=False)
    tmpl = TEMPLATES["expand_node"]
    for label, mk in _EXPAND_MERGE_KEY.items():
        cypher = tmpl.cypher_for(ExpandNodeParams(label=label, id="x"), False)
        assert f"(p:{label} {{{mk}:$id}})" in cypher
        assert "*1..1]-(n)" in cypher
        assert cypher.endswith("RETURN path LIMIT $limit")
        assert "IN_PROJECT" not in cypher
        assert "$caller" not in cypher
        assert cypher in set(all_cypher_variants())


def test_expand_node_owner_variant_has_predicate(monkeypatch):
    """owner-variant: nodes(path) AND relationships(path)에 visibility_predicate + $caller."""
    _enable_owner_scope(monkeypatch)
    tmpl = TEMPLATES["expand_node"]
    for label in _EXPAND_MERGE_KEY:
        cypher = tmpl.cypher_for(ExpandNodeParams(label=label, id="x"), True)
        assert "all(_n IN nodes(path) WHERE (_n.scope = 'company' OR _n.owner_id = $caller))" in cypher
        assert (
            "all(_r IN relationships(path) WHERE (_r.scope = 'company' OR _r.owner_id = $caller))"
            in cypher
        )
        assert cypher in set(all_cypher_variants())


def test_expand_node_predicate_kind_is_owner():
    assert TEMPLATES["expand_node"].predicate_kind == "owner"
    assert TEMPLATES["expand_node"].slug_resolution == ()
    assert TEMPLATES["expand_node"].owner_bind == ()
    assert TEMPLATES["expand_node"].bind_params == ("id",)


def test_expand_entity_cypher_is_company_always(monkeypatch):
    """company_always: owner_scope 인자 무관 단일 변형, MENTIONED_IN + RETURN path +
    entity_key 바인딩, $caller/owner_id 부재."""
    _enable_owner_scope(monkeypatch)
    tmpl = TEMPLATES["expand_entity"]
    cypher_on = tmpl.cypher_for(ExpandEntityParams(entity_key="company:person:x"), True)
    cypher_off = tmpl.cypher_for(ExpandEntityParams(entity_key="company:person:x"), False)
    assert cypher_on == cypher_off  # owner_scope 무시(단일 변형)
    assert "$caller" not in cypher_on and "owner_id" not in cypher_on
    assert "MENTIONED_IN" in cypher_on
    assert "{entity_key:$entity_key}" in cypher_on
    assert cypher_on.endswith("RETURN path LIMIT $limit")
    assert tmpl.predicate_kind == "company_always"
    assert tmpl.log_drop_params == ("entity_key",)
    assert cypher_on in set(all_cypher_variants())


def test_expand_write_keywords_absent():
    """신규 3그룹이 all_cypher_variants()에 포함되고 write 키워드가 없다(정적검사 대상)."""
    variants = set(all_cypher_variants())
    from orthus.kg.templates import _EXPAND_BY_LABEL, _EXPAND_ENTITY, _EXPAND_OWNER_BY_LABEL

    for v in (*_EXPAND_BY_LABEL.values(), *_EXPAND_OWNER_BY_LABEL.values(), _EXPAND_ENTITY):
        assert v in variants
    forbidden = ("CREATE", "MERGE", "DELETE", "SET ", "REMOVE", "DETACH")
    for v in (*_EXPAND_BY_LABEL.values(), *_EXPAND_OWNER_BY_LABEL.values(), _EXPAND_ENTITY):
        upper = v.upper()
        for kw in forbidden:
            assert kw not in upper, f"write keyword {kw!r} in {v}"


def test_entity_key_roundtrip():
    """namespaced :Entity node id ↔ unprefixed kg_entities.entity_key 왕복."""
    unprefixed = "person:김대표"
    node_id = entity_node_key_for(COMPANY_NS, unprefixed)
    assert node_id == "company:person:김대표"
    assert entity_key_from_node_id(node_id) == unprefixed
    # 접두 없는 값(비정상 입력)은 그대로 반환(removeprefix no-op).
    assert entity_key_from_node_id("person:김대표") == "person:김대표"
