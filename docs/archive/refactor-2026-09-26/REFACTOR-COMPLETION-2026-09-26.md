# Full-plan execution ledger

Updated 2026-09-26. Branch `codex/refactor-safety-routing`, based on original `1071a81` with foundation `bf854cd`. The user authorized the entire revised plan. This ledger separates source implementation, isolated verification, applied operations and evidence of human benefit. The repaired branch has not replaced the installed runtime or been pushed to GitHub.

## Implementation by original plan item

| Plan item | Implemented result | Qualification and remaining boundary |
|---|---|---|
| 1.1–1.6 routing and accounting | Controlled corpus roles, metered editor/distill/Codex semantics, billed failures, finite usage, denied Kiro/Fable overrides | Synthetic Claude probes exposed and fixed canonical model naming and exact read-only tool permissions; other live roles need qualification |
| 1.7–1.11 ingestion and corpus repair | Hard chunk bounds, no silent embedding truncation, atomic source/child replacement, identity-aware dedup, cached resumable rechunk and UUID-project repair | Real restored corpus rehearsal passed; no live corpus rewrite or paid embedding |
| 1.12 relationship lifecycle | Grace period, permanent provenance, expiry repair, correct incoming neighbors and symmetric dedup | Migration applied on restored clone; directed reverse edges retained |
| 1.13 and 1.19 migrations/test isolation | Transactional migration ledger/advisory lock; disposable socket-only database and precollection OS/provider guards | Actual rollback, concurrency and isolation regressions; installed database never a test target |
| 1.14 data layer/schema | Threaded pool, scalar projections, transactional repository writers, vocabulary/cost/index migration, role/index review manifest | Restored snapshot accepted migration 016; runtime-role separation still needs deployment |
| 1.15 MCP | Validated/clamped inputs, topic/source vocabulary, generic operational errors, correct graph neighbors | Boundary regressions and native read-only list call passed |
| 1.16–1.18 discovery integrity | Separate grounding auditor, binary votes without audit leakage, deterministic target authorization, ordered locks, atomic candidate/memory/edge ledger, truthful partial results/digest and run lock | Native scripted-provider pipeline and real SQL rollback/reuse tests; live usefulness unproven |
| 1.20 recovery/security/dependencies | Encrypted source/config/DB/KB bundles, owner-executable permission preservation, safe restore/cancellation, recovery key copy, AWS residue permissions, locked dependency updates, sanitized fixtures | Actual v2 full restore passed. Local key copy verified. Independent cloud key custody, static-key replacement and legacy plaintext retirement remain open |
| 2.1–2.5 truthful operation | Shared wrapper, bounded batch jobs, service health/heartbeats, group cancellation, durable status, output-silence checks, distinct required-probe unknown exit, capture/skim failure taxonomy | Deliberately failed/silent/interrupted channel tests passed; installed schedules unchanged |
| 3 code retirement | Unreachable backend seams, old staging/importers/canary, graph writer, dead helpers and deferred fields removed or archived | Codex adapter/Opus pricing and newly used arrival helpers retained deliberately. Graph tables and ignored artifacts have private inventories; 1,512 ignored files were separately encrypted and restored with matching hashes, without original deletion |
| 4a and 4e DB/vocabulary | Canonical pool/columns/serialization/repositories, one briefing transaction and source/type/class vocabulary | Real DB round trips; historical live values inventoried before constraints |
| 4b capture | Shared preflight/window/render/ingest/result framework; native Kiro IDE session parser; direct chats; bounded Codex turn assembly; saved bookmark discovery | Synthetic source identity, append/refresh, DST, alias and failure cases; live native IDE completeness remains unverified |
| 4c backends | Shared metrics, usage normalization, parser, pricing and private temporary-file mechanics | Returned-model checking and failed-call accounting retained; no unknown-cost attempt is treated as free |
| 4d tests | Shared invoker/connection/strategies, real pipeline/rollback tests replacing brittle SQL mocks, meaningful rerank and non-vacuous persistence assertions | The 20,000-line target is not met. Substantive integrity/isolation coverage is retained; line-count reduction is not a correctness measure |
| 5 decomposition | Discovery stages/agents/budget/result/repositories, pure digest and retrieval components, supported grounding replay API | 36-query retrieval fixture was run before and after retrieval extraction |
| 6.1–6.6 documentation/specs | Canonical component guides, generated inventory, owned hazards, archived historical docs/specs and 12 maintained spec families | Original dirty documents preserved. Current authority terminates on source/tests, with live state kept in dated evidence |
| 7 guardrails | Routing, usage, ingestion, relationships, mutations, migrations, liveness, fixture privacy and counted documentation regressions | Full integration result is recorded below; synthetic success is not human benefit |
| Product evidence | Frozen-source retrieval-versus-discovery runner, isolated worker/timeout, blind packets, exact source/config manifest and explicit human ratings | Eight-record Kiro/AI-DLC trial prepared privately and authorization requested. No new private model transfer or human outcome yet |

## Verification evidence

**Final isolated suite: 1,724 passed, one Starlette/httpx deprecation warning, 96.68 seconds.** Evidence: `/private/tmp/sbtest-xhkzov6p`; the cluster stopped. Earlier overlapping targeted totals must not be added together. All tests run through `scripts/test_isolated.py` in the worktree's locked virtual environment. The first integrated run exposed obsolete test boundaries and an unscoped qualification provider stub; both were repaired and the complete suite then passed.

Independent agents reviewed data integrity, discovery/model routing, capture/operations and the backup/trial boundaries. Review found and repaired incomplete source trees, poisoned embedding caches, status drift, reversed directed edges, private-file modes, process-tree cancellation, ambient CLI context, model fallback and missing exact MCP permissions.

## Actual recovery and corpus rehearsal

The selected v2 encrypted bundle includes the original database, repository source/configuration/documentation and 1,366 configured knowledge-source files. Its SHA-256 is `9de0118248b4e593ef096e2408cfe5f7b0d0df267d34065484f5779ee00c5934`. Content and 0600/0700 permission checks passed. Using the OneDrive key copy, the full dump restored into a new no-TCP cluster, migration 016 applied, 2,692 metadata repairs and 70 proven duplicate-source merges completed, and a fresh post-repair dedup plan was empty. The clone and plaintext were removed after shutdown. No provider calls or live database mutations occurred.

Post-dedup rechunk planning identified 43,320 operations and up to 131,020 embedding calls for 348,375,000 input characters. At the explicit planning rate of $0.02 per million tokens, four characters per token gives about $1.74; charging every planned call at the model's maximum 8,192 input tokens gives about $21.47 before retries. These are scenarios, not a guaranteed bill or complete cost ceiling; cache reuse may reduce calls. Confirm regional pricing and exact live row versions before any paid application. [AWS Titan V2 rate](https://aws.amazon.com/blogs/machine-learning/get-started-with-amazon-titan-text-embeddings-v2-a-new-state-of-the-art-embeddings-model-on-amazon-bedrock/), [model limits](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html), [current pricing](https://aws.amazon.com/bedrock/pricing/).

Private receipts and manifests live in the workspace's `refactor-review-2026-09-26/`, outside this source repository. The rehearsal manifests identify the disposable clone and must not be applied to the live database. Earlier draft manifests are superseded. Metadata normalization must precede final dedup planning because project identity can change.

## Provider and effectiveness boundary

The installed ASBX Claude wrapper silently replaced a raw Bedrock Sonnet identifier with Opus 5.5 in the first synthetic probe. Passing canonical `claude-sonnet-5` selected Sonnet correctly in two probes. The adapter now maps recognized identifiers for the CLI argument while preserving requested provenance and checking served-model metadata.

An actual native call then exposed missing read-tool permissions. Exact grants for the five read-only endpoints fixed it. Claude Sonnet on Bedrock successfully called `memory_list(limit=1)` against a verified empty isolated database, with built-in tools disabled and PostgreSQL read-only. The wrapper's AWS account identity remains unverified; an AWS_PROFILE environment variable alone does not prove it. Production embedding-based search and every configured reasoning role have not been live-qualified by that list test.

Known reported synthetic costs total $0.0148917. One earlier harness attempt lost its response before accounting and therefore has unknown spend; that attempt is retained separately. These are client-reported estimates, not invoice reconciliation. No private records were sent during these probes.

The proposed product trial freezes eight intact records (33,888 characters) from current Kiro/adoption/PLI work and historical workshop decisions/artifacts. It does not contain demonstrated customer productivity outcomes. Manifest `55a3d75d971cc83ce9b6de611f6ed5b700a768ee629d5c15a0807c6ffcadd17c` names Bedrock destinations, a $3 reasoning stop threshold that may overshoot, separate embedding cost and omitted native acquisition/distillation. Human ratings remain unknown. Earlier authorization for 35 Codex records does not cover it.

## Remaining operational work

1. Verify the recovery key through OneDrive from another device/session. Its local path is `/Users/kerrfir/Library/CloudStorage/OneDrive-amazon.com/Second Brain Recovery/backup-key`; byte equality and local 0600 permissions are verified, cloud key sync is not.
2. Replace or stop the installed legacy backup path before retiring historical plaintext cloud copies. The old job remains loaded and still contains plaintext KB/config sync. Remote inventory observed 1,366 KB files and 432 config files. New encryption does not retroactively protect them.
3. Provision/qualify an unattended temporary-credential provider, then revoke the identified old IAM key. The static job profile remains active; five AWS residue files were restricted to 0600. SSO validity alone is not unattended readiness.
4. Qualify and apply a separately targeted runtime release: preserve original dirty files, stop affected writers, apply schema/runtime-role changes, normalize metadata, regenerate live dedup/rechunk manifests, price/authorize embedding, then restore intended job enablement. Neither an isolated branch nor a passing suite installs this release.
5. The core backup excludes backup/log directories. A separate encrypted archive now preserves all 1,512 inventoried ignored files (73,993,646 source bytes), including the mixed real/test metrics file; it was decrypted using the recovery key and every file hash verified. Original deletion and selective test-row cleanup remain unapplied. Retire graph tables only with the recorded recovery and dependency checks and an explicit migration. Installed plist copies and orphaned Kiro agents have metadata-only inventories; no unreviewed relinking/deletion occurred.
6. Complete the approved private discovery trial and obtain human grounding/novelty/usefulness/uptake/outcome judgments. Source tests and panel agreement do not demonstrate improved work.

All 128 originally modified/untracked source files still match their saved hashes; installed HEAD remains `1071a81`. The original, historical GitHub comparison and current refactor remain distinct implementations/artifacts.
