#!/usr/bin/env bash
# K7.5 (W3) — install a launchd periodic schedule for the KG boundary/health monitor cycle.
#
# `install_launchd_scheduler.sh`의 heredoc-generator 패턴을 그대로 따른다(신규
# scripts/node/launchd/ 디렉터리/템플릿 컨벤션을 만들지 않는다). label prefix는 generator와
# 동일한 `ai.orthus.*` 계열(`deploy/autostart/`의 com.example.* standalone plist와는 다른
# 계열 — 혼동 금지). plist 존재 = 활성화 acceptance artifact다(operations.md §2.1).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
# KG monitor 기본 주기 1h(BACKLOG_STALE 임계 1h와 정합 — boundary-health cadence는 그 2배 여유).
INTERVAL_SECONDS="${2:-${INTERVAL_SECONDS:-3600}}"

load_node_env "${NODE}"

if ! [[ "${INTERVAL_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${INTERVAL_SECONDS}" -lt 60 ]]; then
  die "INTERVAL_SECONDS must be an integer >= 60"
fi

uid="$(id -u)"
label="ai.orthus.$(sanitize_db_suffix "${NODE}").kg-monitor"
launch_dir="${HOME}/Library/LaunchAgents"
plist="${launch_dir}/${label}.plist"
log_dir="${ORTHUS_NODE_ROOT:-${NODES_ROOT}/${NODE}}/logs"

mkdir -p "${launch_dir}" "${log_dir}"

cat > "${plist}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO_ROOT}/scripts/node/kg_monitor_cycle.sh</string>
    <string>${NODE}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>${log_dir}/launchd-kg-monitor.out.log</string>
  <key>StandardErrorPath</key>
  <string>${log_dir}/launchd-kg-monitor.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/${uid}" "${plist}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${uid}" "${plist}"
launchctl enable "gui/${uid}/${label}"
launchctl kickstart -k "gui/${uid}/${label}" >/dev/null 2>&1 || true

echo "installed ${label}"
echo "plist: ${plist}"
echo "interval_seconds: ${INTERVAL_SECONDS}"
echo "logs: ${log_dir}/launchd-kg-monitor.{out,err}.log"
echo "sentinels: ${log_dir}/kg-monitor/state-*.sentinel"
