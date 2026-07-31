#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
INTERVAL_SECONDS="${2:-${INTERVAL_SECONDS:-900}}"
CONNECTORS="${CONNECTORS:-${ORTHUS_CONNECTOR_TICK_CONNECTORS:-gws_gmail gws_drive codex_sessions claude_sessions}}"

load_node_env "${NODE}"
assert_not_repo_wiki_store

if ! [[ "${INTERVAL_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${INTERVAL_SECONDS}" -lt 60 ]]; then
  die "INTERVAL_SECONDS must be an integer >= 60"
fi

uid="$(id -u)"
label="ai.orthus.$(sanitize_db_suffix "${NODE}").sync"
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
    <string>${REPO_ROOT}/scripts/node/sync_cycle.sh</string>
    <string>${NODE}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ORTHUS_CONNECTOR_TICK_CONNECTORS</key>
    <string>${CONNECTORS}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>${log_dir}/launchd-sync.out.log</string>
  <key>StandardErrorPath</key>
  <string>${log_dir}/launchd-sync.err.log</string>
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
echo "connectors: ${CONNECTORS}"
echo "logs: ${log_dir}/launchd-sync.{out,err}.log"
