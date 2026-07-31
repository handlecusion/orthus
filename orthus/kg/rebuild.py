"""`python -m orthus.kg.rebuild` — full projection + prune (backs `make kg-rebuild`).

SoR(company scope Postgres + wiki-store frontmatter)을 권위로 그래프 전체를
수렴시킨다: 노드/엣지 MERGE 후 SoR에 없는 잔존물을 detach-delete(prune)한다 —
`kg-sync`가 반영하지 못하는 삭제의 권위 경로다(kg-model §3 삭제 의미론).
2회 연속 실행 무변경(멱등)이 수용 기준 1이다.

CLI는 fail-loud다(구현 명세 §2.6): flag off/Neo4j 미가용이면 exit≠0 + stderr —
스케줄러/운영자가 인지해야 한다.
"""

from __future__ import annotations

import sys

from orthus.audit.logger import audit
from orthus.kg import project, store
from orthus.kg.bootstrap import apply_schema, ensure_kg_meta
from orthus.kg.client import (
    KgDisabled,
    KgUnavailable,
    get_kg_driver,
    kg_enabled,
    kg_write_session,
    require_company_node,
)
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.settings import get_settings


def run_rebuild() -> dict[str, int]:
    """full rebuild — counts를 반환한다. KgDisabled/KgUnavailable은 호출자 처리.

    K7.5: rebuild와 sync/worker 동시 실행 방지는 **boolean HOLD**(`:KgMeta.rebuild_in_progress`)
    가 좌우한다 — 시작에 True, 완료/abort에 clear한다(clock 만료 release 없음). 비정상
    종료로 clear가 누락되면 stuck-true가 되고, monitor가 `rebuild_started_at` 경과로 감지한다
    (자동치유 없음 → 복구는 rebuild 재실행, operations.md §2.1). Neo4j가 처음부터 미가용이면
    HOLD를 켜지도 못하고 abort하므로 stuck-true는 'HOLD 세팅 후 사망' 창에서만 생긴다.
    """
    require_company_node()
    driver = get_kg_driver()
    # kg-bootstrap 선행 실행 없이도 동작하도록 멱등 재실행(§4.1).
    apply_schema(driver)
    ensure_kg_meta(driver)

    with audit("kg.rebuild") as span:
        snapshot_started_at = project.pg_now()
        with kg_write_session() as s:
            store.set_meta(
                s,
                rebuild_in_progress=True,
                rebuild_started_at=snapshot_started_at,
                rebuild_hold_kind="rebuild",
            )
        try:
            # owner-scope hard-guard 단일 지점 — flag off면 COMPANY(v1 동작 불변).
            rows = project.load_all(scope_filter=project.active_scope_filter())
            plan = project.build(rows)
            assert plan.expected_keys is not None  # full load 계약(§4.6)

            with kg_write_session() as s:
                store.merge_all(s, plan)
                prune_report = store.prune(s, plan.expected_keys)
                rebuild_finished_at = project.pg_now()
                rebuild_seconds = (rebuild_finished_at - snapshot_started_at).total_seconds()
                # watermark는 처리 완료 시각이 아니라 스냅샷 시작 시각이다(§4.7) —
                # projection 도중 commit된 row를 다음 sync가 놓치지 않는다. HOLD clear와
                # 같은 트랜잭션에 묶어 완료 시점에 원자적으로 푼다.
                store.set_meta(
                    s,
                    last_rebuild_at=rebuild_finished_at,
                    last_sync_at=snapshot_started_at,
                    kg_schema_version=KG_SCHEMA_VERSION,
                    rebuild_in_progress=False,
                    rebuild_started_at=None,
                    rebuild_hold_kind=None,
                    last_rebuild_seconds=rebuild_seconds,
                )
        except BaseException:
            # abort(예외/취소)도 HOLD를 clear한다 — best-effort(Neo4j가 막 죽었으면 이 clear도
            # 실패할 수 있고, 그건 stuck-true로 남아 monitor가 잡는다).
            try:
                with kg_write_session() as s:
                    store.set_meta(
                        s,
                        rebuild_in_progress=False,
                        rebuild_started_at=None,
                        rebuild_hold_kind=None,
                    )
            except Exception:  # noqa: BLE001 — clear 실패는 stuck-true로 남기고 원예외를 올린다
                pass
            raise
        counts = dict(plan.counts)
        counts["pruned_edges"] = prune_report.edges_deleted
        counts["pruned_placeholders"] = prune_report.placeholders_deleted
        counts["pruned_nodes"] = prune_report.nodes_deleted
        # headroom WARN(soft) — measured rebuild가 lock의 0.5배를 넘으면 활성화 staleness SLA
        # headroom이 빠듯하다(§6.3 chunked rebuild 검토 신호). exit는 올리지 않는다.
        lock_seconds = get_settings().kg_rebuild_lock_minutes * 60
        span.add_meta(**counts, rebuild_seconds=rebuild_seconds)
        if rebuild_seconds > 0.5 * lock_seconds:
            print(
                f"warning: kg rebuild took {rebuild_seconds:.0f}s > 0.5×lock "
                f"({0.5 * lock_seconds:.0f}s) — staleness headroom tight, "
                "see docs/kg-implementation-spec.md §6.3 (chunked rebuild).",
                file=sys.stderr,
            )
    return counts


def main() -> int:
    if not kg_enabled():
        print("kg rebuild refused: ORTHUS_KG_ENABLED=false (fail-closed)", file=sys.stderr)
        return 1
    try:
        counts = run_rebuild()
    except (KgDisabled, KgUnavailable) as exc:
        print(f"kg rebuild failed: {exc}", file=sys.stderr)
        return 1
    print("kg rebuild complete: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
