"""`python -m orthus.kg.monitor` — read-only KG 운영 요약 (구현 명세 §9.4).

신규 public route를 만들지 않는다(operations.md §9: canonical dashboard는 아직
없음). 운영자가 CLI로 한 번에 보는 read-only 요약만 제공한다:

- outbox: pending/applied/dead 건수 + 최고(最古) pending 적체 나이
- query_runs: 최근 N일 status 분포(ok/rejected/timeout/error)
- KgMeta: last_sync_at/last_rebuild_at 나이 + schema version
- graph: :Entity 노드 수, placeholder(미materialized WikiPage) 수

PG 부분은 항상, Neo4j 부분(KgMeta/graph)은 가용할 때만 채운다(fail-open).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from orthus.audit.logger import audit
from orthus.db import session
from orthus.kg import store
from orthus.kg.client import kg_available, kg_enabled, kg_owner_scope_enabled, kg_read_session
from orthus.kg.outbox import status_counts
from orthus.settings import get_settings
from orthus.tables import audit_log, kg_outbox, kg_query_runs

# main() 종료 코드 — 운영자/스케줄러가 read-only로 구분한다. could-not-verify와
# violation-found가 **다른 코드**여야 한다(plan §2): 검증 못한 monitor가 exit 0으로
# 거짓 안심을 만들면 monitor 없는 것보다 나쁘다.
EXIT_OK = 0
EXIT_DEAD_EVENTS = 1  # 기존 — dead outbox 적체
EXIT_BOUNDARY_VIOLATION = 3  # owner 경계 invariant 위반(must-be-zero 카운터 > 0)
EXIT_COULD_NOT_VERIFY = 4  # **owner-scope on에서만** 경계 카운터 확인 실패(fail-closed).
# flag-off의 검증 실패는 best-effort라 exit를 올리지 않는다(exit_code 참조, 코드리뷰 R2).
EXIT_BACKLOG_STALE = 2  # pending 최고령이 임계 초과 — Neo4j 장기 미가용/적체 신호(리뷰 교정).
# K7.5 (W3) — fresh rebuild 진행 중(informational). false-clean을 억제하되 누출 경보(3/4)보다
# **아래** 우선순위다. stuck rebuild(rebuild_started_at 경과>lock)는 별도 코드를 늘리지 않고
# could-not-verify(4)로 매핑한다(stuck rebuild = 경계 검증불가, distinct sentinel/메시지로 구분).
EXIT_REBUILD_IN_PROGRESS = 5

# pending 적체가 이 나이를 넘으면 backlog-stale로 비-0 종료한다 — Neo4j 장기 outage 동안
# enqueue는 계속 INSERT되는데 worker가 못 비우는 무증거 적체를 운영자/스케줄러가 잡게 한다.
# trim은 applied만 reap하므로 pending은 무한정 쌓일 수 있다(outbox down-path는 release만).
# 임계는 60초 SLA(K3 §5.7)의 ×60 = 1시간 — 정상 worker는 5초 poll로 분 단위에 비운다.
BACKLOG_STALE_AGE_SECONDS = 3600.0

# K7.5 (review #1) — erase HOLD 전용 stuck 임계(초). erase는 카운트+DETACH 2회로 sub-second에
# 끝나야 하므로, clear-실패 stuck을 rebuild headroom(기본 30분)이 아니라 5분에 잡는다. rebuild는
# 정당하게 길 수 있어 kg_rebuild_lock_minutes를 유지하지만, erase HOLD가 5분 넘게 걸려 있으면
# clear 누락(worker/sync 정지 + read 보류)이 거의 확실하다 → could-not-verify로 빨리 escalate.
ERASE_HOLD_STUCK_SECONDS = 300.0


def rebuild_stuck_threshold_seconds(hold_kind: str | None) -> float:
    """HOLD 종류별 stuck 판정 임계(초). erase HOLD는 sub-second로 끝나야 하므로 짧은 고정
    임계, rebuild(및 미설정 보수값)는 headroom lock(기본 30분)을 쓴다(review #1)."""
    if hold_kind == "erase":
        return ERASE_HOLD_STUCK_SECONDS
    return get_settings().kg_rebuild_lock_minutes * 60


class OutboxSummary(BaseModel):
    by_status: dict[str, int] = Field(default_factory=dict)  # pending/applied/dead → count
    oldest_pending_age_seconds: float | None = None  # 최고 pending 적체 나이(없으면 None)


class QueryRunsSummary(BaseModel):
    window_days: int
    by_status: dict[str, int] = Field(default_factory=dict)  # ok/rejected/timeout/error → count
    total: int = 0


class GraphSummary(BaseModel):
    entity_count: int = 0
    placeholder_count: int = 0  # page_id IS NULL인 미materialized WikiPage


class BoundarySummary(BaseModel):
    """owner 경계 invariant tripwire(B-invariants) — RLS 없는 Community에서 prod
    유일 방어선이다(plan §2). 네 tripwire는 **항상 0**이어야 한다. 각 tripwire는 LIMIT-1
    presence probe(0/1)로 "위반이 하나라도 있나"만 본다 — 위반이 **있으면** 첫 매치에서
    단축되어 싸지만, healthy(위반 0, 정상)면 부재 증명을 위해 스캔이 끝까지 간다. label-
    agnostic `MATCH (n)`/`()-[r]->()`는 추가한 :WikiPage 인덱스로 단축되지 않고, split
    카운트는 LIMIT 없는 full scan이다 — 현 company 규모(~9천)에선 가볍지만 personal-inclusive
    로 커지면 매 tick이 전 그래프 스캔이다. 대규모에서의 인덱싱/쿼리 결합과 cross-owner
    personal 엣지 tripwire 추가는 follow-up이다(projection 측 doc_visible_to/resolve_dst가
    cross-owner 엣지 생성을 이미 막으므로 1차 방어는 투영 단계에 있다).

    **flag-off에서도 측정한다(코드리뷰 #3):** 비활성화 rollback(flag off, rebuild로 personal
    노드를 purge하기 전, deactivation runbook)에서 그래프엔 아직 personal 노드/엣지가 남아
    있는데 이전엔 boundary=None/EXIT_OK라 그 잔여를 못 봤다 — 누출이 가장 있을 시점에 장님.
    이제 flag 상태와 무관하게 네 tripwire를 돌리고, **flag-off일 땐 personal 데이터 잔존
    자체(`residual_personal`)를 위반으로 본다**(company-only 모드엔 personal 노드가 0이어야
    한다). flag-off의 잔여 측정은 split full-count 대신 presence probe(LIMIT-1)라 비용이 작다.
    """

    owner_scope_enabled: bool = True
    # must-be-zero tripwires (presence probe 0/1) — flag 상태와 무관하게 항상 측정
    personal_nodes_without_owner: int = 0  # B-shape: scope='personal' AND owner_id IS NULL
    personal_edges_without_owner: int = 0  # B-shape(edge) — owner 자신 traversal 탈락 위험
    stale_v1_placeholders: int = 0  # B4: page_id IS NULL AND ns_slug IS NULL (cross-owner 오라클)
    overpermissive_edges: int = 0  # B3: company 엣지가 personal 끝점을 노출
    # read-time wire tripwire(코드리뷰 #8): 게이트 `_map_records`가 foreign-personal 노드/엣지를
    # drop하며 남긴 `kg.boundary_violation` audit 행 수(최근 window). 위 graph presence probe는
    # 그래프 데이터 정합을 보지만, 이건 **쿼리 술어 누락**(predicate가 foreign를 못 거름)이라는
    # 별개 실패 모드를 잡는다 — owner_variants_present 부트가드를 통과한 잔여 누수의 최후 신호.
    # flag 상태 무관하게 위반으로 친다(0이어야 함). audit 행은 foreign 식별자를 안 남긴다(§1.2).
    read_time_boundary_violations: int = 0
    # flag-OFF에서만 위반: rollback 잔여 personal 노드/엣지 presence(LIMIT-1). flag-ON이면
    # personal 데이터가 기대되므로 위반이 아니다(violations에서 무시).
    residual_personal: int = 0
    # informational split — flag-ON에서만 채운다(full count). flag-OFF는 0.
    company_nodes: int = 0
    personal_nodes: int = 0
    company_edges: int = 0
    personal_edges: int = 0
    # 카운터 확인 실패(쿼리 에러/Neo4j 미가용)면 set. owner-scope ON이면 fail-CLOSED
    # (exit 4); OFF면 advisory(rollback 잔여 검사는 best-effort라 exit 미상승, 코드리뷰 R2).
    verify_error: str | None = None

    @property
    def violations(self) -> int:
        """트립된 must-be-zero invariant 수. >0이면 경계 위반. flag-OFF에선 personal 잔존
        (`residual_personal`)도 위반으로 친다(company-only 모드엔 personal 데이터가 0)."""
        base = (
            self.personal_nodes_without_owner
            + self.personal_edges_without_owner
            + self.stale_v1_placeholders
            + self.overpermissive_edges
            + self.read_time_boundary_violations  # flag 무관 — read-time 누수 신호도 위반
        )
        if not self.owner_scope_enabled:
            base += self.residual_personal
        return base

    @property
    def healthy(self) -> bool:
        return self.verify_error is None and self.violations == 0


class KgMonitorSummary(BaseModel):
    kg_enabled: bool
    neo4j_available: bool
    outbox: OutboxSummary
    query_runs: QueryRunsSummary
    last_sync_age_seconds: float | None = None
    last_rebuild_age_seconds: float | None = None
    kg_schema_version: int | None = None
    graph: GraphSummary | None = None  # Neo4j 미가용이면 None
    boundary: BoundarySummary | None = None  # owner-scope on일 때만(아니면 None)
    # K7.5 (W3) — rebuild HOLD 상태. rebuild_in_progress=True인데 rebuild_started_at 경과가
    # lock(headroom)을 넘으면 rebuild_stale=True(SIGKILL stuck-true → could-not-verify로 매핑).
    rebuild_in_progress: bool = False
    rebuild_age_seconds: float | None = None
    rebuild_stale: bool = False
    last_rebuild_seconds: float | None = None
    rebuild_hold_kind: str | None = None  # "rebuild" | "erase" — stuck 임계/메시지 구분(review #1)


class BoundaryHealthReadout(BaseModel):
    """(M-r3) 최소 admin boundary-health readout — RLS 없는 Community에서 책임 admin이
    경계 상태를 볼 단일 신호. **counts-only**(owner_id/slug 없음).

    `last_verified_at`은 **스케줄 monitor 실행 타임스탬프**(`kg.boundary_health` audit
    occurred_at) 기반이다(수동 활성화 값 아님). 스케줄이 기대 주기 내 미실행이면
    `stale_cadence=True` → `healthy=False`(dead cadence가 healthy로 위장 불가). 풀
    per-report compliance 뷰는 §8."""

    healthy: bool
    stale_cadence: bool
    last_verified_at: datetime | None = None
    # must-be-zero counter VALUES (owner_id/slug 없음) — 마지막 스케줄 실행 시점 값.
    personal_nodes_without_owner: int = 0
    personal_edges_without_owner: int = 0
    stale_v1_placeholders: int = 0
    overpermissive_edges: int = 0
    read_time_boundary_violations: int = 0
    residual_personal: int = 0


class EntityConnectionSample(BaseModel):
    """K9 (R8) — 다리 junk-rate eyeballing용 엔티티 1건. owner_id/이메일 없음(display_name은
    company-scope carve-out person 이름일 수 있으나 이미 wiki page가 노출하는 이름이며 운영자
    전용 CLI다 — kg-k9-plan §1 PII)."""

    name: str  # display_name
    kind: str  # person | org | project | system
    mention_count: int  # page-only degree(projection §2.1)
    above_threshold: bool  # mention_count >= hub_threshold → 다리 매개에서 제외(C-E/U1)
    sample_pages: list[str] = Field(default_factory=list)  # 이 엔티티를 언급한 page slug 샘플


class EntityJunkReport(BaseModel):
    """K9 (R8) — 튜닝 패스 junk-rate 측정 도구(eyeballing 방지). top-N entity-by-mention_count +
    샘플 neighbor를 print해 운영자가 hub 제외선(`$hub_threshold`)과 희소 다리 신호 품질을
    실데이터로 본다(C-E done-criterion). read-only — graph/PG 읽기만, 쓰기/경계 표면 0."""

    kg_available: bool = False
    hub_threshold: int = 0
    entity_total: int = 0
    hub_entities_excluded: int = 0  # mention_count >= threshold(다리에서 제외되는 흔한 엔티티)
    top_entities: list[EntityConnectionSample] = Field(default_factory=list)
    # entity_neighbors query_runs 통계(window) — 다리 hit rate(있는 데이터로 cheap, R8).
    neighbors_runs_total: int = 0  # status=ok인 entity_neighbors 실행 수
    neighbors_runs_nonempty: int = 0  # 그 중 result_count>0(비명시 연결을 실제로 찾은 비율)
    verify_error: str | None = None  # graph 읽기 실패(fail-open advisory)


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def _compute_boundary(owner_scope_enabled: bool) -> BoundarySummary:
    """owner 경계 tripwire를 Neo4j에서 측정한다. **fail-CLOSED**: 어떤 카운터 쿼리든
    실패/타임아웃이면 verify_error를 set해 호출자가 could-not-verify로 비-0 종료한다.

    네 must-be-zero tripwire는 **flag 상태와 무관하게** 돈다(코드리뷰 #3 — rollback 잔여를
    flag-off에서도 잡기 위함). tripwire는 LIMIT-1 presence probe(`WITH ... LIMIT 1 RETURN
    count`)라 위반 결과 수는 1로 수렴하지만, 위반이 **없으면** 부재 증명을 위해 매치 없이
    끝까지 스캔한다(healthy가 오히려 비싼 케이스). label-agnostic 스캔이라 :WikiPage
    인덱스로 단축되지 않는다 — 대규모 인덱싱/결합은 follow-up(BoundarySummary docstring).

    flag-OFF: split full-count 대신 `residual_personal` presence probe(LIMIT-1)만 — company-only
    모드엔 personal 데이터가 0이어야 하므로 잔존 유무만 본다(violations가 위반으로 친다).
    flag-ON: personal 데이터가 기대되므로 split full-count로 운영자 가시성을 채운다."""
    b = BoundarySummary(owner_scope_enabled=owner_scope_enabled)
    try:
        with kg_read_session() as rs:

            def probe(cypher: str) -> int:
                return rs.run(cypher).single()["c"]

            b.personal_nodes_without_owner = probe(
                "MATCH (n) WHERE n.scope = 'personal' AND n.owner_id IS NULL "
                "WITH n LIMIT 1 RETURN count(n) AS c"
            )
            b.personal_edges_without_owner = probe(
                "MATCH ()-[r]->() WHERE r.scope = 'personal' AND r.owner_id IS NULL "
                "WITH r LIMIT 1 RETURN count(r) AS c"
            )
            b.stale_v1_placeholders = probe(
                "MATCH (p:WikiPage) WHERE p.page_id IS NULL AND p.ns_slug IS NULL "
                "WITH p LIMIT 1 RETURN count(p) AS c"
            )
            b.overpermissive_edges = probe(
                "MATCH (a)-[r]->(b) "
                "WHERE r.scope = 'company' AND (a.scope = 'personal' OR b.scope = 'personal') "
                "WITH r LIMIT 1 RETURN count(r) AS c"
            )
            if owner_scope_enabled:
                # split (informational) — 현 company 규모(~9천)에서 count는 가볍다.
                b.company_nodes = probe("MATCH (n) WHERE n.scope = 'company' RETURN count(n) AS c")
                b.personal_nodes = probe(
                    "MATCH (n) WHERE n.scope = 'personal' RETURN count(n) AS c"
                )
                b.company_edges = probe(
                    "MATCH ()-[r]->() WHERE r.scope = 'company' RETURN count(r) AS c"
                )
                b.personal_edges = probe(
                    "MATCH ()-[r]->() WHERE r.scope = 'personal' RETURN count(r) AS c"
                )
            else:
                # flag-OFF: personal 노드/엣지 잔존(rollback 미purge) presence — LIMIT-1.
                b.residual_personal = probe(
                    "MATCH (n) WHERE n.scope = 'personal' WITH n LIMIT 1 RETURN count(n) AS c"
                ) + probe(
                    "MATCH ()-[r]->() WHERE r.scope = 'personal' WITH r LIMIT 1 "
                    "RETURN count(r) AS c"
                )
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED: 검증 불가는 could-not-verify
        b.verify_error = f"{type(exc).__name__}: {exc}"
    return b


def kg_monitor_summary(*, query_runs_days: int = 7) -> KgMonitorSummary:
    now = datetime.now(UTC)
    outbox_counts = status_counts()  # outbox.py와 단일 출처 공유
    with session() as s:
        oldest_pending = s.execute(
            select(func.min(kg_outbox.c.enqueued_at)).where(kg_outbox.c.status == "pending")
        ).scalar_one_or_none()
        since = now - timedelta(days=query_runs_days)
        qr_counts = dict(
            s.execute(
                select(kg_query_runs.c.status, func.count())
                .where(kg_query_runs.c.created_at >= since)
                .group_by(kg_query_runs.c.status)
            ).all()
        )
        # read-time wire tripwire 신호(코드리뷰 #8) — PG-only라 Neo4j 가용성과 무관하게 측정.
        rt_boundary = int(
            s.execute(
                select(func.count())
                .select_from(audit_log)
                .where(
                    audit_log.c.node == "kg.boundary_violation",
                    audit_log.c.occurred_at >= since,
                )
            ).scalar_one()
        )

    summary = KgMonitorSummary(
        kg_enabled=kg_enabled(),
        neo4j_available=False,
        outbox=OutboxSummary(
            by_status={k: int(v) for k, v in outbox_counts.items()},
            oldest_pending_age_seconds=_age_seconds(oldest_pending, now),
        ),
        query_runs=QueryRunsSummary(
            window_days=query_runs_days,
            by_status={k: int(v) for k, v in qr_counts.items()},
            total=sum(int(v) for v in qr_counts.values()),
        ),
    )

    if not (kg_enabled() and kg_available()):
        # 일반 요약은 fail-open(PG-only). 단 owner-scope on이면 경계 검증을 못한 것이므로
        # boundary를 could-not-verify로 채운다(fail-CLOSED — monitor가 거짓 안심 금지).
        if kg_owner_scope_enabled():
            summary.boundary = BoundarySummary(
                owner_scope_enabled=True,
                verify_error="neo4j unavailable (cannot verify owner boundary)",
            )
        # read-time tripwire는 PG 신호라 Neo4j 미가용이어도 보고한다 — boundary가 없으면
        # 그것만 담는 최소 summary를 만들어 exit 3로 올린다(코드리뷰 #8).
        if summary.boundary is None and rt_boundary:
            summary.boundary = BoundarySummary(owner_scope_enabled=kg_owner_scope_enabled())
        if summary.boundary is not None:
            summary.boundary.read_time_boundary_violations = rt_boundary
        return summary

    with kg_read_session() as rs:
        meta = store.get_meta(rs)
        entity_count = rs.run("MATCH (e:Entity) RETURN count(e) AS c").single()["c"]
        placeholder_count = rs.run(
            "MATCH (p:WikiPage) WHERE p.page_id IS NULL RETURN count(p) AS c"
        ).single()["c"]
    summary.neo4j_available = True
    summary.graph = GraphSummary(
        entity_count=int(entity_count), placeholder_count=int(placeholder_count)
    )
    if meta is not None:
        summary.kg_schema_version = meta.kg_schema_version
        summary.last_sync_age_seconds = _age_seconds(meta.last_sync_at, now)
        summary.last_rebuild_age_seconds = _age_seconds(meta.last_rebuild_at, now)
        summary.last_rebuild_seconds = meta.last_rebuild_seconds
        # K7.5 (W3) — rebuild HOLD 상태 + stuck 판정. lock(headroom) 경과면 stale(stuck rebuild
        # = SIGKILL 등으로 clear 누락) → could-not-verify로 매핑(자동치유 없음, 복구는 rebuild 재실행).
        summary.rebuild_in_progress = meta.rebuild_in_progress
        summary.rebuild_age_seconds = _age_seconds(meta.rebuild_started_at, now)
        summary.rebuild_hold_kind = meta.rebuild_hold_kind
        # review #1 — stuck 임계는 HOLD 종류별로 다르다(erase 5분, rebuild 30분 headroom).
        stuck_threshold = rebuild_stuck_threshold_seconds(meta.rebuild_hold_kind)
        summary.rebuild_stale = bool(
            meta.rebuild_in_progress
            and summary.rebuild_age_seconds is not None
            and summary.rebuild_age_seconds > stuck_threshold
        )
    # 경계 tripwire는 flag 상태와 무관하게 측정한다(코드리뷰 #3) — flag-off에서도 rollback
    # 잔여 personal 노드를 잡는다. flag-off의 추가 비용은 presence probe(LIMIT-1) 두 번뿐
    # (split full-count는 flag-on에서만, _compute_boundary).
    summary.boundary = _compute_boundary(kg_owner_scope_enabled())
    summary.boundary.read_time_boundary_violations = rt_boundary  # read-time 신호 합류(코드리뷰 #8)
    return summary


# --- (M-r3) admin boundary-health readout ------------------------------------------

# kg.boundary_health audit를 못 본 지 이 시간을 넘으면 stale cadence(스케줄 dead로 간주).
# 스케줄 간격(예: 1h)의 약 2배 — 한 번 놓쳐도 곧바로 stale로 깜빡이지 않게 여유를 둔다.
DEFAULT_HEALTH_CADENCE_SECONDS = 2 * 3600.0

_BOUNDARY_COUNTER_KEYS = (
    "personal_nodes_without_owner",
    "personal_edges_without_owner",
    "stale_v1_placeholders",
    "overpermissive_edges",
    "read_time_boundary_violations",
    "residual_personal",
)


def record_boundary_health(summary: KgMonitorSummary) -> None:
    """스케줄 monitor 실행마다 `kg.boundary_health` audit 1행 — readout의 last_verified_at
    출처. **counts-only**(owner_id/slug 금지). boundary 미측정(Neo4j down/flag-off)이면
    healthy=False로 기록(거짓 안심 금지)."""
    b = summary.boundary
    healthy = b is not None and b.healthy
    meta: dict[str, int | bool] = {"healthy": healthy}
    if b is not None:
        for k in _BOUNDARY_COUNTER_KEYS:
            meta[k] = int(getattr(b, k))
    try:
        with audit("kg.boundary_health") as span:
            span.add_meta(**meta)
    except Exception:  # noqa: BLE001 — 기록 실패는 관측 누락(monitor 결과엔 영향 없음)
        pass


def boundary_health_readout(
    *, expected_cadence_seconds: float = DEFAULT_HEALTH_CADENCE_SECONDS
) -> BoundaryHealthReadout:
    """최근 `kg.boundary_health` audit를 읽어 admin readout을 만든다(counts-only).

    last_verified_at = 그 audit occurred_at(스케줄 실행 타임스탬프). 기대 주기 내 미실행이면
    stale_cadence=True → healthy=False(dead cadence가 healthy로 위장 불가). audit 부재(스케줄
    한 번도 안 돎)도 stale_cadence=True."""
    now = datetime.now(UTC)
    with session() as s:
        row = s.execute(
            select(audit_log.c.occurred_at, audit_log.c.meta)
            .where(audit_log.c.node == "kg.boundary_health", audit_log.c.phase == "exit")
            .order_by(audit_log.c.occurred_at.desc())
            .limit(1)
        ).first()
    if row is None:
        # 스케줄이 한 번도 실행 안 됨 — 검증된 적 없음 = stale, not healthy.
        return BoundaryHealthReadout(healthy=False, stale_cadence=True, last_verified_at=None)
    last_verified_at = row.occurred_at
    age = _age_seconds(last_verified_at, now)
    # occurred_at은 PG func.now()(트랜잭션 시작), now는 이후 python datetime.now() — 쓰기↔읽기
    # 간 clock skew로 age가 음수가 될 수 있다(미래 타임스탬프). 음수는 "방금 검증"(0)으로 클램프하고
    # cadence 경계는 inclusive(>=)로 본다 — 정확히 cadence면 stale(보수적, dead cadence가 healthy로
    # 위장 불가). 이로써 cadence=0 + 갓 기록된 행도 결정론으로 stale이 된다(skew 부호 무관).
    stale = age is None or max(age, 0.0) >= expected_cadence_seconds
    meta = row.meta or {}
    counters = {k: int(meta.get(k, 0)) for k in _BOUNDARY_COUNTER_KEYS}
    recorded_healthy = bool(meta.get("healthy", False))
    return BoundaryHealthReadout(
        healthy=recorded_healthy and not stale,
        stale_cadence=stale,
        last_verified_at=last_verified_at,
        **counters,
    )


def entity_junk_report(
    *, top_n: int = 20, sample_pages: int = 3, window_days: int = 7
) -> EntityJunkReport:
    """K9 (R8) — entity 다리 junk-rate 측정 리포트. **read-only, fail-open**: graph 미가용/예외면
    `verify_error`만 채운 빈 리포트(절대 raise 금지 — 운영 advisory 도구). entity_neighbors 통계는
    PG(kg_query_runs)라 graph 무관하게 채운다.

    운영자 사용처(C-E done-criterion): hub 제외선(`$hub_threshold`)이 흔한 엔티티를 제대로
    걸러내는지, 임계 바로 아래 희소 엔티티 연결이 의미 있는지를 실데이터로 본다. top-N은
    mention_count 내림차순이라 hub가 상단에 온다 — `above_threshold` 표기로 제외 여부를 함께 본다."""
    report = EntityJunkReport(hub_threshold=get_settings().kg_entity_hub_threshold)
    threshold = report.hub_threshold
    since = datetime.now(UTC) - timedelta(days=window_days)
    # PG — entity_neighbors hit rate(graph 가용성 무관).
    with session() as s:
        row = s.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(case((kg_query_runs.c.result_count > 0, 1), else_=0)),
                    0,
                ),
            ).where(
                kg_query_runs.c.template_name == "entity_neighbors",
                kg_query_runs.c.status == "ok",
                kg_query_runs.c.created_at >= since,
            )
        ).one()
        report.neighbors_runs_total = int(row[0])
        report.neighbors_runs_nonempty = int(row[1])

    if not (kg_enabled() and kg_available()):
        report.verify_error = "neo4j unavailable"
        return report
    try:
        with kg_read_session() as rs:
            report.kg_available = True
            report.entity_total = rs.run("MATCH (e:Entity) RETURN count(e) AS c").single()["c"]
            report.hub_entities_excluded = rs.run(
                "MATCH (e:Entity) WHERE e.mention_count >= $t RETURN count(e) AS c",
                t=threshold,
            ).single()["c"]
            top = rs.run(
                "MATCH (e:Entity) "
                "RETURN e.entity_key AS ek, e.display_name AS name, e.entity_kind AS kind, "
                "coalesce(e.mention_count, 0) AS mc "
                "ORDER BY mc DESC, e.entity_key ASC LIMIT $n",
                n=top_n,
            ).data()
            for r in top:
                pages = rs.run(
                    "MATCH (e:Entity {entity_key:$ek})-[:MENTIONED_IN]->(q:WikiPage) "
                    "RETURN q.slug AS slug ORDER BY q.slug LIMIT $s",
                    ek=r["ek"],
                    s=sample_pages,
                ).value("slug")
                mc = int(r["mc"])
                report.top_entities.append(
                    EntityConnectionSample(
                        name=r["name"] or "(이름없음)",
                        kind=r["kind"] or "?",
                        mention_count=mc,
                        above_threshold=mc >= threshold,
                        sample_pages=[p for p in pages if p],
                    )
                )
    except Exception as exc:  # noqa: BLE001 — fail-open advisory(절대 raise 금지)
        report.verify_error = f"{type(exc).__name__}: {exc}"
    return report


def _print_entity_junk(report: EntityJunkReport) -> int:
    """`--entity-junk` 출력(read-only advisory) — 항상 exit 0(health check 아님)."""
    nonempty_rate = (
        report.neighbors_runs_nonempty / report.neighbors_runs_total
        if report.neighbors_runs_total
        else None
    )
    rate_s = f"{nonempty_rate * 100:.0f}%" if nonempty_rate is not None else "-"
    print(
        f"entity-junk: hub_threshold={report.hub_threshold} "
        f"entities={report.entity_total} hub_excluded={report.hub_entities_excluded} "
        f"neighbors_runs={report.neighbors_runs_total} nonempty={rate_s}"
    )
    if report.verify_error is not None:
        print(
            f"  (graph unavailable — {report.verify_error}; PG-only stats above)", file=sys.stderr
        )
    for e in report.top_entities:
        mark = "EXCL" if e.above_threshold else "keep"
        pages = ", ".join(e.sample_pages) if e.sample_pages else "-"
        print(f"  [{mark}] {e.kind}:{e.name} mc={e.mention_count} → {pages}")
    return 0


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{seconds:.0f}s"


def exit_code(summary: KgMonitorSummary) -> int:
    """종료 코드 결정(순수 — DB-free 테스트 대상). **우선순위 = 아래 if 체크 순서**이며 숫자
    코드값과 무관하다(K7.5 reconciliation). 단일 권위 체크 순서(먼저 매칭되는 것 반환):

      could-not-verify(4, *stale rebuild 포함*) → boundary-violation(3) → dead(1)
      → backlog-stale(2) → rebuild-in-progress(5, fresh) → ok(0).

    즉 기존 monitor.py의 4-먼저-3 순서를 유지하고(committed could-not-verify를 boundary보다
    먼저 반환), 그 아래에 dead → backlog-stale(K7.4) → in-progress(5) → ok를 잇는다.

    **could-not-verify(exit 4)는 owner-scope ON 전용이다(코드리뷰 R2):** 활성화 상태에선
    personal 데이터가 그래프에 살아 있으므로 경계를 못 본 monitor는 거짓 안심을 만든다 →
    fail-closed. flag-OFF에선 boundary 검사가 rollback 잔여를 잡는 **best-effort**라, 확인된
    잔여(violations>0)는 exit 3로 올리되 검증 실패(probe 에러/Neo4j 미가용)는 advisory로 두고
    exit를 올리지 않는다. **stuck rebuild(rebuild_stale)는 flag 상태 무관하게 검증불가(4)로
    매핑한다** — clear 누락된 HOLD가 worker/sync를 영구 정지시킨 상태라 경계 검증이 무의미하다.
    fresh in-progress(5)는 정상 일시 상태라 누출 경보(3/4)보다 아래·false-clean(0)보다 위다."""
    b = summary.boundary
    # could-not-verify(4): owner-scope ON 경계 검증 실패 OR stuck rebuild(clear 누락 HOLD).
    if (b is not None and b.verify_error is not None and b.owner_scope_enabled) or (
        summary.rebuild_stale
    ):
        return EXIT_COULD_NOT_VERIFY
    if b is not None and b.violations > 0:
        return EXIT_BOUNDARY_VIOLATION
    if summary.outbox.by_status.get("dead", 0) > 0:
        return EXIT_DEAD_EVENTS
    # backlog-stale: pending 최고령이 임계 초과 — Neo4j 장기 미가용/적체 신호(리뷰 교정).
    # dead보다 낮은 우선순위(dead는 운영자 처리 필요, stale-backlog는 보통 자동 복구).
    age = summary.outbox.oldest_pending_age_seconds
    if age is not None and age > BACKLOG_STALE_AGE_SECONDS:
        return EXIT_BACKLOG_STALE
    # fresh rebuild 진행 중 — informational(false-clean 억제). 누출 경보보다 아래.
    if summary.rebuild_in_progress:
        return EXIT_REBUILD_IN_PROGRESS
    return EXIT_OK


def _print_boundary_health(readout: BoundaryHealthReadout) -> int:
    """`--boundary-health` admin readout 출력. healthy면 exit 0, 아니면 비-0(스크립트용)."""
    state = "healthy" if readout.healthy else ("stale" if readout.stale_cadence else "VIOLATION")
    last = readout.last_verified_at.isoformat() if readout.last_verified_at else "never"
    print(f"boundary-health: {state} last_verified_at={last}")
    print("counters: " + " ".join(f"{k}={getattr(readout, k)}" for k in _BOUNDARY_COUNTER_KEYS))
    return 0 if readout.healthy else (4 if readout.stale_cadence else EXIT_BOUNDARY_VIOLATION)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--boundary-health" in args:
        return _print_boundary_health(boundary_health_readout())
    if "--entity-junk" in args:
        # K9 (R8) — 튜닝 패스 junk-rate 측정(read-only advisory, 항상 exit 0).
        return _print_entity_junk(entity_junk_report())
    summary = kg_monitor_summary()
    # (M-r3) last_verified_at 출처 audit는 **스케줄 실행만** 기록한다(counts-only). 수동
    # `make node-kg-monitor` 점검이 staleness 시계를 리셋해 죽은 스케줄을 가리는 것을 막는다
    # ('수동 활성화 값 아님'). kg_monitor_cycle.sh가 ORTHUS_KG_MONITOR_SCHEDULED=1을 설정한다.
    if os.environ.get("ORTHUS_KG_MONITOR_SCHEDULED") == "1":
        record_boundary_health(summary)
    ob = summary.outbox
    qr = summary.query_runs
    print(f"kg_enabled={summary.kg_enabled} neo4j_available={summary.neo4j_available}")
    print(
        "outbox: "
        + " ".join(f"{k}={v}" for k, v in sorted(ob.by_status.items()))
        + f" oldest_pending={_fmt_age(ob.oldest_pending_age_seconds)}"
    )
    print(
        f"query_runs({qr.window_days}d): total={qr.total} "
        + " ".join(f"{k}={v}" for k, v in sorted(qr.by_status.items()))
    )
    if summary.graph is not None:
        print(
            f"graph: entities={summary.graph.entity_count} "
            f"placeholders={summary.graph.placeholder_count} "
            f"schema_version={summary.kg_schema_version}"
        )
        print(
            f"meta: last_sync={_fmt_age(summary.last_sync_age_seconds)} "
            f"last_rebuild={_fmt_age(summary.last_rebuild_age_seconds)}"
        )
    else:
        print("graph: (Neo4j unavailable — PG-only summary)")
    b = summary.boundary
    if b is not None:
        if b.verify_error is not None and b.owner_scope_enabled:
            print(f"boundary: COULD-NOT-VERIFY — {b.verify_error}", file=sys.stderr)
        elif b.verify_error is not None:
            # flag-off: 검증 실패는 advisory(exit 미상승) — rollback 잔여 검사는 best-effort.
            print(
                f"boundary: advisory — residue check skipped ({b.verify_error})",
                file=sys.stderr,
            )
        elif b.violations > 0:
            print(
                "boundary: VIOLATION "
                f"personal_no_owner={b.personal_nodes_without_owner} "
                f"personal_edge_no_owner={b.personal_edges_without_owner} "
                f"stale_v1_placeholder={b.stale_v1_placeholders} "
                f"overpermissive_edge={b.overpermissive_edges} "
                f"residual_personal={b.residual_personal}(off-only)",
                file=sys.stderr,
            )
        elif b.owner_scope_enabled:
            print(
                f"boundary: healthy company={b.company_nodes}n/{b.company_edges}e "
                f"personal={b.personal_nodes}n/{b.personal_edges}e"
            )
        else:
            # flag-off: split full-count를 안 돌린다 — tripwire 0 + personal 잔여 0 확인만.
            print("boundary: healthy (owner-scope off — no residual personal data)")
    age = summary.outbox.oldest_pending_age_seconds
    if age is not None and age > BACKLOG_STALE_AGE_SECONDS:
        print(
            f"outbox: BACKLOG-STALE — oldest pending {_fmt_age(age)} "
            f"> {_fmt_age(BACKLOG_STALE_AGE_SECONDS)} (Neo4j 미가용/적체 의심)",
            file=sys.stderr,
        )
    # K7.5 (W3) — rebuild HOLD 상태. stuck(stale)은 could-not-verify(4)와 **구분되는 메시지**
    # (스케줄러가 distinct sentinel로 가른다). fresh in-progress는 informational(exit 5).
    if summary.rebuild_stale:
        hold_kind = summary.rebuild_hold_kind or "rebuild"
        threshold_m = rebuild_stuck_threshold_seconds(summary.rebuild_hold_kind) / 60
        print(
            f"rebuild: STUCK — {hold_kind} HOLD held for {_fmt_age(summary.rebuild_age_seconds)} "
            f"(> {threshold_m:.0f}m). 자동치유 없음 — "
            "`make node-kg-rebuild NODE=company` 재실행으로 복구(operations.md §2.1).",
            file=sys.stderr,
        )
    elif summary.rebuild_in_progress:
        hold_kind = summary.rebuild_hold_kind or "rebuild"
        print(
            f"rebuild: {hold_kind} HOLD in progress ({_fmt_age(summary.rebuild_age_seconds)}) — "
            "informational, worker/sync paused until completion."
        )
    # 경계(검증불가/위반) > dead 적체 > backlog-stale > rebuild-in-progress > ok. 스크립트가 코드로 구분.
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
