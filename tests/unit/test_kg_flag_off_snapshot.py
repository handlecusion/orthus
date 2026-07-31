"""K7.1 §1.5 — flag-off SELECT byte-identical 회귀 (DB 불필요, SQL 컴파일만).

owner-scope hard-guard가 꺼졌을 때(=COMPANY 필터) projection SELECT가 v1과
구조적으로 동일해야 한다 — 이 동일성이 **fast-rollback 전제**다(코드만 머지하고
flag를 안 켜면 동작 불변). owner-scope 컬럼/WHERE 위닝이 flag-off 질의에 새지
않음을 컴파일된 SQL에서 고정한다.

검사는 **구조 마커**(컬럼명·`IS NOT NULL` 키워드)로 한다 — scope 값('company'/
'personal')은 bound parameter(`:scope_1`)로 렌더되어 SQL 문자열에 리터럴로 나타나지
않기 때문이다. `.compile()`은 offline(연결 불필요)이라 CI/로컬 어디서나 DB 없이 돈다.
"""

from __future__ import annotations

from orthus.kg.project import ScopeFilter, _doc_select, _fact_select, _page_select

_SELECTS = {
    "page": _page_select,
    "doc": _doc_select,
    "fact": _fact_select,
}


def _sql(fn, scope_filter) -> str:
    return str(fn(scope_filter).compile())


def test_company_filter_has_no_owner_widening():
    # flag-off 경로엔 owner-inclusive WHERE(IS NOT NULL OR ...) 위닝이 없다.
    for name, fn in _SELECTS.items():
        sql = _sql(fn, ScopeFilter.COMPANY)
        assert "IS NOT NULL" not in sql, f"{name}: flag-off가 owner-inclusive로 넓어짐"
    # owner 컬럼을 SELECT하지 않는다 (page/fact→owner_id, doc→user_id).
    assert "owner_id" not in _sql(_page_select, ScopeFilter.COMPANY)
    assert "owner_id" not in _sql(_fact_select, ScopeFilter.COMPANY)
    assert "user_id" not in _sql(_doc_select, ScopeFilter.COMPANY)


def test_owner_filter_widens_to_owner_scope():
    # flag-on(COMPANY_PLUS_OWNER)은 owner 컬럼을 SELECT하고 owner-inclusive로 넓어진다.
    page_sql = _sql(_page_select, ScopeFilter.COMPANY_PLUS_OWNER)
    fact_sql = _sql(_fact_select, ScopeFilter.COMPANY_PLUS_OWNER)
    doc_sql = _sql(_doc_select, ScopeFilter.COMPANY_PLUS_OWNER)
    assert "owner_id" in page_sql and "owner_id" in fact_sql
    # documents는 owner_id 컬럼이 없어 user_id를 owner_id로 라벨링한다.
    assert "owner_id" in doc_sql and "user_id" in doc_sql
    # owner-inclusive WHERE 위닝(`<owner_col> IS NOT NULL`)이 세 SELECT 모두에 있다.
    for sql in (page_sql, fact_sql, doc_sql):
        assert "IS NOT NULL" in sql


def test_company_and_owner_filters_differ():
    # 두 필터가 실제로 다른 SQL을 낸다(flag가 의미 있게 작동).
    for fn in _SELECTS.values():
        assert _sql(fn, ScopeFilter.COMPANY) != _sql(fn, ScopeFilter.COMPANY_PLUS_OWNER)
