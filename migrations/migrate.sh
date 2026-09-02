#!/bin/bash
# Run all pending migrations against the Second Brain database.
# Uses the local (native) psql client.
set -euo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(cd "$(dirname "$0")" && pwd)}"
MIGRATION_LOCK_NAME="second-brain-schema-migrations"

if [ -n "${DB_PASSWORD:-}" ]; then
  export PGPASSWORD="$DB_PASSWORD"
fi

run_sql() {
  psql -X -v ON_ERROR_STOP=1 \
    -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" "$@"
}

# Bootstrap the tracking table under the same advisory lock used by migrations.
run_sql \
  --set=lock_name="$MIGRATION_LOCK_NAME" \
  --set=migration_path="$MIGRATIONS_DIR/000_migrations_table.sql" <<'PSQL'
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended(:'lock_name', 0));
\i :migration_path
COMMIT;
PSQL

for migration in "$MIGRATIONS_DIR"/[0-9]*.sql; do
  VERSION=$(basename "$migration")
  [ "$VERSION" = "000_migrations_table.sql" ] && continue

  # Selection, schema changes, and the tracking row share one transaction.
  # The lock makes concurrent runners re-check applied state serially.
  run_sql \
    --set=lock_name="$MIGRATION_LOCK_NAME" \
    --set=migration_path="$migration" \
    --set=migration_version="$VERSION" <<'PSQL'
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended(:'lock_name', 0));
SELECT EXISTS (
  SELECT 1 FROM schema_migrations WHERE version = :'migration_version'
) AS migration_applied \gset
\if :migration_applied
  \echo skip: :migration_version (already applied)
\else
  \echo apply: :migration_version
  \i :migration_path
  INSERT INTO schema_migrations (version) VALUES (:'migration_version');
\endif
COMMIT;
PSQL
done

echo "done"
