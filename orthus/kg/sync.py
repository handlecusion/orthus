"""`python -m orthus.kg.sync` — incremental watermark projection (backs `make kg-sync`).

`:KgMeta.last_sync_at` watermark 이후 변경분만 upsert한다 — 삭제는 보지 못하므로
삭제 수렴은 주기 `kg-rebuild`(권위)와 K3 outbox delete 이벤트가 담당한다
(kg-model §3). 스키마 버전 불일치/최초 실행은 rebuild를 요구하며 그래프를
건드리지 않는다(§4.7).
"""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from orthus.audit.logger import audit
from orthus.kg import project, store
from orthus.kg.client import (
    KgDisabled,
    KgUnavailable,
    get_kg_driver,
    kg_enabled,
    kg_write_session,
    require_company_node,
)
from orthus.kg.schema import KG_SCHEMA_VERSION

# commit-시각/`updated_at` 시계 차 흡수 보정(§4.7) — 중복 재처리는 MERGE 멱등으로
# 무해하다. 60초 초과 트랜잭션은 현 코드베이스에 없다(구현 명세 §12 가정).
OVERLAP = timedelta(seconds=60)


class SyncResult(BaseModel):
    status: Literal["ok", "rebuild_required", "locked"]
    reason: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


def run_sync() -> SyncResult:
    """incremental sync — KgDisabled/KgUnavailable은 호출자 처리."""
    require_company_node()
    get_kg_driver()  # 연결 실패를 audit span 밖에서 fail-loud로
    with kg_write_session() as s:
        meta = store.get_meta(s)
    if meta is None or meta.last_sync_at is None:
        return SyncResult(status="rebuild_required", reason="no sync watermark (empty graph?)")
    if meta.kg_schema_version != KG_SCHEMA_VERSION:
        return SyncResult(
            status="rebuild_required",
            reason=f"kg_schema_version {meta.kg_schema_version} != {KG_SCHEMA_VERSION}",
        )
    snapshot_started_at = project.pg_now()
    # K7.5 — boolean HOLD(clock 산술 없음). rebuild가 완료/abort에 clear한다.
    if meta.rebuild_in_progress:
        return SyncResult(status="locked", reason="rebuild in progress")

    with audit("kg.sync") as span:
        since = meta.last_sync_at - OVERLAP
        # owner-scope hard-guard 단일 지점 — flag off면 COMPANY(v1 동작 불변).
        rows = project.load_changed(since, scope_filter=project.active_scope_filter())
        plan = project.build(rows)  # 변경분만 — prune 없음(§4.1)
        with kg_write_session() as s:
            store.merge_all(s, plan)
            store.set_meta(s, last_sync_at=snapshot_started_at)
        span.add_meta(**plan.counts)
    return SyncResult(status="ok", counts=plan.counts)


def main() -> int:
    if not kg_enabled():
        print("kg sync refused: ORTHUS_KG_ENABLED=false (fail-closed)", file=sys.stderr)
        return 1
    try:
        result = run_sync()
    except (KgDisabled, KgUnavailable) as exc:
        print(f"kg sync failed: {exc}", file=sys.stderr)
        return 1
    if result.status == "rebuild_required":
        print(f"kg sync refused: rebuild required — {result.reason}", file=sys.stderr)
        print("run `make kg-rebuild` first", file=sys.stderr)
        return 1
    if result.status == "locked":
        # rebuild 진행 중 — 다음 tick에 재시도하면 되므로 스케줄러 관점에선 정상.
        print(f"kg sync skipped: {result.reason}")
        return 0
    print("kg sync complete: " + ", ".join(f"{k}={v}" for k, v in sorted(result.counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
