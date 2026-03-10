"""PostgreSQL connection and query helpers.

Handles connection pooling, memory CRUD operations, relationship management,
semantic search, and hybrid retrieval (BM25 + vector + RRF reranking).
"""

import os
import json
import math
import re
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "memory_bank"),
    "user": os.environ.get("DB_USER", "memory_bank"),
    "password": os.environ.get("DB_PASSWORD", "memory_bank"),
}


def get_connection():
    """Get a database connection."""
    return psycopg2.connect(**DB_CONFIG)


def is_reachable():
    """Check if the database is reachable. Returns True/False."""
    try:
        conn = get_connection()
        conn.close()
        return True
    except psycopg2.OperationalError:
        return False


# --- Memory CRUD ---

def create_memory(type, title, content, embedding=None, tags=None, source_url=None,
                   source_type=None, metadata=None, status="active", confidence=1.0,
                   parent_id=None, summary=None):
    """Insert a new memory. Returns the UUID."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memories (type, title, content, summary, embedding, tags,
                    source_url, source_type, metadata, status, confidence, parent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (type, title, content, summary, embedding, tags or [],
                  source_url, source_type, json.dumps(metadata or {}),
                  status, confidence, parent_id))
            memory_id = cur.fetchone()[0]
        conn.commit()
        return str(memory_id)
    finally:
        conn.close()


def get_memory(memory_id):
    """Fetch a single memory by ID. Returns dict or None."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM memories WHERE id = %s", (memory_id,))
            return cur.fetchone()
    finally:
        conn.close()


ALLOWED_UPDATE_FIELDS = {
    "title", "content", "summary", "embedding", "tags", "source_url",
    "source_type", "metadata", "status", "confidence", "type",
}


def update_memory(memory_id, **fields):
    """Update specific fields on a memory. Only allowlisted fields accepted."""
    fields = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_FIELDS}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [memory_id]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE memories SET {set_clause}, updated_at = now() WHERE id = %s", values)
        conn.commit()
    finally:
        conn.close()


def list_memories(type=None, status=None, source_type=None, limit=50, offset=0):
    """List memories with optional filters."""
    conditions = []
    params = []
    if type:
        conditions.append("type = %s")
        params.append(type)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if source_type:
        conditions.append("source_type = %s")
        params.append(source_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT %s OFFSET %s", params)
            return cur.fetchall()
    finally:
        conn.close()


# --- Semantic Search ---

def search_similar(embedding, limit=10, type=None, status=None):
    """Find memories most similar to the given embedding vector."""
    conditions = ["embedding IS NOT NULL"]
    params = [str(embedding)]
    if type:
        conditions.append("type = %s")
        params.append(type)
    if status:
        conditions.append("status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT *, 1 - (embedding <=> %s::vector) AS similarity
                FROM memories {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (params[0], params[0], *params[1:]))
            return cur.fetchall()
    finally:
        conn.close()


# --- Relationships ---

def create_relationship(source_id, target_id, relation_type, note=None):
    """Create a relationship between two memories."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memory_relationships (source_id, target_id, relation_type, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET note = EXCLUDED.note
            """, (source_id, target_id, relation_type, note))
        conn.commit()
    finally:
        conn.close()


def get_relationships(memory_id):
    """Get all relationships for a memory (both directions)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT r.*, m.title, m.type
                FROM memory_relationships r
                JOIN memories m ON m.id = CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END
                WHERE r.source_id = %s OR r.target_id = %s
            """, (memory_id, memory_id, memory_id))
            return cur.fetchall()
    finally:
        conn.close()


# --- Deduplication helpers ---

def get_processed_source_urls(source_type=None):
    """Get set of source_urls already in the database. Used for deduplication."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if source_type:
                cur.execute("SELECT source_url FROM memories WHERE source_type = %s AND source_url IS NOT NULL", (source_type,))
            else:
                cur.execute("SELECT source_url FROM memories WHERE source_url IS NOT NULL")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


# --- Hybrid Search (BM25 + Vector + RRF) ---

def hybrid_search(query_text, query_embedding, limit=10, type=None, status=None):
    """Combine pgvector cosine search + PostgreSQL full-text search via RRF.

    Returns list of dicts with 'rrf_score' field, sorted by fused rank.
    """
    k = 60  # RRF constant
    prefetch = limit * 4  # fetch more candidates for fusion

    conditions = ["embedding IS NOT NULL"]
    params_base = []
    if type:
        conditions.append("type = %s")
        params_base.append(type)
    if status:
        conditions.append("status = %s")
        params_base.append(status)
    where = "WHERE " + " AND ".join(conditions)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Vector search
            vec_params = [str(query_embedding)] + params_base + [prefetch]
            cur.execute(f"""
                SELECT id, 1 - (embedding <=> %s::vector) AS similarity
                FROM memories {where}
                ORDER BY embedding <=> %s::vector LIMIT %s
            """, (vec_params[0], vec_params[0], *vec_params[1:]))
            vec_results = {str(r["id"]): i + 1 for i, r in enumerate(cur.fetchall())}

            # Full-text search
            ts_query = " | ".join(re.sub(r"[^\w\s]", "", query_text).split())
            if ts_query.strip():
                fts_params = [ts_query] + params_base + [prefetch]
                fts_where = where + " AND search_vector IS NOT NULL"
                cur.execute(f"""
                    SELECT id, ts_rank(search_vector, to_tsquery('english', %s)) AS rank
                    FROM memories {fts_where}
                    ORDER BY rank DESC LIMIT %s
                """, fts_params)
                fts_results = {str(r["id"]): i + 1 for i, r in enumerate(cur.fetchall())}
            else:
                fts_results = {}

            # RRF fusion
            all_ids = set(vec_results) | set(fts_results)
            scored = []
            absent_rank = prefetch + 1
            for mid in all_ids:
                vec_rank = vec_results.get(mid, absent_rank)
                fts_rank = fts_results.get(mid, absent_rank)
                rrf = 1.0 / (k + vec_rank) + 1.0 / (k + fts_rank)
                scored.append((mid, rrf))
            scored.sort(key=lambda x: x[1], reverse=True)

            # Fetch full records for top results
            top_ids = [s[0] for s in scored[:limit]]
            if not top_ids:
                return []
            rrf_map = dict(scored)
            cur.execute("SELECT * FROM memories WHERE id = ANY(%s::uuid[])", (top_ids,))
            rows = cur.fetchall()

        for row in rows:
            row["rrf_score"] = rrf_map.get(str(row["id"]), 0)
        rows.sort(key=lambda r: r["rrf_score"], reverse=True)
        return rows
    finally:
        conn.close()


def rerank(results, query_text):
    """Utility reranking: recency, type boost, token overlap, access reinforcement.

    Mutates results in-place, adding 'rerank_score'. Returns sorted list.
    """
    if not results:
        return results

    query_tokens = set(re.sub(r"[^\w\s]", "", query_text).lower().split())
    now = datetime.now(timezone.utc)

    for r in results:
        # Token overlap
        content_tokens = set(re.sub(r"[^\w\s]", "", (r.get("content") or "")[:2000]).lower().split())
        title_tokens = set(re.sub(r"[^\w\s]", "", (r.get("title") or "")).lower().split())
        all_tokens = content_tokens | title_tokens
        overlap = len(query_tokens & all_tokens) / max(1, len(query_tokens))
        title_overlap = len(query_tokens & title_tokens) / max(1, len(query_tokens))

        # Recency decay (τ=60 days)
        created = r.get("created_at")
        if created and hasattr(created, "timestamp"):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_old = max(0, (now - created).total_seconds() / 86400)
            recency = math.exp(-days_old / 60)
        else:
            recency = 0.35

        # Content length signal (longer = more substance, capped)
        content_len = len(content_tokens)
        length_score = min(1.0, content_len / 80)

        # Type boost (ideas/syntheses/insights > raw sources)
        mem_type = r.get("type", "")
        type_boost = 0.06 if mem_type in ("idea", "synthesis", "insight", "decision") else 0.0

        # Retrieval reinforcement (logarithmic, diminishing returns)
        access = r.get("access_count") or 0
        reinforcement = 0.03 * math.log1p(access)

        r["rerank_score"] = (
            0.35 * r.get("rrf_score", 0)
            + 0.20 * overlap
            + 0.20 * title_overlap
            + 0.15 * recency
            + 0.10 * length_score
            + type_boost
            + reinforcement
        )

    results.sort(key=lambda r: r["rerank_score"], reverse=True)
    return results


def increment_access_count(memory_ids):
    """Bump access_count for retrieved memories (retrieval reinforcement)."""
    if not memory_ids:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET access_count = coalesce(access_count, 0) + 1 WHERE id = ANY(%s::uuid[])",
                (memory_ids,)
            )
        conn.commit()
    finally:
        conn.close()
