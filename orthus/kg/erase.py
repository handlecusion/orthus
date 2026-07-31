"""`python -m orthus.kg.erase <page_id> ...` — K6 erasure 전파 (구현 명세 §9.4).

운영자 수동 erasure 절차(operations.md §8.4)의 KG 단계를 결정론 코드로 내린다.
self-service UI는 없다 — 삭제 요청/사고 대응 시 운영자가 지워지는 company wiki
page id 목록으로 이 명령을 실행한다. 절차(§8.4):

  ① 지워진 page의 mention(`kg_entity_mentions`) detach
  ② surviving company mention이 0인 `kg_entities` row 삭제(person-entity orphan)
  ③ **wiki page SoR 삭제**(`wiki_store.delete_item` — `wiki_pages`/`wiki_links`/
     `wiki_chunks`/`embeddings` row + markdown). 이게 핵심: SoR row가 남으면 다음
     full rebuild가 WikiPage 노드를 재생성(resurrection)하므로 erase가 직접 지운다.
  ④ 지워진 page는 `op='delete'` outbox 이벤트로 worker가 WikiPage 노드 drop(준실시간,
     K6이 outbox `op='delete'`의 최초 호출처 — K3엔 호출처가 없었다; rebuild도 SoR
     부재로 prune하므로 이중 안전)
  ⑤ orphan `:Entity` 노드는 즉시 Neo4j detach-delete(outbox `entity_kind`가 entity를
     지원하지 않으므로 — 가장 민감한 PII인 사람 이름은 지연 없이 제거)
  ⑥ 남은 source-layer row(`documents`/`corpus_chunks`/`notion_rows`/`connector_*`)는
     운영자가 operations.md §8.4 목록대로 지우고 `make node-kg-rebuild`로 최종 수렴

PG SoR 삭제(①②③)는 KG 가용 여부와 무관하게 항상 수행한다(privacy 보증의 권위;
wiki authoring은 KG 독립). Neo4j detach(⑤)는 가용할 때 best-effort이고, 미가용이면
rebuild가 수렴시킨다(이때 ③ 덕에 노드가 재생성되지 않는다). company-node 전용
(`require_company_node`) — entity는 company scope(owner_id NULL)다.

원자성·복구: 단계가 여러 트랜잭션/스토어(PG mention·entity·outbox / per-page
delete_item / Neo4j)에 걸쳐 있어 cross-phase atomic이 아니다(PG+Neo4j 2PC 불가).
가장 민감한 PII(person entity **PG row**)는 ①②에서 먼저 commit·삭제되므로 권위 SoR는
항상 지워진다. 그러나 ⑤의 Neo4j `:Entity` 노드 detach가 누락되는 창(예: PG commit 직후
프로세스 사망, 또는 KG 미가용)에서는 orphan `:Entity` 노드가 그래프에 잔존할 수 있고,
이때 **erase 재실행은 이를 복구하지 못한다** — 재실행 시 mention이 이미 삭제돼
`affected`가 비고 detach 단계가 skip되기 때문이다. 이 누락의 권위 복구는
`make node-kg-rebuild`다(prune이 `kg_entities` row 부재로 orphan `:Entity`를 detach).
그래서 operations.md §8.4의 마지막 rebuild는 PII 보증을 위해 **필수**다. PG/wiki SoR
삭제(①②③)와 op='delete'(④)는 멱등하므로 재실행으로 완결되지만, 그래프 orphan 노드
복구만은 rebuild 전용이다.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from orthus.audit.logger import audit
from orthus.db import session
from orthus.kg import store
from orthus.kg.client import kg_available, kg_enabled, kg_write_session, require_company_node
from orthus.kg.gate import owner_footprint
from orthus.kg.outbox import enqueue_kg_event
from orthus.schemas.canonical import OwnerErasureReport, OwnerFootprint
from orthus.tables import kg_entities, kg_entity_mentions, wiki_pages
from orthus.wiki import store as wiki_store

# (M-r1) HR/직원 대상 정규 문구 — 확정 일자 없음(SLA 날짜 약속 아님). content-blind.
_WHAT_REMAINS = (
    "그래프 표시 제거 완료. 저장된 전체 기록의 완전 삭제는 이후 단계에서 진행되며 "
    "확정 일자는 없습니다. / Graph-view suppression complete; full record deletion is "
    "pending in a later phase with no committed date."
)

# (P-r1) confirm 전 CLI WARNING — PG-first 강제. KG-only erase는 forget action이 아니다.
_CONFIRM_WARNING = (
    "경고: 이 명령은 그래프 표시만 제거합니다. 저장된 PG 사본은 삭제되지 않으며(이후 단계), "
    "그래프 재구축(rebuild) 시 다시 투영될 수 있습니다. 완전 삭제는 PG-delete-first 런북 "
    "절차를 따르십시오(operations.md §8.4)."
)


class ErasureReport(BaseModel):
    pages: int = 0  # 입력 page 수
    mentions_deleted: int = 0  # 삭제된 kg_entity_mentions row
    entities_deleted: int = 0  # orphan으로 삭제된 kg_entities row(PG)
    entity_keys: list[str] = Field(default_factory=list)  # 삭제된 orphan entity_key
    wiki_pages_deleted: int = 0  # SoR(rows+markdown)까지 삭제한 wiki page 수
    page_delete_events: int = 0  # enqueue 시도한 op='delete' wiki_page 이벤트
    graph_entities_detached: int | None = (
        None  # 즉시 detach된 :Entity 노드 수(KG off/down이면 None)
    )
    kg_enabled: bool = False
    neo4j_available: bool = False


def erase_kg_for_pages(page_ids: list[UUID]) -> ErasureReport:
    """company wiki page 목록 erasure의 KG 전파. 절차/계약은 모듈 docstring 참조.

    KgDisabled(personal node)는 require_company_node가 던진다 — 호출자 처리."""
    require_company_node()
    report = ErasureReport(pages=len(page_ids), kg_enabled=kg_enabled())
    if not page_ids:
        return report

    with audit("kg.erase") as span:
        with session() as s:
            # wiki page SoR 삭제(③)와 op='delete'(④)를 위해 page_id → (kind, slug)를
            # 삭제 전에 resolve한다. **company scope로 한정**한다 — 이 명령은 company KG
            # erasure이고 mention/entity가 전부 company 전용이다. company node에 공존할 수
            # 있는 owner-scope personal wiki_pages(P8)를 실수로 지우거나(SoR) op='delete'로
            # 노드를 떨어뜨리지 않도록 막는다(worker delete는 page_id만 매치해 scope 가드가
            # 없으므로 — K7이 personal 노드를 page_id로 투영하면 cross-scope detach 위험).
            page_meta = s.execute(
                select(wiki_pages.c.page_id, wiki_pages.c.kind, wiki_pages.c.slug).where(
                    wiki_pages.c.page_id.in_(page_ids),
                    wiki_pages.c.scope == "company",
                    wiki_pages.c.owner_id.is_(None),
                )
            ).all()
            # ① 지워질 page를 mention하던 entity_id를 먼저 포착(삭제 전).
            affected = set(
                s.execute(
                    select(kg_entity_mentions.c.entity_id)
                    .where(kg_entity_mentions.c.page_id.in_(page_ids))
                    .distinct()
                ).scalars()
            )
            del_m = s.execute(
                delete(kg_entity_mentions).where(kg_entity_mentions.c.page_id.in_(page_ids))
            )
            report.mentions_deleted = del_m.rowcount or 0

            # ② 이번 erasure로 mention이 0이 된 entity만 orphan으로 삭제(선별적 —
            #    무관한 기존 row를 건드리지 않는다). surviving mention 조회는 삭제 후.
            if affected:
                surviving = set(
                    s.execute(
                        select(kg_entity_mentions.c.entity_id)
                        .where(kg_entity_mentions.c.entity_id.in_(affected))
                        .distinct()
                    ).scalars()
                )
                orphan_ids = [eid for eid in affected if eid not in surviving]
                if orphan_ids:
                    report.entity_keys = list(
                        s.execute(
                            delete(kg_entities)
                            .where(kg_entities.c.entity_id.in_(orphan_ids))
                            .returning(kg_entities.c.entity_key)
                        ).scalars()
                    )
                    report.entities_deleted = len(report.entity_keys)

            # ④ 지워진 company page 노드는 op='delete' 이벤트로 worker가 drop(enqueue는
            #    kg_disabled면 no-op). raw page_ids가 아니라 resolve된 company page에만
            #    enqueue한다(C6 — company-only 불변을 구조적으로). 같은 PG 트랜잭션에 적재.
            for r in page_meta:
                enqueue_kg_event(
                    s, entity_kind="wiki_page", entity_id=r.page_id, op="delete", scope="company"
                )
                report.page_delete_events += 1
            s.commit()

        # ③ wiki page SoR(rows+markdown) 삭제 — 잔존 row가 다음 rebuild에 노드를
        #    재생성하는 것을 차단(resurrection 방지). KG 가용 무관(wiki는 KG 독립).
        #    delete_item은 자체 세션을 쓰며 없는 row는 graceful no-op.
        for r in page_meta:
            wiki_store.delete_item(r.kind, r.slug, scope="company", owner_id=None)
        report.wiki_pages_deleted = len(page_meta)

        # ⑤ orphan :Entity 즉시 detach (entity_kind 미지원 → outbox 우회). best-effort.
        if report.entity_keys and kg_enabled() and kg_available():
            with kg_write_session() as ws:
                report.graph_entities_detached = store.delete_entity_nodes(ws, report.entity_keys)
            report.neo4j_available = True

        span.add_meta(
            pages=report.pages,
            mentions_deleted=report.mentions_deleted,
            entities_deleted=report.entities_deleted,
            wiki_pages_deleted=report.wiki_pages_deleted,
            page_delete_events=report.page_delete_events,
            graph_entities_detached=report.graph_entities_detached,
        )
    return report


# --- K7.5 owner-scope erasure (그래프 표시 제거; 실제 PG 삭제는 P8 위임 — Q2) -----------


@contextmanager
def _erasure_hold() -> Iterator[None]:
    """erase 동안 outbox worker/sync를 직렬화 — rebuild **boolean HOLD**를 재사용한다(같은
    의미: 그래프 일괄 변경 중 worker 미개입). 같은 owner의 큐된 personal 이벤트를 worker가
    동시에 재투영해 방금 지운 노드를 되살리는 race를 막는다. Neo4j 미가용이면 HOLD set 자체가
    실패하고(=erase도 못 함) 호출부가 그 예외를 받는다. clear 실패는 stuck-true로 남겨
    monitor가 잡는다(rebuild 재실행으로 복구)."""
    with kg_write_session() as s:
        store.set_meta(
            s,
            rebuild_in_progress=True,
            rebuild_started_at=datetime.now(UTC),
            rebuild_hold_kind="erase",
        )
    try:
        yield
    finally:
        try:
            with kg_write_session() as s:
                store.set_meta(
                    s,
                    rebuild_in_progress=False,
                    rebuild_started_at=None,
                    rebuild_hold_kind=None,
                )
        except Exception:  # noqa: BLE001 — clear 실패는 stuck-true로 남기고 monitor가 잡는다
            pass


def _erase_owner_graph(owner_id: UUID, actor_user_id: UUID | None) -> OwnerErasureReport:
    """단일 owner의 그래프 표시 제거 코어(HOLD 안에서 호출 — 단일/배치가 공유).

    content-blind: footprint(카운트만)와 owner_id만 본다 — target의 slug/title/entity_key를
    사전 read하지 않는다(`resolve_slug`/`neighbors` 미호출, T3). audit도 counts-only."""
    owner_s = str(owner_id)
    # content-blind 사전 측정 — 카운트만(slug/title 없음). flag-off면 status=unavailable.
    fp_before = owner_footprint(owner_id)
    nodes = edges = 0
    neo4j_avail = False
    with audit("kg.erase.owner") as span:
        if kg_enabled() and kg_available():
            with kg_write_session() as ws:
                nodes, edges = store.delete_owner_scope_graph(ws, owner_s)
            neo4j_avail = True
        # 삭제 후 재측정 — genuine 0이면 status=ready(O-r1). Neo4j 미가용이면 unavailable.
        fp_after = owner_footprint(owner_id) if neo4j_avail else OwnerFootprint()
        report = OwnerErasureReport(
            target_owner_id=owner_id,
            actor_user_id=actor_user_id,
            erased_at=datetime.now(UTC),
            footprint_before=fp_before,
            footprint_after=fp_after,
            nodes_detached=nodes,
            edges_detached=edges,
            removed_from_graph=neo4j_avail,
            pg_copies_pending=True,  # Q2 — PG owner-row 삭제는 P8 위임(구조적 항상 미완료)
            neo4j_available=neo4j_avail,
            what_remains=_WHAT_REMAINS,
        )
        # T3 — counts only. 절대 foreign slug/title/entity_key를 audit에 남기지 않는다.
        span.add_meta(
            target_owner_id=owner_s,
            actor_present=actor_user_id is not None,
            nodes_detached=nodes,
            edges_detached=edges,
            removed_from_graph=neo4j_avail,
            pg_copies_pending=True,
        )
    return report


def erase_kg_for_owner(owner_id: UUID, *, actor_user_id: UUID | None = None) -> OwnerErasureReport:
    """owner-scope KG erasure — **그래프 표시만** 제거한다(Q2: 실제 PG owner-row 삭제는 P8).

    company-node 전용(`require_company_node` — loopback 공유 Neo4j 방어). 권한(target owner
    본인 + content-blind admin)은 호출자(CLI/Makefile 래퍼)가 건다. `KgDisabled`(personal
    node)는 require_company_node가 던진다 — 호출자 처리. 멱등: 이미 비워진 owner면 nodes=0."""
    require_company_node()
    with _erasure_hold():
        return _erase_owner_graph(owner_id, actor_user_id)


def erase_kg_for_owners(
    owner_ids: list[UUID], *, actor_user_id: UUID | None = None
) -> list[OwnerErasureReport]:
    """(M-r2) 배치 — N owner를 **단일 HOLD 윈도우**에서 순차 erase. 각 erase는 자기 owner
    트랜잭션(`owner_id=$owner_id`)이라 owner A가 B를 건드리지 않는다(§exact-scope 양방향).
    counts-only 통합 closeout artifact(reports list)를 반환한다. company-node 전용."""
    require_company_node()
    reports: list[OwnerErasureReport] = []
    with _erasure_hold():
        for owner_id in owner_ids:
            reports.append(_erase_owner_graph(owner_id, actor_user_id))
    return reports


def _print_owner_report(report: OwnerErasureReport) -> None:
    print(
        f"kg owner erase: owner={report.target_owner_id} "
        f"removed_from_graph={report.removed_from_graph} "
        f"nodes_detached={report.nodes_detached} edges_detached={report.edges_detached} "
        f"neo4j_available={report.neo4j_available}"
    )
    fp = report.footprint_after
    if fp.status == "ready":
        print(f"footprint after: {fp.node_count} nodes / {fp.edge_count} edges (status=ready)")
    else:
        # O-r1 — status!=ready면 0을 성공으로 렌더하지 않는다.
        print(f"footprint after: 확인 보류 중(status={fp.status}) — 그래프 다시 읽는 중")
    print(report.what_remains)
    print(
        f"pg_copies_pending={report.pg_copies_pending} — full deletion follows the "
        "PG-delete-first runbook (operations.md §8.4).",
        file=sys.stderr,
    )


def _read_owner_ids_file(path: str) -> list[UUID]:
    with open(path) as f:
        return [
            UUID(line.strip()) for line in f if line.strip() and not line.strip().startswith("#")
        ]


def _main_owner(rest: list[str]) -> int:
    dry_run = "--dry-run" in rest
    confirm: str | None = None
    positional: list[str] = []
    it = iter(rest)
    for a in it:
        if a == "--dry-run":
            continue
        if a == "--confirm":
            confirm = next(it, None)
            continue
        positional.append(a)
    if len(positional) != 1:
        print(
            "usage: python -m orthus.kg.erase --owner <owner-id> [--dry-run] [--confirm <owner-id>]",
            file=sys.stderr,
        )
        return 2
    try:
        owner_id = UUID(positional[0])
    except ValueError as exc:
        print(f"kg erase: invalid owner id — {exc}", file=sys.stderr)
        return 2

    if dry_run:
        try:
            require_company_node()
            fp = owner_footprint(owner_id)
        except Exception as exc:  # noqa: BLE001 — CLI fail-loud
            print(
                f"kg erase --owner --dry-run failed: {type(exc).__name__}: {exc}", file=sys.stderr
            )
            return 1
        print(
            f"[dry-run] owner={owner_id} footprint status={fp.status} "
            f"nodes={fp.node_count} edges={fp.edge_count}"
        )
        print(
            f"[dry-run] would detach {fp.node_count} node(s) / {fp.edge_count} edge(s) "
            "from the graph view. No deletion performed."
        )
        return 0

    # 실행은 --confirm <owner-id> 정확 일치 필수(비가역, 불일치 abort).
    if confirm is None or confirm != str(owner_id):
        print(
            "kg erase --owner: refused — pass --confirm <owner-id> matching the target "
            "exactly (irreversible graph-view removal).",
            file=sys.stderr,
        )
        return 2
    print(_CONFIRM_WARNING, file=sys.stderr)
    try:
        report = erase_kg_for_owner(owner_id)
    except Exception as exc:  # noqa: BLE001 — CLI fail-loud (company-node guard 등)
        print(f"kg erase --owner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    # 멱등 2회차 — 실삭제(nodes>0)와 구분(exit 0). genuine 0 측정일 때만 "already erased".
    if (
        report.footprint_before.status == "ready"
        and report.footprint_before.node_count == 0
        and report.nodes_detached == 0
    ):
        print(f"already erased — owner footprint 0, nothing to do (owner={owner_id}).")
        return 0
    _print_owner_report(report)
    return 0


def _main_owners_file(rest: list[str]) -> int:
    dry_run = "--dry-run" in rest
    confirm_batch = "--confirm-batch" in rest
    positional = [a for a in rest if a not in ("--dry-run", "--confirm-batch")]
    if len(positional) != 1:
        print(
            "usage: python -m orthus.kg.erase --owners-file <path> [--dry-run] [--confirm-batch]",
            file=sys.stderr,
        )
        return 2
    try:
        owner_ids = _read_owner_ids_file(positional[0])
    except (OSError, ValueError) as exc:
        print(f"kg erase --owners-file: cannot read owner ids — {exc}", file=sys.stderr)
        return 2
    if not owner_ids:
        print("kg erase --owners-file: no owner ids in file", file=sys.stderr)
        return 2

    if dry_run:
        try:
            require_company_node()
            for oid in owner_ids:
                fp = owner_footprint(oid)
                print(
                    f"[dry-run] owner={oid} status={fp.status} "
                    f"nodes={fp.node_count} edges={fp.edge_count}"
                )
        except Exception as exc:  # noqa: BLE001 — CLI fail-loud
            print(
                f"kg erase --owners-file --dry-run failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"[dry-run] {len(owner_ids)} owner(s) — no deletion performed.")
        return 0

    if not confirm_batch:
        print(
            "kg erase --owners-file: refused — pass --confirm-batch (irreversible "
            f"graph-view removal for {len(owner_ids)} owner(s)).",
            file=sys.stderr,
        )
        return 2
    print(_CONFIRM_WARNING, file=sys.stderr)
    try:
        reports = erase_kg_for_owners(owner_ids)
    except Exception as exc:  # noqa: BLE001 — CLI fail-loud
        print(f"kg erase --owners-file failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    total_nodes = sum(r.nodes_detached for r in reports)
    total_edges = sum(r.edges_detached for r in reports)
    # 통합 closeout artifact — counts only(slug/title 금지).
    print(
        f"kg owner erase batch: owners={len(reports)} "
        f"total_nodes_detached={total_nodes} total_edges_detached={total_edges}"
    )
    for r in reports:
        print(
            f"  owner={r.target_owner_id} nodes={r.nodes_detached} "
            f"edges={r.edges_detached} removed_from_graph={r.removed_from_graph}"
        )
    print(_WHAT_REMAINS)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--owner":
        return _main_owner(args[1:])
    if args and args[0] == "--owners-file":
        return _main_owners_file(args[1:])
    if not args:
        print(
            "usage: python -m orthus.kg.erase <page_id> [<page_id> ...]\n"
            "       python -m orthus.kg.erase --owner <owner-id> [--dry-run] [--confirm <owner-id>]\n"
            "       python -m orthus.kg.erase --owners-file <path> [--dry-run] [--confirm-batch]",
            file=sys.stderr,
        )
        return 2
    try:
        page_ids = [UUID(a) for a in args]
    except ValueError as exc:
        print(f"kg erase: invalid page id — {exc}", file=sys.stderr)
        return 2
    try:
        report = erase_kg_for_pages(page_ids)
    except Exception as exc:  # noqa: BLE001 — CLI fail-loud (company-node guard 등)
        print(f"kg erase failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "kg erase complete: "
        f"pages={report.pages} wiki_pages_deleted={report.wiki_pages_deleted} "
        f"mentions_deleted={report.mentions_deleted} "
        f"entities_deleted={report.entities_deleted} "
        f"page_delete_events={report.page_delete_events} "
        f"graph_entities_detached={report.graph_entities_detached}"
    )
    # wiki page SoR은 이미 삭제됐으므로 rebuild가 노드를 재생성하지 않는다(③). KG가
    # enabled인데 down이면 orphan :Entity 노드만 미detach 상태 → rebuild가 안전하게 수렴.
    if report.kg_enabled and not report.neo4j_available:
        print(
            "note: Neo4j unavailable — orphan :Entity nodes not detached this run. "
            "Run `make node-kg-rebuild NODE=company` to converge (wiki page SoR already "
            "deleted, so no resurrection).",
            file=sys.stderr,
        )
    # source-layer(documents/corpus/notion 등)는 erase 범위 밖 — operations.md §8.4 참조.
    # rebuild 안내는 KG enabled일 때만(off면 그래프가 없어 무의미·혼란).
    nxt = "next: delete source-layer rows (documents/corpus_chunks/notion_rows/connector_*) per operations.md §8.4"
    if report.kg_enabled:
        nxt += ", then `make node-kg-rebuild NODE=company`"
    print(nxt + ".", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
