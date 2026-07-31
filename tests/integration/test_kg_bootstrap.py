"""K1 — constraints/index 부트스트랩 멱등 + KgMeta 싱글턴 (neo4j-test 필요).

neo4j-test(:7688)가 없으면 skip된다 — `make kg-test-up`으로 기동.
docs/kg-implementation-spec.md §3.4 integration 행.
"""

from __future__ import annotations

from orthus.kg.bootstrap import (
    _CONSTRAINTS,
    _INDEXES,
    KG_SCHEMA_VERSION,
    apply_schema,
    ensure_kg_meta,
)

_CONSTRAINT_NAMES = {name for name, _ in _CONSTRAINTS}
_INDEX_NAMES = {name for name, _ in _INDEXES}


def _drop_kg_schema(driver) -> None:
    """세션 내 선행 테스트가 남긴 constraints/index를 걷어내 결정론 시작점을 만든다."""
    with driver.session(database="neo4j") as s:
        for name in _CONSTRAINT_NAMES:
            s.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
        for name in _INDEX_NAMES:
            s.run(f"DROP INDEX {name} IF EXISTS").consume()


def test_bootstrap_idempotent(kg_clean):
    driver = kg_clean
    _drop_kg_schema(driver)

    first = apply_schema(driver)
    assert set(first.constraints_applied) == _CONSTRAINT_NAMES
    assert set(first.indexes_applied) == _INDEX_NAMES
    assert first.already_present == []
    assert first.kg_schema_version == KG_SCHEMA_VERSION

    second = apply_schema(driver)
    assert second.constraints_applied == []
    assert second.indexes_applied == []
    assert set(second.already_present) == _CONSTRAINT_NAMES | _INDEX_NAMES


def test_bootstrap_creates_kg_meta_once(kg_clean):
    driver = kg_clean

    ensure_kg_meta(driver)
    with driver.session(database="neo4j") as s:
        rows = list(s.run("MATCH (m:KgMeta) RETURN m.id AS id, m.kg_schema_version AS v"))
    assert len(rows) == 1
    assert rows[0]["id"] == "kg_meta"
    assert rows[0]["v"] == KG_SCHEMA_VERSION

    # 기존 노드의 version은 ensure가 건드리지 않는다 — 승급은 K2 rebuild 전용.
    with driver.session(database="neo4j") as s:
        s.run("MATCH (m:KgMeta {id:'kg_meta'}) SET m.kg_schema_version = 999").consume()
    ensure_kg_meta(driver)
    with driver.session(database="neo4j") as s:
        rows = list(s.run("MATCH (m:KgMeta) RETURN m.id AS id, m.kg_schema_version AS v"))
    assert len(rows) == 1
    assert rows[0]["v"] == 999
