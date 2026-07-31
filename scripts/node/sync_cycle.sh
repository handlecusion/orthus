#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
shift || true

load_node_env "${NODE}"
assert_not_repo_wiki_store

DEFAULT_CONNECTORS="gws_gmail gws_drive codex_sessions claude_sessions"
CONNECTORS="${*:-${ORTHUS_CONNECTOR_TICK_CONNECTORS:-${DEFAULT_CONNECTORS}}}"
LOG_DIR="${ORTHUS_NODE_ROOT:-${NODES_ROOT}/${NODE}}/logs"

mkdir -p "${LOG_DIR}"

echo "[node-sync-cycle] ${NODE} start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[node-sync-cycle] connectors: ${CONNECTORS}"

# shellcheck disable=SC2086
bash "${SCRIPT_DIR}/run_python.sh" "${NODE}" -m orthus.connectors.tick ${CONNECTORS}

if [[ "${REBUILD_WIKI:-0}" == "1" ]]; then
  echo "[node-sync-cycle] rebuild wiki"
  ORTHUS_EMBEDDING="${ORTHUS_EMBEDDING:-mock}" ORTHUS_LLM="${ORTHUS_LLM:-mock}" \
    bash "${SCRIPT_DIR}/run_python.sh" "${NODE}" -m orthus.wiki.rebuild
else
  echo "[node-sync-cycle] skip wiki rebuild (set REBUILD_WIKI=1 to force full rebuild)"
fi

# KG incremental sync (K2, docs/kg-model.md §3) — central(company) 전용 opt-in.
# CLI는 fail-loud지만 cycle 전체를 죽이지 않는다(KG는 파생 인덱스 — fail-open).
if [[ "${KG_SYNC:-0}" == "1" && "${ORTHUS_NODE_KIND:-}" != "company" ]]; then
  echo "[node-sync-cycle] skip kg sync (KG_SYNC=1 is company-node only — personal node has no Neo4j)"
elif [[ "${KG_SYNC:-0}" == "1" ]]; then
  echo "[node-sync-cycle] kg sync"
  bash "${SCRIPT_DIR}/run_python.sh" "${NODE}" -m orthus.kg.sync \
    || echo "[node-sync-cycle] kg sync failed (graph converges on next kg-rebuild)"
else
  echo "[node-sync-cycle] skip kg sync (set KG_SYNC=1 on the company node to enable)"
fi

echo "[node-sync-cycle] ${NODE} done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
