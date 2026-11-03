# Validation, review findings, and improvement — original implementation

Source-reviewed 2026-09-10. This is the authoritative distinction between component correctness, operating state and effectiveness. [Start here](START-HERE.md) supplies purpose and version identity.

## What the existing tests establish

The repository contains unit, property, integration, parser and mocked orchestration tests. Read the fixture/call boundaries before describing any test named “E2E” as a full production run. Embeddings and LLM responses are often mocked; real provider access and source freshness are separate gates. This documentation-only review did **not** run the original full suite or modify production data.

| Test family | Contracts checked | Still outside that evidence |
|---|---|---|
| `test_db`, `test_pool_properties`, migrations | CRUD/relationships/pool/schema behavior | Current backup restore, production schema parity, concurrent writer safety |
| `test_cli_chat`, `test_ide_chat`, ingest/migration/QD/YouTube tests | Parsing/mapping/filtering/capture functions | All real versions/surfaces, complete evolving source history |
| `test_distill`, digest tests | Prompt/item/contradiction/digest behavior | Fresh exact-source support and user/assistant authority |
| Search/rerank/question/project/depth tests | Filtering/ranking/formula/normalization | Current recall across source populations and unobserved missed connections |
| Dream-cycle/orchestrator/consensus/prompt tests | Control flow, vote counting, failures, storage | Reviewer truthfulness/independence, full-source evidence verification |
| Backend/resolver/Claude/Codex/MCP-probe tests | Commands, parsing, capabilities, startup signal handling | Current model entitlement, credentials, CLI/service compatibility |
| Express/MCP/API tests | Tool/HTTP/render/gate/preference contracts | Actual seen/sent exact-version content and human benefit |

`tests/conftest.py` creates a configurable test database and deletes memory rows during cleanup. It lacks a guard preventing a production `TEST_DB_NAME`. Migration 009 also targets `memory_bank` explicitly. Therefore run DB tests in a **separate disposable PostgreSQL cluster**, not merely a renamed DB on the live cluster. Inspect env/import ordering and every migration first.

## Existing metrics and their limits

Retrieval evaluation scripts measure golden/curated rank, hit/MRR-style metrics, cold/warm reranking, ablation and consolidation effects. `recall_check.py` compares HNSW output to exact vector search; that measures index recall, not relevance or improved work. Golden queries can be derived from memories, so keep independent held-out questions and avoid training/tuning on the same examples. Evaluation code has alternate scoring functions that must remain consistent with production weights.

Dream-cycle Tier 1 metrics count acceptance, disagreement and strategy diversity. `cost_efficiency` estimates invocation count from candidate totals; it is not measured monetary efficiency. Tier 2 `insight_citation_rate` is actually average access count, and `user_rejection_rate` divides by accepted candidates without establishing the user saw or reviewed them. Missing feedback is unknown, **not approval**. These legacy metric names remain in code and must be interpreted accordingly.

The 35-record controlled comparison generated fresh ideas through multiple role calls but supplied frozen evidence instead of native retrieval and bypassed capture/distillation/live persistence. It cannot certify either complete implementation. Existing unit-test totals, model calls, and accepted-item counts are not effectiveness scores.

## A complete end-to-end protocol

Use the original implementation first, then repeat in the experimental implementation with matching frozen raw inputs and current-work brief. Do not silently substitute one version's repaired functions into the other's path.

1. **Freeze the task and evidence.** Choose the user's real Kiro/AI-DLC adoption planning deliverable and a bounded set of complete raw sources from multiple surfaces/projects. Record source version/hash/time, speaker/author roles, user goal/constraints, excluded sources, and what the user already knows. A folder name alone is not proof of an independent project outcome.
2. **Preflight the actual installation.** Record checkout+diff, schema, config, providers/models/effort, authentication and actual MCP reachability. Probe synthetic content before approved private content. Keep live DB, source watermarks, schedules, email and backup destinations out of the fixture.
3. **Run capture and ingestion.** Use real parsers and writers for the chosen sources. Assert normalized content and all expected chunks survive; distinguish exclusions, empty sources and failures. Re-run/append sources and inject a mid-write failure to measure completeness/retry behavior.
4. **Run real distillation.** Retain prompts and outputs. Check decision authority, source attribution, quotations, uncertainty and coverage. Existing QD distilled records alone cannot demonstrate this stage.
5. **Run native retrieval and discovery.** Keep every query/tool result, slice, candidate, four reviewer calls, vote, dedup decision and persisted operation. Record unavailable stages/fallbacks as failures or omissions, not plausible-looking substitutes.
6. **Deliver through the actual chosen surface.** A local test briefing can avoid sending mail. Save exactly what was shown, its evidence/caveats, and stable run/item/version identifiers in the external evaluation ledger because original lacks delivery receipts.
7. **Blindly judge and compare.** Compare ordinary workflow/simple source search with each implementation's output. Judge material claim support, cross-project relevance, transfer rationale, necessary adaptation, concrete next action, novelty to the user and usefulness. Human novelty/usefulness ratings must come from the user.
8. **Apply a useful discovery.** Record a concrete planning change or workshop experiment and its before/after evidence: e.g. time saved, rework avoided, clearer decision, or delivery/readiness improvement. Control for other help and avoid equating a draft suggestion with a measured result.
9. **Close the feedback loop.** Record the user's response through mechanisms the version actually supports; inspect what the next run retrieves, suppresses or revises. If an outcome/correction mechanism is absent, report that missing stage explicitly.

A small runner and an external evaluation manifest can orchestrate this, but a runner alone cannot supply the user's judgment. For each stage store inputs/outputs/hashes, exact code/provider identity, start/end/exit, side effects, missingness, and the link to the next stage. Every displayed claim should trace to raw evidence. Passing gates with synthetic data precedes a real-world benefit claim.

## Prioritized code-review findings

These findings describe **unrepaired original behavior**. Changes belong in a separately reviewed code patch; this pass modifies documentation only.

| Priority | Finding and source evidence | Practical impact / next step |
|---|---|---|
| High | `ingest.py:148` onward commits parent and chunks separately; URL-existence skips in capture scripts | Partial or resumed sources can remain incomplete. Add atomic source revisions, completion receipts and append/change tests |
| High | `migrate_quick_desktop.py:293` onward advances max IDs before successful storage | Failed imports and changed older rows may not retry. Advance checkpoints only over committed complete work; version source state |
| High | `distill_sessions.py:43` / `valid_items` asks for structured claims but validates neither quotes nor speaker authority | Assistant proposals can become durable decisions. Preserve roles/attribution and deterministic evidence checks |
| High | `dream_cycle/orchestrator.py:590` supplies only source IDs to reviewers | A vote can be confident without evidence access. Supply complete bounded source packets and exact-support validation |
| High | `scripts/jobs/backup.sh` does not gate dump/encryption failures before removal/upload; verify only lists archive | A green job can lack a restorable current backup. Fail truthfully and perform isolated restore checks |
| High | `migrations/migrate.sh` hardcodes port 5432 and lacks `ON_ERROR_STOP`; migration 009 targets `memory_bank`; test fixture has no production guard | Isolation by environment name alone is unsafe. Add explicit target checks and disposable-cluster test runner |
| Medium | Distill/digest/Express still use Kiro alias; resolver covers six dream roles only | Changing the mini/profile backend leaves other stages dependent on Kiro. Unify selection with explicit per-workload provider mapping |
| Medium | `orchestrator.py:283` dedups all accepted operations before UPDATE/SUPERSEDE; `storage.py` commits changes separately | Update can be skipped as duplicate, prior content overwritten or operation partly applied. Separate operation semantics, preserve versions and transact changes |
| Medium | `express.py:160` truncates detail 600chars; empty editor output falls back; push gate and composition differ | Lost qualifications, unwanted fallback, trigger not delivered. Preserve full detail, honor empty selection, gate final eligible items |
| Medium | `dream_cycle_db.py:613` labels average access as citation and accepted candidates as feedback denominator | Metrics overstate evidence of user engagement. Add exact delivery/review/outcome ledger and truthful names |
| Medium | `mcp_server.py:239` graph returns stored target for both incoming/outgoing relationships | Incoming edge can point back to the requested memory. Return both endpoints/direction or correct neighbor ID |
| Medium | `src/db.py` uses `SimpleConnectionPool`; concurrent ingestion workers share it | Pool is not the thread-safe class. Review concurrency behavior before bulk parallel runs |
| Medium | Original Kiro tools permit wildcard/trust-all; capture plist contains a committed token | Prompt intent is not capability isolation. Constrain discovery tools; rotate/remove the token in a separate reviewed operational change |
| Medium | `search.py` uses insertion chronology, project filters and embedding-only lexical candidates | Older imported records can look recent; null embeddings invisible; scoped search hides other-project evidence. Use event-time/revision metadata and evaluate intended cross-project recall |
| Medium | Some import/Thinker failures log and continue with success exits; synthesis wrapper ignores mail failure | Service health can mask missing stages. Track explicit stage outcomes and reconcile produced artifacts |

The experimental repair contains changes addressing some of these themes; it must be reviewed as its own version. Do not infer those fixes are present or deployed in this original checkout.

## Improvement sequence

First restore observability and trustworthy source completeness. Next ensure every candidate/reviewer/delivered output shares the necessary evidence and preserves uncertainty. Then close exact-version delivery/feedback and test whether useful conditional ideas are filtered out. Only then tune role diversity, retrieval strategies, graph enrichment, or model choices using the same held-out tasks and user outcomes. A Bayesian confidence loop is a design decision still requiring a defined belief/evidence/update/outcome model, not a rename of the vote threshold.

The highest-value architectural idea from this review is a **single traceable chain from raw source revision to observed work outcome**. It joins capture completeness, evidence review and usefulness measurement, instead of treating each as a separate success metric.

## Documentation maintenance

The authoritative set is START-HERE, ARCHITECTURE, CAPTURE-COMPONENTS, MODEL-BACKENDS, OPERATIONS, DISASTER-RECOVERY, INTERFACES, VALIDATION, plus the [spec index](SPEC-INDEX.md). Update one owner per contract; do not repeat full configuration, schema, schedule or provider tables in tutorials.

A change must name code revision, affected input/output/state contract, tests and limitations. Runtime facts need host, timestamp and evidence; keep private counts/credentials/payloads in the local operational inventory. “Implemented”, “configured”, “loaded”, “last successful”, “delivered”, and “useful” are separate statuses.

Existing old paths remain short redirects. [The dated archive](archive/2026-09-10/README-ARCHIVE.md) preserves all 41 original docs/readmes with private example/account identifiers generalized and old link destinations retained as literal historical text. Archives retain historical decisions and probes; their scientific claims, prices, operational instructions and test counts are not revalidated current facts. Existing `.kiro/specs` are design records indexed separately, not current deployment authority.

For a future compaction, resume from START-HERE and the current local runtime inventory. Do not resume from June handoff/plan instructions, auto-run old cleanup/push commands, or erase unfinished source work.
