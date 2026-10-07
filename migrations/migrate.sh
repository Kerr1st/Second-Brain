#!/bin/bash
# Run all pending migrations against the Second Brain database.
# Uses the local (native) psql client.
#
# Roles (R7, migration 017): migrations run as the OWNER role, never as the
# application role.
#   DB_MIGRATE_USER      connecting role; must own the database (default memory_bank_owner)
#   DB_OWNER_ROLE        role that owns every schema object after 017 (default = DB_MIGRATE_USER)
#   DB_APP_ROLE          application role that receives DML grants (default DB_USER, else memory_bank)
#   DB_MIGRATE_PASSWORD  password for DB_MIGRATE_USER if pg_hba requires one (falls back to DB_PASSWORD)
#   MIGRATE_UNTIL        stop after this version file (e.g. 017_role_separation.sql); used once, for the bootstrap
# Bootstrap (the only run that is not as the owner): before 017 the application
# role owns the database, so 017 is applied as DB_MIGRATE_USER=memory_bank with
# DB_OWNER_ROLE=memory_bank_owner MIGRATE_UNTIL=017_role_separation.sql; 017 then
# hands the database to the owner role and every later run connects as the owner.
# A run that connects as a role that does not own the database stops before
# touching anything (exit 2). Superuser status is not an exemption.
set -euo pipefail
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:$PATH"

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-memory_bank}"
DB_MIGRATE_USER="${DB_MIGRATE_USER:-memory_bank_owner}"
DB_OWNER_ROLE="${DB_OWNER_ROLE:-$DB_MIGRATE_USER}"
DB_APP_ROLE="${DB_APP_ROLE:-${DB_USER:-memory_bank}}"
MIGRATE_UNTIL="${MIGRATE_UNTIL:-}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(cd "$(dirname "$0")" && pwd)}"
MIGRATION_LOCK_NAME="second-brain-schema-migrations"

if [ -n "${DB_MIGRATE_PASSWORD:-${DB_PASSWORD:-}}" ]; then
  export PGPASSWORD="${DB_MIGRATE_PASSWORD:-$DB_PASSWORD}"
fi

run_sql() {
  psql -X -v ON_ERROR_STOP=1 \
    -h "$DB_HOST" -p "$DB_PORT" -U "$DB_MIGRATE_USER" -d "$DB_NAME" "$@"
}

# Ownership gate: the connecting role must be the database owner or a direct
# member of it (catalog check; a superuser that is not the owner is refused too).
OWNERSHIP=$(run_sql -At <<'PSQL'
SELECT CASE WHEN o.rolname = current_user THEN 'owner'
            WHEN EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                         WHERE m.roleid = d.datdba AND r.rolname = current_user) THEN 'owner'
            ELSE 'not-owner:' || o.rolname END
FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba WHERE d.datname = current_database();
PSQL
)
if [ "$OWNERSHIP" != "owner" ]; then
  echo "refuse: connected as '$DB_MIGRATE_USER' but the database is owned by '${OWNERSHIP#not-owner:}'; run migrate.sh as the owner role (DB_MIGRATE_USER)" >&2
  exit 2
fi

if [ "$DB_APP_ROLE" = "$DB_OWNER_ROLE" ]; then
  echo "refuse: application role and owner role are both '$DB_APP_ROLE'; they must be distinct (DB_APP_ROLE / DB_OWNER_ROLE)" >&2
  exit 2
fi

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
  if [ -n "$MIGRATE_UNTIL" ] && [[ "$VERSION" > "$MIGRATE_UNTIL" ]]; then
    echo "stop: MIGRATE_UNTIL=$MIGRATE_UNTIL reached; $VERSION not applied"
    break
  fi

  # Selection, schema changes, and the tracking row share one transaction.
  # The lock makes concurrent runners re-check applied state serially.
  # The two settings name the roles for migrations that manage privileges (017).
  run_sql \
    --set=lock_name="$MIGRATION_LOCK_NAME" \
    --set=migration_path="$migration" \
    --set=migration_version="$VERSION" \
    --set=owner_role="$DB_OWNER_ROLE" \
    --set=app_role="$DB_APP_ROLE" <<'PSQL'
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended(:'lock_name', 0));
SET LOCAL second_brain.owner_role = :'owner_role';
SET LOCAL second_brain.app_role = :'app_role';
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
