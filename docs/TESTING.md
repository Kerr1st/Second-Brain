# Testing Second Brain

Use three layers. The first two protect software behavior; the third evaluates whether an agent actually helps with a real task. Test count is not a success criterion.

| Layer | Question | Dependencies | When |
|---|---|---|---|
| Behavior | Does a public operation follow its contract? | Scripted external services; temporary files allowed | Every change / PR |
| Integration | Do real components connect correctly? | Disposable PostgreSQL/pgvector, realistic native fixtures, scripted models/embeddings | Every PR |
| AI outcomes | Does the resulting answer support the right next action? | Frozen real source evidence, actual consumer output, explicit review | Changes to capture, distillation, prompts, ranking, or delivery |

## Run deterministic checks

Install `requirements-dev.txt` in `.venv`. The behavior layer needs no database or model service.

```sh
scripts/test.sh behavior
scripts/test.sh integration
scripts/test.sh all
```

Extra arguments go to pytest, for example `scripts/test.sh integration -q --durations=10`. Set `SECOND_BRAIN_PYTHON` to choose a different interpreter. The wrapper runs from the repository root.

The integration/all commands use `second_brain_codex_test` by default and align `DB_NAME` with `TEST_DB_NAME`. Only `second_brain_codex_test` and the legacy `memory_bank_test` are permitted. These databases are recreated; they must contain disposable test data only. Set `DB_HOST`, `DB_PORT`, `DB_USER`, and `DB_PASSWORD` for a dedicated local PostgreSQL/pgvector instance. The migration runner also needs `psql` and database creation privileges. Do not direct tests at a production database server.

Direct pytest selection also works:

```sh
.venv/bin/python -m pytest -m behavior
TEST_DB_NAME=second_brain_codex_test .venv/bin/python -m pytest -m integration
```

The shared fixture redirects application database access and restores it even after setup failure. `db_connection` supplies an autocommit connection for SQL-facing tests; do not create another module-local `test_db` fixture. A missing required database is an error, not a skip.

Each collected test gets exactly one layer marker. A dependency on `test_db`, including through another fixture, means integration. Tests that manage a separate disposable database, such as the migration-runner tests, explicitly declare `pytest.mark.integration`. Everything else is behavior. Conflicting behavior/database declarations fail collection. Markers are registered and unknown marker names fail.

Python socket connections are blocked in both deterministic layers. Behavior tests also cannot open a real PostgreSQL connection. Mock external model/HTTP adapters at their boundary. The guard is not an operating-system sandbox: subprocess-based adapters still need scripted executables or subprocess mocks. Never supply live provider credentials to CI.

Run one suite per shared database; do not use parallel pytest workers without per-worker database isolation. The two CI jobs run on separate runners. The workflow follows [GitHub's PostgreSQL service-container pattern](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers) and uses the same pgvector major version as `docker-compose.yml`.

## Put checks at the right boundary

- Call the actual public operation. For agent invocation, record the request at the external Invoker boundary; do not rebuild the message in a test helper.
- Use decision tables for finite contracts, such as all 16 binary consensus permutations. Preserve invalid input and failure boundaries.
- Keep randomized properties when the varying input could expose a defect: arbitrary text, ordering, deduplication, and numeric boundaries. Avoid asserting constants across random inputs.
- Keep real database coverage for transactional approval, concurrency, and provenance. Similar setup does not imply redundant behavior.
- Extend the Codex reference lifecycle before creating parallel fake “end-to-end” frameworks. `test_real_user_correction_persists_neutral_episode_with_exact_provenance` now covers native capture, semantic persistence, retrieval, context delivery, and the outcome receipt. Its model responses are scripted, so the outcome is explicitly `unknown` for live usefulness.
- `test_integration.py` contains mocked Dream Cycle orchestration tests and therefore belongs to the behavior layer. File names do not determine the layer.

The new invocation tests must fail when Explorer/Thinker invocation is disabled. Scope tests must fail when applicability is bypassed. The consensus truth table must fail if two accepts become sufficient. Faults belong only in temporary test processes, never in deployed source.

## Evaluate AI outcomes

Follow [the outcome protocol](../evaluations/OUTCOMES.md) and copy [the scorecard](../evaluations/OUTCOME-TEMPLATE.md) into an ignored, dated `evaluations/results/` directory. Job Search is the first real-work evaluation. Keep private source transcripts, career details, model outputs, and receipts in that ignored directory.

A real outcome run is intentionally separate from pytest. It must identify its source snapshot, question, actual output, model/configuration, baseline comparison, and reviewer. A replay with scripted semantic output cannot be reported as a live AI quality pass. Agent assessment cannot silently become human approval.

Retrieval benchmarks support this layer but do not replace outcome review:

```sh
.venv/bin/python -m scripts.eval.run_evaluation --dry-run
.venv/bin/python -m scripts.eval.run_evaluation --tier curated
```

The second command uses the database and embedding service selected by the caller's environment. It is not an isolated pytest run. Configure it deliberately for the intended evaluation corpus.

Runner exit codes: **0** means selected benchmarks ran with usable queries (or a history/dry-run report completed); **1** means a tier failed; **2** means a required tier had no usable queries. Failure takes precedence over insufficient evidence. Summary records include status and evaluated query counts. “Completed” means measurements were produced, not that retrieval or usefulness met a quality threshold. `trends` only displays historical reports.

The committed curated seed still contains placeholders. Until real corpus IDs are supplied, curated evaluation correctly returns insufficient evidence. Do not replace placeholders with arbitrary IDs merely to get a zero exit code.

## Review and remaining work

The [September 6 audit](research/TEST-SUITE-REVIEW-2026-09-06.md) remains the historical baseline. This implementation replaces copied Thinker checks, removes six confirmed low-value cases, consolidates the consensus table, unifies database setup, adds actual invocation/scope contracts, extends one lifecycle, and distinguishes failed/empty evaluations.

Broader adapter consolidation, private-method checks, Crawlee coverage, and SQLite ResourceWarnings remain separate follow-ups. The known real-work ownership/coverage limitation remains visible in the outcome baseline; this test reorganization does not repair it or alter the source eligibility policy.

## Implementation verification — September 7, 2026

- Complete suite: 934 passed in 21.49 seconds with coverage instrumentation.
- Behavior alone: 735 passed in 9.19 seconds; integration alone: 199 passed in 10.27 seconds.
- Disabled Explorer/Thinker: all seven new invocation cases failed, as intended.
- Bypassed applicability: six new denial cases failed, as intended.
- Evaluation exception and empty-run checks failed before the runner fix and passed afterward.
- `src` statement coverage rose from 82.5% to 84.0%; branch coverage from 70.9% to 72.3%. This excludes script coverage and is not an AI usefulness measure.
- Existing SQLite ResourceWarnings remain visible in the instrumented run; isolated layer runs emitted the existing Starlette deprecation warning.

The case count changed from 912 to 934: six low-value cases and three copied-payload cases were removed, and 31 contract/isolation/evaluation cases were added. The 16 explicit consensus permutations were retained in one table. Detailed execution evidence and the private historical Job Search baseline are under `evaluations/results/2026-09-07-testing-layers/`.
