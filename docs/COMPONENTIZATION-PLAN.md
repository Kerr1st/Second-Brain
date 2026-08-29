# Componentization Plan — Second Brain

**Status: code-componentization roadmap; documentation spine implemented.**
The canonical component contracts and navigation live in
[`components/index.md`](components/index.md). This document tracks the remaining
code-boundary work needed to make those components independently buildable,
testable, schedulable, and replaceable. The capture deep-dive lives in
`CAPTURE-COMPONENTS.md`.

## Goal & principles
Make each part a component with:
1. **One responsibility** and a **named boundary** (what it owns vs. what it calls).
2. **A defined contract** (the interface other components use — a function surface or the DB schema).
3. **Independent tests** (a component's tests don't require standing up unrelated components).
4. **Its own scripts/jobs/docs.**
5. **One-directional dependencies** — no cycles. The DB schema is the shared contract.

Dependency direction:
```
CAPTURE ─▶ (writes) ─▶ [ memories + relationships ] ─▶ RETRIEVAL ─▶ SYNTHESIS ─▶ DELIVERY
                              shared store/DB                 ▲             │
                                                              └──── MCP interface ────┘
```

## The components

| Component | Owns | Contract / interface | Today | Cohesion |
|---|---|---|---|---|
| **Capture** | source connectors + the spine (store/ingest/distill) | emit markdown → `ingest_content`; writes `memories` | scattered (15 scripts, ext. Node, 3 store paths) | **LOW** → being fixed |
| **Retrieval** | `db`, `embeddings`, `search` (hybrid + cognitive rerank) | `hybrid_search()` / `rerank()` over `memories` | cohesive (`src/search.py`,`db.py`,`embeddings.py`) | **HIGH** |
| **Synthesis** | dream cycle (Explorer→Thinker→panel), `dream_cycle_db`, `agent_invoker`, prompts | reads via Retrieval; writes via `dream_cycle.storage` | already a package (`src/dream_cycle/`) | **HIGH** |
| **Delivery** | Express (`brief`/push/`memory_brief`, feedback) | reads DB; emits briefings | cohesive (`src/express.py`) | **HIGH** |
| **Interface (MCP)** | `mcp_server.py` — exposes capture/retrieval/delivery to agents | the 9 MCP tools | cohesive but holds a duplicate store path | **MED** |
| **Shared infra** | `db` (conn+CRUD), `models`, `classify`/`depth`/`project`, `embeddings` | imported by all | shared leaf utils | **HIGH** |

**Key insight from the audit:** retrieval/synthesis/delivery already have clean boundaries (the
core does **not** import capture). The entanglement is concentrated in **Capture** and in the
**3 duplicate store paths** (`ingest.ingest_content`, `capture_api._store_memory`,
`mcp_server.memory_create`). Fix those two things and the component structure largely falls out.

## Definition of done (per component)
A component is "componentized" when it has: (1) a one-paragraph **contract doc**, (2) a single
**entry/interface module**, (3) **its own test module(s)** that pass in isolation, (4) **no
duplicated** responsibilities with other components, and (5) an entry in the component index
(`docs/components/index.md`).

## Roadmap (incremental — keep the test suite green, commit per step, push origin+mini)

**Phase A — Capture (in progress).** Per `CAPTURE-COMPONENTS.md`: extract shared `store.py` +
`runner.py`; lift connectors (`youtube` ✅ → `kiro_chat` → `web` → `quick_desktop`); retire dead
(`capture_api`, one-off migrations, entity-KG import); generalize `distill`.

**Phase B — Collapse the store primitive (keystone).** Make `store.py` the single writer
(`embed→classify→depth→create_memory`); point `mcp_server.memory_create` and any HTTP writer at
it. This cleanly separates "write a memory" from every component that produces one.

**Phase C — Formalize Retrieval's boundary.** Confirm `search` is the only query path; document
`hybrid_search`/`rerank` as the contract; decide on the inert bits (`encoding_context`,
`schema_context`) — keep, activate, or remove.

**Phase D — Synthesis & Delivery contracts + trim.** Contract documents are
complete. Remaining work is to trim the MCP surface (only `update`/`relate` are
strictly unused) and remove the dead schema-context read path.

**Phase E — (optional, last) Physical repo reorg.** Logical boundaries + contracts matter first.
Only once they're stable, consider `src/{capture,retrieval,synthesis,delivery,infra}/` to make the
structure visible. This is import churn for cosmetics — do it last, in one mechanical pass.

## Sequencing rationale
- **Capture first** — it's where the entanglement and the active work (YouTube/distillation) are.
- **Store primitive second** — the keystone that de-duplicates the write path across components.
- **Retrieval/Synthesis/Delivery** mostly need *documentation + trimming*, not restructuring.
- **Physical reorg last** — highest churn, lowest value; purely makes the existing logical
  structure visible.

## Status
- ✅ Canonical component index plus seven component contract pages.
- ✅ Component model defined (this doc) + capture deep-dive (`CAPTURE-COMPONENTS.md`).
- ✅ Clean core boundaries verified (no core→capture imports); docs reconciled to ground truth.
- ✅ `youtube` connector shipped.
- ▢ Phase A remainder (spine extraction, other connectors, dead-code retirement, distill).
- ▢ Phase B store-primitive collapse.
- ▢ Phases C–E.

## Synthesis-fuel evidence (folded in from the streamlining review)
Provenance of the 8 accepted insights in dream-cycle run `7c712903` (corpus = 121,226 memories, 2026-06-06) — *why* the roadmap protects distillation + diverse capture rather than collapsing to one channel:
- **Fuel is concentrated, not uniform:** `distilled_chat` (31 source-refs, in 6/8 insights) and `quick_desktop_doc` (13 refs, 6/8) dominate; `article` (5/3), `quick_desktop_decision` (3/3), `youtube` (2/2) are a useful long tail.
- **Raw volume ≠ value:** `kiro_ide_chat` is 60% of the corpus (73,250 memories) yet contributed **zero** source-refs to any accepted insight — its value is realized only *after* distillation into `distilled_chat`. **Distillation is the value, not raw capture** → Phase A's "generalize distill" is the high-leverage move.
- **Diversity is load-bearing:** the flagship goal-directed insight spanned 4 source_types / 12 sources. Keep the surviving capture path content-diverse, not narrow.

## Verified dead / inert subsystems (retire in Phase A/C/D)
From the verify-before-cut audit (2026-06-06), still valid:
- **Entity KG** (`entities`, `entity_edges`, `memory_entities`, migration 008) — **0 `src/` references**; imported from QD, never wired into the core. Highest-confidence cut.
- **Schema feature** (`type='schema'` + `derived_from` + migration-007 indexes + the `schema_context` read path) — functionally inert: **0** `type='schema'` memories, **6** `derived_from` edges; write path never built. (NB: there is **no `schema_type` column**.)
- **`encoding_context`** (migration 006) — it *is* the rerank `context_overlap` term (weight 0.10) but populated on only **1/121,226** memories → effectively always 0. Keep-cheap, activate, or cut.
- **Eval harness** (~12 `scripts/eval/` scripts) — tuning scaffolding; keep `recall_check.py` as the health gate, archive the rest.

## Open decisions
1. **How aggressive on capture collapse** — raw volume (`kiro_ide_chat`, 60%) is low-yield; distillation + doc ingest are high-yield → be aggressive on *raw* capture while protecting distillation + long-form ingest.
2. **Rewrite vs. incremental refactor** — verified-inert KG/schema/eval make *prune-in-place* low-risk (the roadmap assumes this); green-field is cleaner but higher-effort.
3. **Drop vs. dormant** for the KG tables (008) + schema-feature indexes (007) — unused either way.
4. **`encoding_context`: keep-cheap vs. activate vs. cut** (Phase C).

## Future considerations (parked, salvaged from the retired REFACTOR-PLAN)
- **embed_priority tiering** — skip vector embeddings for low-value content (keep full-text); demote old low-access memories out of the HNSW index to reclaim space/cost.
- **Per-channel friction config** (`capture_config.json`) — capture liberally vs. selectively per source (Slack DMs vs. channels, Outlook summaries-only).
- **Inverse-metric monitoring** — track "missed content" (searched-for but filtered) + over-consolidation rate, weekly.

## Cross-references
- `components/index.md` — canonical component registry and contract navigation.
- `CAPTURE-COMPONENTS.md` — capture deep-dive + connector contract.
- `ARCHITECTURE.md` — current system (reconciled).
- `AGENTIC-RETRIEVAL-PLAN.md` — why the entity KG stays deferred.
- `QUALITY-BASELINE-2026-06-06.md` — the quality gate that unlocked this design work.
