#!/usr/bin/env bash
# scripts/setup_test_db.sh
# Idempotent: creates orthus_test on the running postgres container,
# grants orthus_ro read access, and runs Alembic migrations against it.
# Safe to run multiple times; never touches the working `orthus` DB.
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-orthus_pg}"
PG_USER="${PG_USER:-orthus}"
PG_PASS="${PG_PASS:-orthus}"
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5433}"
TEST_DB="${TEST_DB:-orthus_test}"
RO_ROLE="${RO_ROLE:-orthus_ro}"

# Helper: run SQL as superuser orthus via docker exec (avoids host psql requirement).
pg() {
  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" "$@"
}

echo "[setup_test_db] ensuring database $TEST_DB exists..."
# CREATE DATABASE cannot run inside a transaction; use \gexec via shell-safe heredoc.
pg -d postgres <<SQL
SELECT 'CREATE DATABASE $TEST_DB OWNER $PG_USER'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$TEST_DB')
\gexec
SQL

echo "[setup_test_db] granting orthus_ro access on $TEST_DB..."
# GRANT CONNECT is on the database level — must connect to postgres.
pg -d postgres -c "GRANT CONNECT ON DATABASE $TEST_DB TO $RO_ROLE;"

# Schema-level grants must be run connected to the target database.
pg -d "$TEST_DB" <<SQL
GRANT USAGE ON SCHEMA public TO $RO_ROLE;
-- Cover tables that already exist (e.g. re-run after migrate).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $RO_ROLE;
-- Cover tables that orthus will create in future migrations.
ALTER DEFAULT PRIVILEGES FOR ROLE $PG_USER IN SCHEMA public
  GRANT SELECT ON TABLES TO $RO_ROLE;
SQL

echo "[setup_test_db] running Alembic migrations against $TEST_DB..."
ORTHUS_PG_DSN="postgresql+psycopg://$PG_USER:$PG_PASS@$PG_HOST:$PG_PORT/$TEST_DB" \
  uv run alembic upgrade head

# After migrate: grant SELECT on any tables that were just created.
echo "[setup_test_db] granting SELECT on all tables in $TEST_DB post-migrate..."
pg -d "$TEST_DB" -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO $RO_ROLE;"

echo "[setup_test_db] done — $TEST_DB is ready for tests."
