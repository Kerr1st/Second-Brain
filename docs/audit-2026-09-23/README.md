# Audit 2026-09-23 — raw track output

Source material behind [../AUDIT-2026-09-23.md](../AUDIT-2026-09-23.md) (the synthesis) and [../PLAN-refactor.md](../PLAN-refactor.md) (the sequenced plan).

Seven read-only review tracks ran in parallel against `main` @ `6b8c2d2`. Six returned; the test-suite track returned empty twice and was done by hand, so its findings exist only in the synthesis (§9) and have no file here.

| File | Track | Scope |
|---|---|---|
| `01-dream-cycle-and-skim.md` | dream cycle + skim | `src/dream_cycle/*`, `src/skim/*`, `src/prompts/*`, `src/dream_cycle_db.py`, `config/skim.toml`, migrations 003/004/012/013/014, 4 scripts |
| `02-capture-and-ingest.md` | capture + ingestion | `src/capture/*`, `src/parsers/*`, `src/ingest.py`, `src/capture_api.py`, `classify`/`depth`/`project`/`qd_profile`, 11 scripts |
| `03-backends-and-routing.md` | model routing | `src/backends/*`, `src/agent_invoker.py`, `config/backends.toml`, `tests/test_resolver.py` |
| `04-data-layer-search-mcp.md` | storage, search, MCP | `src/db.py`, `models`, `embeddings`, `search`, `rerank_weights`, `validation`, `mcp_server`, `express`, migrations 000–014 |
| `05-ops-surface.md` | operations | 18 plists, 20 `scripts/jobs/*.sh`, installed LaunchAgents, logs, repo hygiene |
| `06-documentation.md` | documentation | `README.md`, ~30 `docs/*.md`, 18 `docs/user-guide/*.md`, `scheduling/README.md` |
| `00-raw-combined-output.txt` | all six | unsplit original, for grep |

## Reading these for spec work

Each track reports findings with a severity, a `file:line` citation, a one-line evidence quote, and a recommended fix, and every track ends with an explicit **"what I could NOT verify"** section. Read that section before treating any finding as settled — the tracks were deliberately read-only, so most claims are static code reading rather than observed behaviour.

Findings are numbered **per track**, and the schemes differ (`C1`/`I3`/`R7`/`D2`/`P4`/`S1` in track 01, `R1`–`R11` in 02, `M-5`/`G-1` in 03, `§1.2`/`§4.4` in 04, `Finding 2a` in 05, `§1.10`/`C11` in 06). There is no global ID space, so cite a finding as `track:id` — e.g. `03:G-1` for the unmetered Codex thinker call.

Where a track's premise turned out to be wrong, the track says so and corrects it — see the opening of `04-data-layer-search-mcp.md` (three corrections) and §9 of the synthesis (my brief about the four local `test_db` fixtures was partly wrong; the mechanism is order-dependence, not a hardcoded production DSN). Prefer the correction over my brief.

## Overlaps worth knowing before you split work

These are the same defect seen from two sides. Do not let two specs fix them independently:

- `02:R1` (chunker emits one unbounded chunk) + the embedding truncation in `src/embeddings.py` = the 199 MB finding, synthesis §0. Neither track saw the whole thing; the measurement is in the synthesis only.
- `01:C6` (four disagreeing arrivals filters) + `04:§3` (duplicated SQL) — one shared filter fixes both.
- `03:M-5`/`03:G-1` (unmetered corpus paths) + `01:R5` (budget soft-stop is a no-op on unmetered backends).
- `05:Finding 3a`/`3b` (wrong cadence, 12-vs-18 plists) + `06:§1.6` — same doc defect, found independently.
- `04:§1.2` (migration 009 alters production) + synthesis §9 (conftest applies it) — one cause, two symptoms.
