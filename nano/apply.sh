#!/usr/bin/env bash
# Apply every migration in nano/migrations/ in order, against the Nano's Postgres.
#
#   PG_DSN="postgresql://interpreter_app:PASSWORD@nano.local:5432/interpreter_data" nano/apply.sh
#
# Or run on the Nano itself:  sudo -u postgres PG_DSN=interpreter_data nano/apply.sh
# Migrations are idempotent, so re-running is safe.
set -euo pipefail
cd "$(dirname "$0")"
: "${PG_DSN:?set PG_DSN to a libpq connection string or database name}"
for f in migrations/*.sql; do
  echo "== $f"
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -q -f "$f"
done
echo "done"
