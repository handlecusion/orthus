#!/usr/bin/env bash
# K7.5 (W3) — scheduled KG boundary/health monitor cycle.
#
# `set -e` 금지: monitor의 비-0 exit code를 **직접 분류**해야 하므로 set -e면 첫 비-0에서
# 스크립트가 죽어 sentinel을 못 남긴다. `|| echo` 같은 swallow도 금지(exit code 손실). monitor를
# 돌린 직후 `$?`를 즉시 캡처하고, code별 distinct sentinel 파일을 남겨 스케줄러/운영자가
# read-only로 상태를 구분한다(0/1/2/3/4/5). monitor 자체는 read-only(`orthus.kg.monitor`).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"
# common.sh가 `set -e`를 켠다 — monitor 비-0 exit를 직접 분류해야 하므로 **다시 끈다**.
# (set -e면 monitor의 비-0 종료에서 sentinel 쓰기 전에 스크립트가 죽는다.)
set +e

# 셀프테스트 훅(WSL/CI에서 node env 없이 exit→sentinel 매핑만 검증):
#   ORTHUS_KG_MONITOR_SELFTEST=1 — node env 로드 skip
#   ORTHUS_KG_MONITOR_CMD='exit 4' — monitor 호출 대체(임의 exit code 주입)
#   ORTHUS_KG_MONITOR_SENTINEL_DIR — sentinel 출력 디렉터리 override
if [[ "${ORTHUS_KG_MONITOR_SELFTEST:-0}" == "1" ]]; then
  NODE="${1:-selftest}"
  SENTINEL_DIR="${ORTHUS_KG_MONITOR_SENTINEL_DIR:?ORTHUS_KG_MONITOR_SENTINEL_DIR required in selftest}"
else
  NODE="$(require_node "${1:-${NODE:-}}")"
  load_node_env "${NODE}"
  LOG_DIR="${ORTHUS_NODE_ROOT:-${NODES_ROOT}/${NODE}}/logs"
  SENTINEL_DIR="${ORTHUS_KG_MONITOR_SENTINEL_DIR:-${LOG_DIR}/kg-monitor}"
fi

mkdir -p "${SENTINEL_DIR}"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[kg-monitor-cycle] ${NODE} start ${ts}"

# monitor 실행 — exit code를 **즉시** 캡처(|| 없이). set -e가 없어 비-0도 계속 진행한다.
# ORTHUS_KG_MONITOR_SCHEDULED=1로 boundary-health audit 기록을 켠다(스케줄 경로 전용 —
# 수동 `make node-kg-monitor`는 이 플래그가 없어 last_verified_at을 갱신하지 않는다, M-r3).
if [[ -n "${ORTHUS_KG_MONITOR_CMD:-}" ]]; then
  ORTHUS_KG_MONITOR_SCHEDULED=1 bash -c "${ORTHUS_KG_MONITOR_CMD}"
else
  ORTHUS_KG_MONITOR_SCHEDULED=1 bash "${SCRIPT_DIR}/run_python.sh" "${NODE}" -m orthus.kg.monitor
fi
code=$?

# code별 distinct state — 0:ok 1:dead 2:backlog-stale 3:boundary-violation
# 4:could-not-verify(스케줄/Neo4j 검증불가 + stuck rebuild) 5:rebuild-in-progress(fresh).
case "${code}" in
  0) state="ok" ;;
  1) state="dead_events" ;;
  2) state="backlog_stale" ;;
  3) state="boundary_violation" ;;
  4) state="could_not_verify" ;;
  5) state="rebuild_in_progress" ;;
  *) state="unknown_${code}" ;;
esac

# 현 상태 = 단일 sentinel 파일. 이전 state 파일을 지우고 현재만 남긴다.
rm -f "${SENTINEL_DIR}"/state-*.sentinel
sentinel="${SENTINEL_DIR}/state-${state}.sentinel"
printf '%s exit=%s state=%s\n' "${ts}" "${code}" "${state}" > "${sentinel}"

echo "[kg-monitor-cycle] ${NODE} exit=${code} state=${state} sentinel=${sentinel}"
exit "${code}"
