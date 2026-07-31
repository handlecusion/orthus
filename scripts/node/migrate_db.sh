#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"

load_node_env "${NODE}"
assert_not_repo_wiki_store

echo "[node-migrate] ${NODE} -> ${ORTHUS_NODE_DB}"
(
  cd "${REPO_ROOT}"
  uv run alembic upgrade head
)
