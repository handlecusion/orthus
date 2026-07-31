#!/usr/bin/env bash
#
# Bring up a LOCAL personal node (relying party) on this Mac to test cross-node
# SSO against the live central hub. Uses runtime env overrides only — it does NOT
# edit node.env, so it will not clobber a public personal-a config.
#
# Prereqs:
#   1. The hub must trust this RP's consume origin. On the hub machine run:
#        scripts/sso/configure-sso.sh --node company --role hub \
#          --trusted "https://orthus-personal.example.com,http://localhost:8830" \
#          --secret <same-secret> && restart the hub
#   2. You know the hub's ORTHUS_SSO_SHARED_SECRET (pass via --secret).
#
# Usage:
#   scripts/sso/run-personal-rp-local.sh --owner you@yourco.com --secret <hex>
#   # then: log in at the hub (https://orthus-central.example.com), open http://localhost:3830
#
set -euo pipefail

OWNER=""
SECRET=""
HUB="https://orthus-central.example.com/api"
DB="orthus_personal_ys"
API_PORT=8830
WEB_PORT=3830

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --owner) OWNER="${2:-}"; shift 2 ;;
    --secret) SECRET="${2:-}"; shift 2 ;;
    --hub) HUB="${2:-}"; shift 2 ;;
    --db) DB="${2:-}"; shift 2 ;;
    --api-port) API_PORT="${2:-}"; shift 2 ;;
    --web-port) WEB_PORT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '3,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[ -n "$OWNER" ] || die "--owner <email> required (seeds the personal allowlist)"
[ -n "$SECRET" ] || die "--secret <hex> required (must match the hub's ORTHUS_SSO_SHARED_SECRET)"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DSN="postgresql+psycopg://orthus:orthus@localhost:5433/${DB}"
RO="postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/${DB}"

echo "[rp-local] migrating ${DB} to head..."
ORTHUS_PG_DSN="$DSN" uv run alembic upgrade head >/dev/null

common_env=(
  "ORTHUS_PG_DSN=$DSN" "ORTHUS_PG_DSN_READONLY=$RO"
  "ORTHUS_EMBEDDING=mock" "ORTHUS_LLM=mock"
  "ORTHUS_AUTH_MODE=session" "ORTHUS_NODE_KIND=personal" "ORTHUS_NODE_ID=personal-a"
  "ORTHUS_AUTH_SESSION_SECRET=local-rp-session-secret"
  "ORTHUS_AUTH_PUBLIC_BASE_URL=http://localhost:${API_PORT}"
  "ORTHUS_AUTH_WEB_BASE_URL=http://localhost:${WEB_PORT}"
  "ORTHUS_AUTH_COOKIE_NAME=orthus_session_personal_ys"
  "ORTHUS_AUTH_COOKIE_SECURE=false"
  "ORTHUS_PERSONAL_OWNER_EMAIL=${OWNER}"
  "ORTHUS_SSO_SHARED_SECRET=${SECRET}"
  "ORTHUS_SSO_HUB_BASE_URL=${HUB}"
  "ORTHUS_CORS_ORIGINS=http://localhost:${WEB_PORT}"
)

tmux kill-session -t rp-local-api 2>/dev/null || true
tmux kill-session -t rp-local-web 2>/dev/null || true
tmux new-session -d -s rp-local-api -c "$REPO_ROOT" \
  "$(printf '%s ' "${common_env[@]}") uv run uvicorn orthus.api.main:app --port ${API_PORT}"
tmux new-session -d -s rp-local-web -c "$REPO_ROOT/web" \
  "NEXT_PUBLIC_API_BASE=http://localhost:${API_PORT} NEXT_PUBLIC_DEMO_USER_ID= pnpm exec next dev -p ${WEB_PORT}"

echo "[rp-local] api → http://localhost:${API_PORT}  web → http://localhost:${WEB_PORT}"
echo "[rp-local] tmux sessions: rp-local-api, rp-local-web (stop: tmux kill-session -t rp-local-api -t rp-local-web)"
echo "[rp-local] test: log in at the hub, then open http://localhost:${WEB_PORT} — it should auto-log-in."
echo "[rp-local] reminder: the hub must trust http://localhost:${API_PORT} in ORTHUS_SSO_TRUSTED_RETURN_ORIGINS."
