#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
shift || true

if [[ "$#" -eq 0 ]]; then
  die "python args required, e.g. -m orthus.wiki.rebuild"
fi

load_node_env "${NODE}"
assert_not_repo_wiki_store

echo "[node-python] ${NODE} -> $*"
cd "${REPO_ROOT}"
exec uv run python "$@"
