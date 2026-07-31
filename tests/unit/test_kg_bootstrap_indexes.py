"""K7.2 Step 0 (L1) — owner-bearing scope/owner_id 인덱스가 bootstrap 목록에 존재.

순수 상수 검사(neo4j 불필요). 멱등 적용/생성 자체는
`tests/integration/test_kg_bootstrap.py`가 neo4j-test로 증명한다(거기 `_INDEX_NAMES`가
이 상수에서 파생되므로 신규 인덱스도 자동 커버). 이 unit 테스트는 owner-scope read
predicate/monitor/erasure가 의존하는 인덱스가 **빠지면 즉시 red**가 되도록 고정한다.
"""

from __future__ import annotations

from orthus.kg.bootstrap import _INDEXES

_INDEX_NAMES = {name for name, _ in _INDEXES}


def test_owner_bearing_scope_owner_indexes_present():
    # :WikiPage는 K7.1이, :WikiClaim/:WikiSource는 K7.2 Step 0이 추가한다.
    expected = {
        "wiki_scope_idx",
        "wiki_owner_idx",
        "claim_scope_idx",
        "claim_owner_idx",
        "source_scope_idx",
        "source_owner_idx",
    }
    missing = expected - _INDEX_NAMES
    assert not missing, f"missing owner-scope indexes in bootstrap._INDEXES: {sorted(missing)}"


def test_index_names_unique():
    names = [name for name, _ in _INDEXES]
    assert len(names) == len(set(names)), "duplicate index name in bootstrap._INDEXES"
