# Codex Task Capture — Simplified Build Plan

**Status:** Simplified implementation and completion gate complete; activation awaits approval

**Reference implementation:** Codex Desktop

**Last updated:** 2026-07-23

## Outcome

Second Brain automatically captures Codex Tasks after six hours without activity, preserves their
user prompts and visible final answers, groups those Agent Turns into Topic Segments, and creates
any genuine decisions, insights, or Correction Episodes supported by the segments. Existing Codex
history is backfilled through the same path. A resumed Codex Task refreshes the same Captured Task.

Codex proves this simple path before Kiro, Quick Desktop, Amazon Quick, Amazon Q Developer, Claude
Code, or another integration is changed.

## Settled Behavior

- The source unit is a **Codex Task**, identified by its native thread ID.
- The Codex Source Connector classifies Task Ownership before eligibility: `thread_source`,
  `thread_spawn_edges`, `agent_path`, and structured `source` metadata establish whether a Task is
  user-owned or delegated.
- Only User-Owned Tasks continue. Delegated Tasks and Tasks with Unknown Ownership are skipped and
  reported.
- Capture includes complete user-prompt and visible-final-answer Agent Turns.
- Capture excludes system and developer instructions, hidden reasoning, tool activity, progress
  commentary, and delegated-agent tasks.
- An Agent Turn retains Attachment Descriptors for audit but not attachment bytes or interpreted
  attachment content.
- A Task becomes eligible after six complete hours without activity.
- Existing eligible history uses the same capture path as ongoing activity.
- A Captured Task grows monotonically: stored Agent Turns remain immutable and unseen complete turns
  append in observed order.
- Changed, missing, or reordered known turns are Source Drift and do not rewrite stored evidence.
- Source-disappearance tracking is deferred. Normal recurring inventory rediscovers a source if it
  returns, while captured knowledge remains intact.
- Codex Project, workspace, and available Git context are provenance only; they do not automatically
  populate the semantic `project` field.
- Codex Build 1 defers content-based semantic-project classification and therefore leaves
  `project` unset for its Captured Tasks, Topic Segments, and derived memories.
- A provenance-only source update refreshes title, archive, project, workspace, Git, and timestamp
  metadata without changing stored Agent Turns or invoking the Task Semantic Pass.
- Real Codex data may be used throughout testing and may be committed to Git.

## One Capture Path

```text
Read Codex Tasks
  → select Tasks idle for six hours
  → normalize complete Agent Turns and Attachment Descriptors
  → append unseen turns to the Captured Task
  → run one Task Semantic Pass over the unprocessed tail
  → atomically store Topic Segments, optional memories, and the cursor
```

One scheduled command owns this path. Backfill, task-targeted execution, and dry-run are modes of the
same command, not separate applications or pilot architectures.

## Task Semantic Pass

The model receives:

- the last existing Topic Segment as context when one exists; and
- Agent Turns after the Semantic Processing Cursor.

It returns, in one response:

- Topic Segments identified by titles and ordered Agent Turn IDs; and
- zero or more decisions, insights, or Correction Episodes supported by each segment.

Segments store their original Agent Turns and do not require separate summaries. A derived memory
must cite at least one newly processed Agent Turn, so retrying or extending the prior segment does
not regenerate old memories.

The combined semantic result stores in one transaction. If the model call, validation, embedding,
or write fails:

- the Captured Task remains successfully captured;
- no partial Topic Segments or derived memories are retained;
- the Semantic Processing Cursor does not move; and
- the next capture invocation retries the whole unprocessed tail.

There are no separate segmentation and distillation calls, statuses, cursors, or retry workflows.

## Exact Provenance

Exact Provenance is one traceability chain:

```text
Decision or Insight
  → supporting Agent Turn IDs
  → Topic Segment
  → Captured Task
  → codex://<thread-id>
```

The chain reaches the stored original prompts and visible outcomes. Attachment Descriptors remain
associated with their Agent Turns as an audit trail. Model versions, hashes, timing, usage, and
other processing telemetry are not Codex v1 provenance requirements.

## Storage

Use the existing `memories` hierarchy rather than adding Agent Task tables:

- **Captured Task:** unembedded root source memory containing ordered Agent Turns and source
  metadata. Its metadata also stores the Semantic Processing Cursor.
- **Topic Segment:** embedded child source memory containing a title and its original Agent Turns.
  Completed segments remain stable; only the last segment may extend when new turns resume its
  topic.
- **Decision or Insight:** embedded root semantic memory with supporting Agent Turn IDs and one
  durable `derived_from` relationship to its Topic Segment.
- **Correction Episode:** embedded root episodic memory with neutral, user-attributed content,
  supporting IDs for both the misaligned visible outcome and correcting prompt, and the same durable
  `derived_from` relationship to its Topic Segment.

The only Codex-specific schema additions required are uniqueness for native Codex source identities
and permanence for `derived_from`. Source timestamps, the cursor, workspace, Codex Project, Git
context, ordered turn IDs, and Attachment Descriptors remain in metadata. Codex v1 does not require
capture-revision columns, content/provenance hashes, segment or claim version tables, or processing
telemetry fields.

## Interface

Expose one deep module interface:

```python
run_codex_capture(
    now: datetime,
    *,
    task_id: str | None = None,
    backfill: bool = False,
    dry_run: bool = False,
) -> CaptureReport
```

The interface hides Codex discovery, rollout parsing, eligibility, monotonic append, the combined
semantic call, persistence, and retry behavior. Codex is the only implementation in v1; a shared
source-adapter interface will be introduced only when the second agent integration is built.

Codex also establishes the shared behavioral contract for Task Ownership, prompt/outcome
normalization, namespaced identity, monotonic refresh, and Exact Provenance. Each later connector
must document its own native ownership evidence and prove that mapping with source-native fixtures.
Common runtime code or contract-test helpers are extracted only after that second implementation
shows the genuine seam.

## Correction learning sequence

### Build 1 — Correction evidence

Extend the existing Task Semantic Pass and memory path with `correction_episode`. A qualifying
episode states only what was misaligned and what the user indicated instead, using neutral,
attributed language. It has Exact Provenance to the prior visible outcome and correcting prompt.
Detection is precision-first and abstains for new requirements, ordinary follow-ups, changed
circumstances alone, ambiguous disagreement, negative sentiment without an actionable expectation,
and quoted or pasted third-party text.

Correction Episodes remain separate even when similar. A resumed correction may extend the
immediately preceding Topic Segment; nonadjacent historical correction lookup is deferred. Episodes
are searchable and available to the Dream Cycle but are not proactive Express items and do not
enter automatic steering context. Build 1 adds no live hook, extra model call, category, scope hint,
confidence score, keyword set, occurrence counter, decay state, dedicated table, or source-neutral
runtime.

The behavior-based Proof Gate uses bounded verbatim excerpts from real Codex Tasks and verifies
positive corrections, abstention cases, adjacent-segment extension, Exact Provenance, atomic retry,
idempotency, searchability, Express exclusion, and the unchanged `run_codex_capture` interface. A
bounded live semantic pass must succeed before activation.

### Build 2 — Steering consolidation, documented only

After Build 1 is proven, teach the Dream Cycle to deliberately explore Correction Episodes and
related memories. The Thinker may propose a Steering Candidate from explicit durable direction,
recurring evidence, a consequential misalignment, a contradiction, or a material refinement; there
is no new fixed occurrence threshold. Repeated episodes remain separate, and same-Task versus
cross-Task recurrence is presented distinctly to the evaluators.

The existing four evaluators and three-of-four consensus remain the acceptance gate. Before panel
work, compare the proposal with accepted, pending, rejected, superseded, and retired candidates or
rules. A true duplicate adds evidence silently. A contradiction or material refinement becomes a
versioned Supersession Candidate, passes through consensus, and if accepted is surfaced proactively
once. Previously rejected candidates remain suppressed unless new evidence materially changes the
proposal or the user requests reconsideration.

An accepted Steering Candidate is stored as a memory but remains behaviorally inactive until the
user approves its wording, **Authority Scope** (`project`, `personal`, or `system`), and
**Applicability** (integration, semantic project, repository, topic, tool, language, or path).
Approved Steering Rules may later be exported through target-specific mechanisms. No candidate or
rule automatically edits `AGENTS.md`, steering files, skills, hooks, tests, lint, or CI.

## Explicitly Removed or Deferred

- Synthetic semantic-evaluation harness and numerical quality gates.
- Immediate contradiction planning and `contradicts` link creation. The Dream Cycle handles later
  contradiction discovery.
- Separate segmentation and distillation calls and persistence stages.
- Stage-specific retry, partial-success, version, and observability state.
- Immutable claim versions and create/update/reinforce/suppress reconciliation.
- Retrieval synopses and mandatory segment summaries.
- Model, prompt, schema, timing, token-usage, and hash telemetry.
- A separate write-pilot application and its composition layer.
- Source-neutral protocols created before a second integration exists.
- Source-disappearance status and persistent retry queues.

## Implementation Rewrite

Follow one sequential implementation stream:

1. Reduce migration 012 to the minimal uniqueness and durable-provenance rules above.
2. Keep the working Codex source reader, prompt/final-answer pairing, six-hour eligibility, Source
   Drift behavior, and Attachment Descriptor extraction.
3. Replace the broad contract graph with small internal Task, Turn, Segment, and Semantic Result
   data structures.
4. Replace the multi-stage repository and processor with the single `run_codex_capture` path and one
   semantic transaction.
5. Replace the separate semantic prompts with one combined Task Semantic Pass prompt.
6. Replace synthetic and internal-stage tests with a small real-data test set at the public
   interface.
7. Delete obsolete modules and tests after the replacement path passes.
8. Run a bounded live capture in the isolated test database and inspect the results. Enabling the
   hourly schedule and running the full backfill are separate activation steps requiring explicit
   user approval.

### Deleted Superseded Implementation

The rewrite deletes rather than layers around these implementations:

- `src/capture/task_semantic_evaluation.py`
- `src/capture/task_contradictions.py`
- `src/capture/task_capture.py`
- `src/capture/task_embedding.py`
- `src/capture/task_knowledge.py`
- `src/capture/task_processor.py`
- `src/capture/task_semantics.py`
- `src/capture/structured_invoker.py`
- `src/capture/codex_runtime.py`
- the current large `src/capture/task_repository.py`
- `scripts/pilot_codex_capture.py`
- tests dedicated to the deleted internal interfaces

The working Codex parser and any narrowly useful SQL or normalization logic may be moved into the
replacement path rather than rewritten from scratch.

## Verification

Use real Codex fixtures and the isolated test database to prove only observable behavior:

1. User-Owned Tasks continue to eligibility; Delegated and Unknown-Ownership Tasks are skipped and
   reported using native Codex metadata shapes.
2. An eligible Task captures its complete prompt/final-answer turns and Attachment Descriptors.
3. An unchanged rerun writes nothing.
4. A resumed Task appends unseen turns to the same Captured Task and preserves known turns despite
   Source Drift.
5. One Task Semantic Pass stores segments, optional decisions, insights, or Correction Episodes,
   Exact Provenance, and the Semantic Processing Cursor.
6. A failed semantic pass stores no partial semantic output and succeeds when the same tail retries.
7. A zero-memory segment remains searchable.
8. Backfill and archived Tasks use the same path as ongoing capture.
9. Derived memories are retrievable, and the Dream Cycle ignores raw child source memories.
10. A real user correction produces one neutral, episodic Correction Episode with supporting Agent
   Turn IDs for both the prior visible outcome and the correcting prompt.
11. A correction appended to a resumed Task may extend only its immediately prior Topic Segment.
12. Correction Episodes are retrievable through the Dream Cycle's memory-search interface but are
    absent from proactive Express composition.
13. An unchanged rerun does not duplicate a Correction Episode.
14. The existing Second Brain regression suite still passes.

## Done

The current build goal is complete when the simplified path passes the verification above with real
Task data, including a bounded isolated-database capture and a real resumed-Task refresh. Hourly
scheduling and the full historical backfill remain deliberately disabled until explicit approval.
The next agent integration should be designed only after Codex is proven and activated.

### Completion Evidence — 2026-07-20

- The isolated suite passes all 845 tests, including the five public-interface tests built from a
  real Codex Task that resumed across three prompt/outcome turns and included a real attachment.
- A task-bounded run read that archived Task from the live Codex Desktop index, invoked the live
  Codex semantic backend, and stored one Captured Task, one searchable Topic Segment, three
  searchable derived memories, three durable `derived_from` edges, and a cursor at the final turn
  in `second_brain_codex_test`.
- A repeated run was an unchanged no-op and retained exactly those records.
- The machine had no AWS credentials for the normal Bedrock embedding service. The bounded proof
  therefore injected the same deterministic 1,024-dimensional embedding boundary used by the
  isolated suite after separately proving that an embedding failure leaves no partial semantic
  output. Live Bedrock credential validation remains an environment activation check, not a schema
  or capture-path dependency.
- No Codex LaunchAgent was created or loaded, and the full backfill was not run.

### Correction Episode Build 1 Evidence — 2026-07-23

- Public integration tests use a bounded verbatim two-turn Codex excerpt in which an agent
  conflated Amazon Quick, Kiro, and Amazon Q Developer and the user corrected that distinction.
- The configured `codex_local` semantic backend processed that excerpt in the isolated test
  database with no failures. It produced one independent decision and one `correction_episode`.
- The live Correction Episode used the required neutral attributed format, cited both exact Agent
  Turn IDs, stored as `mem_class='episodic'`, and retained a permanent `derived_from` edge to its
  Topic Segment.
- Two additional bounded real-Task semantic passes verified precision-first abstention: an ordinary
  follow-up about Topic Segments and a new request to capture correction signals each produced zero
  Correction Episodes.
- A 15-case real adjacent-turn calibration was labeled before execution. The first pass matched 14
  cases and exposed one false positive on a conditional clarification. After the semantic contract
  explicitly required that boundary to abstain, the same pair did so on rerun. See
  [Correction Episode Build 1 calibration](CORRECTION-EPISODE-BUILD1-CALIBRATION.md).
- Automated tests prove unchanged-run idempotency, adjacent-segment extension after a Task resumes,
  hybrid/MCP search visibility for Dream Cycle exploration, and exclusion from Express composition.
- The proof used the same deterministic 1,024-dimensional embedding boundary as the existing
  capture proof so no Bedrock credentials or live memory database were required.
- Build 2 remains documented but unimplemented. Its Steering Candidate, consensus, deduplication,
  supersession, and user-approval behavior may be implemented in a separate build now that this
  Proof Gate has passed.
