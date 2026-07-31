-- Read-only role for the 비서(secretary). DB-level half of the double defense
-- (app validation gate is the other half). This role can only SELECT.
-- Runs once at container init, before Alembic migrations create the tables,
-- so DEFAULT PRIVILEGES are used to cover tables created later by `orthus`.

CREATE ROLE orthus_ro WITH LOGIN PASSWORD 'orthus_ro';

GRANT CONNECT ON DATABASE orthus TO orthus_ro;
GRANT USAGE ON SCHEMA public TO orthus_ro;

-- Existing objects (none yet at init, but harmless / future-proof).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO orthus_ro;

-- Anything `orthus` creates later (Alembic migrations) is SELECT-only for orthus_ro.
ALTER DEFAULT PRIVILEGES FOR ROLE orthus IN SCHEMA public
  GRANT SELECT ON TABLES TO orthus_ro;

-- Belt-and-suspenders: make the whole DB read-only at the transaction level too.
ALTER ROLE orthus_ro SET default_transaction_read_only = on;
ALTER ROLE orthus_ro SET statement_timeout = '10s';
