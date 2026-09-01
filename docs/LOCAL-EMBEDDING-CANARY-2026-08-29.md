# Local Embedding and Codex Canary Evidence — 2026-08-29

> **Decision:** keep the preserved Titan vector space. Local BGE-M3 is active and proven for new
> Codex processing, but raw-source backfill and a broader relevance review remain in progress.

## Runtime and schema proof

- Ollama 0.33.0 is installed as a user LaunchAgent with BGE-M3 available locally.
- A real probe returned a normalized 1,024-dimension vector in space
  `ollama:bge-m3:1024`.
- Production migrations 014 and 015 preserve 114,941 Titan vectors in `legacy_embedding`, create a
  separate active `embedding` column, record `embedding_space`, and reject any active-space
  identity other than `ollama:bge-m3:1024`.
- The active Interface fails closed if the legacy Bedrock provider is selected, preventing Titan
  vectors from entering the BGE-M3 column.

## Codex operational proof

The allowlisted Codex task `01a014fd-89cf-73c0-a4ef-001e0f89d231` had already captured its source
record, but its Semantic Processing Cursor was null after Titan failed with `NoCredentialsError`.
The local retry reported:

```json
{"captured":0,"eligible":1,"failed":0,"semantic_processed":1,"semantic_retried":1,"unchanged":1}
```

The retry created topic segment `e21d7f84-5086-4c39-ab17-5fc7627bd1f2` with:

- source URL `codex://01a014fd-89cf-73c0-a4ef-001e0f89d231#segment-0`;
- both supporting Codex turn IDs;
- parent link to captured task `7f80a946-fc4d-4d92-bd4e-4c158eb07c28`; and
- active space `ollama:bge-m3:1024`.

No task recapture, duplicate source record, or partial semantic result was produced.

## Gradual corpus fill

The first 100 preserved rows proved bounded resume behavior. The next run prioritized and completed
all 4,556 preserved non-source memories—decisions, insights, syntheses, connections, and questions.
Raw sources are now filling in batches of at most 5,000 through the low-priority hourly LaunchAgent
`com.second-brain.reembed-local`. Each 32-row database batch commits independently, and an atomic
lock prevents overlapping runs.

## Retrieval evidence

The HNSW indexed result matched forced exact vector search at recall@10 of 1.00 for all 10 existing
real queries (mean 1.00; required threshold 0.85).

A five-query relevance inspection found strong direct results for:

- four-evaluator, three-vote Dream Cycle consensus;
- the decision to replace S3 backup with local, Google Drive, and git; and
- the Memory Context Broker approach to Codex integration.

The six-hour Codex capture query retrieved related Codex evidence but did not directly surface the
policy, and the Correction Episode-to-Steering query returned older steering material rather than
the newer governance decision. These misses are retained as evaluation evidence. Full-text search
now includes unembedded rows, so raw evidence remains discoverable during local backfill without
mixing vector spaces.

## Acceptance decision

- Accept local BGE-M3 for new ingestion, query embeddings, and the Codex canary.
- Continue the resumable raw-source fill.
- Keep Titan immutable in `legacy_embedding`.
- Re-run the real relevance queries after source fill and improve only observed misses.
- Do not drop Titan unless a later explicit retirement decision is supported by retrieval evidence.
