#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
ARG_PORT="${2:-}"

load_node_env "${NODE}"
assert_not_repo_wiki_store

PORT="${ARG_PORT:-${PORT:-${ORTHUS_API_PORT:-8820}}}"
RELOAD_MODE="${ORTHUS_API_RELOAD:-auto}"
RELOAD_MODE_NORMALIZED="$(printf '%s' "${RELOAD_MODE}" | tr '[:upper:]' '[:lower:]')"
PUBLIC_AUTH=false
RELOAD_EFFECTIVE=false

is_local_auth_url() {
  local value="$1"
  if [[ -z "${value}" ]]; then
    return 0
  fi
  [[ "${value}" =~ ^https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$) ]]
}

uses_public_auth_origin() {
  ! is_local_auth_url "${ORTHUS_AUTH_PUBLIC_BASE_URL:-}" || ! is_local_auth_url "${ORTHUS_AUTH_WEB_BASE_URL:-}"
}

if uses_public_auth_origin; then
  PUBLIC_AUTH=true
fi

case "${RELOAD_MODE_NORMALIZED}" in
  auto | "")
    if [[ "${PUBLIC_AUTH}" == "true" ]]; then
      RELOAD_EFFECTIVE=false
    else
      RELOAD_EFFECTIVE=true
    fi
    ;;
  true | 1 | yes | on)
    if [[ "${PUBLIC_AUTH}" == "true" ]]; then
      die "ORTHUS_API_RELOAD=true is not allowed for public auth origins"
    fi
    RELOAD_EFFECTIVE=true
    ;;
  false | 0 | no | off)
    RELOAD_EFFECTIVE=false
    ;;
  *)
    die "ORTHUS_API_RELOAD must be auto, true, or false"
    ;;
esac

# Raise the open-file limit. macOS launchd's default soft limit is 256, which a
# server with long-lived SSE streams (/dashboard/stream) + per-save codex
# distill subprocesses + the DB pool + embedding calls exhausts → the process
# hits "OSError: [Errno 24] Too many open files", the listener stays bound but
# can no longer accept, and the API hangs (prod outage 2026-06-21). The hard
# limit is unlimited, so raise the soft limit. Overridable via ORTHUS_API_NOFILE.
ulimit -n "${ORTHUS_API_NOFILE:-16384}" 2>/dev/null || true

echo "[node-api] ${NODE} (${ORTHUS_NODE_KIND})"
echo "  nofile: $(ulimit -n)"
echo "  port: ${PORT}"
echo "  db: ${ORTHUS_NODE_DB}"
echo "  wiki: ${ORTHUS_WIKI_STORE}"
echo "  reload: ${RELOAD_EFFECTIVE} (${RELOAD_MODE})"

UVICORN_ARGS=(orthus.api.main:app --host 127.0.0.1 --port "${PORT}")
# Bound graceful shutdown so a long-lived connection can't block restart
# forever. The collector notify socket (/collector/ws) is held open by an
# enrolled daemon; without a cap, uvicorn's shutdown hangs on "Waiting for
# connections to close" and a `launchctl kickstart` / release restart never
# becomes ready (observed in the v0.1.22 release). uvicorn force-closes any
# connection still open after this timeout.
UVICORN_ARGS+=(--timeout-graceful-shutdown "${ORTHUS_API_GRACEFUL_TIMEOUT:-10}")
if [[ "${RELOAD_EFFECTIVE}" == "true" ]]; then
  UVICORN_ARGS+=(--reload)
fi

cd "${REPO_ROOT}"
exec uv run uvicorn "${UVICORN_ARGS[@]}"
