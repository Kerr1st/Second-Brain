-- 017: runtime role separation (F-138, audit 2026-09-24; review B §1).
--
-- Before this file the application role (memory_bank) is a SUPERUSER that owns
-- the database, every table, both functions and the vector extension. After it:
--   * the OWNER role (second_brain.owner_role, default memory_bank_owner) owns the
--     database, every relation and function in public, and the vector extension;
--     it is LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE and is used only by
--     migrations/migrate.sh (DB_MIGRATE_USER), the HNSW reindex job and a
--     restore-based rollback;
--   * the APP role (second_brain.app_role, default memory_bank) keeps exactly
--     what the R3-R5 code paths need: USAGE on schema public; SELECT, INSERT,
--     UPDATE, DELETE on every table; USAGE, SELECT on every sequence; EXECUTE on
--     every function (the search_vector trigger function runs as the inserting
--     role); the same by default on objects the owner creates later. No CREATE
--     on the schema, no TRUNCATE, no ownership, no membership of the owner role.
--
-- Bootstrap (who runs this file): the application role itself, while it still
-- owns the database -- migrate.sh refuses any run whose connecting role is not
-- the database owner, so this is the one migration applied as
--   DB_MIGRATE_USER=memory_bank DB_OWNER_ROLE=memory_bank_owner MIGRATE_UNTIL=017_role_separation.sql
-- CREATE ROLE and REASSIGN OWNED need the superuser status memory_bank still has
-- at that moment. From 018 on, migrate.sh connects as memory_bank_owner.
--
-- NOT done here, deliberately: ALTER ROLE memory_bank NOSUPERUSER. A migration
-- that runs as memory_bank cannot safely demote its own session, and the demotion
-- is the one step that changes what the running services may do, so it is a
-- separate, CONFIRM-REQUIRED operator step after the $0 smoke of the app role
-- (R7 release notes, runbook; undo: ALTER ROLE memory_bank SUPERUSER as the
-- cluster superuser). Until it is done the grants below are inert for the app
-- role and the ownership transfer is the only effect.
--
-- Role names come from two settings migrate.sh sets per transaction
-- (SET LOCAL second_brain.owner_role / second_brain.app_role); the file never
-- names a role literally, so the isolated suite runs it with its own roles.
-- Re-runnable: every step tolerates its own effect (role exists, already owned,
-- grants are idempotent). Verification at the end fails the transaction rather
-- than leaving a half-separated state.
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    app   text := current_setting('second_brain.app_role');
    owner text := current_setting('second_brain.owner_role');
    owner_oid oid;
    app_oid oid;
    db_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
    r record;
    bad text;
BEGIN
    IF app IS NULL OR owner IS NULL OR app = '' OR owner = '' THEN
        RAISE EXCEPTION '017: second_brain.app_role and second_brain.owner_role must both be set (migrate.sh does this)';
    END IF;
    IF app = owner THEN
        RAISE EXCEPTION '017: application role and owner role are both %; they must be distinct', app;
    END IF;
    SELECT oid INTO app_oid FROM pg_roles WHERE rolname = app;
    IF app_oid IS NULL THEN
        RAISE EXCEPTION '017: application role % does not exist', app;
    END IF;

    -- 1. Owner role: LOGIN so migrate.sh can connect as it directly (a NOLOGIN
    --    owner would need a second LOGIN member role and SET ROLE for nothing).
    --    No password is set here; authentication is the operator's step.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = owner) THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', owner);
    END IF;
    SELECT oid INTO owner_oid FROM pg_roles WHERE rolname = owner;
    IF EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid = owner_oid AND member = app_oid) THEN
        RAISE EXCEPTION '017: % is a member of %; the application role must never reach owner privileges', app, owner;
    END IF;
    IF (SELECT rolsuper FROM pg_roles WHERE oid = owner_oid) THEN
        RAISE EXCEPTION '017: owner role % must not be a superuser', owner;
    END IF;

    -- 2. Everything the application role owns in this database -- tables with
    --    their indexes, the two functions, the vector extension, the database
    --    itself (a shared object, dbid 0) -- moves to the owner in one statement.
    --    Skipped when the app role owns nothing (fresh clusters), because
    --    REASSIGN OWNED requires privileges on both roles.
    IF EXISTS (SELECT 1 FROM pg_shdepend d
               WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = app_oid AND d.deptype = 'o'
                 AND (d.dbid = db_oid OR d.dbid = 0)) THEN
        EXECUTE format('REASSIGN OWNED BY %I TO %I', app, owner);
    END IF;

    -- 3. Anything in public still owned by a third role (none on the installed
    --    system on 2026-10-06; kept so a re-run or an odd clone converges).
    FOR r IN SELECT c.oid::regclass AS rel, c.relkind
             FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND c.relowner <> owner_oid
             AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
                       CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                      WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END, r.rel, owner);
    END LOOP;
    FOR r IN SELECT p.oid::regprocedure AS fn, p.prokind
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public' AND p.proowner <> owner_oid
             AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I', CASE r.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, r.fn, owner);
    END LOOP;
    IF (SELECT datdba FROM pg_database WHERE oid = db_oid) <> owner_oid THEN
        EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), owner);
    END IF;

    -- 4. Application privileges: DML and nothing structural. Explicit REVOKEs
    --    first so a re-run converges even if something broader was granted.
    EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', app);
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', app);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', app);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', app);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', app);
    -- Our own functions only: pgvector's live in public too, keep their default
    -- PUBLIC EXECUTE and may be owned by the cluster superuser that created the
    -- extension (a GRANT on them by a non-owner would only emit warnings).
    FOR r IN SELECT p.oid::regprocedure AS fn, p.prokind
             FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public'
             AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e')
    LOOP
        EXECUTE format('REVOKE ALL ON %s %s FROM %I', CASE r.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, r.fn, app);
        EXECUTE format('GRANT EXECUTE ON %s %s TO %I', CASE r.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END, r.fn, app);
    END LOOP;
    -- Objects the owner creates in later migrations get the same grants.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', owner, app);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I', owner, app);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO %I', owner, app);

    -- 5. Verify, or fail the whole file.
    SELECT string_agg(c.oid::regclass::text, ', ') INTO bad
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND c.relowner <> owner_oid
       AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid AND x.deptype = 'e');
    IF bad IS NOT NULL THEN RAISE EXCEPTION '017: relations not owned by %: %', owner, bad; END IF;
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO bad
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proowner <> owner_oid
       AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid = 'pg_proc'::regclass AND x.objid = p.oid AND x.deptype = 'e');
    IF bad IS NOT NULL THEN RAISE EXCEPTION '017: functions not owned by %: %', owner, bad; END IF;
    IF (SELECT datdba FROM pg_database WHERE oid = db_oid) <> owner_oid THEN
        RAISE EXCEPTION '017: database % is not owned by %', current_database(), owner;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector' AND extowner = app_oid) THEN
        RAISE EXCEPTION '017: the vector extension is still owned by the application role %', app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid = owner_oid AND member = app_oid) THEN
        RAISE EXCEPTION '017: % must not be a member of %', app, owner;
    END IF;
    IF (SELECT rolsuper FROM pg_roles WHERE oid = app_oid) THEN
        RAISE NOTICE '017: % is still a SUPERUSER; the grants above take effect only after the separate operator step ALTER ROLE % NOSUPERUSER', app, app;
    ELSE
        IF has_schema_privilege(app, 'public', 'CREATE') THEN
            RAISE EXCEPTION '017: % must not have CREATE on schema public', app;
        END IF;
        FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') LOOP
            IF NOT (has_table_privilege(app, r.rel, 'SELECT') AND has_table_privilege(app, r.rel, 'INSERT')
                    AND has_table_privilege(app, r.rel, 'UPDATE') AND has_table_privilege(app, r.rel, 'DELETE')) THEN
                RAISE EXCEPTION '017: % lacks DML on %', app, r.rel;
            END IF;
            IF has_table_privilege(app, r.rel, 'TRUNCATE') OR has_table_privilege(app, r.rel, 'REFERENCES')
               OR has_table_privilege(app, r.rel, 'TRIGGER') THEN
                RAISE EXCEPTION '017: % holds a structural privilege on %', app, r.rel;
            END IF;
        END LOOP;
    END IF;
END $$;
