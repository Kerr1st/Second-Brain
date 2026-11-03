## review_docs

=== Context Gathering Summary ===
FOUND: Two-layer doc structure (uncommitted 2026-09-10 snapshot vs committed post-09-14 feature docs); DEFAULT_ADAPTERS with 4 adapters incl. bedrock (resolver.py:42-48); 5 profiles in config/backends.toml; 7 roles incl. Bedrock-pinned skim (resolver.py:57-75); Step 3.5 skim stage (orchestrator.py:377-384); grounding audit + revision pass (orchestrator.py:484-492, 1131-1174); 3-of-4 consensus (consensus.py:27); 15 migrations 000-014; 18 repo plists vs 17 installed (missing codex-canary); dream-cycle Mon/Wed/Fri 02:00; $9 effective cap (dream_cycle_scheduled.sh:33) vs $5 library default (cost.py:39) vs $6 in backends.toml:119; 4 live capture modules (codex/outlook/slack/agent_tasks); Codex port commits 5386c2b/b6d4484 from origin/main; 27 redirect stubs; 33 orphan docs; zero broken links; zero non-inclusive terms; zero byte-identical live/archive pairs; Kiro --model regression + Fable PII hazard documented only in backends.toml:104-113.
NOT FOUND: Current launchd enabled/disabled state per job; live DB schema_migrations rows; live capture-channel health beyond commit messages; Kiro login and AWS credential validity; remote origin/main divergence beyond the confirmed port; the "local operational inventory" document START-HERE.md defers all runtime facts to (no path given, not in repo); exhaustive per-anchor validation; VALIDATION.md's "41 original docs" count (archive content excluded from audit per instructions).
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Documentation Audit — second-brain @ 6b8c2d2 (read-only; nothing written)

## 0. The structural fact that explains almost everything

The repo has **two documentation layers that were written ~12 days and ~233 commits apart, and they were never reconciled**:

| Layer | Docs | State | Date basis |
|---|---|---|---|
| **A — the 2026-09-10 snapshot** | README.md, START-HERE.md, ARCHITECTURE.md, CAPTURE-COMPONENTS.md, OPERATIONS.md, DISASTER-RECOVERY.md, VALIDATION.md, SPEC-INDEX.md, INTERFACES.md + **26 redirect stubs** | **UNCOMMITTED** working-tree edits (`git status`: 83 entries; `docs/START-HERE.md`, `docs/VALIDATION.md`, `docs/SPEC-INDEX.md`, `docs/archive/` are `??` untracked) | baseline `7bf8d41` |
| **B — the committed feature docs** | INTERFACES.md (Sep 14), MODEL-BACKENDS.md (Sep 20), SKIM-STAGE.md, RESEARCH-skim-model.md, PLAN-skim-stage.md, PLAN-panel-acceptance.md, PLAN-capture-repair.md, PLAN-mcp-review.md | **TRACKED / committed** | 2026-09-14 → 09-23 |

Layer B is broadly accurate. **Layer A is the source of essentially every false claim below.** MODEL-BACKENDS.md is the interesting exception: it is a layer-A doc that was *partially* refreshed on 09-20, so it is correct where it was touched and stale where it was not.

Two important negatives verified up front:
- **No broken internal links.** I resolved every `](...)` target across README.md, scheduling/README.md, docs/*.md, docs/user-guide/*.md. All 39 archive links and all live-doc links resolve to existing files, and every anchor I spot-checked (`OPERATIONS.md#schedule-templates`, `VALIDATION.md#documentation-maintenance`, `START-HERE.md#resume-checklist-after-compaction`, `ARCHITECTURE.md#discovery-and-learning`) exists.
- **No non-inclusive language.** `grep -rni "master|slave|whitelist|blacklist"` over docs/*.md, docs/user-guide/*.md, README.md, scheduling/README.md, src/, config/ returned **zero hits**. Section 7 is empty — nothing to fix.
- **No live doc is byte-identical to its archived ancestor** (`cmp` over all archive/live basename pairs: zero matches). Every stub was genuinely rewritten. But see §4 — they were rewritten into near-identical boilerplate, which is a different problem.

---

## 1. FALSE CLAIMS — doc statement vs. code truth

### 1.1 `bedrock` IS registered in DEFAULT_ADAPTERS (two docs wrong)

> **docs/START-HERE.md:19** — "Registered reasoning adapters | Kiro, Claude Code, Codex. **Direct Bedrock files exist locally but are not in `DEFAULT_ADAPTERS`**"

> **docs/SPEC-INDEX.md:28** — "Their registration/live verification is incomplete: **the active resolver does not register a direct Bedrock adapter.** A docstring claiming registration is ahead of implementation."

> **docs/SPEC-INDEX.md:20** — "[model-backend-adapters] … **These three adapters are registered.**"

**Code — src/backends/resolver.py:42-48:**
```python
DEFAULT_ADAPTERS: dict[str, type] = {
    "kiro": KiroInvoker,
    "claude_code": ClaudeCodeInvoker,
    "codex": CodexInvoker,
    "bedrock": BedrockInvoker,      # ← registered
}
```
Imported at `src/backends/resolver.py:31` (`from src.backends.bedrock import BedrockInvoker`). **Truth: four adapters are registered.** SPEC-INDEX.md:28 has it exactly backwards — the docstring was *ahead* of nothing; the code caught up and the doc did not. **docs/MODEL-BACKENDS.md:21 states this correctly** ("`DEFAULT_ADAPTERS` registers `kiro`, `claude_code`, `codex`, and `bedrock`"), so layer B already contradicts layer A in-tree.

### 1.2 There are five profiles, not two

> **docs/START-HERE.md:20-21** — "Default dream-cycle profile | `laptop` … | Alternative configured profile | `mini` …" (only two profiles named anywhere in the doc)

**Code — config/backends.toml:35, 44, 61, 84, 104:** `[profiles.laptop]`, `[profiles.mini]`, `[profiles.bedrock]`, `[profiles.hybrid]`, `[profiles.controlled]`. **Truth: five.** `controlled` was added 2026-09-17 (config/backends.toml:106) specifically because kiro-cli 2.22 stopped honouring `--model` and silently served `claude-fable-5.1` to the Explorer/Thinker — a model Kiro labels "NOT FOR CUSTOMER DATA, ITAR OR PII" — against the personal corpus (config/backends.toml:107-113). **That is a live PII-routing hazard that START-HERE.md, the designated entry point, does not mention at all.** docs/MODEL-BACKENDS.md:23 names all five correctly.

### 1.3 "all six roles use Kiro" — there are seven roles, and the 7th is never Kiro

> **docs/START-HERE.md:20** — "`laptop`: **all six roles** use Kiro with configured model `claude-opus-4.8`"
> **docs/START-HERE.md:33** — "Dream cycle | Explorer, Thinker, Skeptic, Advocate, Epistemologist, Methodologist; three-of-four review vote"
> **docs/ARCHITECTURE.md:42** — "**Six** role-resolved invocations"
> **docs/ARCHITECTURE.md:93** — "Only the dream cycle's **six roles** are controlled by backend profiles."

**Code — src/backends/resolver.py:57-75:**
```python
AGENT_ROLES = ("explorer","thinker","skeptic","advocate","epistemologist","methodologist")
SKIM_ROLE = "skim"
VALID_ROLES = (*AGENT_ROLES, SKIM_ROLE)
_REQUIRED_ROLES = frozenset(VALID_ROLES)
```
`_validate_profile` (resolver.py:78-104) **raises ValueError at config load** if a profile does not define all seven. **Truth: seven roles; `skim` is pinned to `bedrock`/`us.amazon.nova-2-lite-v1:0` in all five profiles** (config/backends.toml:42, 59, 82, 100, 126) and is Bedrock-only *by enforced rule* — `assert_backend_supports_role` (resolver.py:104) rejects skim on Kiro/Claude Code/Codex. docs/MODEL-BACKENDS.md:35 states "exactly seven required roles" correctly.

Consequence: START-HERE.md's own §"Three distinctions" and ARCHITECTURE.md's pipeline are describing a 6-stage cycle that no longer exists.

### 1.4 Step 3.5 skim stage is absent from every layer-A doc

**Code — src/dream_cycle/orchestrator.py:377-384:**
```python
# Step 3.5: Skim every arrival in the window (cheap Bedrock model, own sub-cap).
skim = self._skim_arrivals(run_id, run_type, started_at)
skim_stats = skim.to_stats() if skim is not None else {}
self._skim_records = self._load_skim_records(skim, run_id, run_type, started_at)
```
Supporting module tree `src/skim/` (`runner.py`, `store.py`, `cluster.py`, `schema.py`), prompts at `src/prompts/skim.py`, sub-cap constants at orchestrator.py:111-112 (`SKIM_SPEND_CAP_USD = 1.00`, `SKIM_RUN_CAP_SHARE = 0.15`), off-switch `SECOND_BRAIN_SKIM_DISABLED` (orchestrator.py:114), persistence via migration `014_memory_skim.sql`, and it feeds the Explorer's inventory (orchestrator.py:703-706).

**docs/ARCHITECTURE.md:17-20** renders the mermaid flow as `Search --> Explorer --> Thinker --> Panel[Four reviewer calls] --> Vote[At least 3 ACCEPT]` — no skim node. ARCHITECTURE.md:86 ("Explorer gets the live memory tools…") is step 2 of a numbered list that never reaches 3.5. **ARCHITECTURE.md is the doc README.md:12 designates authoritative for "Components and data model", and it is missing an entire pipeline stage.**

### 1.5 Panel acceptance (grounding audit + revision) is absent from ARCHITECTURE.md

**Code — src/dream_cycle/orchestrator.py:484-492, 1131, 1155-1174:**
```python
# Grounding audit + one revision before the panel votes (never aborts).
...
audit = self.invoke_evaluator(candidate, GROUNDING_AUDIT_ROLE, stage="grounding_audit")
user_message = build_revision_request(candidate_json, sources_text, audit.blocking_issues, ...)
# Sources may only shrink: the revision must not introduce new citations.
```
Graded verdicts at `src/prompts/panel.py:161-162` ("The acceptance Standard and the graded verdict format shared by all four seats"), revision prompt at `src/prompts/revision.py`, per-seat detail persisted in `dream_cycle_candidates.evaluator_details` (migration 013), audit skipped above 80% of cap (orchestrator.py:1126, `GROUNDING_SPEND_SHARE`).

**docs/ARCHITECTURE.md:89** describes only "`tally_consensus` accepts at least three ACCEPT votes out of four" — no audit, no revision, no 0–100 score, no blocking/advisory issues. docs/MODEL-BACKENDS.md:49 documents all of it correctly, including the measured effect (acceptance 1-of-70 → 4-of-6 → 6-of-8). **So the repo contains a full, accurate description of this stage and its designated architecture doc omits it.**

### 1.6 Schedule: "12 committed plist templates" — there are 18; dream-cycle cadence is wrong

> **docs/OPERATIONS.md:56** — "There are **12 committed plist templates**."
> **docs/OPERATIONS.md:70** — "| `dream-cycle` | **Daily 12:00** | `scripts/jobs/dream_cycle_scheduled.sh` |"
> **docs/START-HERE.md:36** — "Operations | **Twelve schedule templates**, logs, liveness, setup and diagnostics"
> **scheduling/README.md:5** — "Consolidated **all12** template schedules" *(sic — missing space)*

**Truth (`ls scheduling/*.plist` = 18; plistlib dump of each):**

| plist | actual schedule | in OPERATIONS.md table? |
|---|---|---|
| dream-cycle | **Weekday 1,3,5 @ 02:00** (array) | listed as "Daily 12:00" ❌ |
| capture-browser | 01:15 | **missing** |
| capture-outlook | 00:45 | **missing** |
| capture-slack | 01:00 | **missing** |
| codex-capture | StartInterval 21600 | **missing** |
| codex-canary | StartInterval 3600 | **missing** |
| credentials | 07:00 | **missing** |
| bookmarks / youtube / backup / chat-extract / ingest / verify / qd-sync / distill / weekly-digest / liveness / capture-api | match | ✓ |

So OPERATIONS.md's table is missing **6 of 18 rows** and has the cadence of the single most expensive job wrong by both day-set and hour. Confirmed in `scheduling/com.second-brain.dream-cycle.plist` `StartCalendarInterval` array (Weekday 1/3/5, Hour 2, Minute 0) and cross-confirmed by docs/PLAN-panel-acceptance.md:284 ("fires Mon/Wed/Fri 02:00 (plist `StartCalendarInterval` array)").

### 1.7 "All twelve schedule templates were installed" / "six pipeline jobs were disabled"

> **docs/START-HERE.md:44** — "**All twelve schedule templates were installed**; six pipeline jobs were disabled: distill, weekly-digest, ingest, qd-sync, liveness, and dream-cycle."

**Truth:** `~/Library/LaunchAgents/` contains **17** `com.second-brain.*` plists against **18** in-repo. The one repo template **not** installed is `com.second-brain.codex-canary.plist`. The installed set includes capture-outlook, capture-slack, capture-browser and codex-capture — four channels START-HERE.md does not know exist. I could not verify current *enabled/disabled* state (see §8), but the "twelve" count is false on both sides of the sentence.

### 1.8 Capture channels: the flat denial is now false

> **docs/CAPTURE-COMPONENTS.md:23** — "The source tree has **no native automatic Codex conversation, Claude Code conversation, Gmail inbox, or Outlook inbox collector**."

**Code — `ls src/capture/`:** `codex.py`, `outlook.py`, `slack.py`, `agent_tasks.py`, `browser.py`, `youtube.py`, `mcp_client.py`, `sources/codex.py`. Installed and scheduled as `com.second-brain.capture-outlook.plist` (00:45), `com.second-brain.capture-slack.plist` (01:00), `com.second-brain.codex-capture.plist` (6-hourly), `com.second-brain.capture-browser.plist` (01:15). Commit `5177b17` — "docs(capture): **all channels healthy**, monitors fixed, digest off Kiro — phases 1/2/5 done". **Truth: native automatic Codex and Outlook collectors exist, are scheduled, and are reported healthy.** CAPTURE-COMPONENTS.md:16-21 also still frames Slack as graph-import-only and HTTP endpoints as "not a working inbox/browser connector".

`quick_desktop_doc` retirement is documented **only** in docs/PLAN-capture-repair.md:140 ("`quick_desktop_doc` retired (dormant since 2026-06-04, advisory not failure)"). It still appears as a live `source_type` in three archived docs (archive/.../ARCHITECTURE.md:85, user-guide/glossary.md:44, user-guide/database-schema.md:39) — acceptable, those are archive. But **no live doc records the retirement**, so CAPTURE-COMPONENTS.md, the canonical capture doc, neither adds the four new channels nor retires the dead one.

### 1.9 Migration ledger 001–005 vs. 15 migration files

> **docs/START-HERE.md:44** — "**The migration ledger recorded only 001–005**, so it must be reconciled with actual columns/tables before schema changes."

**Truth (`ls migrations/`):** `000_migrations_table.sql` through `014_memory_skim.sql` — **15 files, 000–014**, including `011_backend_provenance.sql`, `012_dream_cycle_slices.sql`, `013_evaluator_details.sql`, `014_memory_skim.sql`, all of which back features the code actively uses (orchestrator writes `stats` JSONB per `src/dream_cycle_db.py:42`, "migration 014"). docs/ARCHITECTURE.md:67 acknowledges only up to `011`. The ledger observation may have been literally true of the DB table on 09-10, but as written in a "Current local observation" section it now misleads: a reader will conclude 006–014 are unapplied when the code demonstrably depends on 013/014.

### 1.10 README's "do not merge" claim is contradicted by git history

> **README.md:12** — "This checkout is the original lineage at `7bf8d41` plus local, unfinished direct-API backend work. **The experimental GitHub lineage is a separate implementation; do not merge or deploy it** by following the original runbook."

**Git facts:**
```
5386c2b Merge branch 'codex-capture-port': recover Codex session capture
        from origin/main and wire it for this machine   (Tue Sep 22 2026)
b6d4484 feat(capture): port Codex session capture from origin/main
778a9e5 feat(capture): wire Codex capture for this machine
```
**Truth: code was ported from `origin/main` into local `main` and merged.** The prohibition was overtaken by events; the remote is now an upstream source for at least one live subsystem (`src/capture/codex.py`, `src/capture/sources/codex.py`). Also stale in the same sentence: HEAD is `6b8c2d2`, not `7bf8d41`, and the direct-API work is no longer "unfinished" (§1.1).

### 1.11 Run cost cap: three different numbers, and the live one is in neither canonical doc

> **docs/MODEL-BACKENDS.md:62** — "`SECOND_BRAIN_RUN_COST_CAP_USD` (**default 5.00**) is a hard stop"

**Code — src/backends/cost.py:39:** `DEFAULT_RUN_COST_CAP_USD = 5.0` ✓ (the library default is right)
**But — scripts/jobs/dream_cycle_scheduled.sh:33:** `export SECOND_BRAIN_RUN_COST_CAP_USD="${SECOND_BRAIN_RUN_COST_CAP_USD:-9}"`, with scripts/jobs/dream_cycle_scheduled.sh:21 commenting "`SECOND_BRAIN_RUN_COST_CAP_USD=9` is the per-run budget".

**Truth: the effective cap for every scheduled run is $9.** The $5 figure is the unused library fallback. No canonical doc (OPERATIONS.md, MODEL-BACKENDS.md) states the $9 operational value; only docs/SKIM-STAGE.md:125 does ("$0.75 at the default $5 cap, $1.00 at the $9 cap used live"). Meanwhile **config/backends.toml:71** documents "default 5.00" and **config/backends.toml:119** says "under the **$6/run** cap" — a third number, inside the config file itself.

---

## 2. INTERNAL CONTRADICTIONS — contested-fact table

| # | Contested fact | Version A (doc:line) | Version B (doc:line) | Version C | Code truth |
|---|---|---|---|---|---|
| C1 | Registered adapters | **3** — START-HERE.md:19; SPEC-INDEX.md:20; SPEC-INDEX.md:28 | **4** — MODEL-BACKENDS.md:21 | — | **4** (resolver.py:42-48) → B |
| C2 | Profile count | **2** (`laptop`,`mini`) — START-HERE.md:20-21 | **5** — MODEL-BACKENDS.md:23 | — | **5** (backends.toml:35,44,61,84,104) → B |
| C3 | Role count | **6** — START-HERE.md:20,33; ARCHITECTURE.md:42,93 | **7** — MODEL-BACKENDS.md:35 | — | **7** (resolver.py:57-75) → B |
| C4 | Plist template count | **12** — OPERATIONS.md:56; START-HERE.md:36,44; scheduling/README.md:5 | — | — | **18** (`ls scheduling/*.plist`) → none |
| C5 | Dream-cycle cadence | **Daily 12:00** — OPERATIONS.md:70 | **Mon/Wed/Fri 02:00** — PLAN-panel-acceptance.md:284; PLAN-skim-stage.md:3 ("the 02:00 LaunchAgent") | — | **Weekday 1,3,5 @ 02:00** → B |
| C6 | Per-run cost cap | **$5** — MODEL-BACKENDS.md:62; backends.toml:71 | **$6** — backends.toml:119; PLAN-panel-acceptance.md:77 | **$9** — SKIM-STAGE.md:125; PLAN-panel-acceptance.md:284 | **$9 effective** (dream_cycle_scheduled.sh:33); $5 library default (cost.py:39) → C |
| C7 | Codex/Outlook collectors exist | **No** — CAPTURE-COMPONENTS.md:23 | **Yes, healthy** — PLAN-capture-repair.md; commit 5177b17 | — | **Yes** (`src/capture/{codex,outlook,slack,agent_tasks}.py`) → B |
| C8 | Remote lineage usable | **"do not merge"** — README.md:12 | **merged** — commits 5386c2b / b6d4484 | — | merged → B |
| C9 | Explorer→panel flow | Explorer→Thinker→Panel→Vote — ARCHITECTURE.md:17-20, 86-89 | +Step 3.5 skim, +grounding audit, +revision — MODEL-BACKENDS.md:49; SKIM-STAGE.md | — | orchestrator.py:377-384, 484-492 → B |
| C10 | Migration high-water mark | **005** (ledger) — START-HERE.md:44 | **011** — ARCHITECTURE.md:67 | **014** — dream_cycle_db.py:42; MODEL-BACKENDS.md:49 (mig. 013) | **014** → C |
| C11 | Thinker model rationale | `hybrid` avoids Fable for PII — backends.toml:90-93 | `bedrock` profile *uses* Fable for Thinker — backends.toml:77; MODEL-BACKENDS.md table | — | both true; **the PII caveat is absent from MODEL-BACKENDS.md's `bedrock` row** — a doc-level safety gap |
| C12 | Kiro `--model` honoured | implied throughout — START-HERE.md:20 (`laptop` = Kiro/opus-4.8) | **broken since kiro-cli 2.22** — backends.toml:106-113 | — | broken → B; `laptop` (the **default_profile**, backends.toml:33) may silently route the personal corpus to Fable |
| C13 | Consensus threshold | 3-of-4 — START-HERE.md:33; ARCHITECTURE.md:20,89; SPEC-INDEX.md:13,33; MODEL-BACKENDS.md:49 | — | — | **3-of-4 ✓** (consensus.py:27) — *the one fact everything agrees on* |

**C11/C12 together are the highest-severity finding in this section**: the default profile is `laptop` (all-Kiro), Kiro 2.22 ignores `--model` and server-side "Auto" has been observed serving `claude-fable-5.1`, and Fable is labelled by Kiro as not for PII. That reasoning exists **only in a TOML comment** (config/backends.toml:104-113). START-HERE.md — the file README.md:5 tells every reader and every compacted agent to open first — still presents `laptop`/Kiro as the normal path with no warning.

---

## 3. STALE DATES AND EVIDENCE CLAIMS

Every dated header, and whether it still holds:

| Doc:line | Header claim | Still true? |
|---|---|---|
| **START-HERE.md:3** | "**Authority:** current orientation… Reviewed 2026-09-10 against original commit `7bf8d41…`" | ❌ **Worst offender.** Claims *authority* + *current* while pinned to a baseline 233 commits behind HEAD `6b8c2d2`. Its adapter list, profile list, role count, plist count and migration ledger are all now false (§1.1,1.2,1.3,1.7,1.9). |
| **START-HERE.md:44** | "A read-only audit … at **2026-09-10 19:58 PDT** … All twelve schedule templates were installed; six pipeline jobs were disabled … Kiro reported not logged in … AWS identity probe reported expired credentials … migration ledger recorded only 001–005" | ❌ Wholesale. 17/18 installed; 4 new capture channels live; commit 5177b17 reports "all channels healthy"; migrations run to 014. The doc *does* hedge ("timestamped observations, not continuing health guarantees") — but it then places them under the heading "**Current** local observation", and the resume checklist (START-HERE.md:50) sends agents here first. |
| **README.md:18** | "These guides were reconciled against code on 2026-09-10." | ❌ For README's own authority table: 5 of 8 targets have since diverged (§1). |
| **README.md:12** | "original lineage at `7bf8d41` plus local, **unfinished** direct-API backend work" | ❌ HEAD is `6b8c2d2`; the work is finished and registered. |
| **ARCHITECTURE.md:3** | "Source-reviewed 2026-09-10" | ❌ Missing skim stage, grounding audit, revision pass, migrations 012-014; says "six roles". |
| **CAPTURE-COMPONENTS.md:3** | "Source-reviewed 2026-09-10. This document lists **actual capture paths**" | ❌ Omits 4 live channels, retains the "no Codex/Outlook collector" denial (line 23), does not retire `quick_desktop_doc`. |
| **OPERATIONS.md:3** | "Source-reviewed 2026-09-10" | ❌ 12-vs-18 templates; dream-cycle cadence; 6 missing rows; no $9 cap. |
| **SPEC-INDEX.md:3** | "Reviewed September 10, 2026 against local source" | ❌ Line 28's "resolver does not register a direct Bedrock adapter" is inverted; line 20's "three adapters"; line 9's "36 documents across 12 families" is now 13 families (`direct-api-backends` exists on disk, untracked). |
| **VALIDATION.md:3** | "Source-reviewed 2026-09-10 … authoritative distinction between component correctness, operating state and effectiveness" | ⚠️ Its *epistemics* are the best in the repo and still valid; its *contents* predate skim/panel-acceptance tests entirely. VALIDATION.md:63 flags "capture plist contains a committed token" as an open Medium finding — **already fixed**: MODEL-BACKENDS.md:102 records the token rotated 2026-09-14, moved to a gitignored mode-0600 `.env`, with `tests/test_repo_hygiene.py` guarding regression. A resolved finding is still presented as open. |
| **VALIDATION.md:81** | "preserves **all 41** original docs/readmes" | ⚠️ Unverified count (not audited per instructions), but the live tree now has ~50 docs — the ratio no longer describes the current set. |
| **DISASTER-RECOVERY.md:3** | "Source-reviewed 2026-09-10 … Current RPO/RTO require a successful timed restore test; **none is certified**" | ✅ Still honest and still true. Best-aged layer-A doc. |
| **INTERFACES.md:3** | "Source-reviewed 2026-09-10" | ⚠️ Committed Sep 14; not re-checked for skim/panel CLI surface. Low risk. |
| **MODEL-BACKENDS.md:3** | "Source-reviewed 2026-09-10; skim-stage additions … 2026-09-20" | ✅ Accurate where refreshed. Residual: `$5` cap (§1.11), no Fable/PII warning on the `bedrock` row (C11). **The healthiest canonical doc.** |
| **SKIM-STAGE.md:3** | "Source-reviewed 2026-09-20 **on branch `skim-stage`**" | ⚠️ Content accurate; the *branch* reference is stale — `skim-stage` merged, HEAD is main at `6b8c2d2`. Same for PLAN-skim-stage.md:3's "the main tree … stays on `panel-acceptance` until this is verified and merged" — it merged. |
| **RESEARCH-skim-model.md:3** | "Research date: 2026-09-19. Status: research complete, no code changed, no model invoked, zero spend." | ⚠️ True *as a research record*, but superseded: the model was since invoked live (MODEL-BACKENDS.md:71 — "164 documents for $0.19", run `05171b48`). Correctly labelled `RESEARCH-`. |
| **PLAN-skim-stage.md:137** | "83 dirty files **as of 2026-09-20**" | ⚠️ Now 83 entries in `git status` again by coincidence, but the referenced blocking condition is gone. |
| **MODEL-BACKENDS.md:35** | "inference-profile ids were confirmed ACTIVE … on **2026-09-14**" | ⚠️ Correctly scoped to a date. Acceptable. |
| **MODEL-BACKENDS.md:57** | Nova 2 Lite "**live-verified 2026-09-20**" | ✅ Correctly scoped. |

**Pattern:** layer-A docs use *absolute, present-tense* authority language ("Authority: current", "Current local observation") pinned to a stale commit. Layer-B docs use *dated, scoped* evidence language ("confirmed ACTIVE on 2026-09-14", "doc-verified, not live", "they record what was measured, not a service guarantee"). Layer B's convention is the one to keep; it is also exactly what VALIDATION.md:79 prescribes — the repo's own standard, honoured only by the docs written after it.

---

## 4. DOC INVENTORY AND OWNERSHIP

### 4.1 Canonical owner per contract

| Contract | Canonical owner | Health |
|---|---|---|
| Orientation / resume | docs/START-HERE.md | ❌ needs rewrite (§1, §3) |
| Components & data model | docs/ARCHITECTURE.md | ❌ missing skim + panel-acceptance |
| Capture channels | docs/CAPTURE-COMPONENTS.md | ❌ missing 4 channels, false denial |
| Models/providers/auth/profiles/caps | docs/MODEL-BACKENDS.md | ✅ best canonical doc; fix cap + Fable warning |
| Schedules / runbook | docs/OPERATIONS.md | ❌ 12-vs-18, wrong cadence |
| Backup & recovery | docs/DISASTER-RECOVERY.md | ✅ |
| MCP/CLI/config/terminology | docs/INTERFACES.md | ⚠️ |
| Tests / evidence limits | docs/VALIDATION.md | ⚠️ one finding already fixed |
| Spec map | docs/SPEC-INDEX.md | ❌ inverted Bedrock claim |
| Skim stage | docs/SKIM-STAGE.md | ✅ (owner should be folded under MODEL-BACKENDS/ARCHITECTURE) |

**Contracts with NO canonical owner** — each lives only in a PLAN or a TOML comment:
- Panel acceptance / grounding audit / revision → only PLAN-panel-acceptance.md + MODEL-BACKENDS.md:49
- The `controlled` profile and the **Kiro-`--model`-broken / Fable-PII hazard** → only config/backends.toml:104-113
- The **$9 effective run cap** → only scripts/jobs/dream_cycle_scheduled.sh:33 + SKIM-STAGE.md:125
- `quick_desktop_doc` retirement → only PLAN-capture-repair.md:140
- The 4 new capture channels → only PLAN-capture-repair.md

### 4.2 Full live-doc inventory (~50 docs)

**Canonical guides (10)** — keep, repair per §1:
| Path | Purpose | Type | Recommendation |
|---|---|---|---|
| README.md | Root entry + authority table | current guide | keep; fix HEAD/`7bf8d41`, drop "do not merge" (§1.10) |
| docs/START-HERE.md | Orientation + resume checklist | current guide | **keep as canonical, rewrite against HEAD** |
| docs/ARCHITECTURE.md | Components, data model, pipeline | current guide | keep; add Step 3.5 + acceptance stage |
| docs/CAPTURE-COMPONENTS.md | Capture/ingest/distill paths | current guide | keep; absorb PLAN-capture-repair channel facts |
| docs/MODEL-BACKENDS.md | Providers, profiles, caps, privacy | current guide | **keep as canonical** (healthiest); fix $9, add Fable/PII |
| docs/OPERATIONS.md | Setup, schedules, troubleshooting | current guide | keep; regenerate schedule table from plists |
| docs/DISASTER-RECOVERY.md | Backup/restore limits | current guide | keep as-is |
| docs/INTERFACES.md | MCP/CLI/config/terminology | current guide | keep; **make canonical glossary owner** (§6) |
| docs/VALIDATION.md | Tests, evidence limits, doc ownership | current guide | keep; close the fixed token finding |
| docs/SPEC-INDEX.md | Map of `.kiro/specs` | current guide | keep; fix §1.1 inversion, 12→13 families |

**Feature docs (2)** — accurate, but title promises reference while body is partly a point-in-time log:
| docs/SKIM-STAGE.md | Skim design, prices, measurements | current guide + log | merge reference content into MODEL-BACKENDS.md + ARCHITECTURE.md; keep measurements or archive |
| docs/RESEARCH-skim-model.md | Model selection research (47 KB) | research artifact | **archive** — decision made, model live |

**PLAN-*.md (4) — all describe completed work, all titled as forward plans:**
| docs/PLAN-panel-acceptance.md | Panel acceptance build log | finished plan | **archive**; promote contract into ARCHITECTURE.md |
| docs/PLAN-skim-stage.md | Skim build log | finished plan | **archive** (branch refs stale) |
| docs/PLAN-capture-repair.md | Capture channel repair | finished plan (commit 5177b17: "phases 1/2/5 done") | **archive**; promote channel facts into CAPTURE-COMPONENTS.md |
| docs/PLAN-mcp-review.md | MCP review | finished plan (HEAD 6b8c2d2 is its dedup follow-up) | **archive** |

All four carry `**Resume here after a restart:** read "Status", then the first unchecked task` — **active-work framing on completed work**, and all four cite stale branch/base commits (`619146d`, `3efb224`, `6bf67bc`, `1db8bf7`).

**Redirect stubs (26 + 1)** — 15 in `docs/`, 18 in `docs/user-guide/`, 1 at `scheduling/README.md`. Every one is ~460-590 bytes with the identical three-part shell: `**Current documentation:** [X]` / one-line rationale / `[Historical document preserved…]`.

`docs/`: AGENTIC-RETRIEVAL-PLAN, COMPONENTIZATION-PLAN, DESIGN-DECISIONS, DREAM-CYCLE-MCP-DECOUPLING, EXPRESS-PLAN, FABLE5-THINKER-PLAN, HYBRID-CHAT-EXTRACTION, MAC-MINI-MIGRATION, MIGRATION-native-postgres, MODEL-BACKENDS-VERIFICATION, PARALLEL-WORK, QUALITY-BASELINE-2026-06-06, QUICK-DESKTOP-INTEGRATION, SECOND-BRAIN-PURPOSE-AND-EVOLUTION, SESSION-2026-06-12-handoff, SESSION-2026-06-16-handoff.
`docs/user-guide/` (all 18): architecture, connect-ai-agent, CONTRIBUTING, database-schema, dream-cycle-design, first-memory, getting-started, glossary, how-memory-works, index, operations, overview, reference, security-model, STYLE, troubleshoot, upgrade, using-second-brain.

**Recommendation: delete all 27.** The archive already holds the originals at `docs/archive/2026-09-10/**` with identical relative paths. Each stub is a redirect nobody can arrive at — §5 shows **33 of them have zero inbound links**. They triple the apparent doc count (50 files, ~13 with content) and they are the bulk of the uncommitted diff. Note the two SESSION-* handoffs are already correctly reduced to stubs — the §4 ask about "every SESSION-*.md describing completed work" resolves to: both are stubs, neither is a live hazard, both should be deleted rather than kept as redirects.

**Duplication pairs to collapse:** `docs/user-guide/architecture.md` ↔ ARCHITECTURE.md · `user-guide/operations.md` ↔ OPERATIONS.md · `user-guide/database-schema.md` ↔ ARCHITECTURE.md §schema · `user-guide/glossary.md` ↔ INTERFACES.md §terminology · `user-guide/security-model.md` ↔ MODEL-BACKENDS.md §privacy · `user-guide/dream-cycle-design.md` ↔ ARCHITECTURE.md §discovery · `scheduling/README.md` ↔ OPERATIONS.md §schedule-templates · `MODEL-BACKENDS-VERIFICATION.md` ↔ MODEL-BACKENDS.md.

---

## 5. NAVIGATION INTEGRITY

**Broken links: none.** All internal `](path)` targets resolve; all checked anchors exist.

**Orphans — 33 live docs with zero inbound links** from README.md, docs/*.md, docs/user-guide/*.md or scheduling/README.md: all 18 `docs/user-guide/*.md`, plus AGENTIC-RETRIEVAL-PLAN, COMPONENTIZATION-PLAN, DESIGN-DECISIONS, DREAM-CYCLE-MCP-DECOUPLING, EXPRESS-PLAN, FABLE5-THINKER-PLAN, HYBRID-CHAT-EXTRACTION, MIGRATION-native-postgres, PARALLEL-WORK, QUALITY-BASELINE-2026-06-06, QUICK-DESKTOP-INTEGRATION, SECOND-BRAIN-PURPOSE-AND-EVOLUTION, SESSION-2026-06-12-handoff, SESSION-2026-06-16-handoff, **and docs/PLAN-capture-repair.md**.

PLAN-capture-repair.md is the significant one: it is the **sole live record** of the four new capture channels and the `quick_desktop_doc` retirement (line 140), and **nothing links to it**. The most current capture truth in the repo is unreachable by navigation.

Inbound-link counts: OPERATIONS 13, ARCHITECTURE 11, MODEL-BACKENDS 11, START-HERE 10, VALIDATION 8, CAPTURE-COMPONENTS 7, INTERFACES 6, RESEARCH-skim-model 4, DISASTER-RECOVERY 4, SKIM-STAGE 3, PLAN-skim-stage 3, PLAN-panel-acceptance 2, PLAN-mcp-review 2, SPEC-INDEX 2, MAC-MINI-MIGRATION 1, MODEL-BACKENDS-VERIFICATION 1.

**README.md ↔ START-HERE.md entry-point map — disagree:**
- README.md:12 designates START-HERE.md as the thing that "maps **every** component". START-HERE.md's component map (lines 28-37) has **9 rows and omits INTERFACES.md entirely**, while README.md:12's table lists INTERFACES.md as authoritative for "MCP, CLI, configuration, terminology". A reader following README's instruction to start at START-HERE never learns INTERFACES.md exists.
- Neither entry point links to SKIM-STAGE.md, PLAN-panel-acceptance.md, or PLAN-capture-repair.md — i.e. **neither entry point reaches any doc describing the last 233 commits.**
- START-HERE.md:36 routes Operations → "Twelve schedule templates" (false, §1.6).

**Circular authority chains:**
1. README.md:5 → START-HERE.md ("identifies this implementation") → START-HERE.md:3 "Authority: current orientation" → START-HERE.md:26 → SPEC-INDEX.md → SPEC-INDEX.md:3 "use [START-HERE] for the complete system" → **back to START-HERE**. Neither terminates on code.
2. START-HERE.md:56 → VALIDATION.md#documentation-maintenance → VALIDATION.md:77 names the authoritative set *including START-HERE* → VALIDATION.md:83 "resume from START-HERE" → **back**.
3. START-HERE.md:44 defers current state to "the local operational inventory"; START-HERE.md:52 says "Consult the current local operational inventory before changing anything" — **that document is not in the repo and is never given a path**, so the chain for all runtime facts terminates nowhere. This is the load-bearing dangling reference: every stale observation in §3 is excused by pointing at a file that does not exist here.
4. ARCHITECTURE.md:93 → MODEL-BACKENDS.md (profiles) and MODEL-BACKENDS.md:12 → SKIM-STAGE.md → SKIM-STAGE.md:3 → PLAN-skim-stage.md. Terminates correctly, but ARCHITECTURE.md's own "six roles" contradicts its own referent (C3).

**docs/SPEC-INDEX.md vs actual `.kiro/specs/`:**
`ls .kiro/specs/` = 13 directories. SPEC-INDEX.md's table (lines 13-24) lists **12** and covers all 12 tracked families correctly by name. The 13th, **`direct-api-backends`** (untracked, `?? .kiro/specs/direct-api-backends/`), is discussed in prose at SPEC-INDEX.md:28 but **absent from the table**, and the prose about it is factually inverted (§1.1). SPEC-INDEX.md:9's "all 36 tracked specification documents across 12 families" matches tracked files (12 × 3) but the directory listing is now 13 families. No spec directory is listed that does not exist.

---

## 6. TERMINOLOGY — one concept, many names

| Concept | Names found (doc/code:line) | Recommend |
|---|---|---|
| The 4-reviewer accept/reject step | "review vote" START-HERE.md:33 · "Four reviewer calls" ARCHITECTURE.md:19 · "reviewers" ARCHITECTURE.md:42 · "quorum" START-HERE.md:64, consensus.py:3,12 · "BFT"/"Byzantine" consensus.py:3,12, SPEC-INDEX.md:33, spec dir `byzantine-consensus-panel` · "consensus" consensus.py, MODEL-BACKENDS.md:49 · "panel" prompts/panel.py, PLAN-panel-acceptance.md · "seats" MODEL-BACKENDS.md:49, revision.py:9 · "evaluators" backends.toml:62, dream_cycle_candidates.evaluator_details · "Tier 1 metrics" VALIDATION.md:25 | **"panel"** for the group, **"consensus"** for the 3-of-4 rule, **"seat"** for a member. Retire *BFT/Byzantine/quorum* from live docs (all three docs already concede it is "an analogy, not a guarantee" — START-HERE.md:64, ARCHITECTURE.md:89, SPEC-INDEX.md:33; saying so three times is itself the duplication). Spec dir name can stay, it is history. |
| Extracting durable claims from one conversation | "distillation" START-HERE.md:24 · "distiller" START-HERE.md:26,60 · "Chat distillation" MODEL-BACKENDS.md:13 · "distill" job/`scripts/distill_sessions.py` · "chat-extract" plist + `scripts/chat_extract.py` · "extract" HYBRID-CHAT-EXTRACTION.md · "redistill" `redistill_capped.py` | **"distillation"** (noun), **"distiller"** (component). Rename the `chat-extract` job to `distill-chat` or document why two separate jobs exist — currently `distill` (05:00) and `chat-extract` (02:30) are distinct plists with overlapping names and no doc explaining the split. |
| The cheap pre-Explorer pass | "skim" (dominant: resolver.py:72 `SKIM_ROLE`, SKIM-STAGE.md, backends.toml:19) · "Step 3.5" orchestrator.py:107 · "triage"/"pre-filter" (RESEARCH-skim-model.md framing) · "one structured record per arriving document" MODEL-BACKENDS.md:12 | **"skim"** — already consistent in code. Keep "Step 3.5" only as a pipeline position, not a synonym. |
| Units of new input | "arrivals" orchestrator.py:377, backends.toml:19, allocation.py `render_arrivals_inventory` · "slices" ARCHITECTURE.md:86, backends.toml:117, migration `012_dream_cycle_slices` · "channels" CAPTURE-COMPONENTS.md:16, PLAN-capture-repair.md · "source_type" ARCHITECTURE.md/glossary · "parents"/"chunks" START-HERE.md:31 · "window" orchestrator.py:382 | These are **three different things wrongly blurred**: **channel** = a capture source; **arrival** = one parent document new in the run's window; **slice** = an Explorer-selected group of memories handed to a Thinker. Define all three in INTERFACES.md and use them strictly — ARCHITECTURE.md:86 currently says "Prompts suggest up to five slices" where the skim operates on arrivals. |
| Lexical search | "full-text" ARCHITECTURE.md:77 · "ts_rank/tsvector" ARCHITECTURE.md:77, SPEC-INDEX.md:34 · "BM25" (disclaimed) AGENTIC-RETRIEVAL-PLAN.md:5, SPEC-INDEX.md:22,34, ARCHITECTURE.md:77 | **"PostgreSQL weighted full-text ranking (`ts_rank`)"**. BM25 is disclaimed in 4 places — delete the disclaimers along with the stubs. |
| Confidence | "Bayesian confidence" (disclaimed) START-HERE.md:66, SPEC-INDEX.md:32, CAPTURE-COMPONENTS.md:51, ARCHITECTURE.md:107 · "effective_confidence" CAPTURE-COMPONENTS.md:51 | **"imported confidence snapshot"**. Disclaimed in 4 places — consolidate to one, in CAPTURE-COMPONENTS.md. |
| Profile-selecting env var | `SECOND_BRAIN_PROFILE` resolver.py:3, MODEL-BACKENDS.md:35 · "selected profile" ARCHITECTURE.md/START-HERE · "per-machine profiles" resolver.py:3 vs role-diverse `bedrock`/`hybrid`/`controlled` which are **not** per-machine | Env var naming is consistent ✅. Drop "per-machine" from resolver.py:3 and backends.toml:1 — 3 of 5 profiles are cost/routing variants, not machines. |
| Cap env vars | `SECOND_BRAIN_RUN_COST_CAP_USD` · `SECOND_BRAIN_SKIM_CAP_USD` · `SKIM_SPEND_CAP_USD` (constant) · `SKIM_RUN_CAP_SHARE` · "run cap"/"sub-cap"/"spend cap"/"hard stop"/"cost envelope" (PLAN-panel-acceptance.md:292) | Names are consistent ✅; only the **values** conflict (C6). Standardise prose on "run cap" / "skim sub-cap". |
| Quick Desktop | "Quick Desktop" · "QD" · `~/.quickwork` · "Quick" qd_profile.py:3 · `quick_desktop_*` source_types | **"Quick Desktop (QD)"** on first use per doc. |

## 7. NON-INCLUSIVE LANGUAGE

**None found.** Zero hits for master/slave/whitelist/blacklist across docs/*.md, docs/user-guide/*.md, README.md, scheduling/README.md, src/, config/. No replacements needed. (Git's default branch here is `main`, not `master` — `git log` confirms `local main`.)

---

## Top 6 fixes, ranked by harm

1. **config/backends.toml:104-113 → promote to MODEL-BACKENDS.md + START-HERE.md.** Default profile is `laptop` (all-Kiro); Kiro 2.22 ignores `--model`; "Auto" was observed serving `claude-fable-5.1`, labelled not-for-PII, against the personal corpus. This safety reasoning is buried in a TOML comment. **Highest harm.**
2. **docs/START-HERE.md** — strip the 2026-09-10 "Current local observation" block; re-pin from `7bf8d41` to `6b8c2d2`; fix adapters (3→4), profiles (2→5), roles (6→7), plists (12→18), migrations (005→014); add INTERFACES.md to the component map.
3. **docs/ARCHITECTURE.md** — add Step 3.5 skim and the grounding-audit/revision stage to the mermaid flow and the numbered walkthrough; "six roles" → seven.
4. **docs/OPERATIONS.md:56-71** — regenerate the schedule table from the 18 plists; fix dream-cycle to Mon/Wed/Fri 02:00; document the **$9** effective cap.
5. **docs/CAPTURE-COMPONENTS.md:23** — delete the "no native Codex/Outlook collector" denial; document the 4 live channels; record `quick_desktop_doc` retired.
6. **Delete the 27 redirect stubs** and archive the 4 PLAN-*.md + RESEARCH-skim-model.md. This removes ~33 orphans and shrinks the doc surface from ~50 files to ~13 with content.

---

## What I could NOT verify, and why

1. **Which launchd jobs are currently enabled/disabled.** START-HERE.md:44's "six pipeline jobs were disabled" needs `launchctl print-disabled gui/$UID` or `~/Library/Preferences/com.apple.launchd.*/overrides.plist`. I verified only *installed* plist presence (17) vs repo templates (18). The count claim is false regardless, but the disabled *set* is unconfirmed.
2. **Actual DB `schema_migrations` ledger rows.** I verified 15 migration *files* (000-014) and that code depends on 013/014. Whether the DB table now lists 000-014 needs a live query; per production-safety rules I ran no DB commands.
3. **Live capture-channel health.** I relied on commit message `5177b17` ("all channels healthy") plus module/plist existence. I did not run monitors or inspect `logs/`.
4. **Credential/Kiro-login state.** START-HERE.md:44's "Kiro not logged in / AWS credentials expired" would require `aws sts get-caller-identity` and a Kiro probe — live-credential operations I did not perform.
5. **Whether `origin/main` and local `main` have further unported divergence.** I confirmed the port direction via `5386c2b`/`b6d4484`; I did not fetch or diff the remote.
6. **VALIDATION.md:81's "all 41 original docs/readmes"** — counting archive contents conflicts with the instruction not to audit archive content. Flagged as ratio-stale only.
7. **Anchor validity exhaustively.** I resolved every link *target file* (zero broken) and spot-checked the five most load-bearing anchors; I did not enumerate every `#fragment` against every heading.
8. **The "local operational inventory"** that START-HERE.md:44 and :52 defer all runtime facts to. It is not in the repo and no path is given, so I could not check any claim it supposedly backs — see §5 chain 3.

RELEVANT FILES (ranked by importance):
  1. docs/START-HERE.md — Designated entry point and self-declared "Authority: current", yet its adapter list (3 vs 4), profile list (2 vs 5), role count (6 vs 7), plist count (12 vs 18) and migration ledger (005 vs 014) are all contradicted by code; also omits INTERFACES.md from its component map and defers all runtime facts to a document not present in the repo.
  2. config/backends.toml — Ground truth for profiles/roles/models and the sole location of the Kiro --model regression and Fable-PII routing hazard (lines 104-113); also holds a third conflicting cap value ($6, line 119).
  3. src/backends/resolver.py — DEFAULT_ADAPTERS (lines 42-48) disproves START-HERE.md:19 and SPEC-INDEX.md:28; VALID_ROLES/SKIM_ROLE (57-75) and _validate_profile (78-104) disprove every "six roles" claim.
  4. docs/OPERATIONS.md — Canonical runbook: asserts 12 plist templates (actual 18), omits 6 rows, states dream-cycle as "Daily 12:00" when plists say Mon/Wed/Fri 02:00, and never records the $9 effective run cap.
  5. docs/MODEL-BACKENDS.md — The healthiest canonical doc and the in-tree contradiction of layer A (correct on 4 adapters, 5 profiles, 7 roles, skim, panel acceptance); residual defects are the $5 cap and the missing Fable/PII warning on the bedrock profile row.

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

