# 12a — Tests: Isolation, Fixtures, Offline Guards

Second-pass audit track (narrow scope). Repo HEAD 6b8c2d2. Read-only review of
`tests/conftest.py` and the four modules that define their own `test_db`
fixtures (`test_ingest_doc_chunks.py`, `test_ingest_eventlog.py`,
`test_ingest_interactions.py`, `test_enrich_qd_tags.py`), plus a grep of
`tests/` for anything that can reach network, money, or the home directory.

Severity: S1 = data-loss / cost / production-touching; S2 = correctness or
isolation defect; S3 = hygiene / refactor seam.

## Findings


### F1 — S1 — Four module-level `test_db` fixtures shadow the conftest one and let the code under test write to the production `memory_bank` DB

**Files:** `tests/test_ingest_doc_chunks.py:156-172`, `tests/test_ingest_eventlog.py:156-172`,
`tests/test_ingest_interactions.py:121-136`, `tests/test_enrich_qd_tags.py:224-241`.

**Mechanism.** `tests/conftest.py:103-134` defines `test_db` (session scope) whose *only* isolation
mechanism is `db.DB_CONFIG.update({... "dbname": TEST_DB_NAME ...})` on the module-global dict in
`src/db.py:17-23`, followed by `close_pool()` so the lazily-built `SimpleConnectionPool`
(`src/db.py:33-39`) is rebuilt with the new dict on next `get_connection()`.

The four modules each define a *function-scoped* fixture with the same name. Pytest resolves the
nearest definition, so inside those modules the conftest fixture is never requested. The local
fixture opens its own `psycopg2.connect(dbname="memory_bank_test")` and yields that connection —
but the code under test does not use it:

- `scripts/migrate/ingest_doc_chunks.py:28` — `from src.db import create_memory, get_processed_source_urls`
- `scripts/migrate/ingest_eventlog.py:20` — `from src.db import create_memory, get_processed_source_urls, get_connection`
- `scripts/migrate/enrich_qd_tags.py:23,89,132` — `from src.db import get_connection`

Every write in those E2E tests therefore goes through `src.db.get_connection()` → `DB_CONFIG`,
i.e. whatever `DB_NAME` currently is (default `"memory_bank"` = production).

**Order dependence.** Whether `DB_CONFIG` points at `memory_bank_test` at the moment these tests
run depends entirely on whether *some other module* in the same session has already requested the
conftest `test_db` (session fixture set up on first use, torn down at session end). Concretely:

- `pytest tests/test_ingest_doc_chunks.py` alone, or `pytest -p randomly` / `-k` selections that
  run these four modules first → `ingest_doc_chunks(..., dry_run=False)` inserts three
  `quick_desktop_doc` memories into **production `memory_bank`**, while the local fixture's
  `DELETE FROM memories WHERE source_url LIKE 'qd-doc://%'` cleans only `memory_bank_test`. The
  assertion `count(*) == 3` then reads the wrong DB and fails/passes unrelated to correctness.
- Full-suite alphabetical run: `test_db.py`, `test_dream_cycle_db.py`, `test_capture_*` etc. sort
  earlier and request the conftest `test_db`, so DB_CONFIG has usually been swapped already and the
  bug is masked. That masking is why this has not been noticed.
- `test_ingest_interactions.py` is the worst case: `enrich_with_interactions(dry_run=False)`
  issues `UPDATE memories SET metadata = ...` through `src.db.get_connection()` — on production if
  run first — while `seeded_feed_events` seeds only the test DB.

**Second defect in the same fixtures.** The `try/except psycopg2.OperationalError: pytest.skip`
wraps the `yield`. A `psycopg2.OperationalError` raised *inside the test body* (or in teardown)
is caught by the fixture and converted into a skip, hiding real failures. It also means
`conn.close()` is never reached on that path.

**Minimal fix.** Delete the four local `test_db` fixtures and take the conftest one. Where the
tests need a raw autocommit cursor into the test DB, add a small conftest fixture:

```python
@pytest.fixture()
def test_conn(test_db):
    conn = psycopg2.connect(**test_db)   # test_db yields the overridden DB_CONFIG dict
    conn.autocommit = True
    yield conn
    conn.close()
```

and rename the local usages `test_db` → `test_conn`. This preserves the "skip when Postgres is
down" behaviour only if the conftest fixture gains it (today it raises, see F4).

**Structural fix (recommended).** Make DB redirection not depend on any fixture being requested:
an autouse *session* fixture in conftest that sets `DB_CONFIG["dbname"] = TEST_DB_NAME` and calls
`close_pool()` before any test runs, plus a guard that fails the session if
`db.DB_CONFIG["dbname"] == "memory_bank"` at collection time. Also add a
`pytest.fixture(autouse=True)` that monkeypatches `psycopg2.connect` to refuse
`dbname="memory_bank"` in unit tests.

### F2 — S2 — `sample_memory_factory` / `clean_tables` are the only cleanup, and they clean only two tables

**File:** `tests/conftest.py:137-146`.

`clean_tables` deletes from `memory_relationships` and `memories` only. Migrations create at least
`documents`, `chunks`, `attention_ledger`, `dream_runs`, `skim_*` etc. (see `migrations/`, F5).
Tests in `test_dream_cycle_db.py`, `test_ledger.py`, `test_skim_store.py`, `test_ingest_v2.py`
write to other tables through the same session `test_db`, so the test DB accumulates rows across
tests and across sessions — `_create_test_db()` (`conftest.py:66-77`) never drops or recreates the
DB. Row-count assertions in one module are then sensitive to what ran before it.

Also the deletion is done *after* yield only (`yield` at line 140), so the very first test of a
session inherits whatever the previous session left behind (e.g. rows from a crashed run).

**Fix:** truncate before *and* after, and truncate every table discovered from
`information_schema.tables WHERE table_schema='public'` (excluding the migration ledger), or
simply `DROP DATABASE`/`CREATE DATABASE` once per session in `test_db`.

### F3 — S3 — Conftest docstring is stale

**File:** `tests/conftest.py:1-8` says `test_db` "creates/migrates the memory_bank_test database"
and lists four fixtures; it does not mention `_skim_store_is_offline` (autouse) and states
`clean_tables` "truncates" (it uses `DELETE`). Minor, but it is the only place a new contributor
learns the isolation contract.


### F4 — S1 — `_apply_migrations` runs `ALTER DATABASE memory_bank SET hnsw.ef_search = 200` against production from a test session

**Files:** `tests/conftest.py:80-100` (`_apply_migrations`), `migrations/009_hnsw_recall.sql:27`.

`_apply_migrations` is executed by the session `test_db` fixture while connected to
`memory_bank_test`. `ALTER DATABASE` names its target explicitly, so migration 009's last statement
mutates the *production* database's per-database GUC even though the connection is to the test DB.
It succeeds because the `memory_bank` role owns both databases.

Evidence (read-only `SELECT` on `pg_db_role_setting`, 2026-09-24):

```
memory_bank             | {hnsw.ef_search=200}
second_brain_codex_test | {hnsw.ef_search=200}
memory_bank_test        | (no row)
```

Two consequences: (a) every test session performs a DDL write on production — a text edit to 009
(e.g. changing 200 to another value for an experiment) would silently retune prod recall on the
next `pytest`; (b) the test DB itself runs at the pgvector default `ef_search = 40`, so recall
behaviour under test does not match production — the exact discrepancy 009 was written to remove.

**Fix:** in 009 replace the hard-coded name with `ALTER DATABASE current_database()` — not valid
SQL; use a `DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database()); END $$;`.
Additionally have `_apply_migrations` refuse to run if `current_database() = 'memory_bank'`.

### F5 — S2 — `_apply_migrations` is all-or-nothing per file, catches only two error classes, and never writes the ledger

**File:** `tests/conftest.py:80-100`.

1. **Docstring is false.** It says "Uses savepoints so that already-applied migrations ... are
   silently skipped". There is no `SAVEPOINT`; each file is sent as one `cur.execute(sql)` and the
   *whole file* is `conn.rollback()`-ed on `DuplicateTable`/`DuplicateObject`.
2. **Consequence for appended statements.** `001_initial_schema.sql:3,23` and
   `003_dream_cycle.sql:10,29,53-56` use bare `CREATE TABLE`/`CREATE INDEX`. On the second and every
   later session they raise `DuplicateTable` (42P07) at the first such statement, and everything
   after it in that file is skipped — including any statement a developer appends later (e.g.
   `003_dream_cycle.sql:61` `ALTER TABLE memory_relationships ADD COLUMN IF NOT EXISTS expired_at`
   only ever ran because it was present on the first session). Editing an existing migration file
   is therefore silently ineffective on an existing `memory_bank_test`, while `migrate.sh` would
   also skip it (ledger says applied) — but for a different reason. The two runners diverge.
3. **Only 42P07/42710 are swallowed.** `DuplicateFunction` is not an issue (005/001 use
   `CREATE OR REPLACE`), but a `CREATE TRIGGER trg_memories_search_vector` (001:53) raises
   `DuplicateObject` — swallowed, fine. Any *other* error (e.g. `UndefinedTable` from a reordered
   file, `InsufficientPrivilege` on 009's `ALTER DATABASE` if the role ever loses ownership) aborts
   the session fixture and every DB test errors with a stack trace pointing into conftest, not into
   the migration.
4. **Ledger never written.** `schema_migrations` in `memory_bank_test` has **0 rows** (verified by
   `SELECT count(*)`); production has 14. `migrate.sh:26-30` is the only writer. Nothing in `tests/`
   exercises the ledger or `migrate.sh` (`test_migration.py` tests chat-migration formatting, not
   schema migrations — a naming collision worth a rename to `test_chat_migration.py`).
5. **Re-executed data statements.** `005_question_weighted_search.sql:104` `UPDATE memories SET
   search_vector = ...` has no duplicate-object guard, so it re-runs every session. Cheap on an
   empty test DB, but it means the "idempotent skip" model is not what actually happens.

**Fix:** make `_apply_migrations` mirror `migrate.sh`: read `schema_migrations`, skip applied
versions, apply each unapplied file once, insert its version. Then the test DB and the shell
runner share one contract and the ledger becomes testable. Alternatively, drop and recreate the
test DB once per session so first-run semantics always hold.

### F6 — S2 — Test fixture identifiers collide with live production `source_url`s

**Files:** `tests/test_ingest_eventlog.py:20,33,44,184,213`, `tests/test_ingest_interactions.py:14-36`,
`tests/test_ingest_doc_chunks.py:19-43`.

Fixture events are `evt_00001..evt_00003` and doc chunks `qd-doc://4176`, `4177`, `8872`.
Production `memory_bank` contains **real** rows with exactly these `source_url`s
(`qd-feed://evt_00001..evt_00009`, `qd-doc://4176/4177/8872`, created 2026-05-17, with real
titles). The tests' cleanup statements are
`DELETE FROM memories WHERE source_url LIKE 'qd-feed://evt_0000%'` /
`... LIKE 'qd-doc://%'` (the latter matches **32,624** production rows). Today those `DELETE`s run
on the explicitly-connected test DB, so they are safe *only because of* the very shadowing fixture
that F1 says to remove. Whoever applies the F1 fix by pointing the local connection at `DB_CONFIG`
without first guaranteeing `DB_CONFIG` is the test DB turns a correctness bug into a mass delete.

Also: production `qd-feed://evt_00001` carries `metadata.interactions` whose timestamps
(`2026-04-13T20:03:51.935207`, `link_click`) are byte-identical to `SAMPLE_INTERACTIONS` in
`test_ingest_interactions.py:15-20`. This is consistent with the fixture having been copied from
real data, and equally consistent with the F1 leak having already fired once; the DB alone cannot
distinguish them (see *Could not verify*).

**Fix:** namespace fixture ids (`qd-feed://test-evt_00001`, `qd-doc://test-4176`) and never use a
bare `LIKE 'qd-doc://%'` in a cleanup statement.


### F7 — S2 — Test DB name is configurable in conftest but hard-coded in the four shadowing fixtures

**Files:** `tests/conftest.py:49` (`TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "memory_bank_test")`)
vs. `test_ingest_doc_chunks.py:163`, `test_ingest_eventlog.py:163`, `test_ingest_interactions.py:128`,
`test_enrich_qd_tags.py:232` (`"dbname": "memory_bank_test"` literal).

If `TEST_DB_NAME` is set (e.g. to run two suites side-by-side, or the `second_brain_codex_test`
database that already exists on this host), the four local fixtures inspect a different database
from the one the code under test writes to, and their `DELETE ... LIKE` cleanups hit the wrong DB.
Same root cause as F1; listed separately because the fix (use `test_db`'s yielded dict) is
different from the order-dependence fix.

Related inconsistency: the conftest fixture raises on `psycopg2.OperationalError` (every DB test
errors), the four local fixtures `pytest.skip`. The suite therefore has two different answers to
"Postgres is down".

### F8 — S2 — No autouse guard for Bedrock / boto3 / CLI subprocesses; offline-ness is per-test discipline only

**Scope grep of `tests/` (81 files, 1,630 tests collected):**

| pattern | files | assessment |
|---|---|---|
| `boto3` | 0 | never imported directly in tests |
| `src.embeddings` / `generate_embedding` | 14 | all call sites patched at the *importing* module (`src.ingest`, `src.mcp_server`, `src.dream_cycle.storage`, `src.capture_api`, `scripts.migrate.*`, `scripts.weekly_digest`, `scripts.distill_sessions`) — correct target, but there is **no fallback** if a new call site is added |
| `bedrock` | 9 | `test_bedrock.py` constructs `BedrockInvoker` without a client for shaping/config tests and never calls `.invoke()`; invoke tests pass `client=`. `src/backends/bedrock.py:220-233` builds a real `boto3.client` lazily on first invoke — one forgotten `client=` would go live |
| `subprocess` | 17 | backends (`kiro`, `claude_code`, `codex`) patched via `@patch("src.backends.X.subprocess.run")`; `test_repo_hygiene.py:24,44` runs real `git` (local, safe); `test_codex_canary_job.py`, `test_codex_capture_job.py`, `test_monitor_jobs.py` run the real shell scripts with a stubbed `SECOND_BRAIN_PYTHON`/`AWS_BIN` on `PATH` (safe by construction, but they **inherit `os.environ`** — see below) |
| `kiro-cli` / `claude` / `codex` | many | string references only, or patched adapters |
| `requests.` / `httpx` | 0 | (`httpx` reaches tests only via FastAPI `TestClient`) |
| `Path.home()` / `expanduser` | 1 | `test_monitor_jobs.py:209` passes the real `HOME` to `verify_credentials.sh --print-tools`; that branch (`scripts/jobs/verify_credentials.sh:52-58`) only echoes resolved tool paths and `exit 0` — no AWS call |

**Tests that would spend money or leave the machine if a single patch were missing (no second line
of defence):**

1. Every `generate_embedding` consumer above — `src/embeddings.py:20-21` creates a live
   `bedrock-runtime` client on first call with whatever credentials the shell has (this host has
   `sso-default` and `second-brain-jobs` profiles per `verify_credentials.sh:98-99`). Unlike the skim
   store, `src.embeddings._get_client` has **no autouse refusal** in conftest.
2. `test_ingest_eventlog.py:203-204` and `test_ingest_doc_chunks.py:189-190` call the ingest twice
   inside one `patch` context — fine — but `ingest_eventlog` swallows every exception per event
   (`scripts/migrate/ingest_eventlog.py:143-145`), so an unpatched Bedrock call with *no* credentials
   would surface only as `stats["failed"]`, and with credentials would silently bill 3 Titan calls.
3. `test_codex_canary_job.py:12-13,37-42`, `test_codex_capture_job.py:46-50`,
   `test_monitor_jobs.py:80-87` spread `**os.environ` into the child. The scripts consult
   `SECOND_BRAIN_PYTHON`/`AWS_BIN` first, so this is currently safe, but any new env-driven branch in
   those scripts (e.g. a real `AWS_PROFILE` check) would run against the developer's live profile.
4. `BedrockInvoker` (`src/backends/bedrock.py:211-233`): no conftest guard on `_lazy_client`; a
   test that resolves a Bedrock backend via `default_resolver()` and forgets to stub the adapter
   would call Converse.

**Existing good pattern to copy:** `tests/conftest.py:26-40` `_skim_store_is_offline` (autouse,
function-scoped, monkeypatch → restored automatically). The same three lines applied to
`src.embeddings._get_client`, `src.backends.bedrock.BedrockInvoker._client`/lazy getter, and a
`subprocess.run` wrapper that raises when `argv[0]` is one of `kiro-cli`, `claude`, `codex`, `aws`
would make the whole suite fail closed. Tests that legitimately need the real thing opt out with a
marker (`@pytest.mark.live`).

### F9 — S3 — `.hypothesis/` directory

`/Users/kerrfir/second-brain/.hypothesis/` exists (3 entries, mtime 2026-09-24 05:41), is listed in
`.gitignore:7`, and `git ls-files .hypothesis` returns nothing — correctly untracked. No finding
beyond noting that `pytest.ini` has no `[hypothesis]` profile; `deadline=None`/`max_examples` are
set ad hoc per test (`test_capture_api.py:361-408`, `test_claude_code_stream_json_probe_preservation.py:136-210`,
`test_classify.py:52`). A `register_profile("ci", ...)` in conftest would centralise this.

### F10 — S3 — Fixture naming and structure recommendations

Recommended `tests/conftest.py` shape (all names new unless noted):

```
# --- autouse, function scope, fail-closed guards -------------------------
_skim_store_is_offline     (exists)                 src.skim.store.get_connection → raise
_embeddings_are_offline    (new)                    src.embeddings._get_client → raise
_bedrock_is_offline        (new)                    src.backends.bedrock lazy client → raise
_no_agent_cli_subprocess   (new)                    subprocess.run/Popen argv[0] in {kiro-cli, claude, codex, aws} → raise
# --- autouse, session scope, DB redirection ------------------------------
_test_db_config            (new, autouse=True)      DB_CONFIG["dbname"]=TEST_DB_NAME; close_pool(); assert != "memory_bank"
# --- opt-in DB fixtures --------------------------------------------------
test_db        → rename  test_schema  (session)     create DB, apply migrations *with ledger*, yield DB_CONFIG
test_conn                  (new, function)          autocommit psycopg2 connection to TEST_DB_NAME (replaces the 4 local `test_db`)
clean_tables   → rename  truncate_all (function)    before+after, every public table except schema_migrations
mock_embedding → rename  deterministic_embedding    (it is a function, not a mock)
sample_memory_factory      (keep)
```

Renaming `test_db` matters beyond taste: pytest collects fixtures named `test_*` without
complaint, but `--collect-only -q` and IDE test explorers show them adjacent to real tests, and the
name invites exactly the module-level shadowing seen in F1.

## Could not verify

- Whether the F1 leak has ever actually fired. Production `qd-feed://evt_00001/2` carry
  `metadata.interactions` identical to `SAMPLE_INTERACTIONS` timestamps; the fixture was plainly
  copied from real Quick Desktop data, so identity of values is expected either way. No test-only
  marker (title, tag, source_url prefix) distinguishes leaked rows from real ones — that is itself
  finding F6.
- Actual run-order behaviour (pytest was not executed; the analysis is from fixture resolution
  rules and the alphabetical default order of the 81 modules).
- Whether `second_brain_codex_test` (a third database on this host, also carrying
  `hnsw.ef_search=200`) was created from a template or by an earlier `ALTER DATABASE` edit; it is
  outside the repo's `TEST_DB_NAME` default and no test references it.
- Behaviour of `src.embeddings._get_client` under the developer's real shell credentials (not
  exercised, by rule).
