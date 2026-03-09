#!/bin/bash
# Run all pending migrations against the Second Brain database.
# Usage: ./migrations/migrate.sh
#
# Applies migrations in order (by filename). Skips any already recorded
# in the schema_migrations table. Safe to run repeatedly.

set -euo pipefail

DB_HOST="${DB_HOST:-localhost}"
DB_USER="${DB_USER:-memory_bank}"
DB_NAME="${DB_NAME:-memory_bank}"
MIGRATIONS_DIR="$(cd "$(dirname "$0")" && pwd)"

PSQL="psql -h $DB_HOST -U $DB_USER -d $DB_NAME"

# Ensure migrations tracking table exists
$PSQL -f "$MIGRATIONS_DIR/000_migrations_table.sql" 2>/dev/null

for migration in "$MIGRATIONS_DIR"/[0-9]*.sql; do
  VERSION=$(basename "$migration")

  # Skip the tracking table itself
  [ "$VERSION" = "000_migrations_table.sql" ] && continue

  # Check if already applied
  APPLIED=$($PSQL -t -c "SELECT 1 FROM schema_migrations WHERE version = '$VERSION';" 2>/dev/null | tr -d ' ')
  if [ "$APPLIED" = "1" ]; then
    echo "skip: $VERSION (already applied)"
    continue
  fi

  echo "apply: $VERSION"
  $PSQL -f "$migration"
  $PSQL -c "INSERT INTO schema_migrations (version) VALUES ('$VERSION');"
done

echo "done"
