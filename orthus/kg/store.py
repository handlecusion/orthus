"""K2 — Neo4j MERGE batch writer + prune(삭제 수렴) + KgMeta read/write.

이 모듈은 그래프 *쓰기* 절반이다 — 입력은 project.py가 만든 plan이고, 모든
쓰기는 `kg_write_session()` 세션으로만 받는다(client.py 호출 규약 §2.3).
라벨/관계/MERGE 키는 schema.py enum에서만 조립한다 — 사용자 입력이 Cypher
문자열에 보간되는 일이 없다(파라미터는 전부 바인딩).

멱등 계약: 모든 쓰기는 MERGE + `SET += props`라 같은 plan을 두 번 적용해도
그래프가 변하지 않는다(`test_rebuild_idempotent_two_runs`). `last_accessed_at`은
projection props에 없으므로 ON MATCH SET에서 보존된다(§4.5).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from orthus.kg.schema import (
    PROJECTS,
    WIKI_LINK_RELS,
    KgEdgeRow,
    Label,
    Rel,
)
from orthus.kg.visibility import COMPANY_NS, entity_node_key_for

if TYPE_CHECKING:
    import neo4j

    from orthus.kg.project import ExpectedKeys, KgProjectionPlan

BATCH_SIZE = 1000  # rows/tx (kg-model §3)

# prune 대상에서 제외되는 동기화 메타 라벨(§4.6 4단계).
_META_LABELS = (Label.KG_META, Label.OUTBOX_APPLIED)

# 라벨별 정체성 MERGE 키 — placeholder(:WikiPage by slug)만 예외.
_LABEL_KEYS: dict[Label, str] = {
    Label.WIKI_PAGE: "page_id",
    Label.WIKI_CLAIM: "page_id",
    Label.WIKI_SOURCE: "page_id",
    Label.DOCUMENT: "doc_id",
    Label.STRUCTURED_FACT: "row_id",
    Label.PROJECT: "name",
    Label.ENTITY: "entity_key",  # K6
}


class KgMeta(BaseModel):
    """`:KgMeta` 싱글턴 사상 — watermark/버전/락(§4.7). PG에 KG 상태를 저장하지
    않는다(kg-model §3): volume 유실 시 watermark도 함께 사라져 full rebuild로
    자연 수렴한다."""

    last_sync_at: datetime | None = None
    last_rebuild_at: datetime | None = None
    kg_schema_version: int | None = None
    # K7.5 — clock-락(`rebuild_lock_until`)을 **boolean HOLD**로 교체. rebuild 시작에 True,
    # 완료/abort에 clear한다. sync/worker는 시간 산술 없이 이 플래그만 본다(코드리뷰 단순화).
    # SIGKILL stuck-true의 자동치유는 없다 — monitor가 `rebuild_started_at` 경과로 감지하고
    # 복구는 rebuild 재실행이다(operations.md §2.1 stuck-rebuild 스텝).
    rebuild_in_progress: bool = False
    rebuild_started_at: datetime | None = None  # stuck-rebuild 경과 판정용
    last_rebuild_seconds: float | None = None  # 마지막 full rebuild 소요(headroom 측정)
    # K7.5 (review #1) — HOLD를 건 작업 종류("rebuild" | "erase"). 같은 boolean HOLD를
    # 재사용하되, erase HOLD는 sub-second로 끝나야 하므로 monitor가 clear-실패 stuck을
    # rebuild의 30분 headroom이 아니라 훨씬 짧은 고정 임계로 잡게 한다(감지 latency만 종류별
    # 분리). 미설정(None)은 rebuild로 수렴 — 기존 그래프/레거시 HOLD도 안전한 보수값.
    rebuild_hold_kind: str | None = None


class PruneReport(BaseModel):
    edges_deleted: int = 0
    placeholders_deleted: int = 0
    nodes_deleted: int = 0


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _to_native(value: Any) -> Any:
    """neo4j.time.DateTime → datetime (driver 타입을 모듈 밖으로 내보내지 않는다)."""
    if value is None:
        return None
    to_native = getattr(value, "to_native", None)
    return to_native() if callable(to_native) else value


# --- 노드/엣지 MERGE -------------------------------------------------------------


def merge_nodes(
    session: "neo4j.Session",
    label: Label,
    key: str,
    rows: list[dict[str, Any]],
    *,
    batch_size: int = BATCH_SIZE,
    on_create_only: bool = False,
) -> int:
    """rows: [{"merge_value": str, "props": dict}]. `on_create_only`는 placeholder
    전용 — 같은 slug의 노드(placeholder든 실체든)가 이미 있으면 건드리지 않는다."""
    if not rows:
        return 0
    on_match = "" if on_create_only else "ON MATCH SET n += row.props"
    cypher = (
        f"UNWIND $rows AS row "
        f"MERGE (n:{label.value} {{{key}: row.merge_value}}) "
        f"ON CREATE SET n += row.props, n.last_accessed_at = null "
        f"{on_match}"
    )
    total = 0
    for chunk in _chunks(rows, batch_size):
        session.run(cypher, rows=chunk).consume()
        total += len(chunk)
    return total


def promote_placeholders(
    session: "neo4j.Session", rows: list[dict[str, Any]], *, batch_size: int = BATCH_SIZE
) -> int:
    """실체 :WikiPage upsert 전에 같은 **ns_slug**의 placeholder를 승격한다(§4.4) —
    같은 네임스페이스 slug의 placeholder/실체 노드가 중복 공존하지 않게 한다. K7 v2는
    bare slug가 아니라 ns_slug로 매칭하므로 owner-A의 placeholder가 company 실체
    page로 잘못 승격되지 않는다(B4). rows는 merge_nodes와 같은 형태(실체 page rows —
    props.ns_slug 필수, build()가 모든 실체 WikiPage에 싣는다)다."""
    if not rows:
        return 0
    cypher = (
        "UNWIND $rows AS row "
        "MATCH (ph:WikiPage {ns_slug: row.props.ns_slug}) WHERE ph.page_id IS NULL "
        "SET ph.page_id = row.merge_value, ph += row.props"
    )
    for chunk in _chunks(rows, batch_size):
        session.run(cypher, rows=chunk).consume()
    return len(rows)


def merge_edges(
    session: "neo4j.Session",
    rel: Rel,
    src_label: Label,
    src_key: str,
    dst_label: Label,
    dst_key: str,
    rows: list[dict[str, Any]],
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """rows: [{"src": str, "dst": str, "props": dict}]. src/dst 미존재 row는 MATCH에서
    조용히 탈락한다 — 노드 MERGE가 항상 선행하므로 정상 경로에선 발생하지 않는다."""
    if not rows:
        return 0
    cypher = (
        f"UNWIND $rows AS row "
        f"MATCH (s:{src_label.value} {{{src_key}: row.src}}) "
        f"MATCH (d:{dst_label.value} {{{dst_key}: row.dst}}) "
        f"MERGE (s)-[r:{rel.value}]->(d) "
        f"SET r += row.props"
    )
    total = 0
    for chunk in _chunks(rows, batch_size):
        session.run(cypher, rows=chunk).consume()
        total += len(chunk)
    return total


def replace_page_link_edges(
    session: "neo4j.Session", src_page_id: str, edges: list[KgEdgeRow]
) -> None:
    """단일 page의 wiki_links 유래 엣지를 통째로 재투영한다 — K3 outbox 단일
    이벤트 적용이 사용(`store._replace_links`의 그래프판, §4.5). EXTRACTED_FROM/
    IN_PROJECT는 컬럼 유래라 삭제 대상에서 제외한다."""
    session.run(
        "MATCH (s {page_id: $src})-[r]->() WHERE type(r) IN $link_rels DELETE r",
        src=src_page_id,
        link_rels=[r.value for r in WIKI_LINK_RELS],
    ).consume()
    for edge in edges:
        if edge.rel not in WIKI_LINK_RELS or edge.src[1] != src_page_id:
            continue
        merge_edges(
            session,
            edge.rel,
            edge.src_label,
            edge.src[0],
            edge.dst_label,
            edge.dst[0],
            [{"src": edge.src[1], "dst": edge.dst[1], "props": edge.props}],
        )


def merge_all(session: "neo4j.Session", plan: "KgProjectionPlan") -> dict[str, int]:
    """plan 전체를 의존 순서대로 MERGE한다: Project/Document/Fact/Claim/Source →
    실체 WikiPage(placeholder 승격 선행) → placeholder → 엣지."""
    by_label: dict[tuple[Label, str, bool], list[dict[str, Any]]] = {}
    placeholders: list[dict[str, Any]] = []
    real_pages: list[dict[str, Any]] = []
    for node in plan.nodes:
        row = {"merge_value": node.merge_value, "props": node.props}
        if node.label is Label.WIKI_PAGE:
            # K7 v2: placeholder는 ns_slug로 키잉된다(실체 page는 page_id).
            (placeholders if node.merge_key == "ns_slug" else real_pages).append(row)
        else:
            by_label.setdefault((node.label, node.merge_key, False), []).append(row)

    applied: dict[str, int] = {}
    for (label, key, _), rows in by_label.items():
        applied[f"merged_{label.value}"] = merge_nodes(session, label, key, rows)
    promote_placeholders(session, real_pages)
    applied[f"merged_{Label.WIKI_PAGE.value}"] = merge_nodes(
        session, Label.WIKI_PAGE, "page_id", real_pages
    )
    applied["merged_placeholder"] = merge_nodes(
        session, Label.WIKI_PAGE, "ns_slug", placeholders, on_create_only=True
    )

    by_shape: dict[tuple[Rel, Label, str, Label, str], list[dict[str, Any]]] = {}
    for edge in plan.edges:
        shape = (edge.rel, edge.src_label, edge.src[0], edge.dst_label, edge.dst[0])
        by_shape.setdefault(shape, []).append(
            {"src": edge.src[1], "dst": edge.dst[1], "props": edge.props}
        )
    for (rel, src_label, src_key, dst_label, dst_key), rows in by_shape.items():
        count = merge_edges(session, rel, src_label, src_key, dst_label, dst_key, rows)
        applied[f"merged_edges_{rel.value}"] = applied.get(f"merged_edges_{rel.value}", 0) + count
    return applied


# --- prune (rebuild 전용 삭제 수렴, §4.6) ----------------------------------------


def prune(session: "neo4j.Session", expected: "ExpectedKeys") -> PruneReport:
    report = PruneReport()

    # 1. 기대 외 엣지 — 그래프 전수 조회 후 Python diff(9천 page 규모 — 단순함 우선).
    meta_guard = " AND ".join(
        f"NOT a:{label.value} AND NOT b:{label.value}" for label in _META_LABELS
    )
    records = session.run(
        f"MATCH (a)-[r]->(b) WHERE {meta_guard} "
        "RETURN elementId(r) AS rid, "
        # entity_key 포함 — K6 MENTIONED_IN/RELATES_TO 엣지의 :Entity 끝점이
        # diff 키로 잡힌다(누락 시 매 rebuild마다 entity 엣지 전멸 — 검토 BLOCKER).
        # K7 v2: ns_slug를 slug보다 먼저 — placeholder dst의 diff 키가 ns_slug라
        # expected.edge_triples(ns_slug 사용)와 일치한다. 잔존 v1 placeholder(ns_slug
        # NULL)는 slug로 떨어져 expected에 없으니 그 엣지가 prune된다.
        "coalesce(a.page_id, a.doc_id, a.row_id, a.name, a.entity_key, a.ns_slug, a.slug) AS src, "
        "type(r) AS rel, "
        "coalesce(b.page_id, b.doc_id, b.row_id, b.name, b.entity_key, b.ns_slug, b.slug) AS dst"
    )
    stale_edge_ids = [
        rec["rid"]
        for rec in records
        if (rec["src"], rec["rel"], rec["dst"]) not in expected.edge_triples
    ]
    for i in range(0, len(stale_edge_ids), BATCH_SIZE):
        session.run(
            "MATCH ()-[r]->() WHERE elementId(r) IN $ids DELETE r",
            ids=stale_edge_ids[i : i + BATCH_SIZE],
        ).consume()
    report.edges_deleted = len(stale_edge_ids)

    # 2. 기대 외 placeholder + v1→v2 stale placeholder purge(B4). expected.placeholder_slugs는
    # 이제 ns_slug 공간이다. `ns_slug IS NULL`(=v1 bare-slug placeholder)도 함께 지운다 —
    # 안 지우면 personal-origin slug를 단 v1 placeholder가 scope='company'로 orphan돼
    # 모든 B1 predicate를 통과하는 cross-owner 존재 오라클이 된다(plan §2). v2 rebuild
    # 1회로 수렴하며, monitor의 `page_id NULL AND ns_slug NULL` 카운터가 잔존을 상시 감시.
    summary = session.run(
        "MATCH (p:WikiPage) WHERE p.page_id IS NULL "
        "AND (p.ns_slug IS NULL OR NOT p.ns_slug IN $ns_slugs) DETACH DELETE p",
        ns_slugs=sorted(expected.placeholder_slugs),
    ).consume()
    report.placeholders_deleted = summary.counters.nodes_deleted

    # 3. 라벨별 실체 노드 — `key IS NOT NULL`이 placeholder(2에서 처리)를 명시 제외.
    # wiki 3종은 라벨별 set이다 — 합집합이면 kind가 바뀐 page_id의 구 라벨
    # 노드가 prune을 영원히 통과한다(ExpectedKeys docstring).
    expected_by_label: dict[Label, set[str]] = {
        Label.WIKI_PAGE: expected.page_ids_by_label.get(Label.WIKI_PAGE.value, set()),
        Label.WIKI_CLAIM: expected.page_ids_by_label.get(Label.WIKI_CLAIM.value, set()),
        Label.WIKI_SOURCE: expected.page_ids_by_label.get(Label.WIKI_SOURCE.value, set()),
        Label.DOCUMENT: expected.doc_ids,
        Label.STRUCTURED_FACT: expected.row_ids,
        Label.PROJECT: set(PROJECTS),
        Label.ENTITY: expected.entity_keys,  # K6
    }
    deleted = 0
    for label, keys in expected_by_label.items():
        key = _LABEL_KEYS[label]
        summary = session.run(
            f"MATCH (n:{label.value}) "
            f"WHERE n.{key} IS NOT NULL AND NOT n.{key} IN $keys DETACH DELETE n",
            keys=sorted(keys),
        ).consume()
        deleted += summary.counters.nodes_deleted
    report.nodes_deleted = deleted
    # 4. :KgMeta / :OutboxApplied는 건드리지 않는다(동기화 메타).
    return report


def delete_entity_nodes(session: "neo4j.Session", entity_keys: list[str]) -> int:
    """:Entity 노드를 detach-delete (K6 erasure §9.4).

    outbox `entity_kind`는 entity를 지원하지 않으므로(wiki_page/document/structured_row
    뿐) person-entity orphan은 이 동기 경로로 지운다. 입력은 PG `kg_entities.entity_key`
    (`'kind:name_norm'`, unprefixed)이고, 그래프 노드 키는 `company:` 접두(K7 v2)라
    **반드시 `entity_node_key_for`로 접두해야** 접두 노드를 놓치지 않는다(erasure 누락
    = 잔존 PII). 키는 driver 바인딩으로만 전달. 삭제된 노드 수를 반환한다(없으면 0)."""
    if not entity_keys:
        return 0
    keys = [entity_node_key_for(COMPANY_NS, k) for k in entity_keys]
    summary = session.run(
        "UNWIND $keys AS k MATCH (e:Entity {entity_key: k}) DETACH DELETE e",
        keys=keys,
    ).consume()
    return summary.counters.nodes_deleted


def delete_owner_scope_graph(session: "neo4j.Session", owner_id: str) -> tuple[int, int]:
    """K7.5 — owner-scope(`scope='personal' AND owner_id=$owner_id`) 노드 DETACH DELETE +
    **명시적 personal-owned 엣지 sweep**(defense-in-depth). `(nodes_deleted, edges_deleted)` 반환.

    노드 DETACH DELETE는 삭제된 personal 노드에 인접한 모든 엣지를 함께 제거한다. 추가 엣지
    sweep은 **양 끝점이 company인데 엣지 자체가 owner-personal-owned**인 mixed-endpoint 케이스를
    잡는다(monitor `personal_edges` tripwire와 정합). `owner_id`는 driver 바인딩($owner_id)으로만
    전달한다(f-string 보간 금지). label-agnostic `MATCH (n)`/`()-[r]->()`라 owner 모든 라벨을
    덮는다. :KgMeta/:OutboxApplied 동기화 메타는 scope/owner_id 속성이 없어 매치되지 않는다.

    :Entity는 관여하지 않는다 — owner-scope 엔티티는 personal-entity 슬라이스로 연기되어
    오늘 `kg_entities.owner_id`는 항상 NULL(`tables.py` "K6=NULL")이라 owner-scope 엔티티가
    0개다(영구 설계 주장 아님, 현 단계 skip)."""
    node_summary = session.run(
        "MATCH (n) WHERE n.scope = 'personal' AND n.owner_id = $owner_id DETACH DELETE n",
        owner_id=owner_id,
    ).consume()
    edge_summary = session.run(
        "MATCH ()-[r]->() WHERE r.scope = 'personal' AND r.owner_id = $owner_id DELETE r",
        owner_id=owner_id,
    ).consume()
    nodes_deleted = node_summary.counters.nodes_deleted
    edges_deleted = (
        node_summary.counters.relationships_deleted + edge_summary.counters.relationships_deleted
    )
    return nodes_deleted, edges_deleted


# --- KgMeta ---------------------------------------------------------------------


def get_meta(session: "neo4j.Session") -> KgMeta | None:
    record = session.run("MATCH (m:KgMeta {id:'kg_meta'}) RETURN m").single()
    if record is None:
        return None
    node = record["m"]
    return KgMeta(
        last_sync_at=_to_native(node.get("last_sync_at")),
        last_rebuild_at=_to_native(node.get("last_rebuild_at")),
        kg_schema_version=node.get("kg_schema_version"),
        # 미설정(None)은 boolean default(False)로 수렴 — never-set graph도 not-locked.
        rebuild_in_progress=bool(node.get("rebuild_in_progress")),
        rebuild_started_at=_to_native(node.get("rebuild_started_at")),
        last_rebuild_seconds=node.get("last_rebuild_seconds"),
        rebuild_hold_kind=node.get("rebuild_hold_kind"),
    )


def set_meta(session: "neo4j.Session", **fields: Any) -> None:
    """None 값은 속성 제거다(Neo4j null SET 의미론) — lock 해제가 이 경로를 쓴다."""
    session.run("MERGE (m:KgMeta {id:'kg_meta'}) SET m += $props", props=fields).consume()


def touch_last_accessed(page_ids: list[str]) -> None:
    """K4 읽기 경로 응답에 포함된 WikiPage/WikiClaim 노드의 `last_accessed_at` SET.

    관측(observability) 전용 속성이다 — kg-model §2 각주 계약. 템플릿 실행은
    READ 세션이므로 질의 종료 후 이 별도 WRITE 세션에서 best-effort로 수행하며,
    실패 처리(swallow + audit meta 카운트)는 호출자(K4 게이트)의 책임이다.
    write session import를 게이트가 아니라 이 모듈(K2 Neo4j writer)이 보유한다
    (§2.3 호출 규약 — 게이트 코드에는 write 경로가 등장하지 않는다)."""
    if not page_ids:
        return
    from orthus.kg.client import kg_write_session  # noqa: PLC0415 — 순환 import 회피

    with kg_write_session() as session:
        for label in ("WikiPage", "WikiClaim"):
            session.run(
                f"UNWIND $ids AS id MATCH (n:{label} {{page_id: id}}) "  # noqa: S608 — 라벨은 고정 상수
                "SET n.last_accessed_at = datetime()",
                ids=page_ids,
            ).consume()
