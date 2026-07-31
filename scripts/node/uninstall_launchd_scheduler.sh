#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
uid="$(id -u)"
label="ai.orthus.$(sanitize_db_suffix "${NODE}").sync"
plist="${HOME}/Library/LaunchAgents/${label}.plist"

launchctl bootout "gui/${uid}" "${plist}" >/dev/null 2>&1 || true
rm -f "${plist}"

echo "uninstalled ${label}"
