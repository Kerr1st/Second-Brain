# Audit 2026-09-24 — second pass, raw material

Read in this order:

1. [../AUDIT-2026-09-24.md](../AUDIT-2026-09-24.md) — the synthesis (what is wrong, why it matters, corrected numbers).
2. [../PLAN-refactor.md](../PLAN-refactor.md) — the plan v2 (what to do, in what order, with the five decisions).
3. [00-FINDINGS-REGISTER.md](00-FINDINGS-REGISTER.md) — **the single source of truth for findings.** 176 rows `F-001…F-176`, deduplicated across all tracks, each with severity, file:line, evidence, source tracks, relation to the first audit, and fix. Includes a 38-row *Conflicts resolved* table and counts. Cite findings by `F-nnn`.
4. [00-ERRATA-for-AUDIT-2026-09-23.md](00-ERRATA-for-AUDIT-2026-09-23.md) — what the first audit got wrong (30 rows), what its plan must change (29 rows), and what it got right (21 bullets).

Then the per-track reports, only when a register row needs its full context:

| File | Track | Note |
|---|---|---|
| `01-verify-prior-audit-A.md` | adversarial re-check of first audit §0–2 | 30 claims graded; re-measured the corpus numbers |
| `02-verify-prior-audit-B.md` | adversarial re-check of first audit §3–9 | ~89 claims graded; dead-weight list re-verified against tests |
| `03-codex-capture-deep.md` | Codex capture subsystem | includes the `fetch_task` (CC 79) decomposition in §7 and the origin/main diff |
| `04-express-briefing-feedback.md` | Express, digest, feedback loop | delivery state and the open feedback loop |
| `05-prompts-and-llm-contracts.md` | prompts vs parsers vs schemas | 18 prompt/code mismatches; injection surface |
| `06-live-data-quality.md` | live `memory_bank` measurements | duplication, expiry ages, ledger vs candidates, costs |
| `07-quick-desktop-and-migrate-scripts.md` | `scripts/migrate/`, QD pipeline, distill | writer map; knowledge-graph writer; distill routing |
| `08a-specs-vs-code-A.md`, `08b-specs-vs-code-B.md` | the 13 `.kiro/specs` families | per-spec verdicts; eight missing families |
| `09a-search-ranking.md`, `09b-eval-harness-project-classify.md` | retrieval | formula reconciliation; the `hybrid_search` decomposition; no eval gate exists |
| `10a-secrets-and-data-at-rest.md`, `10b-egress-and-trust-boundaries.md` | security & privacy | egress table; secrets by key name only |
| `11-git-archaeology.md` | git history | churn, fix-on-fix chains, unported upstream fixes, disjoint roots |
| `12a-…`, `12b-…`, `12c-…` | tests | isolation; mock-shaped tests; the 20-behaviour coverage table |
| `static/` | ruff, vulture, radon, full pytest log | 601 lint hits (no correctness class), CC hotspots, 1,630 passed |

**Conventions.** Every track was read-only: no capture/ingest/digest/dream-cycle/backup script was run, no model invoked, no job started or stopped, database access SELECT-only. Each report ends with a *Could not verify* section — read it before treating a finding as settled. Finding ids inside the raw reports are per-track (`C-1`, `F2.8`, `QD-08`…); the register maps them to global `F-nnn`.

**Relation to the first pass.** [../audit-2026-09-23/](../audit-2026-09-23/) holds the first pass's six raw tracks and its archived plan. Its synthesis, [../AUDIT-2026-09-23.md](../AUDIT-2026-09-23.md), carries a supersession notice.
