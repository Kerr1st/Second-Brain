# Componentization Plan — Second Brain

**Status: roadmap.** How we turn Second Brain into components that can be built, tested,
scheduled, and replaced **independently**. The capture deep-dive lives in
`CAPTURE-COMPONENTS.md`; this is the whole-system plan and sequencing.

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
(this file's status table).

## Roadmap (incremental — keep 643 tests green, commit per step, push origin+mini)

**Phase A — Capture (in progress).** Per `CAPTURE-COMPONENTS.md`: extract shared `store.py` +
`runner.py`; lift connectors (`youtube` ✅ → `kiro_chat` → `web` → `quick_desktop`); retire dead
(`capture_api`, one-off migrations, entity-KG import); generalize `distill`.

**Phase B — Collapse the store primitive (keystone).** Make `store.py` the single writer
(`embed→classify→depth→create_memory`); point `mcp_server.memory_create` and any HTTP writer at
it. This cleanly separates "write a memory" from every component that produces one.

**Phase C — Formalize Retrieval's boundary.** Confirm `search` is the only query path; document
`hybrid_search`/`rerank` as the contract; decide on the inert bits (`encoding_context`,
`schema_context`) — keep, activate, or remove.

**Phase D — Document Synthesis & Delivery contracts + trim.** Already cohesive; write their
contract docs, trim the MCP surface (only `update`/`relate` are strictly unused), and remove the
dead schema-context read path.

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
- ✅ Component model defined (this doc) + capture deep-dive (`CAPTURE-COMPONENTS.md`).
- ✅ Clean core boundaries verified (no core→capture imports); docs reconciled to ground truth.
- ✅ `youtube` connector shipped.
- ▢ Phase A remainder (spine extraction, other connectors, dead-code retirement, distill).
- ▢ Phase B store-primitive collapse.
- ▢ Phases C–E.

## Cross-references
- `CAPTURE-COMPONENTS.md` — capture deep-dive + connector contract.
- `ARCHITECTURE.md` — current system (reconciled).
- `STREAMLINING-DESIGN.md` — what to cut (dead-code findings still valid).
- `AGENTIC-RETRIEVAL-PLAN.md` — why the entity KG stays deferred.
