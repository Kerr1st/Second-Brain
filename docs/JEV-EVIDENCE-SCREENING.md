# Jev evidence screening: investigation and execution plan

Prepared September 26, 2026. Implementation base: original Second Brain refactor `74ba02df87b04ca3f21cd3781949b023afb856d5`. Work branch: `codex/jev-evidence-screening`. This plan covers a working, evaluated optional component. Production activation is a separate evidence gate, not an automatic consequence of successful connectivity.

Execution is complete. See the [measured results and decision](JEV-EVIDENCE-SCREENING-RESULTS-2026-09-26.md): 504 successful judgments, about two cents, a failed injection-flagging gate, and automatic runtime screening kept off.

## What the investigation found

Second Brain is designed to capture work context, retrieve evidence, discover useful cross-project connections, review their grounding, and deliver suggestions that improve a current decision. Retrieval is one stage in that loop. Jev produces typed decisions, so its strongest fit is narrow evidence judgments between retrieval and generative discovery.

The installed checkout is `/Users/kerrfir/second-brain` at `1071a81`. It contains extensive existing uncommitted work and is the path used by the installed launchd jobs. A separate refactor at `74ba02d` passed 1,724 isolated tests according to its completion ledger and was clean when inspected. It repairs ingestion, source identity, retrieval, routing, accounting and operations. It has not replaced the installed application. The experimental GitHub implementation is another history and is not this integration's base.

Current retrieval combines PostgreSQL full-text and pgvector rankings with reciprocal-rank fusion, deduplicates, and then applies deterministic utility reranking. The reranker uses lexical overlap, source type, recency, access, depth, project and supersession signals. This provides an explicit baseline but does not directly distinguish a meaningful answer from a lexical near-match or identify a transferable mechanism.

Important code boundaries:

- `src/search.py:hybrid_search` truncates candidates before `rerank`; a later scorer cannot recover excluded evidence.
- `src/retrieval.py:build_filters` can remove other named projects. Cross-project comparisons therefore use a common candidate pool without an implicit project filter.
- `src/search.py:rerank` mutates its input and uses the current time. Comparisons must deep-copy candidates and freeze the clock.
- Public `memory_search` increments access counts and renders 500-character previews. It must not be used to collect an unbiased experiment. The read-only interface avoids reinforcement, but still previews content.
- `src/backends/base.py` protects existing corpus routes. Jev is a separate explicit client, not a replacement for the controlled Bedrock roles, Explorer, embeddings or the reviewers.
- Existing retrieval tests cover 36 synthetic concept queries through the real SQL/vector path. They establish regression behavior, not semantic screening accuracy or human usefulness.

Jev connectivity is already verified through OpenRouter with a synthetic Choice/Score/Noul request. The served model was `typesafe/jev-1.13-20260917`; the reported cost was $0.000020916 for 498 input tokens. The dedicated account key has a $1 total limit with no reset and is stored in macOS Keychain.

## Chosen design

Implement `src/evidence_screening.py` as an explicit optional component. It receives a query and bounded full passage snapshots and returns five independent probabilities: relevance, direct evidence, conflicting evidence, transferable mechanism, and instruction injection. Titles and passage text are untrusted data. Source identity, source hash, baseline order, rubric version, served model and actual usage remain inspectable.

Shadow mode returns every original candidate in its original order. It preserves `rerank_score`, does not mutate its inputs, and treats errors and unscored passages as unknown. It never changes memory contents, reinforcement counts, database schema, schedules or existing provider profiles. The pure reranker gains no hidden network call.

Use a fixed OpenRouter Decisions endpoint and a pinned Jev model. Reject oversized input rather than silently cutting off trailing evidence. Enforce finite probability/usage validation, bounded requests and responses, timeout, no redirects and no automatic retry. Reserve a conservative cost estimate before each call, reconcile actual reported usage, and stop after unknown spend. Provider-side key limits remain a separate backstop, not proof of a strict invoice ceiling.

## Execution sequence

1. Preserve the installed checkout and existing refactor. Create a separate worktree from the exact refactor commit. The app's managed-worktree tool cannot resolve that commit from the Jev wrapper repository, so use the actual Second Brain repository's Git worktree support after recording that failure.
2. Build the client, ordered annotation API and failure/budget tests.
3. Build a versioned synthetic-only corpus: 84 query bundles, six passages each, covering direct evidence, contradiction, cross-project transfer, lexical near-misses, missing information, long-tail evidence and injection. These are four query variants for each of 21 scenario families, not 84 independent scenarios. Separate 28 calibration bundles (seven families) from 56 holdout bundles (14 families). Record all source/label/configuration hashes before paid evaluation.
4. Compare the actual deterministic reranker with a proposed semantic order over the same frozen pool. The semantic score is `max(evidence, conflict, transfer, 0.5 * relevance)`. Injection is a separate review flag, not a reason to delete evidence. Freeze any threshold selection on calibration data before looking at holdout results.
5. Run meaningful offline tests through `scripts/test_isolated.py`, which creates a disposable database and denies provider/network access. Keep live synthetic calls outside that runner. Use the qualified refactor's locked Python environment without changing its packages.
6. Run the frozen synthetic experiment with a $0.10 client budget and the existing $1 key cap. Retain partial receipts if any call fails. If projected requests exceed the client budget, stop and report incompleteness instead of silently shrinking the holdout.
7. Have a separate reviewer inspect failure semantics, source preservation, budget handling and benchmark leakage. Repair findings and run the relevant tests, followed by the complete isolated suite.
8. Commit the implementation and its tests locally. Record measured results, costs, limitations and rollback commands in the execution record. Do not push or merge unrelated refactor work as part of this experiment.

## Evidence gates

Correctness gates are mandatory: disabled mode makes no calls; shadow mode preserves every baseline candidate/order/score; no writes or access reinforcement; invalid/missing/nonfinite provider data is not a relevance judgment; unknown spend prevents further calls; receipts are durable without credentials or private passages in errors.

Synthetic quality targets on the locked holdout are direct-evidence recall@3 at least 95%, conflict recall@3 at least 95%, transferable-evidence recall@3 at least 90%, no regression in relevant hit@3 compared with the same-pool baseline, and at least 20% relative reduction in irrelevant top-three passages when baseline noise is nonzero. Injection flags target at least 95% recall and at most 5% false positives. Report denominators, raw counts, errors and latency rather than hiding them behind an aggregate pass.

Passing these targets establishes only a synthetic experiment. Private-corpus use requires a frozen exact payload/destination manifest and the applicable transfer authorization. The existing eight-record Bedrock trial is a different destination and must not be reused for OpenRouter. A real trial should compare blinded full sources and retain contradictory and cross-project evidence. Human novelty, usefulness, uptake and work outcome remain unknown until supplied by the user. A failed gate leaves the implementation available in shadow mode; it does not justify enabling automatic filtering.

## Rollback and scope

The experiment runs only from this isolated branch. Stopping the CLI ends it; the installed runtime continues using its current code. The changes require no database rollback, migration or service restart. Local commits can be reverted on this branch. Disable the dedicated OpenRouter key to prevent additional calls if necessary; do not delete the account or purchased credits.

Git can revert code and configuration. It cannot undo an external data transfer or a completed API charge. That is why the first executed dataset is synthetic and spending is separately bounded.

## Sources

- `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, `docs/MODEL-BACKENDS.md`, `docs/REFACTOR-COMPLETION-2026-09-26.md` and the named implementation/tests.
- Private workspace orientation and dated installation inventory under `/Users/kerrfir/Documents/ChatGPT/Getting second brain right/`.
- [OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request).
- [OpenRouter Jev guide](https://openrouter.ai/blog/tutorials/how-to-use-jev/) and [model pricing](https://openrouter.ai/typesafe/jev-1.13).
- [TypeSafe model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
