# 08a — Specs vs Code, Track A (2026-09-24, HEAD 6b8c2d2)

Scope: six spec families under `.kiro/specs/`: dream-cycle, byzantine-consensus-panel,
model-backend-adapters, direct-api-backends, claude-code-stream-json-probe-fix,
data-layer-decomposition.

Method: read requirements.md / design.md / tasks.md (or bugfix.md) per family, then grep `src/`
for every named module/function/class and every checked task. Findings carry severity
(S1 = spec and code disagree on a user-visible behaviour or a "done" task is unimplemented;
S2 = stale name / stale checkbox / numeric drift; S3 = cosmetic), `path:line`, evidence, fix.

Sections are appended one per family as each finishes.



## 1. dream-cycle

Files read: `requirements.md` (268 l), `design.md` (1122 l, headers + all named symbols), `tasks.md` (293 l).
Both requirements.md and design.md carry a header note saying the panel is now 4 evaluators / BFT and
that DEFERRED, two-strike and Strategy #11 were removed — but the body text and every checked task
still describe the 3-evaluator design. The notes are accurate as far as they go; the drift below is
everything the notes do *not* mention.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S1 | tasks.md 10.1 "Evaluator crash/timeout: treat as REJECT"; 10.2 "Test evaluator timeout treated as REJECT" | Checked as done | Contradicted by requirements.md 16.3 in the *same family* ("MUST NOT be recorded as REJECT … abort loudly") and by code: `src/dream_cycle/orchestrator.py:606-649` `_invoke_evaluator_safe` retries 3× then raises; `:487-496` sets `panel_aborted=True`. Test `tests/test_dream_cycle.py:1215 test_unrecoverable_evaluator_aborts_run` asserts the opposite of task 10.2. | Rewrite tasks 10.1/10.2 to match req 16.3; the requirement was updated, the tasks were not. |
| 2 | S1 | requirements.md 13.1, tasks.md 3.1, design.md:1102-1118 | Agents run as `kiro --no-interactive` with `--system-prompt` / `--mcp-config` flags; "no direct LLM API calls" (tasks.md Notes) | `src/backends/kiro.py:41,185-186`: `kiro-cli chat --no-interactive --agent <name>` with a generated agent config; no `--system-prompt`/`--mcp-config` flags. `src/backends/resolver.py:42-47` registers 4 adapters incl. `bedrock` (direct Converse API). `config/backends.toml:32` `default_profile = "laptop"` (Kiro), but `scripts/jobs/dream_cycle_scheduled.sh:32` forces `controlled` (Explorer on Claude Code, everything else direct Bedrock). The scheduled run therefore never touches Kiro. | Requirement 13 needs a rewrite to "role → backend via `config/backends.toml`"; note that the effective default for the scheduled job is `controlled`, not `laptop`. |
| 3 | S1 | requirements.md 3.1, 11.2; design.md:1085,1098; tasks.md 8.1 | "11 named strategies", #11 = deferred evidence gathering; note says #11 removed | `src/prompts/explorer.py:7-77`: 12 strategies; #11 is ELABORATIVE RE-INTERROGATION, #12 is ARRIVALS SYNTHESIS. Neither is in any spec. `src/dream_cycle/allocation.py:60` `ARRIVALS_STRATEGY = "arrivals_synthesis"` is *enforced* (orchestrator adds a guaranteed arrivals slice, `:782` `_allocate_attention`). | Update req 3.1 to the 12 strategies and add the breadth/arrival-anchoring budget (explorer.py `BUDGET_SECTION`) as a requirement — it is a hard rule the orchestrator enforces and no spec states it. |
| 4 | S1 | requirements.md 1 / tasks.md 6.4 pipeline: Explorer → Thinker → 3 evaluators | Pipeline shape | Code has two stages no spec mentions: **skim** (`orchestrator.py:400` `_skim_arrivals`, Step 3.5, `src/skim/*`, role `skim` in `resolver.py:66`) and **grounding audit + one revision** before the panel (`orchestrator.py:466` `_ground_candidate`, `:1116-1200`; `src/prompts/revision.py`). A candidate emptied by revision is stored REJECTED with four synthetic `"REJECT"` votes and identical reasoning (`orchestrator.py:470-477`) — i.e. fabricated panel verdicts, contrary to the spirit of req 16.3 and to req 2.4 (independent evaluators). | Add skim + grounding to the spec (they are documented only in `docs/PLAN-skim-stage.md` / `docs/PLAN-panel-acceptance.md`). Store the grounding-rejection as a distinct `final_verdict` (e.g. `REJECTED_GROUNDING`) rather than four cloned votes. |
| 5 | S1 | requirements.md 1.4, 2.1-2.3; design.md "BFT" note; `consensus.py:3-4` docstring "Lamport 3f+1, BFT quorum" | Byzantine fault tolerance | `src/dream_cycle/consensus.py:26-27` is a plain count: `accept_count >= 3`. No signed messages, no rounds, no equivocation handling — it is a 3-of-4 supermajority over independent votes. Calling it BFT is inaccurate; a single "Byzantine" (colluding/bad) evaluator can still flip a 3-1 to 2-2 → REJECT, so f=1 tolerance applies only to *accept*-side faults. | Rename in docstring/spec to "3-of-4 supermajority (tolerates one dissenting or omitted vote)". See §2 for the same issue in the BFT spec. |
| 6 | S2 | requirements.md 4.1; tasks.md 6.2; design.md:277 `get_recent_rejections(n_cycles=3)` | Feedback from last **3** cycles | `src/dream_cycle/feedback.py:17` `FEEDBACK_CYCLES = 2`; `:44-46` passes 2. Also adds two sources not in spec: `get_accepted_dissents`, `get_user_rejections` (`dream_cycle_db.py:191,232`). Bounded to 4000 chars (`:21`). | Update req 4.1/4.2 to 2 cycles + bounds; the docstring at `feedback.py:31-38` explains why. |
| 7 | S2 | requirements.md 13.2 "timeout defaulting to 300 s" | | `src/backends/kiro.py:113` default 300 is preserved, but the orchestrator overrides for Explorer/Thinker: `orchestrator.py:109-110` `EXPLORER_TIMEOUT_S`/`THINKER_TIMEOUT_S` default **1200**. | State per-role defaults in the spec. |
| 8 | S2 | tasks.md 13.2 [x] "Weekly Sunday 4AM"; requirements.md 19.1; design.md:1096 | Schedule | `scheduling/com.second-brain.dream-cycle.plist:17-37`: Weekday 1/3/5 at Hour 2 → Mon/Wed/Fri 02:00. | Update spec; note the collision with `backup.sh` at 02:00 flagged in the 09-23 audit. |
| 9 | S2 | tasks.md 13.1 [x] `scripts/job_wrapper.sh`; 13.3 [x] `scripts/golden_queries.py`; 6.1 [x] `src/dream_cycle.py`; design.md:1045-1071 module tree | Paths | Actual: `scripts/jobs/job_wrapper.sh`, `scripts/eval/golden_queries.py`, `src/dream_cycle/` package (8 modules: allocation, consensus, digest, feedback, ledger, orchestrator, packet, storage). `src/agent_invoker.py` is a 34-line shim re-exporting `KiroInvoker as AgentInvoker` (`src/agent_invoker.py:14-22`). | Refresh the module tree in design.md. |
| 10 | S2 | requirements.md 19.5; tasks.md 13.1 | Exit codes "0 success, 1 partial failure, 2 total failure" | `scripts/dream_cycle_run.py:94-104` `exit_code_for`: only 0 or 2; exit 1 is never produced (docstring explicitly says so). | Drop "1 partial failure" from spec. |
| 11 | S2 | requirements.md 18.1 "deferred-to-accepted conversion rate"; 18.4; 14.2 `deferred_twice_rejected` column | Tier-1 metric / column | `tests/test_dream_cycle_db.py:1047,1063` assert `deferred_to_accepted_rate` is **absent** from `get_tier1_metrics`. Column still exists in `migrations/003_dream_cycle.sql:46` but is written by nothing in `src/`. `dream_cycle_db.py` has no `get_deferred_candidates` (tasks.md 2.2 [x]). | Mark 18.1-part/18.4/2.2 as removed; consider a migration dropping `deferred_twice_rejected`. |
| 12 | S2 | requirements.md 6.5 "exactly one verdict … ACCEPT or REJECT with non-empty reasoning" | | `orchestrator.py:1108-1114`: `verdict=output.get("verdict", "REJECT")`, `reasoning=output.get("reasoning", "")`. A parsed-but-malformed evaluator reply (missing `verdict`) silently becomes a REJECT with empty reasoning — a fabricated vote, which req 16.3 forbids for the crash case and 6.5 forbids for the empty-reasoning case. | Raise `ValueError` when `verdict` ∉ {ACCEPT, REJECT} or reasoning is empty so `_invoke_evaluator_safe` retries. |
| 13 | S2 | design.md:1074 "Files modified: `src/db.py` — add `expired_at` to relationship queries" | | `grep expired_at src/db.py` → none. Only `dream_cycle_db.py:849-850` (writer) and `express.py:242` (one reader) use it; `memory_graph`/relationship reads in `db.py` ignore expiry. | Either implement or strike the line. |
| 14 | S2 | requirements.md 11.4 "limit the Thinker to 1-2 candidates" (session_start) | | `orchestrator.py:112` `MAX_CANDIDATES_PER_SLICE=3` for all other run types; `:437` `cap = 2 if run_type == "session_start"`. Session-start cap is 2 (upper bound honoured); other modes cap at 3 per slice while the Thinker prompt asks for ≤2 — spec never states the 3. | Document the 2/3 caps. |
| 15 | S2 | requirements.md 9 / 16.1: Explorer failure → "complete run with zero candidates" | | `orchestrator.py:409-418`: for `scheduled`/`session_start` an Explorer crash now **continues** with the guaranteed arrivals slice. Only post_learn/user_triggered abort. `test_explorer_failure_completes_run_with_zero_candidates` (`tests/test_dream_cycle.py:1121`) still exists — check which run_type it uses. | Update req 16.1. |
| 16 | S3 | requirements.md Glossary/Intro "74K+ memories", "MCP Server 8 tools" (design.md:49) | Stale figures | `src/mcp_server.py` exposes 10 tools (memory_brief, memory_learn, memory_recent_activity added since). | Cosmetic. |
| 17 | S3 | requirements.md 12.5 digest "generated, accepted, deferred, rejected"; design.md:776 `generate_digest(run_id, accepted, deferred, …)` | | `src/dream_cycle/digest.py:16` `generate_digest(run_id, accepted, rejected)`; `DreamCycleResult` (`src/models.py:13-24`) has no `candidates_deferred`. Header note already flags the signature change; the requirement text was not updated. | Cosmetic. |

Stale checkboxes (unchecked-but-implemented): none — every task is `[x]`. Done-but-not-implemented: 10.1/10.2 (semantics reversed), 13.2 (schedule), 13.3/13.1 (paths), 7.1/7.2 and 2.2 (implemented then removed; still `[x]`).

Contradiction inside the family: requirements.md 16.3 vs tasks.md 10.1 (row 1). Cost cap: this spec never mentions a cap; see §3 model-backend-adapters for the $5/$6/$9 split.



## 2. byzantine-consensus-panel

Files read: `requirements.md` (162 l), `design.md` (521 l), `tasks.md` (111 l). This spec is the most
faithfully implemented of the six: the 4-role panel, `tally_consensus`, migration 004, digest
annotations and feedback sections all exist as described. The problems are the label "BFT", a claimed
veto that does not exist, and a handful of removed-but-"retained" artefacts.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S1 | Header note on all three files (added 2026-09-10): "the repaired consensus also has a **material-objection veto**"; `docs/SPEC-INDEX.md:13` "Repaired code additionally vetoes material objections" | A blocking issue from any evaluator vetoes acceptance | `src/dream_cycle/consensus.py:26-27` is `accept_count >= 3` only. `EvaluatorVerdict.has_blocking` (`src/models.py:75-77`) is referenced by **nothing** in `src/` (`grep -rn has_blocking src/` → definition only; used in `tests/test_grounding.py:59,73`). `blocking_issues` are stored (`orchestrator.py:527`) but never consulted for the verdict. A 3-ACCEPT/1-REJECT candidate whose lone REJECT lists a blocking defect is still ACCEPTED. | Either implement the veto in `tally_consensus` (and in the truth table at design.md:383) or delete the claim from the three headers and SPEC-INDEX. This is the reconciliation note itself being wrong. |
| 2 | S1 | requirements.md Intro/Glossary "Byzantine_Fault_Tolerance", "BFT_Quorum … Castro & Liskov 1999"; design.md:18-30; `consensus.py:3-4,12-13` | Panel is Byzantine fault tolerant | The header disclaimer already concedes the analogy "does not establish … independent failure modes". Concretely: no message authentication, no rounds, no view-change; verdicts are four independent scalar votes. `resolver.py`/`backends.toml` put **the same model** (`gpt-6-astra`) on two of four seats in `controlled`, `bedrock` and `hybrid` (`config/backends.toml:79,81,100,102,127,129`) — a correlated failure of that model is f=2, beyond the claimed f=1. | Rename to "3-of-4 supermajority"; drop Lamport/PBFT citations from code docstrings. Keep the rationale prose as history. |
| 3 | S2 | requirements.md 6.5 "`DreamCycleResult` SHALL set `candidates_deferred` to 0 (field retained)"; design.md:178; tasks.md 4.1 [x] "ensure candidates_deferred is always 0" | Field retained | `src/models.py:13-24` `DreamCycleResult` has **no** `candidates_deferred` field. Only the SQL literal `candidates_deferred = 0` in `dream_cycle_db.py:65` survives. | Update 6.5 to "field removed from the dataclass; column kept". |
| 4 | S2 | tasks.md 5.7 [x] "Deprecate `get_deferred_candidates()` and `mark_deferred_twice_rejected()` — add deprecation docstring notes"; design.md:322-324 "retained for backward compatibility" | Retained with deprecation notes | Neither function exists in `src/dream_cycle_db.py` (full `^def` list checked, 33 functions). `grep -rn get_deferred_candidates src/ tests/` → nothing. Task is checked but its described outcome (retain + annotate) is not what happened (deleted). | Reword 5.7 to "removed". |
| 5 | S2 | design.md:180 "### 3. `src/prompts.py` — Methodologist Prompt Template" | Module path | Actual module is `src/prompts/panel.py:131-149` (`_ROLE_DESCRIPTIONS`, `_ROLE_CRITERIA`). tasks.md 2.1 has the correct path; design.md does not. | Fix path. |
| 6 | S2 | design.md:347 "### 9. `docs/DREAM-CYCLE-DESIGN.md`"; tasks.md Notes "`docs/DREAM-CYCLE-DESIGN.md` was already updated"; dream-cycle/design.md:332,360 "source of truth" | Document exists | `ls docs/DREAM-CYCLE-DESIGN.md` → no such file; not in `docs/archive/2026-09-10/` top level either (contains `docs/`, `scheduling/` subdirs — not descended). Requirement 10 (7 criteria) therefore has no verifiable target. | Locate/restore or point Requirement 10 at `docs/DESIGN-DECISIONS.md`. |
| 7 | S2 | requirements.md 3.1 / design.md:135-147 pipeline: Thinker → 4 evaluators | Panel is the only judging step | `orchestrator.py:466` `_ground_candidate` runs a graded audit by the `skeptic` seat's backend (`GROUNDING_AUDIT_ROLE`, `:111`) and a revision **before** the four votes; the skeptic then votes on the revised text. The skeptic's backend therefore influences the candidate twice — not independent in the sense of req 3.4. | Document the grounding stage in this spec and consider a dedicated `auditor` role. |
| 8 | S2 | design.md:368-381 verdicts dict "8 keys"; tasks.md 4.1 [x] "8-key verdicts_dict"; Property 4 | Exactly 8 keys | `orchestrator.py:516-530`: 9 keys — adds `evaluator_details` (migration 013 `evaluator_details` JSONB). Any test asserting `len(verdicts_dict) == 8` would now fail. | Update spec to 9 keys / describe `evaluator_details`. |
| 9 | S2 | requirements.md 1.6, Glossary "binary ACCEPT or REJECT … with reasoning text" | Verdict shape | Panel output schema is now graded: `verdict`, `score` (0-100), `blocking_issues[]`, `advisory_issues[]`, `reasoning` (`src/dream_cycle/packet.py:51-66` `EVALUATOR_VERDICT_SCHEMA`; `src/prompts/panel.py:196-208` `_VERDICT_V2`). The score is stored and shown but does not affect consensus. | Add score/issue lists to Glossary and req 1.6; state explicitly that only `verdict` counts. |
| 10 | S3 | tasks.md Notes "The existing 204 tests must continue to pass" | Test count | Stale; not re-counted (no pytest allowed). Cosmetic. | Drop the number. |

Stale checkboxes: 5.7 (described outcome ≠ actual), 4.1 sub-claim on `candidates_deferred`. Everything else checked is present.

Contradictions with dream-cycle spec: dream-cycle/requirements.md 2, 3.1, 10, 11 still say 3/3, DEFERRED, 11 strategies (acknowledged in its header). dream-cycle/tasks.md 10.1 ("treat evaluator crash as REJECT") directly contradicts this spec's req 3.2 and design.md:462-466 (omission fault must not become a REJECT) — the two families disagree on the same behaviour and only this one matches the code.



## 3. model-backend-adapters

Files read: `requirements.md` (535 l, 23 requirements — header + all SHALL lines), `design.md` (391 l),
`tasks.md` (116 l). The two adapters, the `AgenticCliInvoker` base, the probe helper and resolver
registration all exist (`src/backends/agentic_cli.py`, `claude_code.py`, `codex.py`, `mcp_probe.py`,
`resolver.py:42-47`) with matching tests (`tests/test_claude_code.py`, `test_codex.py`, `test_mcp_probe.py`,
`test_resolver.py`, `test_backends.py`). Drift is in the world around them.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S1 | requirements.md 2.3 "request JSON output via `--output-format json`"; design.md:114-165; tasks.md 4.1 [x] "`--output-format json`" | Claude Code envelope format | `src/backends/claude_code.py:212,220`: `--output-format stream-json --verbose`; `_envelope_from` (`:418-430`) parses a JSONL event list and picks the terminal `{"type":"result"}` event. This is the change made by the `claude-code-stream-json-probe-fix` spec (§5) — that spec updated its own docs but **not** this one, so the two specs disagree on the flag. | Update req 2.3 / design §2 to `stream-json --verbose`, citing the probe-fix spec. |
| 2 | S1 | tasks.md 7 [x] "Codex (manual): write a smoke-test checklist (`docs/` or the spec dir)"; requirements.md 22.5; tasks.md 8 [x] "note the Codex smoke-checklist location" in MODEL-BACKENDS.md | Checklist exists | `grep -rln smoke docs/*.md .kiro/specs/model-backend-adapters/` → only the three spec files themselves; no checklist file; `docs/MODEL-BACKENDS.md` has no "smoke"/"checklist" string. Task 7 is checked but its Codex half was not delivered. | Uncheck 7 (Codex half) or write the checklist. |
| 3 | S1 | design.md:13-15 "Only `kiro` has a registered adapter today"; tasks.md Notes "Out of scope (deferred): direct-API adapters (Bedrock/…), per-role model selection, evaluator-panel diversity"; Architecture "After" diagram `DEFAULT_ADAPTERS = { kiro, claude_code, codex }` (3 adapters) | 3 adapters, no direct API, no per-role models | `src/backends/resolver.py:42-47`: **4** adapters incl. `"bedrock": BedrockInvoker` (`src/backends/bedrock.py`, 673 l, on `DirectApiInvoker` in `direct_api.py`). `config/backends.toml` has 5 profiles with per-role models and a deliberately mixed panel (`:76-78`). Every "deferred/out-of-scope" item in this spec's Notes is now shipped (by the `direct-api-backends` spec, §4) and this spec was not annotated. | Add a "superseded by direct-api-backends for the Bedrock adapter" note; fix the diagram to 4 adapters. |
| 4 | S2 | requirements.md 2.7 / tasks.md 4.1: `CLAUDE_CLI` "default `claude`"; req 2.8 "SHALL NOT depend on the ASBX Cecelia wrapper" | Default binary | `src/backends/claude_code.py:86`: default is `~/.toolbox/bin/claude` — the ASBX toolbox install path, not bare `claude`. The command *surface* is still public-flags-only, so 2.8 is honoured in spirit, but the default path bakes in the ASBX build the spec says to isolate. | Default to `claude` (PATH) and let the env override pick the toolbox binary; or document the deviation. |
| 5 | S2 | requirements.md 20.2 "pass all approximately 685 existing tests"; tasks.md 1 [x] "record the passing count" | Baseline number | No recorded count anywhere in the spec dir (task 1 says "record", nothing is recorded). Byzantine spec says 204, this says 685 — the number is a moving target and never verified here. | Remove counts from requirements. |
| 6 | S2 | requirements.md 19.4 / tasks.md 6: Explorer on `claude_code`/`codex` "passes `assert_backend_supports_role`" — guard described as Explorer-only | Guard scope | `src/backends/base.py:204-215`: guard now has a **second** rule — `role == "skim"` rejects any `supports_mcp=True` backend. Also `BackendCapabilities` has a 4th field `structured_output` (`base.py:141-143`) absent from design.md Data Models (`:253-266`). | Update Data Models and the guard description. |
| 7 | S2 | design.md:60 "`config/backends.toml` already ships a `mini` profile selecting `claude_code`"; requirements.md Glossary Laptop_Profile "(all-Kiro) default" | Kiro default is operative | `backends.toml:32` `default_profile = "laptop"` is true, but every scheduled job overrides it: `scripts/jobs/dream_cycle_scheduled.sh:32`, `weekly_digest.sh:36`, `codex_capture.sh:38` all export `controlled`. `MODEL-BACKENDS.md:27` still labels `laptop` "(default)". The default-preserving guarantee (Req 20) protects a profile production never runs. | State in Req 20 that `laptop` is the *test* default and `controlled` the *operational* default. |
| 8 | S2 | requirements.md 19.2 / tasks.md 6 [x]: `codex` registered and resolvable | Reachability | Registered (`resolver.py:45`) but no profile in `config/backends.toml` selects `backend = "codex"`; the one job that intended to (`scripts/jobs/codex_capture_canary.sh:25` → `SECOND_BRAIN_PROFILE=codex_local`) names a profile that does not exist → `default_resolver` raises `KeyError` (`resolver.py:232-236`). 620-line adapter reachable only from tests (already flagged in 09-23 audit C-2/C-3; still true at 6b8c2d2). | Add a `codex_local` profile or remove `codex` from `DEFAULT_ADAPTERS`. |
| 9 | S3 | design.md Overview "No abstraction changes, no orchestrator changes" | | Orchestrator gained `_role_has_tools` (`orchestrator.py:174-179`) reading `BACKEND_CAPABILITIES.supports_mcp` to switch between MCP and context-packet delivery, and passes `schema=` to Direct-API invokers (`:1087`). True at the time; false now. | Historical — add a dated note. |

Stale checkboxes: 7 (Codex checklist missing), 1 (baseline count never recorded), 8 (doc note about checklist location absent).

Cost cap (asked for explicitly): this spec never mentions a cap; the split is `src/backends/cost.py:39` `DEFAULT_RUN_COST_CAP_USD = 5.0`, `config/backends.toml:71` "default 5.00", `:119` "under the $6/run cap", `docs/MODEL-BACKENDS.md` "Spend guardrails" "(default 5.00)", and the operative value `scripts/jobs/dream_cycle_scheduled.sh:33` `SECOND_BRAIN_RUN_COST_CAP_USD=9`. Four values; only the shell script is what runs. Belongs to §4 direct-api-backends where the cap is specified.



## 4. direct-api-backends

Files read: `requirements.md` (503 l, 14 requirements), `design.md` (449 l), `tasks.md` (192 l),
`tasks.meta.json`. This is the one family with **unchecked tasks that are implemented** — the
checkbox state is the inverse of the other five. This spec also has no "Historical" header,
although it is the most out-of-date on cost and governance.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S1 | requirements.md 13.2 "THE feature SHALL NOT change the default routing (Laptop_Profile stays all-Kiro), so **no real memory content reaches Bedrock** until an operator selects a `bedrock`-using profile"; 10.1 | Laptop = no Bedrock content | `config/backends.toml:42`: the `laptop` profile's `skim` role is `backend = "bedrock"` (Nova 2 Lite) and — per `:25-26` — "sends full document text to its model". `orchestrator.py:400` runs the skim on every run before the Explorer. So on the default profile every parent-level arrival's full text goes to Bedrock. The governance gate in 13.2 is bypassed by design and the requirement was never amended. | Rewrite 13.2: the skim is a deliberate Bedrock-only stage in all profiles; the *agent* roles stay Kiro on `laptop`. |
| 2 | S1 | tasks.md 4 `[ ]`, 4.4 `[-]`, 4.5 `[~]`, 5 `[~]`, 6 `[~]`, 7 `[~]` | Not done / in progress | All implemented: `_map_exception` `src/backends/bedrock.py:628-675` maps ReadTimeout/ModelTimeout→TimeoutError, Throttling/ServiceUnavailable/InternalServer/AccessDenied→RuntimeError (4.4); `tests/test_bedrock.py` 30 tests in 7 classes incl. `TestExceptionMapping` and `tools=True` rejection (`:367`) (4.5); `resolver.py:46` `"bedrock": BedrockInvoker` + `tests/test_resolver.py` (5); `docs/MODEL-BACKENDS.md` "Bedrock request rules (live-verified 2026-09-14)" (6/7). `tasks.meta.json` records execution history for these tasks. **Stale checkboxes — the reverse defect.** | Check 4, 4.4, 4.5, 5, 7. See row 3 for 6. |
| 3 | S2 | tasks.md 6 "Record it as **Part C** of `docs/MODEL-BACKENDS-VERIFICATION.md` … document the required IAM permissions" | Verification doc | `docs/MODEL-BACKENDS-VERIFICATION.md` is now a 7-line stub redirecting to the archive (`archive/2026-09-10/docs/MODEL-BACKENDS-VERIFICATION.md`, not descended). No "Part C", no `bedrock:InvokeModel`/`GetInferenceProfile` string anywhere in `docs/*.md` top level. The live verification is asserted only in a `MODEL-BACKENDS.md` heading. | Point task 6 at the archive path or restore the IAM section in MODEL-BACKENDS.md. |
| 4 | S1 | requirements.md — **no spend-cap requirement at all** (`grep -i spend|cost|cap|kill` → one incidental hit at :159); tasks.md Notes "Out of scope: … a blessed mixed profile (so a whole live dream-cycle run on Bedrock is gated …)" | Cost control out of scope | The feature that actually gates metered spend — `src/backends/cost.py` (`SpendTracker`, `SpendCapExceeded`, `kill_switch_engaged`, `DEFAULT_RUN_COST_CAP_USD = 5.0` at `:39`) — is wired through `orchestrator.py:181-205,296-300,318-326` and is the operative guardrail for the very backend this spec adds. It has **no spec anywhere** in `.kiro/specs/`. The cap's real value is `$9` (`scripts/jobs/dream_cycle_scheduled.sh:33`), while `cost.py:39` and `MODEL-BACKENDS.md` say `$5` and `backends.toml:119` says `$6`. | Add a Requirement 15 "Spend guardrails" here (or a new spec) naming one canonical constant; make `dream_cycle_scheduled.sh` read it rather than override it. |
| 5 | S2 | tasks.md Notes "Out of scope (deferred): … per-role routing and a blessed mixed profile … evaluator-panel model diversity" | Deferred | Shipped: `backends.toml` `bedrock`, `hybrid`, `controlled` profiles are mixed per-role; `:76-78` "Two model families sit on the evaluator panel on purpose". | Annotate as delivered. |
| 6 | S2 | design.md:175 `BedrockInvoker(… max_tokens=4096, max_retries=4 …)`; tasks.md 4.1 `max_tokens=4096` | Default output budget | `src/backends/bedrock.py:74` `DEFAULT_MAX_TOKENS = 32768` (env `BEDROCK_MAX_TOKENS`); `:81` legacy thinking budgets up to 32768. Eight-fold change because adaptive thinking is billed as output (`:21`). | Update design/task text. |
| 7 | S2 | design.md / tasks.md 4.1 constructor `BedrockInvoker(model, *, region, client, max_tokens, max_retries, system_prompt_delivery)` | Signature | `bedrock.py:132-141` adds `use_invoke_model`, `temperature`, `service_tier` (the last two via `ADAPTER_OPTIONS`, `resolver.py:51-53`). The `options = {…}` per-role table (`backends.toml:11-17`) is unspecified in any of the six families. | Add adapter-options to Requirement 4 or 9. |
| 8 | S2 | requirements.md 4 (request construction) / design.md:207 `inferenceConfig={"maxTokens"…}` — generic Converse | Per-model request shaping | `bedrock.py:400-460` `_additional_fields`: Claude 5 `thinking.adaptive`+`output_config.effort`, Claude 4 `budget_tokens`, OpenAI `reasoning.effort`, Nova `reasoningConfig`; `:193-200,249-253` three structured-output modes (forced tool / auto tool / `outputConfig.textFormat`); `serviceTier`. Live-verified rules are documented only in `MODEL-BACKENDS.md` "Bedrock request rules", not in the spec. | Lift the per-vendor table into design.md. |
| 9 | S3 | requirements.md 12.1 region "default `us-east-1`" | | `bedrock.py:152` matches. OK — recorded as verified. | — |

Stale checkboxes: 4, 4.4, 4.5, 5, 6, 7 (all implemented, marked not-done). Checked-but-missing: none.

Contradictions with the other families: model-backend-adapters tasks.md Notes says direct-API adapters are "out of scope (deferred)" while this spec ships them — neither cross-references the other. model-backend-adapters req 20 and this spec's req 10/13.2 both guarantee "Laptop_Profile stays all-Kiro"; the `skim` role (added 2026-09-20, `backends.toml:19-29`) makes both false.



## 5. claude-code-stream-json-probe-fix

Files read: `bugfix.md` (134 l), `design.md` (488 l, symbol grep + notes), `tasks.md` (165 l). The fix
itself is fully implemented and its checkbox state is accurate: `src/backends/claude_code.py:220`
emits `stream-json --verbose`; `_events_from` (`:443`), `_envelope_from` (`:418`, last `type=="result"`
event), `_extract_raw` returns `""` when no result event (`:416`); `_run_probe` passes
`events=self._events_from(...)` (`:338`); `MCPStartupProbe.instruction()` (`src/backends/mcp_probe.py:181-201`)
is the softened wording with `PROBE_NAME`/`TOOL`/`QUERY` retained; `tests/test_claude_code.py` (48 tests)
has JSONL fixtures and `test_no_result_event_raises_valueerror` (`:340`); exploration/preservation
tests exist as `tests/test_claude_code_stream_json_probe.py` (2) and `…_preservation.py` (11);
`tests/test_mcp_probe.py:185` `test_instruction_omits_injection_flagged_phrases`.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S2 | tasks.md 4 [x] "Update `docs/MODEL-BACKENDS.md` and `docs/MODEL-BACKENDS-VERIFICATION.md` to mark the agentic path fixed/verified: note the switch to `--output-format stream-json --verbose`, the terminal-result-event extraction, the event-stream probe confirmation, and the Finding-2 instruction rewording" | Docs updated | `docs/MODEL-BACKENDS.md:75` has one clause: "Claude uses stream-JSON with tool-result probes". None of the four listed items (flag, terminal-event extraction, event-stream probe, Finding-2 rewording) is documented; `MODEL-BACKENDS-VERIFICATION.md` is a 7-line redirect stub. Task checked; doc half not delivered. | Add a short "Claude Code agentic path" paragraph to MODEL-BACKENDS.md or uncheck the doc part of task 4. |
| 2 | S2 | bugfix.md Intro "recorded in `docs/MODEL-BACKENDS-VERIFICATION.md`, Finding 1 … build `2.1.178.386`"; design.md refs | Source evidence location | The findings now live only under `docs/archive/2026-09-10/docs/MODEL-BACKENDS-VERIFICATION.md` (per the stub's link; not descended). The spec's evidence trail points at a file that no longer holds it. | Repoint to the archive path. |
| 3 | S2 | bugfix.md 3.6 / tasks.md 4 / design.md:136,345 "814 passing" | Baseline count | Three specs give three baselines (204, ~685, 814) — none verifiable from the repo, none current. | Drop counts. |
| 4 | S2 | tasks.md Notes "The fix is contained to two files — `claude_code.py` … and `mcp_probe.py` — plus the `tests/test_claude_code.py` fixture rework" | Blast radius | Two extra test files were added (`tests/test_claude_code_stream_json_probe.py`, `…_preservation.py`) — fine — but the `instruction()` change also alters every **Codex** agentic prompt (`src/backends/codex.py` prepends the shared instruction). bugfix.md 3.5 acknowledges this; tasks.md Notes contradicts it by saying Codex is "unchanged". | Reword the Note. |
| 5 | S1 (cross-spec) | — | | The parent spec `model-backend-adapters` req 2.3 / design §2 / task 4.1 still mandate `--output-format json` (see §3 row 1). This bugfix spec changes the flag and never amends the parent, so the two specs now prescribe incompatible commands for the same adapter. | Amend model-backend-adapters req 2.3 with a pointer to this spec. |
| 6 | S3 | design.md "Single code path" decision: `stream-json` for both `tools=True` and `tools=False` | | Confirmed: `claude_code.py:220` is unconditional. OK. | — |

Stale checkboxes: 4 (doc half). Done-but-missing: none in code.



## 6. data-layer-decomposition

Files read: `requirements.md` (123 l, 10 requirements), `design.md` (716 l, grep for named symbols,
weights, export lists), `tasks.md` (82 l). The extraction happened and its tests exist
(`tests/test_search_properties.py`, Properties 1-6). But the spec froze a snapshot of `db.py`/`search.py`
that both modules have since outgrown, and the "exactly" clauses are now false while the property test
written for them passes — because the test checks a subset, not equality.

| # | Sev | Spec location | Claim | Code evidence | Fix |
|---|-----|---------------|-------|---------------|-----|
| 1 | S1 | requirements.md 3.1; design.md:200-204; tasks.md 7.2 [x] "Property 2 … `0.35×rrf + 0.20×content_overlap + 0.20×title_overlap + 0.15×recency + 0.10×length + type_boost + reinforcement`" | Rerank formula | `src/rerank_weights.py:19-27` `COEFFICIENTS = {rrf .30, overlap .18, title_overlap .18, context_overlap .10, recency .12, length .08, depth .05}`, `REINFORCEMENT_COEFF .03`, plus `MEM_CLASS_BOOST`, `PROJECT_PENALTY -0.15`, `SUPERSEDED_PENALTY -0.20`, `STALENESS_PENALTY_MAX -0.05` (`:32-37`). `src/search.py:19` imports these. Two signals (`context_overlap`, `depth`) and four penalties are absent from the spec; every listed coefficient is wrong. The property test `tests/test_search_properties.py:93-140` checks only sort order / purity / determinism — **not** the formula in 3.1, so 7.2's "Validates 3.1" is untrue. | Requirement 3.1 is superseded by `.kiro/specs/rerank-weights-single-source-of-truth/` (out of my scope); add a pointer and delete the literal formula here. |
| 2 | S1 | requirements.md 5.1 "THE Data_Access_Layer SHALL export **exactly** these public functions: get_connection, is_reachable, create_memory, get_memory, update_memory, list_memories, search_similar, create_relationship, get_relationships, get_processed_source_urls" (10); tasks.md 7.4 [x] "Verify `src.db` exports exactly …" | Exact export set | `src/db.py` `^def` list: 19 public functions — adds `close_pool`, `get_memories_by_ids`, `get_arrivals_summary`, `find_temporal_neighbors`, `get_schema_with_constituents`, `find_schemas_for_memory`, `find_schemas_for_memories`, `get_memory_provenance` (`:67,117,200,314,345,369,388,415`) plus `_LIST_COLUMNS`. `tests/test_search_properties.py:183-187` `test_db_exports_expected_public_names` asserts `hasattr` for each expected name — a **subset** check — so the "exactly" requirement is violated and the test still passes. | Either drop "exactly" from 5.1 or make the test compare the full public set (and then update the list). |
| 3 | S2 | requirements.md 1.1 "THE Service_Layer SHALL contain exactly three public functions" | `search.py` API | `src/search.py` `^def`: `hybrid_search`, `compute_spacing_bonus` (`:167`), `token_overlap` (`:192`), `rerank`, `increment_access_count` = 5 public; plus `OVERLAP_PREFIX_MIN` constant. | Reword to "at least". |
| 4 | S2 | tasks.md 1.1 [x] "Imports remaining in `db.py` after extraction: `os`, `json`, `psycopg2`, `RealDictCursor`" (4); design.md `db.py` = "connection, memory CRUD, search_similar, relationships, dedup helpers" | `db.py` imports / role | `src/db.py:8-14` adds `from contextlib import contextmanager` and `from psycopg2.pool import SimpleConnectionPool`; `get_connection` (`:28-65`) is now a pooled context manager (see `.kiro/specs/db-layer-hardening/`, out of scope). `db.py` also grew schema/knowledge-graph readers (row 2). | Historical note pointing at db-layer-hardening. |
| 5 | S2 | requirements.md 2.1 `hybrid_search(query_text, query_embedding, limit, type, status)`; tasks.md 7.1 [x] signature check | Signature | `src/search.py:45-46`: `hybrid_search(query_text, query_embedding, limit=10, type=None, status=None, project=None, source_type=None, created_after=None)`; `rerank(results, query_text, query_project=None)` (`:217`). Test `:15-33` checks only the five original names, so passes. | Update 2.1 / 3.1 signatures. |
| 6 | S2 | requirements.md 7.1; tasks.md 5.1 [x] `scripts/golden_queries.py`; design.md:79 | Path | Actual `scripts/eval/golden_queries.py:29` `from src.search import hybrid_search, rerank`. The property test was silently repointed (`tests/test_search_properties.py:261` reads `scripts/eval/golden_queries.py`) while the spec was not. | Fix path in spec (same defect as dream-cycle §1 row 9). |
| 7 | S2 | requirements.md 6.2 "Presentation_Layer SHALL import create_memory, get_memory, update_memory, list_memories, create_relationship, get_relationships from the Data_Access_Layer" (6 names); tasks.md 3.1 "After:" block | `mcp_server.py` import line | `src/mcp_server.py:25-29` imports 10 names from `src.db` (adds `get_arrivals_summary`, `find_temporal_neighbors`, `find_schemas_for_memories`, `get_schema_with_constituents`). Design.md:28 arrow still lists `search_similar` among MCP→DB calls, contradicting 6.3 in the same spec. | Update 6.2; fix the diagram at design.md:28. |
| 8 | S3 | requirements.md 9.1-9.3 / every checkpoint "all 157 tests" | Test count | Fourth different baseline across the six families (157 / 204 / ~685 / 814). | Drop counts. |
| 9 | S3 | requirements.md 3.4 default recency 0.35 → `search.py:260` ✓; 3.5 type boost 0.06 → `rerank_weights.py:32` ✓; 4.1 empty-list no-op → `test_search_properties.py:157` ✓; 8.1-8.5 unchanged imports → `storage.py:8`, `ingest.py:11`, `dream_cycle_db.py:12`, `crawlee_ingest.py:21`, `ingest_chats.py:12` ✓ | Verified true | — | — |

Stale checkboxes: 7.2 and 7.4 are checked as validating 3.1 / 5.1 but the tests they name do not test those clauses. Done-but-missing: none.

---

## Cross-family contradictions (summary)

| Topic | Spec A says | Spec B says | Code (6b8c2d2) |
|---|---|---|---|
| Panel size / verdicts | dream-cycle reqs 1.4, 2: 3 evaluators, 3/3, DEFERRED | byzantine: 4, ≥3/4, binary | 4 roles, `accept_count >= 3` (`consensus.py:27`) |
| Evaluator crash | dream-cycle tasks 10.1: "treat as REJECT" | dream-cycle req 16.3 & byzantine 3.2: retry then abort, never REJECT | retry ×3 then abort (`orchestrator.py:606-649`) |
| "Material-objection veto" | byzantine headers + SPEC-INDEX:13: exists | (no spec defines it) | **does not exist**; `has_blocking` unused |
| Strategies | dream-cycle: 11, #11 = deferred evidence | byzantine: #11 removed | 12; #11 elaborative re-interrogation, #12 arrivals synthesis (`explorer.py:7-77`) |
| Adapters | model-backend-adapters: 3 (`kiro, claude_code, codex`); direct-API "out of scope" | direct-api-backends: adds `bedrock` | 4 (`resolver.py:42-47`) |
| Claude Code output flag | model-backend-adapters 2.3: `--output-format json` | probe-fix 2.4: `stream-json --verbose` | `stream-json --verbose` (`claude_code.py:220`) |
| Kiro as default | model-backend-adapters req 20, direct-api 10/13.2: laptop all-Kiro default, nothing reaches Bedrock | — | `default_profile = "laptop"` but `skim` is Bedrock in every profile; scheduled jobs export `controlled` (`dream_cycle_scheduled.sh:32`) |
| Run cost cap | no spec defines it | — | `$5` (`cost.py:39`, MODEL-BACKENDS.md), `$6` (`backends.toml:119` comment), **`$9` operative** (`dream_cycle_scheduled.sh:33`) |
| Skim stage / grounding audit / revision | absent from all six specs | — | present: `_skim_arrivals` (`orchestrator.py:400`), `_ground_candidate` (`:466`), `_revise_candidate` (`:1151`), `src/skim/*`, `src/prompts/revision.py` |
| Schedule | dream-cycle 19.1: Sunday 04:00 weekly | — | Mon/Wed/Fri 02:00 (`plist:17-37`) |
| Test baseline | 157 (data-layer) / 204 (byzantine) / ~685 (adapters) / 814 (probe-fix) | — | not re-run (pytest forbidden for this track) |
| `scripts/golden_queries.py`, `scripts/job_wrapper.sh` | dream-cycle, data-layer | — | `scripts/eval/golden_queries.py`, `scripts/jobs/job_wrapper.sh` |
| `docs/DREAM-CYCLE-DESIGN.md` | dream-cycle design ("source of truth"), byzantine req 10 | — | file does not exist at `docs/` |

## Refactor seams surfaced by this track

1. `tally_consensus` is the natural home for the promised veto (`has_blocking`) — one function, one test file (`tests/test_consensus.py`).
2. The grounding-rejection path (`orchestrator.py:466-478`) fabricates four cloned votes; a distinct `final_verdict` value would stop `get_tier1_metrics`/feedback from counting them as panel rejections.
3. The cost cap needs a single owner: `cost.py:DEFAULT_RUN_COST_CAP_USD` should be the value `dream_cycle_scheduled.sh` *reads*, not overrides.
4. `deferred_twice_rejected` (migration 003) and `candidates_deferred` (SQL literal in `dream_cycle_db.py:65`) are dead columns awaiting a migration.
5. `docs/MODEL-BACKENDS-VERIFICATION.md` is a stub that three specs cite as evidence; either restore a current "Verified facts" section or repoint the specs at the archive.

## Could not verify

- Whether the archived `docs/archive/2026-09-10/docs/MODEL-BACKENDS-VERIFICATION.md` and any archived `DREAM-CYCLE-DESIGN.md` contain the Finding 1/2 evidence and Requirement-10 text — I did not descend into `docs/archive/` (scope discipline; the top-level `docs/` files were the stated targets).
- Whether any current test asserts `len(verdicts_dict) == 8` (byzantine §2 row 8) — would need pytest or a targeted grep of `tests/test_dream_cycle.py`, `tests/test_integration.py` that I did not run.
- Which `run_type` `tests/test_dream_cycle.py:1121 test_explorer_failure_completes_run_with_zero_candidates` uses (dream-cycle §1 row 15) — if `scheduled`, that test is now asserting the pre-change behaviour and would fail; not run.
- Actual passing test counts (157/204/685/814) — pytest was forbidden for this track.
- Whether `get_tier1_metrics` still emits a strategy-diversity or cost-efficiency figure matching req 18.1 — I confirmed only the `4 * gen` factor (`dream_cycle_db.py:613`) and the absence of `deferred_to_accepted_rate`.
- Live behaviour of `kiro-cli --model` (the `controlled` profile comment at `backends.toml:107-112` claims Kiro 2.22 ignores it) — environment-dependent, not testable read-only.
- Whether `scripts/jobs/job_wrapper.sh` actually emits the macOS notification on exit 2 (dream-cycle req 19.4) — I confirmed the plist wires it (`plist:10`), not the script body.
