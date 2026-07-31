#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/node/common.sh
source "${SCRIPT_DIR}/common.sh"

NODE="$(require_node "${1:-${NODE:-}}")"

load_node_env "${NODE}"
assert_not_repo_wiki_store

case "${ORTHUS_PG_DSN}" in
  */orthus | */orthus\?*)
    die "ORTHUS_PG_DSN points at default orthus DB, not a node DB"
    ;;
esac

if [[ "${ORTHUS_NODE_KIND}" == "personal" && "${ORTHUS_NODE_DB}" == *company* ]]; then
  die "personal node DB name looks like company DB: ${ORTHUS_NODE_DB}"
fi
if [[ "${ORTHUS_NODE_KIND}" == "company" && "${ORTHUS_NODE_DB}" == *personal* ]]; then
  die "company node DB name looks like personal DB: ${ORTHUS_NODE_DB}"
fi

echo "[node-smoke] config"
echo "  node: ${ORTHUS_NODE_ID} (${ORTHUS_NODE_KIND})"
echo "  db: ${ORTHUS_NODE_DB}"
echo "  wiki: ${ORTHUS_WIKI_STORE}"

cd "${REPO_ROOT}"
uv run python - <<'PY'
from pathlib import Path
from sqlalchemy import text

from orthus.db import get_engine
from orthus.settings import get_settings

s = get_settings()
wiki = Path(s.wiki_store_path).expanduser().resolve()
repo_wiki = Path("wiki-store").resolve()
if wiki == repo_wiki:
    raise SystemExit("wiki store points at repo wiki-store")
with get_engine().connect() as conn:
    db = conn.execute(text("select current_database()")).scalar_one()
    users = conn.execute(text("select count(*) from users")).scalar_one()
print(f"  db_ok: {db}")
print(f"  users: {users}")
print(f"  settings: {s.node_id}/{s.node_kind}")
print(f"  cors: {','.join(s.cors_origin_list())}")
PY

# KG boundary checks (docs/kg-implementation-spec.md §3.3). 죽은 URI를 주입해
# KG-off 경로가 Neo4j 연결을 시도하지 않음(lazy import + fail-closed)을 검증한다.
# neo4j 패키지는 uv 환경에 항상 설치돼 있어 "패키지 부재"는 검증 대상이 아니다.
echo "[node-smoke] kg"
ORTHUS_KG_URI="bolt://127.0.0.1:1" uv run python - <<'PY'
from orthus.kg import kg_available, kg_enabled
from orthus.kg.client import KgDisabled, get_kg_driver
from orthus.settings import get_settings

s = get_settings()
if s.node_kind == "personal" and s.kg_enabled:
    raise SystemExit("personal node must keep ORTHUS_KG_ENABLED=false (no Neo4j on personal)")
if s.kg_enabled:
    print(f"  kg_enabled: true (company node, uri={s.kg_uri})")
else:
    if kg_enabled() is not False:
        raise SystemExit("kg_enabled() must be False when ORTHUS_KG_ENABLED is unset/false")
    try:
        get_kg_driver()
        raise SystemExit("get_kg_driver() must refuse when KG is disabled")
    except KgDisabled:
        pass
    if kg_available() is not False:
        raise SystemExit("kg_available() must be False (no exception) when KG is disabled")
    print("  kg_off_ok: fail-closed + lazy import verified against dead URI")
PY

if [[ "${CHECK_SERVICES:-0}" == "1" ]]; then
  api_port="${ORTHUS_API_PORT:-8820}"
  web_port="${ORTHUS_WEB_PORT:-3820}"
  echo "[node-smoke] services"
  curl -fsS "http://127.0.0.1:${api_port}/health" >/dev/null
  echo "  api_ok: ${api_port}"
  curl -fsS "http://127.0.0.1:${web_port}" >/dev/null
  echo "  web_ok: ${web_port}"
fi

echo "[node-smoke] ok"
