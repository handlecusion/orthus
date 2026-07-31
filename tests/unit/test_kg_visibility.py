"""K7.1 — owner-scope visibility 헬퍼 (순수 함수, DB 불필요).

ns_key/entity_node_key는 placeholder/entity 정체성의 단일 출처다 — cross-owner
키 분리(B4)와 entity 접두 SLOT 예약을 여기서 고정한다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from orthus.kg.visibility import (
    COMPANY_NS,
    doc_visible_to,
    entity_node_key,
    entity_node_key_for,
    ns_for,
    ns_key,
    owner_inclusive_sql,
    owner_scope_columns,
    visibility_predicate,
)
from orthus.tables import documents, structured_rows, wiki_pages


def test_ns_for_company_vs_personal():
    owner = uuid.uuid4()
    assert ns_for("company", None) == COMPANY_NS
    assert ns_for("company", owner) == COMPANY_NS  # company는 owner를 무시
    assert ns_for("personal", None) == COMPANY_NS  # owner 부재는 company로 수렴(방어)
    assert ns_for("personal", owner) == str(owner)


def test_ns_key_cross_owner_separation():
    a, b = uuid.uuid4(), uuid.uuid4()
    slug = "q3-roadmap"
    company = ns_key("company", None, slug)
    pa = ns_key("personal", a, slug)
    pb = ns_key("personal", b, slug)
    # 같은 slug가 세 개의 서로 다른 키 — B4 cross-owner 존재 오라클 차단.
    assert company == f"company:{slug}"
    assert pa == f"{a}:{slug}"
    assert len({company, pa, pb}) == 3


def test_ns_key_accepts_uuid_or_str():
    owner = uuid.uuid4()
    assert ns_key("personal", owner, "s") == ns_key("personal", str(owner), "s")


def test_entity_node_key_company_prefix():
    # D2: 모든 K7 entity는 company 접두. merge 의미 0, 접두 SLOT만 예약.
    assert entity_node_key("company", "person", "김대표") == "company:person:김대표"
    assert entity_node_key("company", "org", "nova") == "company:org:nova"


def test_entity_node_key_for_matches_kind_name_form():
    # entity_node_key_for(PG entity_key)와 entity_node_key(kind, name_norm)는 동형 —
    # build() 엣지 끝점(entity_key only)과 노드(kind/name) 키가 일치해야 MATCH 성립.
    assert entity_node_key_for(COMPANY_NS, "person:김대표") == "company:person:김대표"
    assert entity_node_key_for(COMPANY_NS, "person:김대표") == entity_node_key(
        COMPANY_NS, "person", "김대표"
    )


# --- write 경계 술어 (코드리뷰 #10 — 단일 출처) -------------------------------------


def test_doc_visible_to_company_doc_visible_to_all():
    a = uuid.uuid4()
    # B4 — company doc은 누구에게나 visible(공유 지식 다리).
    assert doc_visible_to("personal", a, COMPANY_NS, None) is True
    assert doc_visible_to("company", None, COMPANY_NS, None) is True


def test_doc_visible_to_personal_doc_same_owner_only():
    a, b = uuid.uuid4(), uuid.uuid4()
    # B4 — personal doc은 같은 owner에게만. cross-owner 금지.
    assert doc_visible_to("personal", a, "personal", a) is True
    assert doc_visible_to("personal", a, "personal", b) is False
    # owner 부재 personal doc은 누구에게도(방어).
    assert doc_visible_to("personal", a, "personal", None) is False


def test_owner_inclusive_sql_predicate_shape():
    # SQLAlchemy column 술어 — 컴파일된 SQL 문자열로 형태를 고정한다(company OR
    # (personal AND owner IS NOT NULL)). write 경계 술어의 단일 출처(read의 owner=$caller와 구분).
    from sqlalchemy import column

    expr = owner_inclusive_sql(column("scope"), column("owner_id"))
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "scope = 'company'" in sql
    assert "scope = 'personal'" in sql
    assert "owner_id IS NOT NULL" in sql


# --- owner_scope_columns SoT 헬퍼 (E1a — SELECT-column owner 매핑 단일 출처) ------


def test_owner_scope_columns_wiki_pages():
    scope_col, owner_col = owner_scope_columns(wiki_pages)
    assert scope_col is wiki_pages.c.scope
    assert owner_col is wiki_pages.c.owner_id  # 순수 컬럼 객체


def test_owner_scope_columns_structured_rows():
    scope_col, owner_col = owner_scope_columns(structured_rows)
    assert scope_col is structured_rows.c.scope
    assert owner_col is structured_rows.c.owner_id


def test_owner_scope_columns_documents_case():
    scope_col, owner_expr = owner_scope_columns(documents)
    assert scope_col is documents.c.scope
    sql = str(select(owner_expr).compile())
    # SELECT-column 판: user_id를 owner_id로 라벨, company는 NULL(불변 company ⇒ owner NULL).
    assert "user_id" in sql
    assert "owner_id" in sql  # .label("owner_id")
    assert "CASE" in sql.upper()  # else NULL 분기 존재


def test_owner_scope_columns_unmapped_raises():
    from orthus.tables import users  # owner-컬럼 없는 임의 테이블

    with pytest.raises(KeyError):
        owner_scope_columns(users)


# --- K7.2 read-side predicate (pure) -------------------------------------------


def test_visibility_predicate_binary_form_var_and_caller_only():
    # owner-only binary: scope OR owner_id=$caller. NO role term (B-admin).
    assert visibility_predicate("n") == "(n.scope = 'company' OR n.owner_id = $caller)"
    assert visibility_predicate("e", "caller") == "(e.scope = 'company' OR e.owner_id = $caller)"
    # value goes through driver binding ($caller) — only identifiers are interpolated.
    frag = visibility_predicate("x", "viewer")
    assert "$viewer" in frag and "role" not in frag.lower()


def test_visibility_predicate_byte_identical_across_vars():
    # Same (var, caller) => byte-identical string, so a hand-edit cannot diverge one
    # template's predicate from another (the registry-completeness anchor).
    assert visibility_predicate("a") == visibility_predicate("a")


def test_visibility_module_carries_sharing_hard_prerequisite_sentence():
    # docs-check/grep anchor: turning the $visible_ids slot on is structurally blocked
    # until the dynamic proof harness lands.
    import orthus.kg.visibility as vis

    doc = visibility_predicate.__doc__ or ""
    sentence = (
        "Binding `$visible_ids` to a non-empty set requires the dynamic-`$visible_ids` "
        "proof harness to land first; until then `$visible_ids` is never bound and "
        "`shared-collaborator` grants no visibility."
    )
    assert sentence in doc
    # the predicate builder must not even accept a visible_ids/role parameter in K7.
    import inspect

    params = set(inspect.signature(vis.visibility_predicate).parameters)
    assert params == {"var", "caller_param"}
