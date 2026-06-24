#!/bin/bash
# Run all pending migrations against the Second Brain database.
# Uses the local (native) psql client.
set -euo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

CONTAINER="${DB_CONTAINER:-second-brain-db}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"
MIGRATIONS_DIR="$(cd "$(dirname "$0")" && pwd)"

run_sql() { psql -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" "$@"; }

# Ensure migrations tracking table exists
run_sql < "$MIGRATIONS_DIR/000_migrations_table.sql" 2>/dev/null

for migration in "$MIGRATIONS_DIR"/[0-9]*.sql; do
  VERSION=$(basename "$migration")
  [ "$VERSION" = "000_migrations_table.sql" ] && continue

  APPLIED=$(run_sql -t -c "SELECT 1 FROM schema_migrations WHERE version = '$VERSION';" 2>/dev/null | tr -d ' ')
  if [ "$APPLIED" = "1" ]; then
    echo "skip: $VERSION (already applied)"
    continue
  fi

  echo "apply: $VERSION"
  run_sql < "$migration"
  run_sql -c "INSERT INTO schema_migrations (version) VALUES ('$VERSION');"
done

echo "done"
