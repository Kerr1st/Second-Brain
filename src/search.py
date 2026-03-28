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

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from src.db import get_connection


# --- Hybrid Search (BM25 + Vector + RRF) ---

def hybrid_search(query_text, query_embedding, limit=10, type=None, status=None, project=None):
    """Combine pgvector cosine search + PostgreSQL full-text search via RRF.

    Returns list of dicts with 'rrf_score' field, sorted by fused rank.
    """
    k = 60  # RRF constant
    prefetch = limit * 4  # fetch more candidates for fusion

    conditions = [sql.SQL("embedding IS NOT NULL")]
    params_base = []
    if type:
        conditions.append(sql.SQL("type = %s"))
        params_base.append(type)
    if status:
        conditions.append(sql.SQL("status = %s"))
        params_base.append(status)
    if project:
        conditions.append(sql.SQL("(project = %s OR project IS NULL)"))
        params_base.append(project)
    where = sql.SQL("WHERE ") + sql.SQL(" AND ").join(conditions)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Vector search
            emb_str = str(query_embedding)
            vec_params = [emb_str] + params_base + [emb_str, prefetch]
            vec_query = sql.SQL(
                "SELECT id, 1 - (embedding <=> %s::vector) AS similarity"
                " FROM memories {where}"
                " ORDER BY embedding <=> %s::vector LIMIT %s"
            ).format(where=where)
            cur.execute(vec_query, vec_params)
            vec_results = {str(r["id"]): i + 1 for i, r in enumerate(cur.fetchall())}

            # Full-text search
            ts_query = " | ".join(re.sub(r"[^\w\s]", "", query_text).split())
            if ts_query.strip():
                fts_params = [ts_query] + params_base + [prefetch]
                fts_where = where + sql.SQL(" AND search_vector IS NOT NULL")
                fts_query = sql.SQL(
                    "SELECT id, ts_rank(search_vector, to_tsquery('english', %s)) AS rank"
                    " FROM memories {where}"
                    " ORDER BY rank DESC LIMIT %s"
                ).format(where=fts_where)
                cur.execute(fts_query, fts_params)
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


def compute_spacing_bonus(last_accessed_at, now=None):
    """Compute the spacing bonus for spaced retrieval reinforcement.

    Args:
        last_accessed_at: datetime or None. When the memory was last accessed.
        now: datetime or None. Current time (defaults to utcnow if not provided).

    Returns:
        float in [0.0, 1.0]. NULL last_accessed_at → 1.0, 0 days → 0.0, 7+ days → 1.0.
    """
    if last_accessed_at is None or not hasattr(last_accessed_at, "timestamp"):
        return 1.0
    if now is None:
        now = datetime.now(timezone.utc)
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=timezone.utc)
    days_since = max(0.0, (now - last_accessed_at).total_seconds() / 86400)
    return min(1.0, days_since / 7.0)


def rerank(results, query_text, query_project=None):
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

        # Memory classification boost (semantic > procedural > episodic)
        mem_class = r.get("mem_class")
        mem_class_boost = {"semantic": 0.04, "procedural": 0.02}.get(mem_class, 0.0)

        # Depth score from metadata (numeric depth of explanation)
        depth_score = (r.get("metadata") or {}).get("depth_score", 0.0)

        # Spaced retrieval reinforcement (logarithmic, modulated by spacing bonus)
        access = r.get("access_count") or 0
        spacing_bonus = compute_spacing_bonus(r.get("last_accessed_at"), now)
        reinforcement = 0.03 * math.log1p(access) * spacing_bonus

        # Cross-project penalty
        mem_project = r.get("project")
        project_penalty = -0.15 if (query_project and mem_project and mem_project != query_project) else 0.0

        r["rerank_score"] = (
            0.30 * r.get("rrf_score", 0)
            + 0.18 * overlap
            + 0.18 * title_overlap
            + 0.12 * recency
            + 0.08 * length_score
            + 0.05 * depth_score
            + type_boost
            + mem_class_boost
            + reinforcement
            + project_penalty
        )

    results.sort(key=lambda r: r["rerank_score"], reverse=True)
    return results


def increment_access_count(memory_ids):
    """Bump access_count for retrieved memories (retrieval reinforcement)."""
    if not memory_ids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET access_count = coalesce(access_count, 0) + 1, last_accessed_at = now() WHERE id = ANY(%s::uuid[])",
                (memory_ids,)
            )
        conn.commit()
