# Streamlining Design Target (DRAFT)

**Status: DRAFT for iteration. No code changes.** This is the design-on-paper that the quality
gate unlocked (see `QUALITY-BASELINE-2026-06-06.md`). The decisions on *how simple* and
*rewrite-vs-refactor* remain explicitly **open** — this doc frames them, it does not settle them.

## North Star
Accomplish everything the system does now, but **simpler — to best demonstrate "memory that
thinks" in one sitting.** Primary purpose: a legible, narratable demonstration. Secondary: a tool
the user keeps using. Simpler = fewer moving parts a reader must hold in their head to "get it."

## The spine that must survive
Three sacred capabilities + the minimum substrate they ride on:
1. **Retrieval substrate** — `memories` + embeddings + hybrid search (BM25 + vector + RRF) + the
   cognitive-grounded rerank.
2. **Synthesis** — the consensus-gated dream cycle (Explorer → Thinker → 4 evaluators).
3. **Delivery** — Express (`brief` / push / `memory_brief` + feedback loop).
Plus the connective minimum: one ingestion path, `memory_relationships`, the MCP server.

## Keep / Collapse (with reasoning)
| Subsystem | Verdict | Reasoning |
|---|---|---|
| `memories` + embeddings + hybrid + rerank | **KEEP** | Substrate for all three capabilities. |
| `memory_relationships` (contradicts, etc.) | **KEEP** | Feeds Express contradictions + dream-cycle edges. Verified load-bearing. |
| Dream cycle (4-agent) | **KEEP** | The differentiator; validated on 4.8. |
| Express (brief/push/memory_brief/feedback) | **KEEP** | The delivery half; makes synthesis legible. |
| MCP server | **KEEP (trim)** | How agents read/write. Trim 9 tools → only those the core needs. |
| **Entity KG** (`entities`, `entity_edges`, `memory_entities`, migration 008) | **COLLAPSE** | **Verified: zero references in `src/`.** Not read by retrieval, synthesis, or Express. Imported from Quick Desktop, never wired into the core. Highest-confidence cut. |
| Multi-channel capture (QD sync, Crawlee, Capture API server, browser, mobile, Slack graph import) | **COLLAPSE → 1 path** | Most of the operational surface. Collapse the *machinery* to the single richest ingest path (see tension below). |
| Eval harness (~12 scripts in `scripts/eval/`) | **COLLAPSE → 1** | golden/curated/ablation/cold_warm/consolidation/trends were *tuning scaffolding*. Keep `recall_check.py` as a health gate; archive the rest. |
| Ops sprawl (9 launchd jobs, GPG+rclone to GDrive+S3, SSO mgmt, weekly digest, redistill) | **COLLAPSE** | Keep one backup path + the dream-cycle→Express schedule. The rest mostly maintains collapsed subsystems. |
| Schema extras (`schema_type`, `encoding_context`) | **INVESTIGATE** | Rerank uses `encoding_context` (context_overlap term) → likely keep. `schema_type` usage unclear → verify before cutting. |

## The central tension: capture breadth vs. synthesis quality
The best 4.8 insights drew on *diverse* sources — a leadership PDF (`AITransformation100`),
file-management decisions, an Anthropic job description, a CSS playbook. **Synthesis quality is
partly a function of corpus diversity.** So "collapse capture to one path" risks starving the
differentiator. Design implication: collapse the *machinery* (many ingest daemons, the KG import,
Slack-graph import) but **preserve content diversity** by keeping one flexible ingest path (e.g.,
the existing chat/document extraction, which already carries PDFs, chats, and docs) rather than
many bespoke channels. This is the most important design judgment to get right.

## Simplest architecture (target sketch)
```
one ingest path → memories (+embeddings, +relationships) in Postgres/pgvector
   → hybrid retrieval + rerank (via MCP)
      → dream cycle (synthesis, consensus-gated)
         → Express (delivery: brief / email / in-context)
```
One schedule (nightly dream cycle, then Express). One backup. One health check (`recall_check`).

## What "simpler" removes
- The entire entity-KG subsystem + its import/enrichment scripts + migration-008 tables (verified unused).
- ~12 eval scripts → 1.
- N capture channels → 1; drop the Capture API server, browser/mobile, Slack-graph import.
- Most launchd jobs.
Net: **the demo is the spine; everything not on the spine is archived or removed.**

## Open decisions (deferred — your call)
1. **How aggressive on capture collapse** — the breadth-vs-quality tension above.
2. **Rewrite vs. incremental refactor** (framing #4). The verified-unused KG + eval scaffolding
   make a *prune-in-place* first cut low-risk; a green-field minimal repo is cleaner but higher-effort.
3. **Drop vs. dormant** for the KG tables — unused either way; dropping is cleaner, keeping is cheaper.

## Verify-before-cut checklist (honesty)
- [x] Entity KG unused by the core — **verified** (no `src/` references).
- [ ] `schema_type` / `encoding_context` actual usage in the rerank path.
- [ ] Which single capture path carries the richest content diversity for synthesis.
- [ ] Which MCP tools the dream cycle + Express actually require (to trim the rest).
