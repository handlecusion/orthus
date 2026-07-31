#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"

load_node_env "${NODE}"
assert_not_repo_wiki_store

cd "${REPO_ROOT}"
exec uv run python scripts/ops/auth_allowlist.py "${ACTION:-list}"
