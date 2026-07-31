#!/usr/bin/env bash
#
# Configure cross-node SSO (S2) env on a node's node.env.
#
# The central node is the SSO hub (issues tickets); personal nodes are relying
# parties (consume tickets to mint their own session). Both sides must share the
# same ORTHUS_SSO_SHARED_SECRET. See docs/cross-node-sso-quickstart.md.
#
# Usage:
#   # Hub (central) — generate a fresh shared secret and print it once:
#   scripts/sso/configure-sso.sh --node company --role hub \
#     --trusted "https://orthus-personal.example.com" --gen --print-secret
#
#   # Relying party (personal) — paste the SAME secret the hub printed:
#   scripts/sso/configure-sso.sh --node personal-a --role rp \
#     --hub https://orthus-central.example.com/api --secret <hex-secret-from-hub>
#
# Re-running replaces the existing ORTHUS_SSO_* lines (safe to rotate).
# This script never restarts the node — it prints the restart command at the end.

set -euo pipefail

NODE=""
ROLE=""
TRUSTED=""
HUB=""
SECRET=""
GEN=0
PRINT_SECRET=0
TTL=""

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --trusted) TRUSTED="${2:-}"; shift 2 ;;
    --hub) HUB="${2:-}"; shift 2 ;;
    --secret) SECRET="${2:-}"; shift 2 ;;
    --ttl) TTL="${2:-}"; shift 2 ;;
    --gen) GEN=1; shift ;;
    --print-secret) PRINT_SECRET=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown arg: $1 (use --help)" ;;
  esac
done

[ -n "$NODE" ] || die "--node is required"
case "$ROLE" in hub|rp) ;; *) die "--role must be hub or rp" ;; esac

NODE_ENV="${HOME}/.orthus/nodes/${NODE}/node.env"
[ -f "$NODE_ENV" ] || die "node.env not found: $NODE_ENV (run 'make node-bootstrap NODE=${NODE}' first)"

if [ "$GEN" = "1" ]; then
  [ -z "$SECRET" ] || die "use either --gen or --secret, not both"
  SECRET="$(openssl rand -hex 32)"
fi
[ -n "$SECRET" ] || die "shared secret required: pass --secret <hex> or --gen"

# remove any prior ORTHUS_SSO_* lines + the section comment, then append fresh.
tmp="$(mktemp)"
grep -vE '^ORTHUS_SSO_|^# Cross-node SSO \(S2\)' "$NODE_ENV" > "$tmp" || true
{
  echo ""
  echo "# Cross-node SSO (S2) — ${ROLE} role. Shared secret must match the other node."
  echo "ORTHUS_SSO_SHARED_SECRET=${SECRET}"
  if [ "$ROLE" = "hub" ]; then
    [ -n "$TRUSTED" ] || die "--trusted <rp-consume-origins-csv> is required for hub role"
    echo "ORTHUS_SSO_TRUSTED_RETURN_ORIGINS=${TRUSTED}"
  else
    [ -n "$HUB" ] || die "--hub <hub-api-base-url> is required for rp role"
    echo "ORTHUS_SSO_HUB_BASE_URL=${HUB}"
  fi
  [ -n "$TTL" ] && echo "ORTHUS_SSO_TICKET_TTL_SECONDS=${TTL}"
} >> "$tmp"
mv "$tmp" "$NODE_ENV"

echo "[configure-sso] wrote SSO ${ROLE} env to ${NODE_ENV}"
echo "  ORTHUS_SSO_SHARED_SECRET=<set, sha:$(printf '%s' "$SECRET" | shasum | cut -c1-12)>"
if [ "$ROLE" = "hub" ]; then
  echo "  ORTHUS_SSO_TRUSTED_RETURN_ORIGINS=${TRUSTED}"
else
  echo "  ORTHUS_SSO_HUB_BASE_URL=${HUB}"
fi
[ -n "$TTL" ] && echo "  ORTHUS_SSO_TICKET_TTL_SECONDS=${TTL}"

if [ "$PRINT_SECRET" = "1" ]; then
  echo ""
  echo "  shared secret (copy to the OTHER node, then clear your scrollback):"
  echo "    ${SECRET}"
fi

echo ""
echo "[configure-sso] restart the node to apply:"
if launchctl list 2>/dev/null | grep -q "com.example.orthus-${NODE}.api"; then
  echo "    launchctl kickstart -k gui/\$(id -u)/com.example.orthus-${NODE}.api"
  echo "    launchctl kickstart -k gui/\$(id -u)/com.example.orthus-${NODE}.web"
else
  echo "    make node-api NODE=${NODE}   # (and: make node-web NODE=${NODE})"
fi
