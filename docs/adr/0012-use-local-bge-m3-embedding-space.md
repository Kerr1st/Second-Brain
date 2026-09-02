---
status: accepted
---

# Use local BGE-M3 as the active embedding space

Second Brain uses Ollama with BGE-M3 as its active embedding implementation. The public embedding
Interface remains `generate_embedding(text)` and records the vector-space identity as
`ollama:bge-m3:1024`. BGE-M3 was selected because its published model metadata matches the existing
1,024-dimension PostgreSQL vector shape, supports an 8,192-token context, and can execute locally
without AWS credentials.

The prior Amazon Titan vectors are immutable migration evidence. The schema renames their column
to `legacy_embedding` and creates a new `embedding` column for the active local space. Retrieval
never compares vectors from the two spaces. PostgreSQL full-text retrieval continues to cover the
whole corpus while the local vector space is filled incrementally.

## Considered options

- Restore AWS authentication and retain Titan as the only embedding implementation.
- Overwrite Titan vectors in place without preserving the prior space.
- Add a fully normalized multi-model embedding table before proving a second active model.
- Preserve Titan in a legacy column and activate BGE-M3 through a small provider-neutral Interface.

## Consequences

The Codex operational canary can complete without AWS infrastructure. Existing callers keep their
small Interface and Ollama can embed batches natively. A resumable job will fill the active column;
until then, hybrid retrieval combines local vectors where available with full-text results across
the corpus.

The legacy column is not deleted merely because local backfill completes. Retirement requires
real-query retrieval evidence and a separate explicit decision. A normalized multi-space table is
deferred until a second simultaneously active vector space proves that complexity is necessary.

## Evidence

- [Ollama BGE-M3 model page](https://ollama.com/library/bge-m3)
- [Ollama BGE-M3 model metadata](https://ollama.com/library/bge-m3/blobs/6eaafd7b20ee)
- [BAAI BGE-M3 model card](https://huggingface.co/BAAI/bge-m3)
