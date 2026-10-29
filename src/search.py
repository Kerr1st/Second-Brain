"""Search and retrieval — hybrid search, reranking, and retrieval reinforcement.

Extracted from db.py. Contains the multi-step hybrid retrieval algorithm
(vector search + BM25 + RRF fusion), ranking business logic (utility reranking
with scoring formulas), and retrieval reinforcement (access count bumping).

Note: search_similar() stays in the src.db repository — it's a data-access primitive (single SQL query),
not a retrieval algorithm.
"""

import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone


from src.db import get_connection

from src.rerank_weights import (
    COEFFICIENTS,
    MEM_CLASS_BOOST,
    OVERLAP_WINDOW_CHARS,
    PROJECT_PENALTY,
    REINFORCEMENT_COEFF,
    STALENESS_PENALTY_MAX,
    SUPERSEDED_PENALTY,
    TYPE_BOOST,
    TYPE_BOOST_TYPES,
)


from src.retrieval import (build_filters, tokenize_query, fts_passes, rrf_fuse,
                           normalise_rrf, dedup_candidates)
from src.db.search_queries import fetch_candidates

logger = logging.getLogger(__name__)


def hybrid_search(query_text, query_embedding, limit=10, type=None, status=None, project=None,
                  source_type=None, created_after=None, per_parent_cap=2):
    """Fuse vector/full-text legs, increasing the bounded pool if duplicates crowd it.

    ``per_parent_cap`` bounds the rows one parent (a document and its chunks) may
    contribute; the MCP search tools pass 1 so each result is a distinct parent."""
    if limit <= 0:
        return []
    prefetch = max(limit * 4, 4)
    ceiling = min(max(limit * 64, 512), 4096)
    while True:
        vec, fts, rows = fetch_candidates(query_text, query_embedding, prefetch,
            type=type, status=status, project=project, source_type=source_type, created_after=created_after)
        scored = rrf_fuse(vec, fts, absent_rank=prefetch + 1)
        results = dedup_candidates(scored, rows, limit, per_parent_cap)
        if len(results) >= limit or max(len(vec), len(fts)) < prefetch or prefetch >= ceiling:
            return results
        prefetch = min(prefetch * 2, ceiling)


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


#: Query tokens at least this long also match content tokens they are a prefix of
#: (and vice versa), so "skim" matches "skimmed" and "stage" matches "stages".
OVERLAP_PREFIX_MIN = 4


def token_overlap(query_tokens, doc_tokens) -> float:
    """Fraction of query tokens present in ``doc_tokens``.

    A query token counts as present when an identical token exists, or when a
    doc token and the query token share a prefix relationship and the shorter
    of the two is at least ``OVERLAP_PREFIX_MIN`` characters — a cheap,
    stemmer-free way to tolerate inflection ("skim"/"skimmed", "stage"/"stages")
    that the full-text leg already tolerates through its stemmer. Exact matches
    are checked first, so the O(query × doc) prefix scan runs only for tokens
    that missed.
    """
    if not query_tokens:
        return 0.0
    hits = 0
    for q in query_tokens:
        if q in doc_tokens:
            hits += 1
            continue
        if len(q) >= OVERLAP_PREFIX_MIN and any(
            (t.startswith(q)) or (len(t) >= OVERLAP_PREFIX_MIN and q.startswith(t)) for t in doc_tokens
        ):
            hits += 1
    return hits / len(query_tokens)


def rerank(results, query_text, query_project=None):
    """Utility reranking: recency, type boost, token overlap, access reinforcement.

    Mutates results in-place, adding 'rerank_score'. Returns sorted list.
    """
    if not results:
        return results

    query_tokens = set(re.sub(r"[^\w\s]", " ", query_text).lower().split())
    now = datetime.now(timezone.utc)

    for r in results:
        # Token overlap (inflection-tolerant: "skim" matches "skimmed", see token_overlap)
        content_tokens = set(re.sub(r"[^\w\s]", " ", (r.get("content") or "")[:OVERLAP_WINDOW_CHARS]).lower().split())
        title_tokens = set(re.sub(r"[^\w\s]", " ", (r.get("title") or "")).lower().split())
        all_tokens = content_tokens | title_tokens
        overlap = token_overlap(query_tokens, all_tokens)
        title_overlap = token_overlap(query_tokens, title_tokens)

        # Encoding context overlap (Godden & Baddeley 1975: contextual reinstatement)
        encoding_ctx = r.get("encoding_context") or ""
        if encoding_ctx:
            ctx_tokens = set(re.sub(r"[^\w\s]", " ", encoding_ctx).lower().split())
            context_overlap = token_overlap(query_tokens, ctx_tokens)
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


class AccessCountWriter:
    """Applies retrieval reinforcement off the request path.

    ``schedule(ids)`` only records the ids and returns; one daemon thread writes
    them with ``write(ids)`` (``increment_access_count`` by default, its own
    pooled connection). The UPDATE is non-HOT (``last_accessed_at`` is indexed),
    so every bumped row inserts into every index of ``memories``, including the
    HNSW index, which kept MCP searches waiting for seconds.

    Ids scheduled while a write runs are coalesced: an id retrieved n times gets
    n increments in the next write, so counts stay exact. At most ``max_pending``
    distinct ids wait; further ids are dropped and counted in ``dropped``. A
    failed write is logged (never raised, never printed to stdout) and its
    batch is lost. ``close()`` flushes what is pending and stops the thread.
    """

    def __init__(self, write=None, *, max_pending=10000):
        self._write = write or increment_access_count
        self._max_pending = max_pending
        self._pending = Counter()
        self._busy = False
        self._closed = False
        self._thread = None
        self._cond = threading.Condition()
        self.dropped = 0

    def schedule(self, memory_ids):
        """Record one retrieval of each distinct id; never touches the database."""
        ids = {str(i) for i in memory_ids or ()}
        if not ids:
            return
        with self._cond:
            if self._closed:
                logger.warning("access-count writer closed; %d bumps dropped", len(ids))
                self.dropped += len(ids)
                return
            for mid in ids:
                if mid in self._pending or len(self._pending) < self._max_pending:
                    self._pending[mid] += 1
                else:
                    self.dropped += 1
            if self.dropped and len(self._pending) >= self._max_pending:
                logger.warning("access-count queue full (%d ids); %d bumps dropped so far",
                               self._max_pending, self.dropped)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="access-count-writer", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _run(self):
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if not self._pending:
                    return
                batch, self._pending = self._pending, Counter()
                self._busy = True
            try:
                for level in range(1, max(batch.values()) + 1):
                    self._write(sorted(mid for mid, n in batch.items() if n >= level))
            except Exception:
                logger.warning("access-count write failed; %d ids not bumped", len(batch), exc_info=True)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def flush(self, timeout=5.0):
        """Wait until every scheduled bump has been written (or failed). True if drained."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def close(self, timeout=5.0):
        """Flush pending bumps, then stop the writer thread (server shutdown)."""
        drained = self.flush(timeout)
        with self._cond:
            self._closed = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout if drained else 0)
        return drained
