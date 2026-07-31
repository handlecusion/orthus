#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"

ensure_node_files "${NODE}"
load_node_env "${NODE}"
assert_not_repo_wiki_store

if [[ ! "${ORTHUS_NODE_DB}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  die "invalid ORTHUS_NODE_DB=${ORTHUS_NODE_DB}"
fi

PG_CONTAINER="${ORTHUS_PG_CONTAINER:-orthus_pg}"
PG_USER="${ORTHUS_PG_USER:-orthus}"
RO_ROLE="${ORTHUS_RO_ROLE:-orthus_ro}"

pg() {
  docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" "$@"
}

if ! docker inspect "${PG_CONTAINER}" >/dev/null 2>&1; then
  die "postgres container ${PG_CONTAINER} not found; run make up"
fi

echo "[node-bootstrap] ensuring read-only role ${RO_ROLE} exists..."
pg -d postgres <<SQL
SELECT 'CREATE ROLE ${RO_ROLE} LOGIN PASSWORD ''orthus_ro'''
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${RO_ROLE}')
\gexec
ALTER ROLE ${RO_ROLE} SET default_transaction_read_only = on;
ALTER ROLE ${RO_ROLE} SET statement_timeout = '10s';
SQL

echo "[node-bootstrap] ensuring database ${ORTHUS_NODE_DB} exists..."
pg -d postgres <<SQL
SELECT 'CREATE DATABASE ${ORTHUS_NODE_DB} OWNER ${PG_USER}'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${ORTHUS_NODE_DB}')
\gexec
GRANT CONNECT ON DATABASE ${ORTHUS_NODE_DB} TO ${RO_ROLE};
SQL

echo "[node-bootstrap] granting read access on ${ORTHUS_NODE_DB}..."
pg -d "${ORTHUS_NODE_DB}" <<SQL
GRANT USAGE ON SCHEMA public TO ${RO_ROLE};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${RO_ROLE};
ALTER DEFAULT PRIVILEGES FOR ROLE ${PG_USER} IN SCHEMA public
  GRANT SELECT ON TABLES TO ${RO_ROLE};
SQL

echo "[node-bootstrap] running migrations for ${NODE} (${ORTHUS_NODE_DB})..."
(
  cd "${REPO_ROOT}"
  uv run alembic upgrade head
)

echo "[node-bootstrap] granting post-migrate SELECT on ${ORTHUS_NODE_DB}..."
pg -d "${ORTHUS_NODE_DB}" -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${RO_ROLE};"

echo "[node-bootstrap] ready: ${NODE}"
echo "  DB: ${ORTHUS_NODE_DB}"
echo "  wiki: ${ORTHUS_WIKI_STORE}"
