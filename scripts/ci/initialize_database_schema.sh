#!/usr/bin/env bash
set -euo pipefail

pg_host="${PGHOST:-postgres}"
pg_user="${PGUSER:-nautilus}"
pg_database="${PGDATABASE:-nautilus}"

cat schema/sql/types.sql \
  schema/sql/tables.sql \
  schema/sql/functions.sql \
  schema/sql/partitions.sql |
  psql \
    -h "$pg_host" \
    -U "$pg_user" \
    -d "$pg_database" \
    -v ON_ERROR_STOP=1 \
    --single-transaction \
    --echo-errors
