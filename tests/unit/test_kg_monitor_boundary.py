"""K7.1 — monitor 경계 exit-code 단위 테스트 (DB 불필요).

`exit_code()`는 순수 함수라 fail-closed 구분(could-not-verify ≠ violation-found)을
DB 없이 고정한다. 실제 카운터 측정(`_compute_boundary`)은 integration에서 본다.
"""

from __future__ import annotations

from orthus.kg.monitor import (
    BACKLOG_STALE_AGE_SECONDS,
    EXIT_BACKLOG_STALE,
    EXIT_BOUNDARY_VIOLATION,
    EXIT_COULD_NOT_VERIFY,
    EXIT_DEAD_EVENTS,
    EXIT_OK,
    EXIT_REBUILD_IN_PROGRESS,
    BoundarySummary,
    KgMonitorSummary,
    OutboxSummary,
    QueryRunsSummary,
    exit_code,
)


def _summary(
    *,
    dead: int = 0,
    boundary: BoundarySummary | None = None,
    oldest_pending_age_seconds: float | None = None,
    rebuild_in_progress: bool = False,
    rebuild_stale: bool = False,
) -> KgMonitorSummary:
    return KgMonitorSummary(
        kg_enabled=True,
        neo4j_available=True,
        outbox=OutboxSummary(
            by_status={"dead": dead} if dead else {},
            oldest_pending_age_seconds=oldest_pending_age_seconds,
        ),
        query_runs=QueryRunsSummary(window_days=7),
        boundary=boundary,
        rebuild_in_progress=rebuild_in_progress,
        rebuild_stale=rebuild_stale,
    )


def test_exit_ok_when_no_boundary_no_dead():
    # owner-scope off → boundary None → 경계 검사 건너뜀(동작 불변).
    assert exit_code(_summary()) == EXIT_OK


def test_exit_dead_events():
    assert exit_code(_summary(dead=2)) == EXIT_DEAD_EVENTS


def test_exit_boundary_healthy_is_ok():
    b = BoundarySummary(company_nodes=10, company_edges=5)
    assert b.healthy is True
    assert exit_code(_summary(boundary=b)) == EXIT_OK


def test_exit_violation_distinct_from_could_not_verify():
    # violation-found
    viol = BoundarySummary(stale_v1_placeholders=1)
    assert viol.violations == 1 and viol.healthy is False
    assert exit_code(_summary(boundary=viol)) == EXIT_BOUNDARY_VIOLATION
    # could-not-verify (검증 불가) — 다른 코드여야 한다(fail-closed).
    cnv = BoundarySummary(verify_error="neo4j unavailable")
    assert cnv.healthy is False
    assert exit_code(_summary(boundary=cnv)) == EXIT_COULD_NOT_VERIFY
    assert EXIT_BOUNDARY_VIOLATION != EXIT_COULD_NOT_VERIFY


def test_could_not_verify_takes_precedence_over_dead():
    # 검증 불가 monitor는 dead 여부와 무관하게 비-0(거짓 안심 금지).
    cnv = BoundarySummary(verify_error="boom")
    assert exit_code(_summary(dead=3, boundary=cnv)) == EXIT_COULD_NOT_VERIFY


def test_violation_takes_precedence_over_dead():
    viol = BoundarySummary(overpermissive_edges=1)
    assert exit_code(_summary(dead=3, boundary=viol)) == EXIT_BOUNDARY_VIOLATION


def test_violations_counts_all_four_tripwires():
    b = BoundarySummary(
        personal_nodes_without_owner=1,
        personal_edges_without_owner=1,
        stale_v1_placeholders=1,
        overpermissive_edges=1,
    )
    assert b.violations == 4


def test_residual_personal_is_violation_only_when_flag_off():
    # 코드리뷰 #3 — flag-OFF에서 personal 잔존(rollback 미purge)은 위반(company-only 모드엔
    # personal 데이터가 0이어야 한다). flag-ON에선 personal이 기대되므로 위반이 아니다.
    off = BoundarySummary(owner_scope_enabled=False, residual_personal=1)
    assert off.violations == 1 and off.healthy is False
    assert exit_code(_summary(boundary=off)) == EXIT_BOUNDARY_VIOLATION

    on = BoundarySummary(owner_scope_enabled=True, residual_personal=1)
    assert on.violations == 0 and on.healthy is True
    assert exit_code(_summary(boundary=on)) == EXIT_OK


def test_flag_off_clean_graph_is_healthy():
    # flag-OFF + 잔여 0 → healthy(rollback 후 rebuild로 purge 완료된 상태).
    b = BoundarySummary(owner_scope_enabled=False, residual_personal=0)
    assert b.healthy is True
    assert exit_code(_summary(boundary=b)) == EXIT_OK


def test_could_not_verify_is_flag_on_only():
    # 코드리뷰 R2 — could-not-verify(exit 4)는 owner-scope ON 전용. flag-ON에선 검증 실패가
    # fail-closed(exit 4)지만, flag-OFF에선 best-effort라 exit를 올리지 않는다(advisory).
    on = BoundarySummary(owner_scope_enabled=True, verify_error="boom")
    assert exit_code(_summary(boundary=on)) == EXIT_COULD_NOT_VERIFY

    off = BoundarySummary(owner_scope_enabled=False, verify_error="boom")
    assert exit_code(_summary(boundary=off)) == EXIT_OK
    # flag-OFF에서도 확인된 잔여(probe 성공 + residual>0)는 여전히 violation(exit 3).
    off_resid = BoundarySummary(owner_scope_enabled=False, residual_personal=1)
    assert exit_code(_summary(boundary=off_resid)) == EXIT_BOUNDARY_VIOLATION


def test_flag_off_verify_error_does_not_mask_dead_events():
    # flag-OFF advisory verify_error는 exit를 안 올리므로 dead 적체가 정상적으로 드러난다.
    off = BoundarySummary(owner_scope_enabled=False, verify_error="boom")
    assert exit_code(_summary(dead=2, boundary=off)) == EXIT_DEAD_EVENTS


def test_read_time_tripwire_is_violation_flag_agnostic():
    # 코드리뷰 #8 — read-time wire tripwire(kg.boundary_violation audit)는 flag 상태와 무관하게
    # 위반(exit 3)이다. graph presence probe가 0이어도(그래프 데이터는 정합) 쿼리 술어 누락으로
    # foreign가 wire에 새어 drop된 흔적이므로 must-be-zero. flag-ON/OFF 둘 다 위반으로 친다.
    on = BoundarySummary(owner_scope_enabled=True, read_time_boundary_violations=1)
    assert on.violations == 1 and on.healthy is False
    assert exit_code(_summary(boundary=on)) == EXIT_BOUNDARY_VIOLATION

    off = BoundarySummary(owner_scope_enabled=False, read_time_boundary_violations=2)
    assert off.violations == 2 and off.healthy is False
    assert exit_code(_summary(boundary=off)) == EXIT_BOUNDARY_VIOLATION


# --- backlog-stale exit escalation (리뷰 교정 — pending 적체가 무증거로 자라는 것 방지) ---


def test_backlog_stale_escalates_exit():
    # pending 최고령이 임계 초과 → 비-0 종료(Neo4j 장기 미가용/적체 신호).
    over = BACKLOG_STALE_AGE_SECONDS + 1
    assert exit_code(_summary(oldest_pending_age_seconds=over)) == EXIT_BACKLOG_STALE


def test_backlog_under_threshold_is_ok():
    under = BACKLOG_STALE_AGE_SECONDS - 1
    assert exit_code(_summary(oldest_pending_age_seconds=under)) == EXIT_OK
    # 적체 없음(None)도 OK.
    assert exit_code(_summary(oldest_pending_age_seconds=None)) == EXIT_OK


def test_dead_takes_precedence_over_backlog_stale():
    # dead는 운영자 처리 필요라 stale-backlog(보통 자동 복구)보다 우선.
    over = BACKLOG_STALE_AGE_SECONDS + 1
    assert exit_code(_summary(dead=1, oldest_pending_age_seconds=over)) == EXIT_DEAD_EVENTS


def test_boundary_violation_takes_precedence_over_backlog_stale():
    over = BACKLOG_STALE_AGE_SECONDS + 1
    viol = BoundarySummary(stale_v1_placeholders=1)
    assert (
        exit_code(_summary(boundary=viol, oldest_pending_age_seconds=over))
        == EXIT_BOUNDARY_VIOLATION
    )


# --- K7.5 (W3) rebuild HOLD exit-code 사다리 reconciliation ----------------------------


def test_fresh_rebuild_in_progress_exits_five():
    # fresh in-progress는 informational(5) — false-clean(0) 위, 누출 경보(3/4) 아래.
    assert exit_code(_summary(rebuild_in_progress=True)) == EXIT_REBUILD_IN_PROGRESS


def test_stuck_rebuild_maps_to_could_not_verify():
    # stuck(stale) rebuild = clear 누락 HOLD → 경계 검증불가(4)로 매핑(flag 무관).
    assert (
        exit_code(_summary(rebuild_in_progress=True, rebuild_stale=True)) == EXIT_COULD_NOT_VERIFY
    )


def test_boundary_violation_takes_precedence_over_fresh_rebuild():
    # 누출 경보(3)가 fresh in-progress(5)보다 위.
    viol = BoundarySummary(overpermissive_edges=1)
    assert exit_code(_summary(boundary=viol, rebuild_in_progress=True)) == EXIT_BOUNDARY_VIOLATION


def test_dead_takes_precedence_over_fresh_rebuild():
    assert exit_code(_summary(dead=1, rebuild_in_progress=True)) == EXIT_DEAD_EVENTS


def test_stuck_rebuild_takes_precedence_over_dead():
    # stuck rebuild(4)는 dead(1)보다 위(검증불가가 우선 — 거짓 안심 금지).
    assert (
        exit_code(_summary(dead=2, rebuild_in_progress=True, rebuild_stale=True))
        == EXIT_COULD_NOT_VERIFY
    )


def test_erase_hold_uses_shorter_stuck_threshold_than_rebuild():
    """review #1 — erase HOLD는 sub-second로 끝나야 하므로 stuck 임계가 rebuild headroom보다
    훨씬 짧다(erase 5분 고정 < rebuild kg_rebuild_lock_minutes×60). None/rebuild는 headroom 유지."""
    from orthus.kg.monitor import ERASE_HOLD_STUCK_SECONDS, rebuild_stuck_threshold_seconds
    from orthus.settings import get_settings

    rebuild_threshold = get_settings().kg_rebuild_lock_minutes * 60
    assert rebuild_stuck_threshold_seconds("erase") == ERASE_HOLD_STUCK_SECONDS
    assert rebuild_stuck_threshold_seconds("rebuild") == rebuild_threshold
    # 미설정(None)은 보수적으로 rebuild headroom으로 수렴(레거시 HOLD 안전값).
    assert rebuild_stuck_threshold_seconds(None) == rebuild_threshold
    # erase 임계가 실제로 더 짧아야 빠른 escalate가 성립한다.
    assert ERASE_HOLD_STUCK_SECONDS < rebuild_threshold
