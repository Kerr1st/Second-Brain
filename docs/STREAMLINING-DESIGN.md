# Streamlining Design Target (DRAFT v2)

> **⚠️ PARTIALLY SUPERSEDED (2026-06-08).** The **capture** conclusions here are out of date: we
> decided to **KEEP + FIX + componentize YouTube** (not collapse it) — see `CAPTURE-COMPONENTS.md`
> and `src/capture/youtube.py`. The "collapse YouTube / Crawlee" lines below reflect the earlier,
> pre-investigation view. Still valid: the **dead-code** findings (entity KG, schema feature, dead
> `capture_api`, eval scaffolding) and the synthesis/distillation emphasis.

**Status: DRAFT for iteration. No code changes.** Design-on-paper unlocked by the quality gate
(see `QUALITY-BASELINE-2026-06-06.md`). v2 folds in the **goal-directed-synthesis** framing and
**closes the verify-before-cut checklist** with live-DB evidence (run `7c712903`, corpus = 121,226
memories, 2026-06-06). The two big decisions — *how aggressive on capture collapse* and
*rewrite-vs-refactor* — remain explicitly **open**; this doc frames them, it does not settle them.

## North Star — sharpened to goal-directed synthesis
"Best demonstrate *memory that thinks*" now has a concrete shape. The most compelling thing the
system produced was **goal-directed synthesis**: pointed at a real objective (the Anthropic DNB CSM
role), it mapped ~2yr of AWS-adoption memories onto the job's competencies and produced **cited
evidence + a drafted action** (competency map + cover-letter draft). So the North Star is a flow:

> **synthesis → the user's real goals → action.**

This *sharpens* the streamlining case rather than reversing it: **keep what feeds that flow; cut the
rest.** Simpler = fewer moving parts a reader must hold in their head to "get it" in one sitting.

## The spine that must survive
The goal-directed-synthesis pipeline, end to end:

```
capture → retrieval substrate → synthesis (dream cycle) → delivery (Express) → aimed at a goal → action
```

1. **Retrieval substrate** — `memories` + embeddings + hybrid search (BM25 + vector + RRF) + the
   cognitive-grounded rerank.
2. **Synthesis** — the consensus-gated dream cycle (Explorer → Thinker → 4 evaluators).
3. **Delivery** — Express (`brief` / push / `memory_brief` + feedback loop).

Plus the connective minimum: **one flexible ingest path + the distillation step**,
`memory_relationships`, and the MCP server.

## Keep / Collapse (with reasoning — checklist findings folded in)
| Subsystem | Verdict | Reasoning |
|---|---|---|
| `memories` + embeddings + hybrid + rerank | **KEEP** | Substrate for all three capabilities. |
| `memory_relationships` | **KEEP** | Load-bearing: `related_to` 21,523 (graph traversal), `contradicts` 10 (feeds Express contradictions), `extends`/`supports` feed dream-cycle edges. (`derived_from` 6 belongs to the inert schema feature below.) |
| Dream cycle (4-agent) | **KEEP** | The differentiator; validated on 4.8. |
| Express (brief/push/memory_brief/feedback) | **KEEP** | The delivery half; makes synthesis legible. |
| MCP server (9 tools) | **KEEP (minor trim)** | **Verified:** dream-cycle agents only *read* via MCP (`search`,`read`,`list`,`graph`); evaluators use no MCP; the orchestrator *writes* directly (not via MCP); Express is a direct-DB library that `memory_brief` wraps. Core-required = 5 (`search`,`read`,`list`,`graph`,`brief`). `create`+`learn` = the capture/showcase surface (high demo value, keep). Only `update`+`relate` are strictly unused by the core — tiny cut. **The earlier "trim 9 tools" oversold the savings.** |
| **Entity KG** (`entities`, `entity_edges`, `memory_entities`, migration 008) | **COLLAPSE** | **Verified: zero `src/` references.** Imported from Quick Desktop, never wired into the core. Highest-confidence cut. |
| **Schema feature** (`type='schema'` + `derived_from` + migration 007 indexes + `db.find_schemas_for_memory`/`get_schema_with_constituents` + the `schema_context` block in `memory_search`) | **COLLAPSE** | **Verified functionally inert:** live DB has **0** `type='schema'` memories and **6** `derived_from` edges. The read path is wired (3 code sites) but the **write path was never built**, so `schema_context` always returns empty. NB: there is **no `schema_type` column** — `ARCHITECTURE.md` L92 is a doc bug to fix. Clean cut, slightly more wiring to unpick than the entity KG. |
| `encoding_context` (migration 006) | **KEEP-cheap *or* ACTIVATE (decide)** | **Verified:** it *is* the rerank `context_overlap` term (`src/search.py`, weight 0.10, tested). **But** only **1 of 121,226** memories populate it → the term is ~always 0.0. Wired+tested but vestigial in practice. Either keep as-is (negligible cost, on-theme Godden & Baddeley grounding) or wire the one surviving capture path to populate it so the signal becomes real. Not a clean delete. |
| Multi-channel capture (QD sync + 14 `quick_desktop_*` micro-categories, Crawlee/bookmarks, Capture API server, browser, mobile, Slack-graph import, YouTube) | **COLLAPSE → 1 flexible path + distillation** | See the measured tension below. Collapse the *machinery*; preserve long-form text ingest + the distillation step. |
| Eval harness (~12 scripts in `scripts/eval/`) | **COLLAPSE → 1** | golden/curated/ablation/cold_warm/consolidation/trends were *tuning scaffolding*. Keep `recall_check.py` as a health gate; archive the rest. |
| Ops sprawl (launchd jobs, GPG+rclone to GDrive+S3, SSO mgmt, weekly digest, redistill) | **COLLAPSE** | Keep one backup path + the dream-cycle→Express schedule. The rest mostly maintains collapsed subsystems. |

## The central tension: capture breadth vs. synthesis quality — now measured
The earlier worry was that "collapse to one path starves the differentiator." The provenance of the
8 accepted insights in run `7c712903` makes the picture precise:

- **Synthesis fuel is concentrated, not uniform.** `distilled_chat` (31 source-refs, in **6 of 8**
  insights) and `quick_desktop_doc` (13 refs, in **6 of 8**) dominate; `article` (5/3),
  `quick_desktop_decision` (3/3), and `youtube` (2/2) are a useful long tail.
- **Raw volume ≠ value.** `kiro_ide_chat` is **60% of the corpus (73,250 memories)** yet contributed
  **zero** source-refs to any accepted insight. Its value is realized only *after* distillation into
  `distilled_chat` (4,281 memories, the top fuel). **The distillation step is the value, not the raw
  capture.**
- **Diversity is load-bearing — for the best insights.** The flagship goal-directed insight (AWS
  corpus → Anthropic DNB role) spanned **4 source_types / 12 sources**. The cross-source insights are
  the crown jewels, so the surviving path must stay *content-diverse*, not narrow.

**Design implication:** collapse the capture *machinery* hard (drop the YouTube/Crawlee/Capture-API/
Slack-graph daemons and the 14 `quick_desktop_*` micro-categories), but **preserve two things**:
(1) a single **flexible long-form text/doc ingest** (the existing chat/document extraction already
carries PDFs, articles, docs) and (2) the **distillation step** that turns raw chat into
`distilled_chat`. One flexible path + distillation preserves the diversity that the best insights
need, without the many bespoke channels.

## Simplest architecture (target sketch)
```
long-form text + chat ─▶ [distill] ─▶ memories (+embeddings, +relationships)  [Postgres/pgvector]
                                          │
                                          ▼
                           hybrid retrieval + rerank   (via MCP: search/read/list/graph)
                                          │
                                          ▼
                           dream cycle  (synthesis, consensus-gated)
                                          │
                                          ▼
                           Express  (delivery: brief / memory_brief)
                                          │
                                          ▼
                           aimed at a real goal (a role, a decision, a question)
                                          │
                                          ▼
                           cited evidence + a drafted action
```
One schedule (nightly dream cycle, then Express). One backup. One health check (`recall_check`).

## What "simpler" removes
- The entire **entity-KG** subsystem + its import/enrichment scripts + migration-008 tables (verified unused).
- The inert **schema feature**: `type='schema'`/`derived_from` read path, migration-007 indexes, and the 3 code sites that wire `schema_context` (verified inert) — and fix the `schema_type` doc bug.
- **~12 eval scripts → 1** (`recall_check.py`).
- **N capture channels → 1 flexible text path + distillation**; drop the Capture API server, browser/mobile, Slack-graph import, YouTube, Crawlee/bookmarks, and the 14 `quick_desktop_*` micro-categories.
- Most **launchd jobs**.
- *(optional)* the `memory_update` + `memory_relate` MCP tools.

Net: **the demo is the spine; everything not on the spine is archived or removed.**

## Open decisions (still your call — now evidence-framed)
1. **How aggressive on capture collapse.** The data says raw volume (`kiro_ide_chat`, 60%) is
   low-yield and distillation + doc ingest are high-yield → there's room to be *aggressive on raw
   capture* while *protecting distillation + long-form ingest*. (Recommendation in chat.)
2. **Rewrite vs. incremental refactor.** Verified-inert KG + schema feature + eval scaffolding make a
   *prune-in-place* first cut low-risk; a green-field minimal repo is cleaner but higher-effort.
3. **Drop vs. dormant** for the KG tables (migration 008) and the schema-feature indexes (migration
   007) — unused either way; dropping is cleaner, keeping is cheaper/reversible.
4. **`encoding_context`: keep-cheap vs. activate vs. cut** — it's the rerank context term but
   unpopulated (1/121,226).

## Verify-before-cut checklist — CLOSED
- [x] **Entity KG unused by the core** — verified (no `src/` references).
- [x] **`schema_type` / `encoding_context` usage** — verified. No `schema_type` column exists (doc bug); the schema *feature* is wired but inert (0 schema memories / 6 `derived_from`). `encoding_context` *is* the rerank `context_overlap` term but is populated on only 1/121,226 memories.
- [x] **Richest single capture path** — verified. `distilled_chat` + `quick_desktop_doc` carry 6/8 accepted insights each; raw `kiro_ide_chat` (60% of corpus) contributed zero. The distillation step is the value; keep one flexible long-form text path to preserve cross-source diversity.
- [x] **MCP tools the dream cycle + Express require** — verified. 5 core (`search`,`read`,`list`,`graph`,`brief`) + 2 capture/showcase (`create`,`learn`); only `update`+`relate` are strictly trimmable.
