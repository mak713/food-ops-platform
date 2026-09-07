#!/usr/bin/env bash
# Runs automatically on first container boot via /docker-entrypoint-initdb.d/.
# The postgres image already creates $POSTGRES_DB; this script creates the
# separate, dedicated test database so integration tests never touch dev data.
set -euo pipefail

if [ -z "${POSTGRES_TEST_DB:-}" ]; then
  echo "init-test-db: POSTGRES_TEST_DB not set, skipping test database creation." >&2
  exit 0
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE "$POSTGRES_TEST_DB";
EOSQL

echo "init-test-db: created database '$POSTGRES_TEST_DB'."
