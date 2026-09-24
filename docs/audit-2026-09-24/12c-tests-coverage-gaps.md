# 12c — Test coverage gaps for highest-risk behaviours

Repo: /Users/kerrfir/second-brain @ 6b8c2d2. Method: grep + read only (no pytest run). Scope: 20 named behaviours + resolver no-Kiro/no-Fable breakage list.

## Findings


### (1) `chunk_by_section` never exceeds `max_chunk_chars` for blank-line-free input — **UNPINNED** (S2)
- `grep -rn chunk_by_section tests/` → zero hits. `tests/test_ingest_doc_chunks.py` tests QD `file_chunks` ingestion, not this function.
- Code (`src/ingest.py:63-102`): the fallback `body.split("\n\n")` yields a single paragraph for blank-line-free input; the length guard at :80 only fires when `current` is non-empty, so a single 100 kB paragraph is emitted as one chunk. Merge loop (:93) also never splits an oversized section. So the property is *false* and untested — a test would fail, which is why this matters: it's a latent defect (oversize chunk → embedding truncation, see (2)).
- Fix: add `tests/test_ingest.py::test_chunk_by_section_hard_cap` (Hypothesis `st.text(alphabet=no '\n')` with `min_size=max_chunk_chars+1`) asserting `all(len(c) <= max_chunk_chars for c in chunks)`; then add a hard character split in `chunk_by_section`.

### (2) `generate_embedding` truncation detected/reported — **UNPINNED** (S2)
- `src/embeddings.py:36` silently slices `text[:max_chars]`; no log, no return flag, no counter. `grep -rn truncat tests/` hits only Slack markdown truncation (`test_capture_slack.py:58-61`), a skim orchestrator dict, and table truncation fixtures. No test touches `generate_embedding` truncation at all (`grep -rn generate_embedding tests/` → only mocks/patches, see below).
- Fix: have `generate_embedding` log at WARNING (or return `(vec, truncated: bool)`) and pin with a test that patches `_get_client` and asserts on the `inputText` body length and the warning.


### (3) Each capture channel maps a hard failure to a non-zero exit — mixed (S1 for the unpinned half)
| Channel | Status | Evidence |
|---|---|---|
| slack | **PINNED** | `tests/test_capture_slack_preflight.py::TestMainExitCodes::test_auth_failure_exits_3_and_notifies_once` / `test_transport_failure_exits_4_without_notifying` (`assert code == EXIT_TRANSPORT`) / `test_unexpected_failure_exits_1`; `src/capture/slack.py:353-354` does `sys.exit(main())`. Weak point: `_run_main` stubs `capture()` — a failure raised *inside* `capture()` that is not wrapped in `CaptureFailure` is covered separately at :168 (`test_a_mid_run_failure_is_classified_too`). OK. |
| outlook | **PINNED** | `tests/test_capture_outlook_resilience.py::TestMainExitCodes::test_transport_exits_4_and_does_not_nag_for_credentials` (`assert code == EXIT_TRANSPORT and sent == []`), `test_auth_exits_3_and_notifies_once`, `test_unexpected_error_exits_1`; `src/capture/outlook.py:330-331` `sys.exit(main())`. |
| codex | **PINNED** | `tests/test_capture_codex_cli.py::test_cli_returns_failure_when_any_task_fails` (`assert main([], now=NOW, capture=capture) == 1`); shell wrapper `tests/test_codex_capture_job.py::test_capture_job_fails_loudly_when_codex_home_is_missing` (`assert result.returncode != 0`). `scripts/capture_codex.py:59` `return 1 if report.failed else 0`. |
| browser | **UNPINNED — and the property is false** | `src/capture/browser.py:347-359`: `main()` returns `None`, `__main__` calls `main()` bare — no `sys.exit`. `ingest_batch` failures land in `stats["failed"]` and are only logged (:344). An unhandled exception exits 1 by Python default, but a run with `failed == N` exits 0. No test in `tests/test_capture_browser.py` mentions `main`/exit (grep). |
| youtube | **UNPINNED — false** | `src/capture/youtube.py:192-208` swallow fetch/ingest exceptions into `stats["failed"]`; `main()` :213-223 discards `capture()`'s return, `__main__` :226 bare `main()`. Every degraded run exits 0. No test references `main`/exit (`grep -n main tests/test_capture_youtube.py` → 0 hits). |
| crawlee_ingest | **UNPINNED — false** | `scripts/crawlee_ingest.py:49-50` exits 1 only when PostgreSQL is unreachable; per-URL `ingest_content` failures (:71-73) are counted and logged, then `main()` returns normally → exit 0. No test file references `crawlee_ingest` (grep). |
| ingest_staged.sh | **UNPINNED — false by design** | `scripts/jobs/ingest_staged.sh:9` `set -uo pipefail` (no `-e`); :37 `CHAT_OUT=$(...)` ignores the Python exit code; :48-52 logs "Crawlee ingest FAILED" but continues; :74 unconditional `exit 0`. `job_wrapper.sh:38-40` therefore never notifies for this job. Not referenced by any test. |
| scrape_bookmarks | **UNPINNED — false** | `scripts/scrape_bookmarks.py:94-96` returns `False` on cookie-fetch failure; `main()` :158-159 prints "N failed" and returns; `__main__` :165 bare. Exit 0 with all-failed. Only appears in tests as a *stubbed module* (`test_capture_browser.py:147,163`). |
| qd_sync.sh | **PARTIALLY TRUE, UNPINNED** | `scripts/jobs/qd_sync.sh:5` has `set -euo pipefail`, so any of the six Python scripts failing aborts non-zero — but the caveat is that step 1 failing skips steps 2-6 silently (no per-step logging). Not referenced by any test. |

- Fix: (a) `browser.main`/`youtube.main`/`crawlee_ingest.main`/`scrape_bookmarks.main` return `1 if stats["failed"] else 0` and `sys.exit(main())`; (b) `ingest_staged.sh`: capture the Python exit code (`set +e; ...; rc=$?`) and `exit $((rc_chat || rc_crawlee))`; (c) pin each via a `_run_main`-style test like slack/outlook, plus a bash-level `subprocess.run` test like `test_codex_capture_job.py:90-107` for the two shell jobs.


### (4) Every `source_url` builder rejects empty components — mostly **UNPINNED** (S2)
| Builder | Rejects empty? | Pinned? |
|---|---|---|
| `outlook.mail_source_url` (`src/capture/outlook.py:112-116`) | **No.** Missing `receivedDateTime`/`sentDateTime` → `""[:19]` → `outlook://mail//<hash>`; missing subject+sender → `_hash('', '')`, a constant — all such mails collide on one URL and dedup silently drops them. | `tests/test_capture_outlook.py:61-65` only asserts stability across Graph ids and a prefix. No empty-input test. |
| `outlook.event_source_url` (:149-151) | **No.** Missing `start` → `outlook://event//<hash>`. | `test_capture_outlook.py:101` asserts prefix only. |
| `slack.day_source_url` (`src/capture/slack.py:87-88`) | **No.** `conversation_id=""` → `slack:///2026-…`. | No direct test (`grep day_source_url tests/` → 0 hits). |
| `browser.day_source_url` (:170-171) | n/a — takes a `date`, so cannot be empty. Fine. | Untested directly, but type-safe. |
| youtube (`src/capture/youtube.py:86`) | **No.** `f"https://www.youtube.com/watch?v={vid}"` with `vid` from the playlist entry; empty `vid` → `watch?v=`. | No test. |
| chat staging (`scripts/batch_ingest_staged.py:63-65`) | **No.** `source_id = f.stem.replace("cli_","").replace("ide_","")` — a file named `cli_.md` gives `chat://`. | No test file for `batch_ingest_staged` (grep). |
| codex (`src/capture/sources/codex.py:479` → `AgentTaskRef.__post_init__` `src/capture/agent_tasks.py:72-73`) | **Yes** — `raise ValueError("a Task reference needs native and source identities")`. But `task_id=""` gives `source_identity="codex://"` which is non-empty and passes the check. | `grep 'needs native and source identities' tests/` → 0 hits; `tests/test_codex.py:78-90` `pytest.raises(ValueError)` are for `CodexInvoker`, not the ref. **UNPINNED.** |
| distill (`scripts/distill_sessions.py:175`) | **No.** `f"distill://{source_url}#{i}"` where `source_url` is read from `memories.source_url`, which is nullable → `distill://None#0` for legacy rows. | No test for `distill_sessions` URL building (`tests/test_distill.py` is the dream-cycle distill stage; `grep distill:// tests/` → 0). |

- Fix: a shared `src/validation.py::require_nonempty(*parts)` used by each builder; a single parametrized test `test_source_url_builders_reject_empty` over all seven callables.


### (5) Every MCP tool argument passes through `src/validation.py` — **PARTIALLY TRUE; the gaps are UNPINNED** (S2)
Tool × argument matrix (`src/mcp_server.py`, validators from `src/validation.py:56-128`):

| Tool | Validated via validation.py | **Not validated** (raw pass-through) |
|---|---|---|
| `memory_create` (:60) | type→`one_of`, title/content→`require_text`, tags→`clean_tags`, metadata→`require_mapping` | `source_type`, `source_url`, `project` (only `normalize_project_tag`), `encoding_context` |
| `memory_search` (:121) | query→`require_text`, type/status→`optional_one_of`, limit→`clamp_limit`, since_days→`clamp_days` | `source_type`, `project` |
| `memory_read` (:214) | memory_id→`uuid_or_error` | — |
| `memory_update` (:240) | memory_id, title, content, status, type, tags | `summary` (no `require_text`; `""` passes the `is not None` filter and blanks the column) |
| `memory_relate` (:298) | source_id, target_id, relation_type | `note` |
| `memory_list` (:325) | type, status, limit, since_days | `source_type` |
| `memory_recent_activity` (:360) | **none** — inline `max(1, min(int(days), 90))` and `max(0, min(int(samples_per_source), 20))` (:370-372), duplicating `clamp_days` logic with a different range (90 vs 365) and raising raw `ValueError`/`TypeError` on `days="x"` | `days`, `samples_per_source` |
| `memory_graph` (:387) | memory_id→`uuid_or_error` | — |
| `memory_learn` (:411) | content, topics→`require_text` | `source` |
| `memory_brief` (:481) | window_days→`clamp_days` | `use_llm` (truthiness only) |

- Pinned side: `tests/test_mcp_server.py::TestMemoryCreateValidation::test_rejected_input_returns_error_and_writes_nothing` (:598-624, parametrized; decisive `assert _count() == before` and `_no_embedding()`), `TestMemorySearchValidation` (:647), `TestMemoryUpdateValidation` (:712), `TestMemoryRelateValidation` (:761), `TestMemoryListValidation` (:804), `TestMemoryLearnValidation` (:839), `TestMemoryBriefWindowClamp` (:854), `TestValidationHelpers` (:506).
- Unpinned: no test asserts `source_type` is drawn from a vocabulary (docstring at :75 lists one; `FILTERABLE_TYPES` exists for `type` but nothing analogous for source_type), no test for `memory_update(summary="")`, no test for `memory_recent_activity(days="x")` error shape (only `test_recent_activity_clamps_arguments` :458 for ints). There is no meta-test asserting "every `@mcp.tool` parameter is named in a validation call" — so a new tool can bypass validation silently.
- Fix: add `SOURCE_TYPES` vocabulary + `optional_one_of` for `source_type` in create/search/list; `require_text` on `summary`/`note`/`source` when not None; route `memory_recent_activity` through `clamp_days(..., hi=90)`; add an introspection test iterating `mcp._tool_manager._tools` and asserting each parameter appears in an allowlist of validated names.


### (6) GLOBAL invariant: no profile routes a corpus role to `kiro` or a Fable model — **UNPINNED, and contradicted by the committed config** (S1)
- There is no global test. The only assertions are per-profile: `tests/test_resolver.py:81-95::test_controlled_profile_avoids_kiro_and_fable` (`assert all(p[r].backend != "kiro" for r in VALID_ROLES)`; `assert not any("fable" in p[r].model …)`) and `:69-79::test_hybrid_profile_meters_only_the_astra_seats` (`assert not any("fable" …)` but **asserts** `all(p[r].backend == "kiro" for r in AGENT_ROLES if r not in metered)`).
- The committed `config/backends.toml` violates the rule in three of five profiles: `laptop` (default_profile, lines 37-43: all six agent roles on `kiro`), `hybrid` (:98-104: explorer/thinker/advocate/methodologist on `kiro`), `bedrock` (:79: `thinker … claude-fable-5-1`). `tests/test_resolver.py:64` **asserts the Fable Thinker** (`assert p["thinker"].model == "global.anthropic.claude-fable-5-1"`) and `:97-107::test_laptop_profile_reproduces_today` asserts all-Kiro. So the "rule" in the `controlled` comment (`config/backends.toml:106-116`) is a per-profile choice, not an invariant, and the tests lock in the opposite for other profiles.
- `_validate_profile` (`src/backends/resolver.py:71-105`) has no model-string or backend-denylist check at all; nor does `Resolver.__init__` (:203-213) or `load_profiles` (:139-168).
- Fix (decision needed, not just a test): either (a) delete `laptop`/`hybrid`/`bedrock` or mark them `allow_kiro = true`/`allow_preview_models = true` with a `_validate_profile` denylist (`FORBIDDEN_BACKENDS_FOR_CORPUS = {"kiro"}`, `FORBIDDEN_MODEL_SUBSTRINGS = ("fable",)`) enforced for every profile in `load_profiles`, plus one test `test_no_committed_profile_routes_corpus_to_kiro_or_fable` iterating `load_profiles()[0].items()`; or (b) document that the rule applies only to the nightly default and pin *that*: `assert os.environ default / launchd plist SECOND_BRAIN_PROFILE == "controlled"` (partially exists: `tests/test_codex_capture_job.py:87,121` pin `controlled` for the codex job only — nothing pins the dream-cycle plist).

#### Tests that would BREAK if `_validate_profile` enforced no-Kiro/no-Fable (called only from `default_resolver`, `resolver.py:233`)
Definitely break (they call `default_resolver` with an all-Kiro or Fable profile and expect success, or expect a *different* error):
1. `tests/test_resolver.py:172::TestDefaultResolver::test_default_resolver_uses_laptop` — `assert isinstance(r.invoker_for("skeptic"), KiroInvoker)` on the committed `laptop` profile.
2. `tests/test_resolver.py:213::test_unknown_backend_in_active_profile_rejected` — `_profile_rows()` puts explorer on `kiro`; the per-role loop hits explorer before the `kiroo` skeptic, so `match="unknown backend"` fails (unless the denylist check is placed after the loop).
3. `tests/test_resolver.py:354::TestSkimRole::test_skim_on_kiro_in_the_active_profile_is_rejected_at_load` — agent rows on `kiro`; expects `match="skim role must run on a tool-less…"`.
4. `tests/test_resolver.py:390::TestAdapterOptions::test_unknown_option_key_rejected_at_load` — `_profile_rows()` all-kiro; expects `match=r"option\(s\) \['temprature'\]"`.
5. `tests/test_resolver.py:398::test_options_on_a_backend_without_options_rejected_at_load` — same; expects `match="does not accept"`.
6. Nightly runtime: `scripts/jobs/dream_cycle_scheduled.sh` / any caller with `SECOND_BRAIN_PROFILE` unset → `laptop` → hard fail at load (not a test, but the same code path).

Order-dependent (pass only if the missing/unknown-role checks stay ahead of the denylist): `tests/test_resolver.py:188::test_incomplete_active_profile_rejected`, `:201::test_typoed_role_in_active_profile_rejected`, `:361::test_profile_without_skim_is_rejected_at_load` (all build kiro rows; expect role-set error messages).

Would break only if enforcement were also placed in `load_profiles` or `Resolver.__init__` (they bypass `_validate_profile` today):
- `tests/test_resolver.py:49` (`test_committed_config_parses_with_laptop_default`), `:55-67` (`test_bedrock_profile_is_direct_api_with_mixed_panel` — asserts Fable, `Resolver(p)`), `:69-79` (`hybrid`, `Resolver(p)`), `:97-107` (`test_laptop_profile_reproduces_today`), `TestResolver` :110-169 (all `_kiro()` profiles incl. `:157,:165` Fable Thinker), `:369` (`test_lazy_guard…` kiro skim mutation — expects ValueError anyway), `TestAdapterOptions:379,:407` (`load_profiles` with kiro rows), `:413,:417`.
- `tests/test_skim_orchestrator.py:83::_profile` (all-kiro agent roles, used by every test in that file via `_orch(profile=…)`).
- `tests/test_dream_cycle.py:2534::_bedrock_resolver` (Fable Thinker in a `Resolver({...})`).
- `tests/test_panel_replay.py:128::TestDirectApiOnly::test_agentic_cli_seat_is_refused` (constructs a kiro `RoleBackend` to test a *different* refusal; would still raise but with the wrong message → `match="not a Direct-API backend"` fails only if enforcement is in `RoleBackend.__post_init__`).
- `tests/test_dream_cycle_db.py:86-87` (kiro backend dicts stored as run provenance — DB-only, would not break).
- Not affected (no profile, adapter-level only): `tests/test_bedrock.py` Fable model ids (:92-457), `tests/test_cost.py` Fable pricing (:31-160), `tests/test_backends.py:57` (`assert_backend_supports_role("kiro","explorer")`).

### (17) `express.py` and `distill_sessions.py` route through the resolver — **UNPINNED; both bypass it** (S2)
- `src/express.py:480-483`: `if invoker is None: from src.agent_invoker import AgentInvoker; invoker = AgentInvoker()` — `AgentInvoker` is `KiroInvoker` (`src/agent_invoker.py:19-20` shim), constructed with **no model argument** and no profile lookup. `memory_brief(use_llm=True)` (`src/mcp_server.py:507`) therefore sends briefing material to Kiro's server-side "Auto" routing — the exact failure mode the `controlled` profile comment describes (`config/backends.toml:106-116`).
- `scripts/distill_sessions.py:150`: `invoker = KiroInvoker(model=DISTILL_MODEL)` with `DISTILL_MODEL` from env default `"claude-sonnet-5"` (:41). `distill_sessions` ingests *full chat transcripts* (the corpus) through Kiro regardless of `SECOND_BRAIN_PROFILE`.
- Tests **pin the bypass**: `tests/test_distill.py:149-155::TestDistillInvokerSettings::test_invokes_extraction_model_with_bounded_timeout` — `ctor.assert_called_once_with(model=ds.DISTILL_MODEL)` on a patched `ds.KiroInvoker`. `tests/test_express.py` has zero references to `resolver`/`Invoker` (grep) — `edit_briefing`'s default-invoker path is untested.
- Counter-example that shows the intended pattern: `tests/test_weekly_digest_backend.py:325` asserts `"KiroInvoker(" not in src` for `scripts/weekly_digest.py`. The same source-scan test does not exist for `express.py` or `distill_sessions.py`.
- Fix: add a `distill` role (or reuse `skim`-style Bedrock-only role) to profiles, resolve via `default_resolver().invoker_for(...)`; port the `weekly_digest` source-scan assertion to both modules; drop the `ctor.assert_called_once_with(model=…)` pin.


### (7) An invoker cannot be obtained/invoked without a `SpendTracker` — **UNPINNED; property is false** (S2)
- `Resolver.invoker_for` (`src/backends/resolver.py:221-233`) constructs and returns a bare adapter; `SpendTracker` is not referenced anywhere in `resolver.py` or `base.py` (grep). Metering is opt-in at each call site: the orchestrator's `_record_spend` (`orchestrator.py:181-195`, called at :741,:994,:1095,:1167), `weekly_digest.run` (:232), `skim/runner.py` (:103-109, tracker param), `panel_replay`, `skim_probe`.
- Unmetered call sites obtained from a resolver: `scripts/revise_probe.py:106` (`resolver.invoker_for("thinker")` then `.invoke` at :84 with no `tracker`/`record`). Unmetered call sites bypassing the resolver entirely: `scripts/distill_sessions.py:138,160`, `scripts/redistill_capped.py:141`, `src/express.py:484`.
- No test asserts "every `.invoke(` result flows into a `SpendTracker.record`". `tests/test_weekly_digest_backend.py:198-201` pins it for the digest only (`assert tracker.calls == 1`).
- Fix: make `Resolver.__init__` take a required `tracker` and return a metering wrapper (`MeteredInvoker(adapter, model, tracker)`) from `invoker_for`; then a `test_resolver_requires_tracker` (`pytest.raises(TypeError)`) closes the gap structurally. Add a source-scan test (like `test_weekly_digest_backend.py:325`) asserting no `scripts/*.py` calls `.invoke(` without a `tracker`.

### (8) `SpendTracker` counts zero-cost calls — **PINNED as the OPPOSITE** (S3, doc/contract inconsistency)
- `src/backends/cost.py:199-202`: `if cost == 0.0: return 0.0` **before** `self.calls += 1` — zero-cost calls are not counted.
- `tests/test_cost.py:172-177::TestSpendTracker::test_missing_usage_is_a_no_op`: `assert t.total_usd == 0.0 and t.calls == 0` — pins the *non*-counting. Docstring at :195-196 ("Backends that report no real usage (Kiro) contribute nothing") agrees. The abort message (:211) says "after N metered call(s)", consistent with not counting.
- Consequence: `dream_cycle_runs` stats derived from `tracker.calls` under-report an all-Kiro run as 0 calls (see (13)); `_budget_remaining` docstring (`orchestrator.py:198-201`) acknowledges "nothing is metered". If the requirement is that *all* calls be counted (for run-level telemetry), a separate `attempts` counter is needed — currently untested either way.
- Fix: add `self.attempts += 1` before the zero-cost early return, pin with `assert t.attempts == 2 and t.calls == 0`.


### (9) `memory_graph` returns the neighbour (not `target_id`) for inbound edges — **UNPINNED; defect confirmed** (S2)
- `src/db.py:289-299 get_relationships` returns `r.*` for both directions (`WHERE r.source_id = %s OR r.target_id = %s`) and joins the neighbour's `title`/`type`, but `src/mcp_server.py:402-405` emits `"target_id": str(r["target_id"])` — for an inbound edge (memory is `r.target_id`) that is the queried memory itself. The joined neighbour `title`/`type` are discarded.
- Only test: `tests/test_mcp_server.py:260-268::TestMemoryGraphMcp::test_graph_returns_relationships` — creates `a→b`, calls `memory_graph(a)`, asserts `b in {rel["target_id"] …}`. Outbound only; `memory_graph(b)` would return `target_id == b`, untested.
- Fix: emit `other_id = target_id if source_id == memory_id else source_id`, plus `direction`, `title`, `type`; add `test_graph_inbound_edge_points_at_neighbour` asserting `memory_graph(b)["relationships"][0]["target_id"] == a` (will fail today).

### (10) `get_relationships` filters `expired_at` — **UNPINNED; property is false** (S2)
- `src/db.py:293-298`: no `expired_at IS NULL` predicate. Expired edges (from (11)) still surface in `memory_graph` and every caller (`tests/test_pool_properties.py:145`, `test_search_properties.py:176` only list the function name). `tests/test_codex_capture.py:941` does filter `expired_at IS NULL` in its own raw SQL — showing the intent exists elsewhere.
- `grep -rn expired_at tests/*.py` → only `test_codex_capture.py:744,815,941` (raw SQL in a codex test); nothing on `get_relationships`.
- Fix: add `AND r.expired_at IS NULL` (or an `include_expired` flag); test: create rel, `UPDATE … SET expired_at=now()`, assert `get_relationships(a) == []`.

### (11) `expire_stale_relationships` has a `created_at` grace — **UNPINNED; property is false** (S1)
- `src/dream_cycle_db.py:835-862`: predicate is only `mr.expired_at IS NULL AND NOT EXISTS (memory with last_accessed_at > now()-N days)`. `last_accessed_at` is nullable with no default (`migrations/002_v2_columns.sql:32`) and `create_memory` (`src/db.py:87-`) does not set it. So a relationship created seconds ago between two never-read memories is expired on the next run's Step 9 (`orchestrator.py:565-570`) — and the exception there is swallowed at `debug` level, so a mass-expiry is silent.
- `grep -rn 'expire_stale\|expired_count' tests/*.py` → **0 hits**. Fully untested.
- Fix: add `AND mr.created_at < now() - make_interval(days => %s)` (and treat `last_accessed_at IS NULL` as "use created_at"); pin with a DB test: fresh rel + NULL access → `expire_stale_relationships(90) == 0`; backdate `created_at` → `== 1`.

### (12) No migration names a database literally — **UNPINNED; violated** (S2)
- `migrations/009_hnsw_recall.sql:27`: `ALTER DATABASE memory_bank SET hnsw.ef_search = 200;` (`migrate.sh:8-9` parametrizes `DB_NAME` but the SQL does not use it).
- Consequence for tests: `tests/conftest.py:81-100 _apply_migrations` runs every `[0-9]*.sql` against `memory_bank_test` (:105). Line 27 therefore (a) mutates the *production* `memory_bank` database's setting from the test suite, and (b) leaves `memory_bank_test` at default `ef_search=40` — so recall-sensitive search tests run with a different index parameter than production. If the test role lacked `ALTER DATABASE` on `memory_bank`, the resulting `InsufficientPrivilege` is not one of the two caught exceptions (:97-100) and would abort the session fixture.
- No test scans `migrations/*.sql` for `ALTER DATABASE`/`\c`/database names. `tests/test_migration.py` is about chat-format migration scripts, not SQL.
- Fix: replace with `ALTER DATABASE current_database()`-style via `DO $$ EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database()) $$;`; add `test_migrations_do_not_name_a_database` (regex `ALTER DATABASE\s+\w+` over `migrations/*.sql`).


### (13) `dream_cycle_runs` stats are non-zero after a spend-cap abort — **PINNED as FALSE; contradicts the job's own comment** (S2)
- `src/dream_cycle/orchestrator.py:325-333`: `except SpendCapExceeded: … dream_cycle_db.complete_run(run_id, stats=_ZERO_STATS, digest="")`. The partial-stats preservation at `:455-470` ("no zeroed-stats lie") applies only to the *evaluator-infra* abort path (RuntimeError after retries), not the spend cap, which propagates straight out of `_run_pipeline`.
- Pinned the wrong way: `tests/test_budget_order.py:228-240::test_hard_cap_stays_the_backstop_when_the_soft_stop_is_bypassed` — `assert db.complete_run.call_args.kwargs["stats"]["candidates_generated"] == 0   # _ZERO_STATS on abort`. Also `tests/test_dream_cycle.py:2707-2711` asserts `candidates_accepted == 0` after cap. Contrast `tests/test_dream_cycle.py:1290-1302` which pins *partial* stats for the infra-abort path.
- Inconsistency: `scripts/jobs/dream_cycle_scheduled.sh:22-25` says "Exceeding it aborts the rest of the panel — candidates already accepted are kept" — true for the memory rows (already persisted) but the run record says 0 generated/accepted, so `get_strategy_yields`/feedback (`dream_cycle_db.py:800-833`) under-count those runs. (Same file :16-17 still describes the nightly profile as `hybrid` while :31 exports `controlled`.)
- Fix: catch `SpendCapExceeded` inside `_run_pipeline`'s panel loop like the infra path, or carry `accepted/rejected` on the exception; flip the two asserts.

### (14) A concurrent-run lock exists — **UNPINNED; no lock exists** (S2)
- `grep -rn 'flock|pg_advisory|pidfile|already running|completed_at IS NULL' src/dream_cycle/orchestrator.py src/dream_cycle_db.py scripts/dream_cycle_run.py scripts/jobs/dream_cycle_scheduled.sh` → 0 hits. `should_run_briefing` (frequency cap) applies to `session_start` only (`orchestrator.py:301-303`). A manual `dream_cycle_run.py` overlapping the launchd run, or `codex_capture` and `dream_cycle` both writing candidates, is unguarded; two concurrent runs each pay the spend cap.
- No test. Fix: `pg_try_advisory_lock(hashtext('dream_cycle'))` in `create_run` (raise `RunInProgress`), test with two connections.

### (15) Evaluator verdict strings validated against {ACCEPT, REJECT} — **UNPINNED; no validation** (S2)
- `src/dream_cycle/orchestrator.py:1109`: `verdict=output.get("verdict", "REJECT")` — any string ("Accept", "ACCEPT.", "MAYBE", `None`→ missing key → REJECT default) is stored. `src/models.py:62-74 EvaluatorVerdict` has no `__post_init__`. `src/dream_cycle/consensus.py:26` counts `v.verdict == "ACCEPT"` exactly, so a lower-case `"accept"` silently becomes a REJECT vote — the very "fabricated verdict" the retry logic (`orchestrator.py:59-61`) is meant to prevent.
- `tests/test_consensus.py` validates *count* (`Expected 4 verdicts`, :104,:119,:412) never *values*; `tests/test_eval_properties.py`, `test_orchestrator_properties.py` draw verdicts only from `{"ACCEPT","REJECT"}` (grep for other literals → 0 hits).
- Fix: `one_of(output.get("verdict"), ("ACCEPT","REJECT"), "verdict")` with `.strip().upper()`, raising → retry path; test with `"accept"`, `"Accept "`, `"MAYBE"`, missing.


### (16) `generate_digest` reads `feedback_injected` AFTER it is written — **UNPINNED; property false (ordering bug), tests mask it** (S2)
- `src/dream_cycle/orchestrator.py:563` `digest = generate_digest(run_id, accepted, rejected)` precedes `:578-591` `complete_run(..., explorer_feedback_injected=feedback)` — the only writer of that column (`src/dream_cycle_db.py:46-79`). `digest.py:58-60` calls `was_feedback_injected(run_id)` → always NULL → `"No feedback was injected"` (`digest.py:100-103`) in every digest.
- Every digest test mocks the read: `tests/test_digest_properties.py:114,152,192` and `tests/test_dream_cycle.py:951,1011,…` set `was_feedback_injected.return_value = False`; `tests/test_dream_cycle_db.py:662-716,821` test the DB function in isolation. No test asserts call ordering or that a run with non-empty `feedback` produces the "Feedback from previous cycles" line. (Already diagnosed with live evidence in `04-express-briefing-feedback.md` §F5/line 116; this track adds: the pin is absent and the suite structurally cannot see it.)
- Fix: pass `feedback_injected=bool(feedback)` into `generate_digest`; test: orchestrator run with `build_feedback_injection` returning text → digest contains "Feedback from previous cycles".

### (18) `ingest_batch` dedups within a run — **UNPINNED; property false** (S3)
- `src/ingest.py:212-240`: `already` is loaded once (:222) and never updated after a successful `ingest_content`; two items with the same `source_url` in one `items` list are both ingested (both embed → two rows). Callers that pre-dedup (`browser.py:322-327` `seen` set for *reading* URLs) do so ad hoc; `slack`/`outlook` item lists rely on their own url construction.
- `grep -rn ingest_batch tests/` → only monkeypatched stubs (`test_capture_browser.py:142,159`, `test_capture_outlook_resilience.py:152`, `test_capture_slack_preflight.py:146`). The function itself has **no test**.
- Fix: `already.add(source_url)` after success; test with a duplicated tuple asserting `processed == 1, skipped == 1` and `ingest_content` called once.

### (19) Weekly digest exit codes 2/3/4 — **PINNED** (adequate)
- `scripts/weekly_digest.py:277-288` maps `DigestBackendError→2`, `SpendCapExceeded→3`, `TimeoutError→4`; `:291 sys.exit(main())`.
- `tests/test_weekly_digest_backend.py:173-178::test_main_exits_non_zero_on_refusal` — `assert wd.main() == 2`; `:211-219::test_main_exits_non_zero_on_cap` — `assert wd.main() == 3`; `:239-246::test_timeout_is_a_loud_non_zero_exit` — `assert wd.main() == 4`. Each also asserts `digest_env == []` (nothing stored). Weak spot: a generic `Exception` from `run()` is not mapped (propagates → Python exit 1 with traceback) and untested; acceptable.

### (20) `tests/fixtures/**` — inventory
| Path | Size | Contents (structure) | Tracked? | Load-bearing? |
|---|---|---|---|---|
| `tests/fixtures/codex/` (7 files) | 36 KB | 3 `*.json` task manifests (`fixture_kind: "capture_relevant_excerpt_from_real_codex_task"`, keys `id, created_at, updated_at, source, cwd, title, archived, agent_path, …`), 1 legacy-delegated manifest (`spawned_child`), 3 `*.jsonl` rollout logs (`session_meta`, turn events). Derived from **real** Codex sessions: `real_task.jsonl`/`real_correction_task.jsonl` contain the operator's home paths (7 occurrences of local usernames) and real `cwd`s. | yes (git, commit b6d4484) | **Yes** — `tests/test_codex_capture.py:30-44,986-989` loads all seven as module-level constants; the whole 1,300-line file depends on them. |
| `tests/fixtures/golden_run/` (35 files) | 452 KB (377 KB JSON) | One `explorer.json` + 6 `thinker_*.json` + 28 `evaluator_<role>_<title>.json`, each `{"inputs": {"system_prompt", "user_message"}, "output": …}` — full prompts and raw model outputs from one real `user_triggered` run captured by `scripts/run_golden_capture.py:27`. Explorer system prompt embeds live corpus stats ("74013 memories spanning March 2026 to March 2026" — a `date_range` min==max oddity worth checking in `get_memory_stats`). | **No** — gitignored (`.gitignore:9-10` "contain full prompts and real memory content") | **No** — `grep -rn golden_run tests/ src/ scripts/` → only the capture script. Zero tests read them. They are dead weight on this machine and a personal-data footprint outside `logs/`. |
- Note: `tests/fixtures/codex/*.jsonl` are committed real-session excerpts with local paths; if the repo is ever shared, scrub `cwd`/`originator` fields (S3, hygiene).

## Could not verify
- No tests were executed (rule). All PINNED/UNPINNED verdicts are from static reading of `tests/` and `src/`; a test may exist under a name my greps missed (I searched by function name, exit-code literals, and key strings — not by reading every file).
- Whether the test Postgres role owns `memory_bank` (relevant to (12): `ALTER DATABASE memory_bank` from the test fixture would either mutate production settings or abort the session).
- `~/Library/LaunchAgents/com.second-brain.dream-cycle.plist` exists and does **not** set `SECOND_BRAIN_PROFILE` (grep), so the shell default `controlled` applies at night — but the Python-level default (`config/backends.toml` `default_profile = "laptop"`, all-Kiro) still governs any direct `python scripts/dream_cycle_run.py` invocation; nothing pins the plist/shell default (`tests/test_codex_capture_job.py:121` pins only the codex plist).
- `tests/fixtures/golden_run` personal-content depth: I only read keys and the first 120 chars of one system prompt, per scope.
- Whether `EvaluatorVerdict` outputs from Bedrock structured-output backends are already constrained to ACCEPT/REJECT by a JSON schema (would mitigate (15) for `bedrock`/`claude_code` but not `kiro`).

