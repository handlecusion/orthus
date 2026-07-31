#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"
ARG_PORT="${2:-}"
if (( $# >= 1 )); then
  shift
fi
if (( $# >= 1 )); then
  shift
fi

load_node_env "${NODE}"
load_web_env "${NODE}"

WEB_PORT="${ARG_PORT:-${WEB_PORT:-${ORTHUS_WEB_PORT:-3820}}}"
MODE="${ORTHUS_WEB_MODE:-auto}"
MODE_NORMALIZED="$(printf '%s' "${MODE}" | tr '[:upper:]' '[:lower:]')"

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

# Public auth origins run a production build (`next start`); `next dev`'s Turbopack
# leaks heap under sustained traffic and OOM-crashes. Local/dev nodes keep `next dev`
# for hot reload. Override with ORTHUS_WEB_MODE=dev|prod.
case "${MODE_NORMALIZED}" in
  auto | "")
    if uses_public_auth_origin; then MODE_EFFECTIVE=prod; else MODE_EFFECTIVE=dev; fi
    ;;
  prod | dev)
    MODE_EFFECTIVE="${MODE_NORMALIZED}"
    ;;
  *)
    die "ORTHUS_WEB_MODE must be auto, dev, or prod"
    ;;
esac

echo "[node-web] ${NODE} (${ORTHUS_NODE_KIND})"
echo "  port: ${WEB_PORT}"
echo "  api: ${NEXT_PUBLIC_API_BASE}"
echo "  mode: ${MODE_EFFECTIVE} (${MODE})"

cd "${REPO_ROOT}/web"
CI=1 pnpm install --frozen-lockfile
if [[ "${MODE_EFFECTIVE}" == "prod" ]]; then
  pnpm build
  exec pnpm start -H 127.0.0.1 -p "${WEB_PORT}" "$@"
fi
exec pnpm dev --hostname 127.0.0.1 --port "${WEB_PORT}" "$@"
