"""`python -m orthus.kg.bootstrap` — constraints/index 멱등 적용 + KgMeta 초기화
(backs `make kg-bootstrap`).

DDL 원본은 `docs/kg-model.md` §2 코드블록이 canonical이다. K2 이후 스키마가
늘면 kg-model §2와 같은 PR에서 아래 목록을 갱신한다. 전부 `IF NOT EXISTS`라
2회 실행 무변경(멱등)이 K1 Verify다.

CLI는 fail-loud다(구현 명세 §2.6): flag off/미가용이면 exit≠0 + stderr 사유 —
스케줄러/운영자가 인지해야 한다.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from pydantic import BaseModel

from orthus.audit.logger import audit
from orthus.kg.client import KgDisabled, KgUnavailable, get_kg_driver, kg_enabled
from orthus.kg.schema import KG_SCHEMA_VERSION

if TYPE_CHECKING:
    import neo4j

# (name, DDL) — name은 SHOW CONSTRAINTS/INDEXES 결과와 diff하는 보고용 키.
_CONSTRAINTS: list[tuple[str, str]] = [
    # 정체성 키는 PG PK(page_id/doc_id/row_id)다. slug는 정체성 키가 아니다 —
    # PG 유니크가 (slug, scope, owner_id) 복합이고 Neo4j Community에는 복합
    # 유니크(node key)가 없다(kg-model §2).
    (
        "page_node_id",
        "CREATE CONSTRAINT page_node_id IF NOT EXISTS FOR (p:WikiPage) REQUIRE p.page_id IS UNIQUE",
    ),
    (
        "claim_page_id",
        "CREATE CONSTRAINT claim_page_id IF NOT EXISTS "
        "FOR (c:WikiClaim) REQUIRE c.page_id IS UNIQUE",
    ),
    (
        "source_page_id",
        "CREATE CONSTRAINT source_page_id IF NOT EXISTS "
        "FOR (s:WikiSource) REQUIRE s.page_id IS UNIQUE",
    ),
    (
        "doc_id",
        "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    ),
    (
        "fact_row_id",
        "CREATE CONSTRAINT fact_row_id IF NOT EXISTS "
        "FOR (f:StructuredFact) REQUIRE f.row_id IS UNIQUE",
    ),
    (
        "project_name",
        "CREATE CONSTRAINT project_name IF NOT EXISTS FOR (j:Project) REQUIRE j.name IS UNIQUE",
    ),
    (
        "kg_meta_id",
        # 싱글턴 — id='kg_meta'
        "CREATE CONSTRAINT kg_meta_id IF NOT EXISTS FOR (m:KgMeta) REQUIRE m.id IS UNIQUE",
    ),
    (
        "outbox_marker",
        "CREATE CONSTRAINT outbox_marker IF NOT EXISTS "
        "FOR (o:OutboxApplied) REQUIRE o.outbox_id IS UNIQUE",
    ),
    (
        # K6 — :Entity entity_key UNIQUE. K7 v2부터 그래프 노드 키는 `company:` 접두
        # (`<scope-prefix>:kind:name_norm`, projection-only — PG `kg_entities.entity_key`는
        # `kind:name_norm` 유지). 접두 값도 유니크라 제약 불변(kg-model §4 ADR).
        "entity_key",
        "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_key IS UNIQUE",
    ),
]

_INDEXES: list[tuple[str, str]] = [
    ("wiki_slug_idx", "CREATE INDEX wiki_slug_idx IF NOT EXISTS FOR (p:WikiPage) ON (p.slug)"),
    ("claim_slug_idx", "CREATE INDEX claim_slug_idx IF NOT EXISTS FOR (c:WikiClaim) ON (c.slug)"),
    # K7 (additive — KG_SCHEMA_VERSION bump은 placeholder 재키잉 때문이고 이 인덱스
    # 자체는 additive). ns_slug는 placeholder MERGE/promote/prune 매칭 키이고(없으면
    # 전수 스캔), scope/owner_id는 owner predicate + monitor 경계 카운터의 bounded-cost
    # 전제다(plan §1.1/§2). owner-scope off여도 무해(빈/단일값 인덱스).
    (
        "wiki_ns_slug_idx",
        "CREATE INDEX wiki_ns_slug_idx IF NOT EXISTS FOR (p:WikiPage) ON (p.ns_slug)",
    ),
    ("wiki_scope_idx", "CREATE INDEX wiki_scope_idx IF NOT EXISTS FOR (p:WikiPage) ON (p.scope)"),
    (
        "wiki_owner_idx",
        "CREATE INDEX wiki_owner_idx IF NOT EXISTS FOR (p:WikiPage) ON (p.owner_id)",
    ),
    # K7.2 Step 0 (L1) — claim/source scope+owner_id 인덱스. K7.1이 :WikiPage에만
    # scope/owner 인덱스를 넣었으므로 owner-bearing label 중 남은 :WikiClaim/:WikiSource를
    # 보강한다. monitor/erasure label-scoped 쿼리와 (claim/source를 start node로 잡는
    # 미래 템플릿)의 index-backed 전제다. per-hop traversal 술어는 index-seek 대상이
    # 아니므로 forward-justified이며 K7.2 자체엔 측정 가능한 latency delta가 없다
    # (docs/kg-k7.2-plan.md Step 0 / §6). :Document/:StructuredFact는 같은 이유로 이연.
    # owner-scope off여도 무해(빈/단일값 인덱스).
    (
        "claim_scope_idx",
        "CREATE INDEX claim_scope_idx IF NOT EXISTS FOR (c:WikiClaim) ON (c.scope)",
    ),
    (
        "claim_owner_idx",
        "CREATE INDEX claim_owner_idx IF NOT EXISTS FOR (c:WikiClaim) ON (c.owner_id)",
    ),
    (
        "source_scope_idx",
        "CREATE INDEX source_scope_idx IF NOT EXISTS FOR (s:WikiSource) ON (s.scope)",
    ),
    (
        "source_owner_idx",
        "CREATE INDEX source_owner_idx IF NOT EXISTS FOR (s:WikiSource) ON (s.owner_id)",
    ),
    (
        "fact_record_type",
        "CREATE INDEX fact_record_type IF NOT EXISTS FOR (f:StructuredFact) ON (f.record_type)",
    ),
    (
        "fact_valid_until",
        # TTL 관측/집계용 — v1 질의 TTL 필터는 게이트 결과 매퍼 단일 지점(구현 명세 §6.1)
        "CREATE INDEX fact_valid_until IF NOT EXISTS FOR (f:StructuredFact) ON (f.valid_until)",
    ),
    (
        "last_access_idx",
        "CREATE INDEX last_access_idx IF NOT EXISTS FOR (p:WikiPage) ON (p.last_accessed_at)",
    ),
    (
        "conflict_status_idx",
        "CREATE INDEX conflict_status_idx IF NOT EXISTS FOR ()-[c:CONFLICTS_WITH]-() ON (c.status)",
    ),
    (
        # K6 — entity_mentions 템플릿이 name_norm으로 매칭한다(구현 명세 §9.1).
        "entity_name_norm_idx",
        "CREATE INDEX entity_name_norm_idx IF NOT EXISTS FOR (e:Entity) ON (e.name_norm)",
    ),
]


class BootstrapReport(BaseModel):
    constraints_applied: list[str]
    indexes_applied: list[str]
    already_present: list[str]
    kg_schema_version: int


def apply_schema(driver: "neo4j.Driver") -> BootstrapReport:
    """kg-model §2의 constraints/index DDL을 순서대로 멱등 적용한다."""
    constraints_applied: list[str] = []
    indexes_applied: list[str] = []
    already_present: list[str] = []
    with driver.session(database="neo4j") as s:
        existing = {r["name"] for r in s.run("SHOW CONSTRAINTS YIELD name")}
        existing |= {r["name"] for r in s.run("SHOW INDEXES YIELD name")}
        for name, ddl in _CONSTRAINTS:
            s.run(ddl).consume()
            (already_present if name in existing else constraints_applied).append(name)
        for name, ddl in _INDEXES:
            s.run(ddl).consume()
            (already_present if name in existing else indexes_applied).append(name)
    return BootstrapReport(
        constraints_applied=constraints_applied,
        indexes_applied=indexes_applied,
        already_present=already_present,
        kg_schema_version=KG_SCHEMA_VERSION,
    )


def ensure_kg_meta(driver: "neo4j.Driver") -> None:
    """KgMeta 싱글턴 MERGE — 기존 노드의 version은 건드리지 않는다(version 승급은
    K2 rebuild만 한다, 구현 명세 §4.7)."""
    with driver.session(database="neo4j") as s:
        s.run(
            "MERGE (m:KgMeta {id:'kg_meta'}) "
            "ON CREATE SET m.kg_schema_version=$v, m.last_sync_at=null, m.last_rebuild_at=null",
            v=KG_SCHEMA_VERSION,
        ).consume()


def main() -> int:
    if not kg_enabled():
        print("kg bootstrap refused: ORTHUS_KG_ENABLED=false (fail-closed)", file=sys.stderr)
        return 1
    try:
        driver = get_kg_driver()
        with audit("kg.bootstrap") as span:
            report = apply_schema(driver)
            ensure_kg_meta(driver)
            span.add_meta(
                constraints_applied=len(report.constraints_applied),
                indexes_applied=len(report.indexes_applied),
                already_present=len(report.already_present),
                kg_schema_version=report.kg_schema_version,
            )
    except (KgDisabled, KgUnavailable) as exc:
        print(f"kg bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
