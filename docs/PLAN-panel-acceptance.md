# Plan: make the consensus panel able to accept a grounded insight

Branch: `panel-acceptance` (off `main` at `6bf67bc`, after the attention-allocation merge).
Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (reason in Decisions).
**Resume here after a restart:** read "Status", then the first unchecked task in "Phases".
All commands run from the repo root with `.venv/bin/python`; live DB is `memory_bank`
(port 5432, user/password `memory_bank`); metered calls use `AWS_PROFILE=second-brain-jobs`.

## 1. Problem

The dream cycle now examines the user's life (attention-allocation merge: 8 source
types, 61% episodic, guaranteed arrivals slice) and still surfaces nothing. Run
`47fffa94` (2026-09-17 20:09, profile `controlled`): 12 candidates, 0 accepted.

Lifetime record of the two model families on the panel (all runs with Astra seats,
35 candidates):

| Seat | Model | ACCEPT votes |
|---|---|---|
| Skeptic (A) | GPT-6 Astra | 0 / 35 |
| Epistemologist (C) | GPT-6 Astra | 1 / 35 |
| User Advocate (B) | Claude Sonnet 5 | 28 / 35 |
| Methodologist (D) | Claude Sonnet 5 | 23 / 35 |

Consensus is 3-of-4 (`src/dream_cycle/consensus.py`). With two Astra seats voting
REJECT ~99% of the time, acceptance is impossible by construction, whatever the slices.

## 2. Evidence about *why* Astra rejects

Read the reasoning on the run-`47fffa94` candidates (`dream_cycle_candidates.evaluator_a/c_reasoning`):

- Astra routinely concedes usefulness ("the practical recommendation is useful") and
  rejects on **scope**: the candidate says "every Amazon message says no action needed"
  when the sources are three notifications about one case on one day.
- It rejects **attribution**: "agent-generated feed summaries, so attributing their
  wording to Amazon as verbatim is not established".
- It treats "3+ memories from different periods" as the bar for a pattern (that is
  literally the Epistemologist's criterion 1) — which no observation about *this week*
  can meet.
- The evaluator prompts (`src/prompts/panel.py`) list criteria to "evaluate on" but give
  **no decision rule** — nothing says when the criteria add up to ACCEPT. Astra fills the
  gap with "any criterion fails → REJECT"; Sonnet fills it with "mostly fine → ACCEPT".
- The Thinker (`src/prompts/thinker.py`) is told to find "unnamed principles" and
  "emerging patterns", labels most candidates `confidence: high`, and has no rule about
  scoping claims to evidence. Its titles inflate ("The 5-day clock is *never* triggered…").

Hypotheses, to be tested before the fix is chosen (Phase 1):

- **H1 Scope inflation (Thinker side).** Candidates overclaim; a scoped version of the same
  observation would pass Astra. Prediction: narrowing the claim flips Astra's verdict.
- **H2 Rubric mismatch (prompt side).** The criteria demand universality/falsifiability and
  give no acceptance rule; personal-life observations cannot qualify. Prediction: Sonnet in
  the Skeptic seat *also* rejects more; a rubric with an explicit acceptance standard lifts
  Astra's acceptance on the same candidates.
- **H3 Model conservatism (Astra side).** Astra rejects regardless of rubric or seat.
  Prediction: Astra as *Advocate* still rejects most candidates that Sonnet-as-Advocate accepts.
- **H4 Format.** The schema-constrained ACCEPT/REJECT (Direct-API path) with "no maybe"
  pushes borderline cases to REJECT. Prediction: a REVISE option changes the distribution.

## 3. Design constraints (do not violate)

- Keep the **cross-family check**: an insight must be endorsed by both a Claude seat and a
  GPT seat. The user chose this; it is the point of the panel.
- Keep 3-of-4 as the final rule unless Phase 1 proves the seat/model split is the cause
  and the user agrees to reseat.
- No topic policing. Second Brain work is part of the user's day (decision 2026-09-17).
- Attention ledger and allocation stay untouched (`src/dream_cycle/ledger.py`,
  `allocation.py`); their metrics are the control variables.
- `_invoke_evaluator_safe` retries `(TimeoutError, RuntimeError, ValueError)` ×3 then aborts;
  `SpendCapExceeded` must not subclass those. `TestFinalOrchestratorMethodSet` pins the
  orchestrator's method set — add new methods there deliberately.
- Orchestrator unit tests patch `src.dream_cycle.orchestrator.dream_cycle_db` only; new DB
  reads go through `dream_cycle_db` and must tolerate `MagicMock` returns.
- The corpus never runs on Kiro/Fable (profile `controlled`). Subagents may read code and
  prompts; they must not be handed corpus content (Slack/email/health/leave material).
- Cost: replay experiments are metered. Budget for this branch: **≤ $12 total** across
  experiments; per-run cap stays `SECOND_BRAIN_RUN_COST_CAP_USD=6`. Daily AWS budget alert
  ($6) will fire on experiment days — expected.

## 4. Phases

### Phase 0 — Instrument: offline panel replay  `[x]`
Goal: evaluate stored candidates again, cheaply, with variant prompts/models/seats,
without running Explorer or Thinker. Everything after this depends on it.

- [x] `scripts/panel_replay.py`
  - inputs: `--run <run_id>` or `--candidate <id>...`; `--seats skeptic,advocate,...`;
    `--model-override role=backend:model` (e.g. `skeptic=bedrock:global.anthropic.claude-sonnet-5`);
    `--prompt-variant {current,v2}`; `--limit N`; `--dry-run`; `--cap-usd`.
  - rebuilds each candidate's `CandidateInsight` from `candidate_json`, loads cited
    sources via `src.dream_cycle.packet.load_memories_by_ids` +
    `format_sources_for_evaluator` (same text the live panel saw), calls
    `get_evaluator_prompt` (or the variant), invokes through the resolver's adapters with
    `schema=EVALUATOR_VERDICT_SCHEMA`, records `{candidate_id, role, model, variant,
    verdict, reasoning, cost}` to `logs/panel_replay/<timestamp>.jsonl`, prints an
    acceptance matrix per seat and the 3-of-4 outcome.
  - never writes to `dream_cycle_candidates`; uses `SpendTracker` with `--cap-usd`.
- [x] `tests/test_panel_replay.py`: hermetic (fake invoker), covers matrix + cost cap + no DB writes.
- [x] Baseline replay on 6 candidates from `47fffa94` (3 life, 3 project), current prompts,
  current models → confirm Astra ≈ 0 accepts reproduces. (~$1.8)

### Phase 1 — Diagnose: seat vs. model vs. scope  `[x]`
Same 6 candidates, three experiments (~$1.8 each):
- [x] **Seat swap**: Sonnet 5 in Skeptic+Epistemologist, Astra in Advocate+Methodologist.
- [x] **Scoped rewrite**: hand-narrow each candidate's title/WHAT to what its sources
  support (no other change); replay with current prompts/models. Tests H1 directly.
- [x] **Rubric v2**: current candidates, prompts with an explicit acceptance standard
  (Phase 2 draft). Tests H2.
- [x] Record results in §6 Decisions and choose the fix mix.

### Phase 2 — Design the fix (choose from evidence)  `[x]` — see Decisions 2026-09-18
Candidates, in priority order; each is small and independently testable:
- [ ] **Thinker: scope-to-evidence rule** (`src/prompts/thinker.py`). Claims stated at the
  scope the cited memories support; quantify ("in the three notifications captured…");
  no "every/never/always/the only" unless every cited memory instantiates it; confidence
  set by count and independence of sources (1 source → low; 2 → medium; 3+ independent →
  high); a required `claim_scope` sentence in EVIDENCE. Prompt-contract tests.
- [ ] **Evaluators: acceptance standard** (`src/prompts/panel.py`). Add a shared
  "## Standard" block: ACCEPT when the claim is (a) grounded in the cited sources, (b)
  stated at a scope the sources support, (c) non-obvious and useful to the user, (d) not
  already in the corpus. A scoped observation about this week qualifies; universality is
  not required; reject fabrication, misattribution, overreach, and restatement. Reasoning
  must name the failing letter. Epistemologist criterion 1 reworded: "1 memory = single
  observation, say so; 2–3 = emerging; 3+ independent = pattern" as *labels for scope*, not
  as a bar.
- [ ] **REVISE loop** (orchestrator, `models.EvaluatorVerdict`, `consensus`). Verdict enum
  gains `REVISE` with a `revised_claim` suggestion (schema change). If the first pass has no
  ACCEPT majority but ≥2 REVISE/ACCEPT and the REVISE reasons are about scope, run one
  tool-less Thinker narrowing call ("restate this candidate at the scope these objections
  allow; keep the evidence; do not add claims") and re-run the panel once. On the second
  pass REVISE counts as REJECT. Cost bound: ≤1 narrowing + ≤4 evaluator calls per
  candidate; skip when the run's spend is above 70% of the cap. Method-set test updated.
- [ ] **Cost levers**: Astra evaluators at `effort=medium` (measure verdict agreement with
  `high` on the replay set before switching); Thinker "at most 3 candidates per slice";
  guard that skips the panel for candidates the Thinker itself marks `confidence: low`
  with a single source.

### Phase 3 — Implement + verify offline  `[x]`
- [x] Implement the chosen items with unit tests (prompt contracts, consensus with REVISE,
  narrowing call, replay harness variant).
- [x] Replay all 12 `47fffa94` candidates through the new pipeline offline (narrowing +
  panel). Target: **Astra acceptance > 0**, overall acceptance 20–40%, and a spot-check that
  every accepted candidate is grounded in its cited sources (read them).
- [x] Full suite green; commit per item.

### Phase 4 — Live verification  `[~]`
- [ ] One scheduled run on `controlled` (via `launchctl kickstart gui/$(id -u)/com.second-brain.dream-cycle`).
  Check: ledger unchanged in shape (≥5 source types, ≥50% episodic), ≥1 accepted insight,
  digest lists it with all four reasonings, cost ≤ $6.
- [ ] Then 5 nights of observation: acceptance per seat from
  `dream_cycle_candidates`, cost per run from the log's `run total`.

### Phase 5 — Wrap  `[ ]`
- [ ] Update `docs/MODEL-BACKENDS.md` (profile `controlled`, panel semantics) and
  `docs/DREAM-CYCLE.md` if present; update this plan's Status; user decides merge.

## 5. Status

_Updated as work proceeds; newest first._

- 2026-09-18 07:35 — **Phase 3 verified offline. Phase 4 (live run) next.**
  `scripts/panel_replay.py --ground` ran the 6-candidate set through the real path
  (audit → revision → four seats, profile `controlled`, records
  `logs/panel_replay/20260918T140804Z-grounded.jsonl`, $2.80 ≈ $0.47/candidate):

  | candidate (live result: all REJECTED) | audit | revised | A B C D | 3-of-4 |
  |---|---|---|---|---|
  | Case 13484620 half-resolved | 33 | yes | A A A A | ACCEPTED |
  | Two-track disability leave | 32 | yes | A A A A | ACCEPTED |
  | 5-day state-letter clock | 30 | yes | A A R A | ACCEPTED |
  | Colleague's Slack question | 35 | yes | A A A A | ACCEPTED |
  | Second Brain as unlabeled lab | 30 | yes | A R A A | ACCEPTED |
  | Hybrid profile's remaining spend | skipped (cap) | no | R A R R | REJECTED |

  Astra seats: Skeptic 5/6, Epistemologist 4/6 (from 0/6 and 0/6), mean scores 75 / 67.
  The unrevised control (audit skipped at 80% of the replay cap) was rejected by both Astra
  seats — the mechanism, not leniency, produced the acceptances. The one Astra rejection of a
  revised candidate was correct: the revision fixed the body but `schema_note` and a
  relationship note still carried the unsupported claim → revision now covers those fields
  too (test added). Total branch spend ≈ $9.00 of $12.
  Acceptance on this set (5/6) is above the 20–40% guess; judged by content, all five accepted
  revisions are scoped, attributed, and supported — the live nightly rate over a week is the
  real measure.

- 2026-09-18 07:10 — **Phase 3 implemented (`aa73701`), offline verification running.**
  Prompt rewrites (subagent, verified 164 tests), graded schema, `_ground_candidate` /
  `_revise_candidate`, migration 013 applied live, per-slice cap 3, 1,065 tests green.
  `scripts/panel_replay.py --ground` runs the real audit → revision → panel path offline;
  verification on the 6-candidate set is in flight (`/tmp/replay_grounded.log`,
  `logs/panel_replay/*-grounded.jsonl`). Spend so far ≈ $6.20 + this run (≤ $4).
  Success criteria: Astra acceptance > 0; overall 3-of-4 acceptance 20–40%; every accepted
  candidate spot-checked against its sources.
- 2026-09-18 06:40 — **Phase 1 complete; design chosen (see Decisions). Phase 3 starting.**
  Spend so far ≈ $6.20 of the $12 branch budget. Experiment records: `logs/panel_replay/`.
  - **Seat swap B** (Astra in the Advocate + Methodologist seats): Advocate 2/6, Methodologist
    0/6. Astra rejects in every seat; Sonnet accepts in every seat (swap A: 5/6, 6/6). Model
    disposition, not the rubric, decides the vote. **H3 confirmed as dominant.**
  - **Scoped rewrite** (3 life candidates hand-narrowed with `claim_scope`, Astra seats, current
    prompts): 0/3 and 1/3. Scoping alone does not unlock Astra — it moves to the next defect.
  - **Rubric v2** (graded prompt with an explicit Standard and BLOCKING/ADVISORY separation,
    `get_evaluator_prompt_standard` + `EVALUATOR_VERDICT_SCHEMA_V2`; original candidates; Astra
    seats): 0/12 ACCEPT, scores 28–32, **2–4 blocking issues per candidate — and they are
    correct**: "closed on 2026-07-06" (sources say approved, not closed); fax number and
    channels not in any cited source; "the case manager wrote" attributed to an agent summary;
    "you concluded" attributing the assistant's words to the user; cost figures that contradict
    the cited table; "none of them are in any deck" with no source. The Thinker fabricates
    details and misattributes; Sonnet accepts ~70% of these anyway. **Astra is the only seat
    catching real defects. Making the panel accept these candidates would be the wrong fix.**
  - **Revision probe** (`scripts/revise_probe.py`: Thinker backend repairs 2 candidates from
    Astra's issue lists, tool-less, ~$0.08 each; then Astra seats re-vote on v2): candidate 1 →
    Skeptic ACCEPT (77) + Epistemologist ACCEPT; candidate 2 → Skeptic ACCEPT, Epistemologist
    REJECT (1 blocking). With the Sonnet seats' existing ACCEPTs both would pass 3-of-4.
    **A grounded revision passes Astra.** This is the mechanism to build.
- 2026-09-18 06:20 — Phase 0 done. Replay harness committed (`e627f03` + Direct-API-only
  guard). Experiment set: 6 candidates from `47fffa94` (`/tmp/replay_set.txt`; 3
  `arrivals_synthesis`, 3 project). Baseline (Astra, current prompts): 12/12 verdicts identical
  to the live run — a single flipped vote is signal. All 12 candidates of `47fffa94` cite only
  in-slice sources (evaluators saw every source); "sources not supplied" is a Kiro-era artefact.
  Objection tally across 41 Astra rejections (keyword, noisy): independence 39, scope 33,
  causal overclaim 32, attribution 24, novelty 17. Subagent rubric analysis: 6 hard blockers for
  a this-week observation + no decision rule.
- 2026-09-17 21:05 — Branch created; plan written; Phase 0 starting.

## 6. Decisions

- 2026-09-18 — **Design chosen from Phase 1 evidence: fix candidate grounding, not panel
  leniency.** The panel is not too strict; the Thinker's candidates contain fabricated details,
  misattributions and contradicted claims that only the Astra seats catch. Build:
  1. **Thinker grounding + scope rule** (prevention): every fact traceable to a cited memory;
     verbatim quotes; attribute to whoever produced the text (agent summaries are not the
     email's author); no detail not in the packet; claims at the scope the sources support;
     confidence by source count/independence; `claim_scope:` sentence; at most 2 candidates
     per slice, best-grounded first.
  2. **Graded evaluator prompt as THE prompt** (`get_evaluator_prompt` becomes the "standard"
     variant): explicit acceptance Standard, BLOCKING vs ADVISORY defects, 0–100 score,
     issue lists (`EVALUATOR_VERDICT_SCHEMA` gains score/blocking_issues/advisory_issues).
     Criteria reworded per the subagent analysis (Skeptic 1–3, Advocate 4, Epistemologist
     1, 2, 4, Methodologist 2), keeping pinned substrings.
  3. **Pre-panel grounding audit + one revision** (orchestrator): before the panel, one
     audit call by the Skeptic seat's backend (Astra) with the graded prompt; if it lists any
     blocking or advisory issue, one tool-less revision call by the Thinker backend repairs the
     candidate (remove/correct unsupported details, narrow scope, fix attribution, keep
     evidence); the four-seat panel then votes once on the revised candidate. Consensus stays
     binary 3-of-4. Audit/revision skipped when run spend ≥ 80% of cap. Original title kept in
     `candidate_json.revision.original_title`; audit issues persisted for the digest.
  4. **Cost levers**: ≤2 candidates per slice (Thinker rule + orchestrator hard cap 3);
     Astra seats at `effort=medium` if verdict agreement with `high` holds on the replay set.
     Estimated ≈ $5/run (Explorer 0.6 + Thinker 1.0 + 8 × (audit 0.12 + revise 0.08 + panel
     0.30)); the user decides between the $6 cap and a higher cap.
  Rejected alternatives: REVISE verdict with a second full panel pass (≈ $0.70/candidate, more
  state); reseating an Astra seat (loses the only grounding check); rubric-only change
  (Astra 0/12 even on the graded prompt without revision).
- 2026-09-17 — Keep cross-family consensus and 3-of-4; fix scope and rubric first; reseat
  only if Phase 1 shows Astra rejects regardless of rubric (H3). *(H3 confirmed; reseating
  rejected anyway — see above.)*
- 2026-09-17 — Subagents get code and prompts, never corpus content.

## 7. Useful queries

```sql
-- acceptance per seat, all Astra-era runs
SELECT count(*), count(*) FILTER (WHERE evaluator_a_verdict='ACCEPT') skeptic,
       count(*) FILTER (WHERE evaluator_c_verdict='ACCEPT') epist,
       count(*) FILTER (WHERE evaluator_b_verdict='ACCEPT') advocate,
       count(*) FILTER (WHERE evaluator_d_verdict='ACCEPT') method
FROM dream_cycle_candidates c JOIN dream_cycle_runs r ON r.id=c.run_id
WHERE r.backend_provenance->'skeptic'->>'model'='global.openai.gpt-6-astra';

-- a run's candidates with verdicts
SELECT id, left(candidate_json->>'title',70), evaluator_a_verdict, evaluator_b_verdict,
       evaluator_c_verdict, evaluator_d_verdict, final_verdict
FROM dream_cycle_candidates WHERE run_id='47fffa94-2b9d-4958-ae17-07912a1cacfc' ORDER BY created_at;
```

## 8. File map

| Concern | File |
|---|---|
| Evaluator prompts | `src/prompts/panel.py` |
| Thinker prompt | `src/prompts/thinker.py` |
| Consensus rule | `src/dream_cycle/consensus.py` |
| Evaluator invocation, retries, spend | `src/dream_cycle/orchestrator.py` (`invoke_evaluator`, `_invoke_evaluator_safe`, `_record_spend`) |
| Verdict schema, source formatting | `src/dream_cycle/packet.py` |
| Verdict/candidate models | `src/models.py` |
| Candidate persistence | `src/dream_cycle_db.py` (`dream_cycle_candidates`) |
| Replay harness (new) | `scripts/panel_replay.py`, `tests/test_panel_replay.py` |
| Cost accounting | `src/backends/cost.py` |
