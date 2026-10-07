"""Migration 017 (runtime role separation, F-138) and the owner/app split of migrate.sh.

Three angles:

* the suite's own database -- migrated by the owner role, used by the app role --
  must show exactly the privilege model 017 describes (DML, EXECUTE, nothing
  structural; pg_dump still works as the app role);
* migrate.sh refuses to run as anything but the database owner, and refuses an
  app role equal to the owner role;
* the bootstrap: a database that looks like the installed one before R7 (one
  SUPERUSER application role owning database, tables and the vector extension)
  is driven through 017 as that role, the role is demoted with the separate
  operator statement, and the demoted role can still do the application's work
  while structural statements fail and later migrations only run as the owner.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import psycopg2
import psycopg2.errors
import pytest

from tests.conftest import owner_connect

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
SEVENTEEN = MIGRATIONS / "017_role_separation.sql"
MANIFEST = json.loads(Path(os.environ["SECOND_BRAIN_TEST_MANIFEST"]).read_text())
APP, OWNER = os.environ["DB_USER"], os.environ["DB_MIGRATE_USER"]
BOOT = MANIFEST["bootstrap"]
STRUCTURAL = ("TRUNCATE", "REFERENCES", "TRIGGER")


def _connect(dbname, user):
    return psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]), dbname=dbname, user=user)


def _migrate(dbname, *, user, owner_role=None, app_role=None, directory=MIGRATIONS, until=None):
    env = {**os.environ, "DB_NAME": dbname, "DB_MIGRATE_USER": user, "MIGRATIONS_DIR": str(directory)}
    env.pop("DB_OWNER_ROLE", None)
    env.pop("MIGRATE_UNTIL", None)
    if owner_role:
        env["DB_OWNER_ROLE"] = owner_role
    if app_role:
        env["DB_APP_ROLE"] = app_role
    if until:
        env["MIGRATE_UNTIL"] = until
    return subprocess.run(["/bin/bash", str(MIGRATIONS / "migrate.sh")], env=env, capture_output=True, text=True, timeout=120)


def _psql(dbname, user, statement):
    binary = shutil.which("psql", path=os.environ["PATH"])
    return subprocess.run([binary, "-X", "-v", "ON_ERROR_STOP=1", "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"],
                           "-U", user, "-d", dbname, "-Atc", statement], capture_output=True, text=True, timeout=60)


def _pg_dump(dbname, user, *args):
    binary = shutil.which("pg_dump", path=os.environ["PATH"])
    return subprocess.run([binary, "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"], "-U", user, "-d", dbname, *args],
                          capture_output=True, text=True, timeout=120)


def _dir_through(tmp_path, last):
    for path in sorted(MIGRATIONS.glob("[0-9]*.sql")):
        if int(path.name[:3]) <= last:
            shutil.copyfile(path, tmp_path / path.name)
    return tmp_path


def _ownership(cur, owner):
    """Public relations/functions, the database and the vector extension not owned by ``owner``."""
    cur.execute("""SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f','i') AND c.relowner<>%s::regrole
                   AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid='pg_class'::regclass AND x.objid=c.oid AND x.deptype='e')""", (owner,))
    bad = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                   WHERE n.nspname='public' AND p.proowner<>%s::regrole
                   AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid='pg_proc'::regclass AND x.objid=p.oid AND x.deptype='e')""", (owner,))
    bad += [r[0] for r in cur.fetchall()]
    cur.execute("SELECT datdba::regrole::text FROM pg_database WHERE datname=current_database()")
    if cur.fetchone()[0] != owner:
        bad.append("database")
    return bad


def _acl_privileges(cur, table, role):
    """Privilege letters of ``role`` on ``table`` straight from the ACL (unaffected by superuser status)."""
    cur.execute("SELECT privilege_type FROM information_schema.role_table_grants WHERE table_schema='public' AND table_name=%s AND grantee=%s ORDER BY 1", (table, role))
    return {r[0] for r in cur.fetchall()}


# --------------------------------------------------------------------------- the suite database

def test_017_sets_a_lock_timeout_and_never_names_a_role_literally():
    text = SEVENTEEN.read_text()
    first = next(line for line in text.splitlines() if line.strip() and not line.startswith("--"))
    assert first.strip().upper().startswith("SET LOCAL LOCK_TIMEOUT")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    assert "memory_bank" not in body and "sb_test" not in body
    assert "current_setting('second_brain.app_role')" in body and "current_setting('second_brain.owner_role')" in body
    # The demotion is the operator's CONFIRM-REQUIRED step, never the migration's.
    assert "ALTER ROLE" not in body.upper().replace("ALTER ROLE % NOSUPERUSER'", "")  # the NOTICE text names it, nothing executes it
    assert "NOSUPERUSER" in body.split("CREATE ROLE %I LOGIN")[1].splitlines()[0]  # the owner is created NOSUPERUSER


def test_app_role_holds_dml_execute_and_nothing_structural(test_schema):
    with psycopg2.connect(**test_schema) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user")
        assert cur.fetchone() == (False, False, False)
        cur.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (OWNER,))
        assert cur.fetchone()[0] is False
        assert _ownership(cur, OWNER) == []
        cur.execute("SELECT extowner::regrole::text FROM pg_extension WHERE extname='vector'")
        assert cur.fetchone()[0] != APP
        cur.execute("SELECT has_schema_privilege('public','USAGE'), has_schema_privilege('public','CREATE')")
        assert cur.fetchone() == (True, False)
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
        tables = [r[0] for r in cur.fetchall()]
        assert "memories" in tables and "schema_migrations" in tables
        for table in tables:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege(%s, %s)", (table, privilege))
                assert cur.fetchone()[0] is True, (table, privilege)
            for privilege in STRUCTURAL:
                cur.execute("SELECT has_table_privilege(%s, %s)", (table, privilege))
                assert cur.fetchone()[0] is False, (table, privilege)
        cur.execute("""SELECT p.oid::regprocedure::text, has_function_privilege(p.oid, 'EXECUTE') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                       WHERE n.nspname='public' AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid='pg_proc'::regclass AND x.objid=p.oid AND x.deptype='e')""")
        functions = dict(cur.fetchall())
        assert set(functions) >= {"memories_search_vector_update()", "extract_questions_text(text)"} and all(functions.values())
        # Default privileges: what the owner creates later is usable by the app role.
        cur.execute("SELECT defaclobjtype, defaclacl::text FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace WHERE n.nspname='public' AND d.defaclrole=%s::regrole", (OWNER,))
        defaults = dict(cur.fetchall())
        assert set(defaults) == {"r", "S", "f"} and all(APP in acl for acl in defaults.values())


def test_app_role_cannot_do_structural_work_but_the_trigger_runs(test_schema, clean_tables):
    conn = psycopg2.connect(**test_schema)
    try:
        for statement in ("CREATE TABLE app_made(id int)", "DROP TABLE memory_skim", "TRUNCATE memories",
                          "ALTER TABLE memories ADD COLUMN sneaky int", "CREATE INDEX sneaky ON memories(title)",
                          f"ALTER DATABASE {os.environ['DB_NAME']} SET hnsw.ef_search = 1", "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                          "DROP EXTENSION vector", f"GRANT {OWNER} TO {APP}"):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(statement)
            conn.rollback()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'Trigger check', 'the trigger runs as the inserting role') RETURNING search_vector IS NOT NULL")
            assert cur.fetchone()[0] is True
            cur.execute("SELECT pg_try_advisory_lock(42), pg_advisory_unlock(42)")
            assert cur.fetchone() == (True, True)
        conn.commit()
    finally:
        conn.close()


def test_owner_created_table_is_usable_by_app_role_through_default_privileges(test_schema, clean_tables):
    owner = owner_connect()
    owner.autocommit = True
    try:
        with owner.cursor() as cur:
            cur.execute("CREATE TABLE later_migration_table(id serial PRIMARY KEY, note text)")
        with psycopg2.connect(**test_schema) as app, app.cursor() as cur:
            cur.execute("INSERT INTO later_migration_table(note) VALUES ('ok') RETURNING id")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT currval('later_migration_table_id_seq')")
            assert cur.fetchone()[0] == 1
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE later_migration_table")
    finally:
        with owner.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS later_migration_table")
        owner.close()


def test_pg_dump_works_as_the_app_role_after_separation(test_schema, clean_tables, tmp_path):
    """backup_bundle.py dumps as DB_USER (the app role): SELECT on everything is enough."""
    with psycopg2.connect(**test_schema) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'dumped', 'row')")
        conn.commit()
    dump = tmp_path / "app.dump"
    result = _pg_dump(os.environ["DB_NAME"], APP, "--format=custom", "--no-owner", "--no-acl", "--file", str(dump))
    assert result.returncode == 0, result.stderr
    listing = subprocess.run([shutil.which("pg_restore", path=os.environ["PATH"]), "-l", str(dump)], capture_output=True, text=True, timeout=60)
    assert listing.returncode == 0
    assert "TABLE DATA public memories" in listing.stdout and "TABLE DATA public schema_migrations" in listing.stdout


def test_app_role_dump_restores_as_the_owner_into_a_fresh_database_and_017_regrants(test_schema, clean_tables, tmp_path):
    """The post-separation backup/restore contract (release notes §2.4):

    * backup_bundle.py dumps as the APP role (SELECT on everything suffices);
    * a restore into a *fresh* database is done as the OWNER role with
      ``--no-owner --no-acl --no-comments`` (the vector extension is created by the
      cluster superuser beforehand; the owner does not own it, so its COMMENT must
      be skipped) -- every restored object then belongs to the owner;
    * grants and default privileges are per database and not in the dump, so the
      app role has nothing until 017 is re-applied: delete its ledger row and run
      migrate.sh as the owner (017 is idempotent).  No superuser needed after the
      extension exists.
    """
    target = "sb_restore_roundtrip_test"
    with psycopg2.connect(**test_schema) as conn, conn.cursor() as cur:
        for i in range(3):
            cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', %s, 'row')", (f"roundtrip {i}",))
        conn.commit()
    dump = tmp_path / "app-role.dump"
    dumped = _pg_dump(os.environ["DB_NAME"], APP, "--format=custom", "--no-owner", "--no-acl", "--file", str(dump))
    assert dumped.returncode == 0, dumped.stderr
    binary = shutil.which("pg_restore", path=os.environ["PATH"])
    restored = subprocess.run([binary, "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"], "-U", OWNER, "-d", target,
                               "--exit-on-error", "--no-owner", "--no-acl", "--no-comments", str(dump)],
                              capture_output=True, text=True, timeout=120)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    with _connect(target, OWNER) as conn, conn.cursor() as cur:
        assert _ownership(cur, OWNER) == []
        cur.execute("SELECT count(*) FROM memories")
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT count(*) FROM schema_migrations WHERE version = '017_role_separation.sql'")
        assert cur.fetchone()[0] == 1
    # Nothing was granted in the new database: the dump carries no ACLs.
    with _connect(target, APP) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM memories")
    with _connect(target, OWNER) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM schema_migrations WHERE version = '017_role_separation.sql'")
        conn.commit()
    result = _migrate(target, user=OWNER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply: 017_role_separation.sql" in result.stdout and result.stdout.count("apply:") == 1
    with _connect(target, APP) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories")
        assert cur.fetchone()[0] == 3
        cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'after regrant', 'x') RETURNING search_vector IS NOT NULL")
        assert cur.fetchone()[0] is True
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("TRUNCATE memories")
        conn.rollback()


# --------------------------------------------------------------------------- migrate.sh refusals

def test_migrate_refuses_the_app_role_and_leaves_the_database_untouched():
    name = "sb_migration_app_role_test"
    result = _migrate(name, user=APP)
    assert result.returncode == 2
    assert "refuse" in result.stderr and f"owned by '{OWNER}'" in result.stderr
    with _connect(name, OWNER) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        assert cur.fetchone()[0] is None


def test_migrate_refuses_when_app_role_equals_owner_role():
    name = "sb_migration_app_role_test"
    result = _migrate(name, user=OWNER, app_role=OWNER)
    assert result.returncode == 2 and "must be distinct" in result.stderr
    with _connect(name, OWNER) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        assert cur.fetchone()[0] is None


def test_migrate_as_owner_then_the_app_role_sees_the_ledger():
    name = "sb_migration_app_role_test"
    result = _migrate(name, user=OWNER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply: 017_role_separation.sql" in result.stdout
    with _connect(name, APP) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM schema_migrations")
        assert cur.fetchone()[0] >= "017_role_separation.sql"
        assert _ownership(cur, OWNER) == []


# --------------------------------------------------------------------------- bootstrap (installed shape)

@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory):
    """The installed system before R7: one SUPERUSER application role owns everything."""
    name, legacy, owner = BOOT["database"], BOOT["legacy_user"], BOOT["owner_user"]
    with _connect(name, legacy) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
        assert cur.fetchone()[0] is True
        cur.execute("SELECT datdba::regrole::text, (SELECT extowner::regrole::text FROM pg_extension WHERE extname='vector') FROM pg_database WHERE datname=current_database()")
        assert cur.fetchone() == (legacy, legacy)
    # R3 head: ledger through 016, run by the application role (as installed).
    result = _migrate(name, user=legacy, owner_role=owner, app_role=legacy, directory=_dir_through(tmp_path_factory.mktemp("through016"), 16))
    assert result.returncode == 0, result.stdout + result.stderr
    with _connect(name, legacy) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO memories (type, title, content, source_type, source_url) VALUES ('source', 'pre-017', 'row', 'article', 'test://pre017') RETURNING id")
        memory = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname=%s", (owner,))
        assert cur.fetchone()[0] == 0
        conn.commit()
    return {"name": name, "legacy": legacy, "owner": owner, "memory": memory}


def test_bootstrap_017_runs_as_the_application_role_and_hands_everything_to_the_owner(bootstrapped):
    name, legacy, owner = bootstrapped["name"], bootstrapped["legacy"], bootstrapped["owner"]
    result = _migrate(name, user=legacy, owner_role=owner, app_role=legacy, until="017_role_separation.sql")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply: 017_role_separation.sql" in result.stdout and "stop: MIGRATE_UNTIL" in result.stdout
    assert "apply: 018" not in result.stdout
    assert "still a SUPERUSER" in result.stderr  # the NOTICE: grants are inert until the operator step
    with _connect(name, legacy) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM schema_migrations")
        assert cur.fetchone()[0] == "017_role_separation.sql"
        cur.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=%s", (owner,))
        assert cur.fetchone() == (True, False, False, False)
        assert _ownership(cur, owner) == []
        cur.execute("SELECT extowner::regrole::text FROM pg_extension WHERE extname='vector'")
        assert cur.fetchone()[0] == owner
        cur.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE m.roleid=%s::regrole AND r.rolname=%s", (owner, legacy))
        assert cur.fetchone()[0] == 0
        # Grants are in the ACLs now even though the superuser does not need them yet.
        for table in ("memories", "memory_relationships", "schema_migrations", "corpus_maintenance", "entities"):
            assert _acl_privileges(cur, table, legacy) == {"SELECT", "INSERT", "UPDATE", "DELETE"}, table
        cur.execute("SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace WHERE n.nspname='public' AND d.defaclrole=%s::regrole", (owner,))
        assert cur.fetchone()[0] == 3
    # The database now belongs to the owner: the same bootstrap command is refused.
    again = _migrate(name, user=legacy, owner_role=owner, app_role=legacy, until="017_role_separation.sql")
    assert again.returncode == 2 and f"owned by '{owner}'" in again.stderr, again.stderr


def test_bootstrap_after_017_the_application_role_is_refused_by_migrate_even_as_superuser(bootstrapped):
    name, legacy, owner = bootstrapped["name"], bootstrapped["legacy"], bootstrapped["owner"]
    result = _migrate(name, user=legacy, owner_role=owner, app_role=legacy)
    assert result.returncode == 2 and f"owned by '{owner}'" in result.stderr
    with _connect(name, legacy) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM schema_migrations")
        assert cur.fetchone()[0] == "017_role_separation.sql"


def test_bootstrap_nosuperuser_is_the_separate_operator_step_and_the_app_still_works(bootstrapped, tmp_path):
    name, legacy, owner, memory = bootstrapped["name"], bootstrapped["legacy"], bootstrapped["owner"], bootstrapped["memory"]
    # The operator step (runbook, CONFIRM-REQUIRED). A superuser may demote itself; the
    # installed runbook does it as the cluster superuser. Undo is the reverse statement.
    demote = _psql(name, legacy, f"ALTER ROLE {legacy} NOSUPERUSER NOCREATEDB NOCREATEROLE")
    assert demote.returncode == 0, demote.stderr
    conn = _connect(name, legacy)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            assert cur.fetchone()[0] is False
            # Application work: read, write (trigger), relate, advisory locks, the ledger.
            cur.execute("SELECT title FROM memories WHERE id=%s", (memory,))
            assert cur.fetchone()[0] == "pre-017"
            cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'post-017', 'x') RETURNING id, search_vector IS NOT NULL")
            new_id, vectorised = cur.fetchone()
            assert vectorised is True
            cur.execute("INSERT INTO memory_relationships (source_id, target_id, relation_type) VALUES (%s, %s, 'related_to')", (memory, new_id))
            cur.execute("UPDATE memories SET access_count = access_count + 1 WHERE id=%s", (memory,))
            cur.execute("DELETE FROM memory_relationships WHERE source_id=%s", (memory,))
            cur.execute("SELECT pg_try_advisory_xact_lock(7)")
            cur.execute("INSERT INTO corpus_maintenance (manifest_sha256, operation_key) VALUES ('x', 'y')")
            cur.execute("SELECT count(*) FROM schema_migrations")
            assert cur.fetchone()[0] == 16
        conn.commit()
        for statement in ("CREATE TABLE nope(id int)", "TRUNCATE memories", "DROP TABLE entities",
                          f"ALTER DATABASE {name} SET hnsw.ef_search = 1", "DROP EXTENSION vector", f"SET ROLE {owner}"):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(statement)
            conn.rollback()
    finally:
        conn.close()
    dump = _pg_dump(name, legacy, "--format=custom", "--no-owner", "--no-acl", "--file", str(tmp_path / "legacy.dump"))
    assert dump.returncode == 0, dump.stderr


def test_bootstrap_later_migrations_run_only_as_the_owner(bootstrapped):
    name, legacy, owner = bootstrapped["name"], bootstrapped["legacy"], bootstrapped["owner"]
    refused = _migrate(name, user=legacy, owner_role=owner, app_role=legacy)
    assert refused.returncode == 2
    result = _migrate(name, user=owner, app_role=legacy)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("(already applied)") == 16
    with _connect(name, legacy) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == 16 + result.stdout.count("apply:")
        assert _ownership(cur, owner) == []
        cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'after the owner run', 'x')")
        conn.commit()
