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
from src.rerank_weights import (
    COEFFICIENTS,
    MEM_CLASS_BOOST,
    PROJECT_PENALTY,
    REINFORCEMENT_COEFF,
    STALENESS_PENALTY_MAX,
    SUPERSEDED_PENALTY,
    TYPE_BOOST,
    TYPE_BOOST_TYPES,
)


# --- Hybrid Search (BM25 + Vector + RRF) ---

def hybrid_search(query_text, query_embedding, limit=10, type=None, status=None, project=None,
                  source_type=None, created_after=None):
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
    if source_type:
        conditions.append(sql.SQL("source_type = %s"))
        params_base.append(source_type)
    if created_after:
        conditions.append(sql.SQL("created_at >= %s"))
        params_base.append(created_after)
    where = sql.SQL("WHERE ") + sql.SQL(" AND ").join(conditions)
    # Any WHERE filter post-filters the HNSW result and can make it under-return;
    # iterative scan keeps scanning until `limit` rows satisfy the filter (pgvector >=0.8).
    filtered = bool(type or status or project or source_type or created_after)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if filtered:
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
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

            # Fetch a candidate pool (larger than `limit`) so near-duplicate
            # results can be dropped while still returning `limit` distinct rows.
            cand_ids = [s[0] for s in scored[:prefetch]]
            if not cand_ids:
                return []
            cur.execute("SELECT * FROM memories WHERE id = ANY(%s::uuid[])", (cand_ids,))
            row_by_id = {str(r["id"]): r for r in cur.fetchall()}

        # Dedup (P2): collapse near-identical content and cap results per parent
        # so raw chat/doc chunks and the dual-scheme imports stop flooding results.
        out, seen_content, per_parent = [], set(), {}
        for mid, score in scored[:prefetch]:
            row = row_by_id.get(mid)
            if row is None:
                continue
            ckey = re.sub(r"\s+", " ", (row.get("content") or "")).strip().lower()[:300]
            if ckey and ckey in seen_content:
                continue
            pkey = str(row.get("parent_id") or mid)
            if per_parent.get(pkey, 0) >= 2:
                continue
            seen_content.add(ckey)
            per_parent[pkey] = per_parent.get(pkey, 0) + 1
            row["rrf_score"] = score
            out.append(row)
            if len(out) >= limit:
                break
        return out


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

        # Encoding context overlap (Godden & Baddeley 1975: contextual reinstatement)
        encoding_ctx = r.get("encoding_context") or ""
        if encoding_ctx:
            ctx_tokens = set(re.sub(r"[^\w\s]", "", encoding_ctx).lower().split())
            context_overlap = len(query_tokens & ctx_tokens) / max(1, len(query_tokens))
        else:
            context_overlap = 0.0

        # Access count — read early because recency and forgetting both depend on it
        access = r.get("access_count") or 0

        # Recency decay — power law (Ebbinghaus 1885; Murre & Dros 2015)
        # R = (1 + t/S)^(-b) where S = stability, b = decay constant.
        # Stability increases with access_count: more retrievals = slower decay.
        # This connects the spacing effect to the forgetting curve itself.
        created = r.get("created_at")
        if created and hasattr(created, "timestamp"):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_old = max(0, (now - created).total_seconds() / 86400)
            stability = 30.0 + 10.0 * math.log1p(access)  # base 30 days, grows with retrieval
            decay_b = 0.8  # power-law exponent
            recency = (1.0 + days_old / stability) ** (-decay_b)
        else:
            recency = 0.35

        # Active forgetting pressure (Anderson & Neely 1996: interference theory)
        # Memories never retrieved and older than 90 days get a mild penalty.
        # "Use it or lose it" — unretrieved memories fade more aggressively.
        if access == 0 and created and hasattr(created, "timestamp"):
            days_unretrieved = max(0, (now - created).total_seconds() / 86400)
            if days_unretrieved > 90:
                staleness_penalty = STALENESS_PENALTY_MAX * min(1.0, (days_unretrieved - 90) / 180)
            else:
                staleness_penalty = 0.0
        else:
            staleness_penalty = 0.0

        # Content length signal (longer = more substance, capped)
        content_len = len(content_tokens)
        length_score = min(1.0, content_len / 80)

        # Type boost (ideas/syntheses/insights > raw sources)
        mem_type = r.get("type", "")
        type_boost = TYPE_BOOST if mem_type in TYPE_BOOST_TYPES else 0.0

        # Memory classification boost (semantic > procedural > episodic)
        mem_class = r.get("mem_class")
        mem_class_boost = MEM_CLASS_BOOST.get(mem_class, 0.0)

        # Depth score from metadata (numeric depth of explanation)
        depth_score = (r.get("metadata") or {}).get("depth_score", 0.0)

        # Spaced retrieval reinforcement (logarithmic, modulated by spacing bonus)
        spacing_bonus = compute_spacing_bonus(r.get("last_accessed_at"), now)
        reinforcement = REINFORCEMENT_COEFF * math.log1p(access) * spacing_bonus

        # Cross-project penalty
        mem_project = r.get("project")
        project_penalty = PROJECT_PENALTY if (query_project and mem_project and mem_project != query_project) else 0.0

        # Supersession penalty (interference theory: retroactive interference)
        # Superseded memories should fade in favor of their replacements.
        mem_status = r.get("status", "active")
        superseded_penalty = SUPERSEDED_PENALTY if mem_status == "superseded" else 0.0

        # Store intermediate signals for evaluation/ablation (underscore-prefixed)
        r["_overlap"] = overlap
        r["_title_overlap"] = title_overlap
        r["_context_overlap"] = context_overlap
        r["_recency"] = recency
        r["_length_score"] = length_score
        r["_depth_score"] = depth_score
        r["_type_boost"] = type_boost
        r["_mem_class_boost"] = mem_class_boost
        r["_reinforcement"] = reinforcement
        r["_spacing_bonus"] = spacing_bonus
        r["_project_penalty"] = project_penalty
        r["_superseded_penalty"] = superseded_penalty
        r["_staleness_penalty"] = staleness_penalty

        r["rerank_score"] = (
            COEFFICIENTS["rrf"] * r.get("rrf_score", 0)
            + COEFFICIENTS["overlap"] * overlap
            + COEFFICIENTS["title_overlap"] * title_overlap
            + COEFFICIENTS["context_overlap"] * context_overlap
            + COEFFICIENTS["recency"] * recency
            + COEFFICIENTS["length"] * length_score
            + COEFFICIENTS["depth"] * depth_score
            + type_boost
            + mem_class_boost
            + reinforcement
            + project_penalty
            + superseded_penalty
            + staleness_penalty
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
