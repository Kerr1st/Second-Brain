"""Search and retrieval — hybrid search, reranking, and retrieval reinforcement.

Extracted from db.py. Contains the multi-step hybrid retrieval algorithm
(vector search + BM25 + RRF fusion), ranking business logic (utility reranking
with scoring formulas), and retrieval reinforcement (access count bumping).

Note: search_similar() stays in db.py — it's a data-access primitive (single SQL query),
not a retrieval algorithm.
"""

import math
import re
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from src.db import get_connection


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
