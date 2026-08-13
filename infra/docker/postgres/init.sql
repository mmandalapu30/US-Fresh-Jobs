-- Runs once, on first initialisation of the Postgres data volume.
-- Schema objects are NOT created here: that is Alembic's job. This file only handles
-- cluster-level setup that a migration cannot perform.

-- Extensions are created idempotently by migration 0001 as well; doing it here means a
-- fresh volume is ready even before the first migration runs.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- Separate database for the test suite so `make test` never touches dev data.
SELECT 'CREATE DATABASE jobplatform_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'jobplatform_test')\gexec
