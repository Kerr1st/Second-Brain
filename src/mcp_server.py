"""MCP server — exposes the Second Brain to AI agents.

Run: python -m src.mcp_server (stdio transport for Kiro/Claude Code)

Input validation happens here, at the boundary (vocabularies and helpers in
``src/validation.py``). The database has no CHECK constraints on type/status/
relation_type, so this is what keeps ``type="bogus"`` out of the corpus, and it
is what stops a malformed id from surfacing a raw Postgres error (SQL text,
constraint names) to the client. Invalid input is reported in the shape of each
tool's normal return type instead of raising:

* tools returning ``dict`` (memory_read, memory_graph, memory_search) return
  ``{"error": "<message>"}``;
* tools returning ``str`` (memory_create, memory_update, memory_relate,
  memory_learn) return ``"Error: <message>"``;
* memory_list returns ``list[dict]`` and raises ``ValueError`` instead — FastMCP
  reports that as an ``isError`` result carrying only the message.

Database failures return a safe message without SQL, credentials or private values.
Programming errors remain visible to developers.
"""

import json
from functools import wraps
from typing import Annotated, Any
from pydantic import WithJsonSchema
from psycopg2.pool import PoolError
import psycopg2.errors
from mcp.server.fastmcp import FastMCP
from datetime import datetime, timedelta, timezone
from src.db import (
    create_memory, get_memory, chunk_parent_id, update_memory, list_memories, get_arrivals_summary, iso as _iso,
    create_relationship, get_relationships, find_temporal_neighbors,
    find_schemas_for_memories,
)
from src.search import hybrid_search, rerank, increment_access_count
from src.embeddings import generate_embedding, embed_leaf
from src.classify import classify_memory
from src.depth import compute_depth_score
from src.project import normalize_project_tag
from src.validation import (
    MEMORY_TYPES, FILTERABLE_TYPES, MEMORY_STATUSES, RELATION_TYPES, SOURCE_TYPES, ValidationError,
    uuid_or_error, one_of, optional_one_of, require_text, require_mapping,
    clamp_limit, clamp_days, clean_tags, clean_topics,
)
from src import express

mcp = FastMCP("Second Brain")

#: Integer parameters. FastMCP validates arguments with pydantic, which turns
#: ``true`` into ``1`` for an ``int`` parameter before clamp_limit/clamp_days see
#: it, so booleans were accepted (M-4). These keep the integer schema but pass the
#: raw JSON value to the validators, which reject booleans in the tool's own shape.
Int = Annotated[Any, WithJsonSchema({"type": "integer"})]
OptionalInt = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]})]


def _guarded(fn, *args, **kwargs):
    """Translate residual data-shape errors without exposing database diagnostics."""
    try:
        return fn(*args, **kwargs)
    except psycopg2.errors.InvalidTextRepresentation as exc:
        raise ValidationError("Invalid value format") from exc
    except psycopg2.errors.ForeignKeyViolation as exc:
        raise ValidationError("A referenced memory does not exist") from exc


def _boundary(shape):
    """Keep PostgreSQL and pool diagnostics off the tool wire, preserving signatures."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ValidationError as exc:
                message = str(exc)
            except (psycopg2.OperationalError, psycopg2.InterfaceError, PoolError):
                message = "Memory storage is temporarily unavailable; retry shortly"
            except psycopg2.Error:
                message = "Memory storage could not complete this operation"
            if shape == "dict":
                return {"error": message}
            if shape == "str":
                return f"Error: {message}"
            raise ValueError(message) from None
        return wrapped
    return decorate


@mcp.tool()
@_boundary("str")
def memory_create(
    type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_type: str | None = None,
    source_url: str | None = None,
    metadata: dict | None = None,
    project: str | None = None,
    encoding_context: str | None = None,
) -> str:
    """Create a new memory and generate its embedding.

    type: idea, synthesis, research, insight, question, decision, priority, project, connection, source
    source_type: manual, file, article, youtube, kiro_cli_chat, kiro_ide_chat, slack, outlook_email, browser_history
      (optional; any other registered capture-channel label is accepted, unknown labels are rejected)

    DEPTH REQUIREMENT for ideas/insights/decisions: explain WHAT, WHAT HAPPENS when violated, and WHY.
    Include 'Questions this answers:' with 3-5 natural-language queries.

    encoding_context: Optional. Describe what you were working on when creating this memory
    (e.g., "debugging auth flow", "reading about CLS theory"). Improves future retrieval
    by matching the cognitive context at search time (Godden & Baddeley 1975).

    Invalid input (unknown type, empty title/content, tags that are not a list of
    strings, metadata that is not an object) returns 'Error: ...' and writes nothing.
    """
    try:
        type = one_of(type, MEMORY_TYPES, "type")
        title = require_text(title, "title")
        content = require_text(content, "content")
        tags = clean_tags(tags)
        metadata = require_mapping(metadata, "metadata")
        source_type = optional_one_of(source_type, SOURCE_TYPES, "source_type")
    except ValidationError as e:
        return f"Error: {e}"

    # Leaf write: embed a bounded prefix of long content and record the fact,
    # rather than sending an over-limit string to the provider.
    embedding, embed_meta = embed_leaf(content, embed=generate_embedding)
    mem_class = classify_memory(type, content)
    depth_score = compute_depth_score(content)
    project = normalize_project_tag(project)
    metadata = dict(metadata or {})  # copy: never mutate the caller's dict
    metadata["depth_score"] = depth_score
    metadata.update(embed_meta)
    mid = create_memory(
        type=type, title=title, content=content, embedding=embedding,
        tags=tags, source_type=source_type, source_url=source_url, metadata=metadata,
        mem_class=mem_class, project=project, encoding_context=encoding_context,
    )

    warnings = []
    if type in ("idea", "synthesis", "insight", "decision"):
        if depth_score < 0.3:
            warnings.append("Low depth: add 'because...' or 'when X, then Y' to explain WHY.")
        if "questions this answers" not in content.lower():
            warnings.append("Missing 'Questions this answers:' — add 3-5 natural queries for better retrieval.")

    result = f"Created memory {mid}"
    if warnings:
        result += "\n⚠ " + "\n⚠ ".join(warnings)
    return result


def search_hits(query, type=None, limit=10, project=None, source_type=None, since_days=None, status=None):
    """Validate, embed, retrieve and rerank; shared by this server and ``src.mcp_readonly``.

    Each result is a distinct parent: the best-fused chunk of a document stands for
    it (``_other_chunks`` counts the others in the candidate pool) and the next
    distinct parents fill ``limit``. Raises ValidationError. Returns (results, project)."""
    query = require_text(query, "query")
    type = optional_one_of(type, FILTERABLE_TYPES, "type")
    status = optional_one_of(status, MEMORY_STATUSES, "status")
    source_type = optional_one_of(source_type, SOURCE_TYPES, "source_type")
    limit = clamp_limit(limit)
    since_days = clamp_days(since_days, "since_days")
    project = normalize_project_tag(project)
    embedding = generate_embedding(query)
    created_after = None
    if since_days is not None and since_days > 0:
        created_after = datetime.now(timezone.utc) - timedelta(days=since_days)
    results = hybrid_search(query, embedding, limit=limit, type=type, project=project,
                            source_type=source_type, created_after=created_after, status=status,
                            per_parent_cap=1)
    return rerank(results, query, query_project=project), project


def format_hit(r):
    return {"id": str(r["id"]), "title": r["title"], "type": r["type"],
            "source_type": r.get("source_type"),
            "created_at": _iso(r.get("created_at")),
            "parent_id": str(r["parent_id"]) if r.get("parent_id") else None,
            "other_matching_chunks": int(r.get("_other_chunks") or 0),
            "score": round(float(r.get("rerank_score", 0)), 3),
            "content": r["content"][:500], "tags": r.get("tags") or []}


@mcp.tool()
@_boundary("dict")
def memory_search(query: str, type: str | None = None, limit: Int = 10, project: str | None = None,
                  source_type: str | None = None, since_days: OptionalInt = None,
                  status: str | None = None) -> dict:
    """Semantic + keyword search across all memories. Hybrid retrieval (full-text +
    vector + RRF) with utility reranking (recency, type boost, token overlap, access
    reinforcement). Also returns `temporal_context` (memories near the top hit in time)
    and `schema_context` (higher-level notes the top hits belong to).

    Filters (all optional, combine freely):
    - type: memory type, e.g. 'decision', 'insight', 'synthesis'.
    - source_type: capture channel, e.g. 'distilled_chat', 'kiro_cli_chat', 'kiro_ide_chat', 'article'.
    - since_days: only memories created in the last N days (e.g. 30 = last month).
    - status: e.g. 'active' to exclude superseded items.
    - project: scope to a project tag.

    limit is clamped to 1–100 and since_days to 0–365. An empty query, an unknown
    type/status filter or a non-integer (e.g. boolean) limit returns {"error": "..."}
    instead of results.

    Each result is a distinct document: the best-matching chunk stands for its parent
    (`parent_id`), `other_matching_chunks` says how many more of its chunks matched.

    Iterate for best results: if hits are thin or off-target, refine the query or
    filters and search again, then call memory_read(id) for full content or
    memory_graph(id) to follow related memories."""
    try:
        results, project = search_hits(query, type=type, limit=limit, project=project,
                                       source_type=source_type, since_days=since_days, status=status)
    except ValidationError as e:
        return {"error": str(e)}

    increment_access_count([str(r["id"]) for r in results])

    formatted = [format_hit(r) for r in results]

    # Temporal context: find neighbors of the top result, deduplicated
    temporal_context = []
    if results:
        top_id = str(results[0]["id"])
        top_record = get_memory(top_id)
        if top_record and top_record.get("created_at"):
            result_ids = {str(r["id"]) for r in results}
            neighbors = find_temporal_neighbors(top_id, top_record["created_at"], limit=3)
            for n in neighbors:
                if n["id"] not in result_ids:
                    created_at = n["created_at"]
                    temporal_context.append({
                        "id": n["id"],
                        "title": n["title"],
                        "type": n["type"],
                        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                        "relation": "temporal_neighbor",
                    })
            temporal_context = temporal_context[:3]

    # Schema context: if any top results belong to schemas, surface the schema
    schema_context = []
    if results:
        top_ids = [str(r["id"]) for r in results[:5]]  # check top 5 results
        schemas_by_memory = find_schemas_for_memories(top_ids)  # one round trip, not one per result
        seen_schemas = set()
        for mid in top_ids:
            for s in schemas_by_memory.get(mid, []):
                sid = str(s["id"])
                if sid not in seen_schemas:
                    seen_schemas.add(sid)
                    schema_context.append({
                        "id": sid,
                        "title": s["title"],
                        "type": "schema",
                        "content": (s.get("content") or "")[:300],
                    })
        schema_context = schema_context[:3]

    return {"results": formatted, "temporal_context": temporal_context, "schema_context": schema_context}


@mcp.tool()
@_boundary("dict")
def memory_read(memory_id: str) -> dict:
    """Read a single memory by ID and return its full content. Use this to expand a
    memory_search hit whose 500-char preview was truncated, before acting on it.

    Returns {"error": "..."} for a malformed id or a memory that does not exist."""
    try:
        memory_id = uuid_or_error(memory_id, "memory_id")
        mem = _guarded(get_memory, memory_id)
    except ValidationError as e:
        return {"error": str(e)}
    if not mem:
        return {"error": "Not found"}
    out = {}
    for k, v in mem.items():
        if k in ("embedding", "search_vector"):  # vectors and the tsvector are storage detail (F-057)
            continue
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif k == "id":
            out[k] = str(v)
        else:
            out[k] = v
    return out


@mcp.tool()
@_boundary("str")
def memory_update(
    memory_id: str,
    title: str | None = None,
    content: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    type: str | None = None,
) -> str:
    """Update fields on a memory. Pass only the fields to change.

    status: active, superseded, archived
    type: idea, synthesis, research, insight, question, decision, priority, project, connection, source

    Returns 'Error: ...' (and changes nothing) for a malformed or unknown memory_id,
    an unknown status/type, an empty title/content, or when no field is passed."""
    try:
        memory_id = uuid_or_error(memory_id, "memory_id")
        if title is not None:
            title = require_text(title, "title")
        if content is not None:
            content = require_text(content, "content")
        if status is not None:
            status = one_of(status, MEMORY_STATUSES, "status")
        if type is not None:
            type = one_of(type, MEMORY_TYPES, "type")
        tags = clean_tags(tags)
        fields = {k: v for k, v in {
            "title": title, "content": content, "status": status,
            "tags": tags, "summary": summary, "type": type,
        }.items() if v is not None}
        if not fields:
            raise ValidationError("nothing to update: pass at least one of title, content, "
                                  "status, tags, summary, type")
        existing = _guarded(get_memory, memory_id)
    except ValidationError as e:
        return f"Error: {e}"
    if not existing:
        return "Error: Not found"

    if "content" in fields:
        fields["embedding"], embed_meta = embed_leaf(fields["content"], embed=generate_embedding)
        existing_metadata = existing.get("metadata") or {}
        if isinstance(existing_metadata, str):
            import json as _json
            existing_metadata = _json.loads(existing_metadata)
        mem_type = fields.get("type") or existing.get("type") or "idea"
        fields["mem_class"] = classify_memory(mem_type, fields["content"])
        existing_metadata["depth_score"] = compute_depth_score(fields["content"])
        # The truncation record describes the current embedding only.
        for key in ("embedding_truncated", "original_chars", "embedded_chars"):
            existing_metadata.pop(key, None)
        existing_metadata.update(embed_meta)
        fields["metadata"] = existing_metadata
    try:
        _guarded(update_memory, memory_id, **fields)
    except ValidationError as e:
        return f"Error: {e}"
    return f"Updated memory {memory_id}"


@mcp.tool()
@_boundary("str")
def memory_relate(
    source_id: str, target_id: str, relation_type: str, note: str | None = None
) -> str:
    """Create a relationship between two memories.

    relation_type: supports, contradicts, extends, inspires, derived_from, related_to, superseded_by

    Both ids must be existing memories and must differ; an unknown relation_type or a
    missing/malformed id returns 'Error: ...' and writes nothing.
    """
    try:
        source_id = uuid_or_error(source_id, "source_id")
        target_id = uuid_or_error(target_id, "target_id")
        relation_type = one_of(relation_type, RELATION_TYPES, "relation_type")
        if source_id == target_id:
            raise ValidationError("source_id and target_id must be different memories")
        missing = [name for name, mid in (("source_id", source_id), ("target_id", target_id))
                   if not _guarded(get_memory, mid)]
        if missing:
            raise ValidationError(f"Not found: {', '.join(missing)}")
        _guarded(create_relationship, source_id, target_id, relation_type, note)
    except ValidationError as e:
        return f"Error: {e}"
    return f"Related {source_id} → {relation_type} → {target_id}"


@mcp.tool()
@_boundary("list")
def memory_list(
    type: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    limit: Int = 20,
    since_days: OptionalInt = None,
) -> list[dict]:
    """List/browse recent memories by filter (type, source_type, status, since_days) —
    newest first, no relevance ranking. Use memory_search for relevance; use this to
    enumerate or audit, e.g. memory_list(source_type='slack', since_days=2) to see
    every Slack conversation-day captured since the day before yesterday.

    A chunk whose parent matches the same filters is left out (the parent stands
    for it); a chunk whose parent is filtered out is listed. Ties on created_at are
    ordered by id.

    limit is clamped to 1–100 and since_days to 0–365. An unknown type/status filter
    or a non-integer (e.g. boolean) limit raises ValueError (reported by the server
    as an error result) rather than returning an empty list — a list has no clean
    slot for an error object, and a silent [] would look like "nothing matched"."""
    type = optional_one_of(type, FILTERABLE_TYPES, "type")
    status = optional_one_of(status, MEMORY_STATUSES, "status")
    source_type = optional_one_of(source_type, SOURCE_TYPES, "source_type")
    limit = clamp_limit(limit)
    since_days = clamp_days(since_days, "since_days")
    created_after = None
    if since_days is not None and since_days > 0:
        created_after = datetime.now(timezone.utc) - timedelta(days=since_days)
    results = list_memories(type=type, status=status, source_type=source_type, limit=limit,
                            created_after=created_after, collapse_chunks=True)
    return [
        {"id": str(r["id"]), "title": r["title"], "type": r["type"],
         "source_type": r["source_type"], "created_at": _iso(r["created_at"]),
         "parent_id": str(r["parent_id"]) if r.get("parent_id") else None,
         "tags": r["tags"]}
        for r in results
    ]


@mcp.tool()
@_boundary("dict")
def memory_recent_activity(days: Int = 7, samples_per_source: Int = 5) -> dict:
    """What arrived in the last N days, by capture channel: document and memory counts
    per source_type plus the newest document titles for each. Channels are the user's
    life streams — slack (one document per conversation-day), outlook_email,
    outlook_calendar, browser_history (one per day), article, youtube, kiro_cli_chat,
    quick_desktop_* — and 'generated:<type>' for memories the system produced itself
    (syntheses, distilled insights). Call this first to orient exploration on what is
    new, then memory_list(source_type=..., since_days=...) or memory_search(...,
    since_days=...) to drill into a channel."""
    days = clamp_days(days, "days", lo=1, hi=90)
    if days is None:
        raise ValidationError("days must be an integer")
    samples_per_source = clamp_limit(samples_per_source, lo=0, hi=20)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    by_source = get_arrivals_summary(since, samples_per_source=samples_per_source)
    return {
        "since": _iso(since), "days": days,
        "total_documents": sum(s["documents"] for s in by_source),
        "total_memories": sum(s["memories"] for s in by_source),
        "by_source": by_source,
    }



@mcp.tool()
@_boundary("dict")
def memory_graph(memory_id: str) -> dict:
    """Get a memory plus its linked relationships (target_id, relation_type, note).
    Use it to traverse from a memory_search hit to related memories, then memory_read
    those target_ids to follow a chain of reasoning.

    Relationships attach mostly to parent memories, but chunks carry edges too
    (the auto-linker and older writers link chunks). Given a chunk id, the
    graph combines the chunk's own edges with its parent's, with
    "resolved_from_chunk" (the id passed) and "memory_id" (the parent id) set;
    each relationship's "anchor_id" says which of the two it hangs from.

    Returns {"error": "..."} for a malformed id or a memory that does not exist."""
    resolved_from = None
    try:
        memory_id = uuid_or_error(memory_id, "memory_id")
        mem = _guarded(get_memory, memory_id)
        if not mem:
            return {"error": "Not found"}
        anchors = [memory_id]
        parent_id = chunk_parent_id(mem)
        parent = _guarded(get_memory, parent_id) if parent_id else None
        if parent:
            resolved_from, memory_id, mem = memory_id, str(parent["id"]), parent
            anchors = [memory_id, resolved_from]
        rels = [(anchor, r) for anchor in anchors for r in _guarded(get_relationships, anchor)]
    except ValidationError as e:
        return {"error": str(e)}

    def _edge(anchor, r):
        incoming = str(r["target_id"]) == anchor
        return {"target_id": str(r.get("neighbor_id") or (r["source_id"] if incoming else r["target_id"])),
                "relation_type": r["relation_type"], "note": r["note"],
                "direction": "incoming" if incoming else "outgoing",
                "title": r.get("title"), "type": r.get("type"),
                **({"anchor_id": anchor} if resolved_from else {})}

    edges, seen = [], set()
    for anchor, r in rels:
        key = (str(r.get("source_id")), str(r.get("target_id")), r.get("relation_type"))
        if key in seen:
            continue
        seen.add(key)
        edges.append(_edge(anchor, r))
    resolution = {"resolved_from_chunk": resolved_from, "memory_id": memory_id} if resolved_from else {}
    return {
        **resolution,
        "memory": {"id": str(mem["id"]), "title": mem["title"], "type": mem["type"]},
        "relationships": edges,
    }


@mcp.tool()
@_boundary("str")
def memory_learn(
    content: str,
    topics: str,
    source: str | None = None,
) -> str:
    """Internalize external knowledge by connecting it to what you already know.

    Use when the user shares an article, talk, paper, video, or any external content.

    This is a TWO-STEP process:
    STEP 1 (this tool): Provide the content and 2-4 key topics. The tool searches
    existing memories for related knowledge and returns both together.

    STEP 2 (you, the agent): Synthesize connections and call memory_create for each
    atomic insight. Each insight MUST:
    1. State the insight (WHAT)
    2. Connect it to an existing memory — extends, contradicts, or explains it (HOW)
    3. Explain the causal mechanism — why this matters for future work (WHY)
    4. Include 'Questions this answers:' as future work queries
    5. Prefix the title with 'From [source]:' for attribution

    Create ONE memory per atomic insight, not one giant summary.
    The value is in the CONNECTIONS to existing knowledge, not the summary.

    Empty content, or topics with no non-blank comma-separated entry, returns 'Error: ...'.
    """
    try:
        content = require_text(content, "content")
        topic_list = clean_topics(topics)
    except ValidationError as e:
        return f"Error: {e}"
    # Search existing memories for each topic
    related_sections = []
    for topic in topic_list:
        embedding = generate_embedding(f"principles and lessons for {topic}")
        results = hybrid_search(f"principles and lessons for {topic}", embedding, limit=5)
        results = rerank(results, topic)
        if results:
            lines = [f"### Existing knowledge: \"{topic}\""]
            for r in results[:3]:
                lines.append(f"- [{r['type']}] {r['title']}")
                lines.append(f"  {r['content'][:300]}")
            related_sections.append("\n".join(lines))

    source_line = f"**Source:** {source}\n" if source else ""
    related = "\n\n".join(related_sections) if related_sections else "(No related memories found — this is new territory)"

    return "\n".join([
        "# External Knowledge to Internalize\n",
        source_line,
        "## New Content\n",
        content,
        "\n## Related Existing Memories\n",
        related,
        "\n## Your Task",
        "Synthesize: for each key insight in the new content, call memory_create with:",
        "1. WHAT is the insight",
        "2. HOW it connects to an existing memory above (extends, contradicts, explains)",
        "3. WHY this matters for future work (causal mechanism)",
        "4. 'Questions this answers:' phrased as future work queries",
        f"5. Prefix title with: 'From {source or '[source]'}:'",
        "\nCreate ONE memory per atomic insight, not one giant summary.",
    ])


@mcp.tool()
@_boundary("str")
def memory_brief(window_days: Int = 14, use_llm: bool = False) -> str:
    """Surface what the second brain has synthesized but hasn't told you yet.

    Call this at the START of a work session to get oriented: it volunteers recent
    cross-project insights, detected contradictions (decisions you may be reversing),
    high-value memories you haven't revisited in a while, the latest activity digest,
    and open questions — connections you'd otherwise have to go looking for.

    This is the "Express" surface: the system pushing its synthesis to you rather than
    waiting to be queried. Use memory_search instead when you have a specific question.

    Args:
        window_days: how far back to pull recent dream-cycle insights (default 14;
            clamped to 0–365).
        use_llm: write polished headlines via an LLM editor pass (metered, policy-checked Bedrock call); default False uses fast deterministic ranking.

    Returns a scannable Markdown briefing (headlines first, detail below).
    """
    try:
        window_days = clamp_days(14 if window_days is None else window_days, "window_days")
        briefing = express.compose_briefing(window_days=window_days)
        briefing = express.edit_briefing(briefing, use_llm=use_llm)
        return express.render_markdown(briefing)
    except ValidationError as exc:
        return json.dumps({"error": str(exc)})


if __name__ == "__main__":
    mcp.run()
