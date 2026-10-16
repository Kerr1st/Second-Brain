"""Persistence for skim records — table ``memory_skim`` (migration 014).

Everything here goes through :func:`src.db.get_connection`, like
``src/dream_cycle_db.py``, so unit tests can fake the connection and cursor.

Privacy note: :func:`fetch_documents` is one of two places in the repository
that read ``memories.content`` for the skim (the other is the probe script) —
the document text goes to the Bedrock model inside the user's account and
nowhere else. :func:`list_arrivals` deliberately fetches no content so the
runner can decide what still needs skimming before loading any text.

The arrivals filter mirrors ``get_arrivals_summary`` / ``get_recent_parent_memories``
(parent-level, not superseded, ``created_at >= window_start``) and additionally
excludes the dream cycle's own outputs — memories ``store_accepted`` created,
recognisable by the ``dream-cycle`` tag and ``metadata.dream_cycle`` — because a
run must not skim what the previous run synthesised.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

from psycopg2.extras import RealDictCursor

from src.db import get_connection

def arrivals_filter_sql(alias: str = "") -> str:
    """SQL fragment: parent-level, non-superseded memories that are not dream-cycle
    syntheses. ``alias`` prefixes the columns (``"m."``) in a join. Used after a
    leading ``created_at >= %s`` condition."""
    a = alias
    return (
        f"{a}parent_id IS NULL"
        f" AND {a}status IS DISTINCT FROM 'superseded'"
        f" AND NOT ('dream-cycle' = ANY(COALESCE({a}tags, '{{}}')))"
        f" AND COALESCE({a}metadata->>'dream_cycle', 'false') <> 'true'"
    )


#: The unaliased filter (``FROM memories`` queries).
ARRIVALS_FILTER_SQL = arrivals_filter_sql()


def _ids(memory_ids: Iterable[Any]) -> list[str]:
    return [str(m) for m in memory_ids]


# --------------------------------------------------------------------------- #
# Arrivals (what to skim)                                                      #
# --------------------------------------------------------------------------- #
def list_arrivals(window_start: datetime, limit: int = 5000) -> list[dict]:
    """Arrivals in the window without their text: ``id, title, source_type,
    created_at``, newest first. See :data:`ARRIVALS_FILTER_SQL`."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT id, title, source_type, created_at
                FROM memories
                WHERE created_at >= %s AND {ARRIVALS_FILTER_SQL}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (window_start, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"])
    return rows


def fetch_documents(memory_ids: Iterable[Any]) -> list[dict]:
    """The documents to skim, with content: ``id, title, source_type, created_at,
    content``, in the order of ``memory_ids`` (ids that no longer exist are skipped)."""
    ids = _ids(memory_ids)
    if not ids:
        return []
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, title, source_type, created_at, content
                FROM memories
                WHERE id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            by_id = {str(r["id"]): dict(r) for r in cur.fetchall()}
    out = []
    for i in ids:
        if i in by_id:
            by_id[i]["id"] = i
            out.append(by_id[i])
    return out


# --------------------------------------------------------------------------- #
# Skim rows                                                                    #
# --------------------------------------------------------------------------- #
def upsert_skim(
    memory_id: str,
    *,
    model: str,
    prompt_version: str,
    skim: dict,
    run_id: str | None = None,
    cluster_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    truncated: bool = False,
) -> str:
    """Insert or replace the skim of ``memory_id`` for ``(model, prompt_version)``.

    A re-skim (same key) overwrites the record, attribution and cost, and
    refreshes ``created_at``. Returns the row id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_skim
                    (memory_id, run_id, model, prompt_version, skim, cluster_id,
                     input_tokens, output_tokens, cost_usd, truncated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (memory_id, model, prompt_version) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    skim = EXCLUDED.skim,
                    cluster_id = EXCLUDED.cluster_id,
                    input_tokens = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    cost_usd = EXCLUDED.cost_usd,
                    truncated = EXCLUDED.truncated,
                    created_at = now()
                RETURNING id
                """,
                (
                    str(memory_id), run_id, model, prompt_version, json.dumps(skim, default=str),
                    cluster_id, input_tokens, output_tokens,
                    round(float(cost_usd), 6) if cost_usd is not None else None, bool(truncated),
                ),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
    return str(row_id)


def fetch_records(memory_ids: Iterable[Any], model: str, prompt_version: str) -> dict[str, dict]:
    """``{memory_id: row}`` for the ids already skimmed with ``(model, prompt_version)``.

    Each row: ``memory_id, run_id, skim, cluster_id, input_tokens, output_tokens,
    cost_usd, truncated, created_at``.
    """
    ids = _ids(memory_ids)
    if not ids:
        return {}
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT memory_id, run_id, skim, cluster_id, input_tokens, output_tokens,
                       cost_usd, truncated, created_at
                FROM memory_skim
                WHERE memory_id = ANY(%s::uuid[]) AND model = %s AND prompt_version = %s
                """,
                (ids, model, prompt_version),
            )
            rows = [dict(r) for r in cur.fetchall()]
    out: dict[str, dict] = {}
    for r in rows:
        r["memory_id"] = str(r["memory_id"])
        if r.get("run_id") is not None:
            r["run_id"] = str(r["run_id"])
        out[r["memory_id"]] = r
    return out


def missing_ids(memory_ids: Iterable[Any], model: str, prompt_version: str) -> list[str]:
    """The ids in ``memory_ids`` (order kept, duplicates dropped) with no skim yet
    for ``(model, prompt_version)`` — what a run still has to pay for."""
    ids: list[str] = []
    seen: set[str] = set()
    for m in _ids(memory_ids):
        if m not in seen:
            seen.add(m)
            ids.append(m)
    if not ids:
        return []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT memory_id::text FROM memory_skim
                WHERE memory_id = ANY(%s::uuid[]) AND model = %s AND prompt_version = %s
                """,
                (ids, model, prompt_version),
            )
            have = {str(r[0]) for r in cur.fetchall()}
    return [i for i in ids if i not in have]


def fetch_window_records(window_start: datetime, model: str, prompt_version: str,
                         limit: int = 5000, extra_ids: Iterable[Any] | None = None) -> list[dict]:
    """Skim records for the arrivals in a window, joined with the memory row —
    the input of the Phase 3 arrivals inventory.

    Each item: ``id, title, source_type, created_at, skim, cluster_id, truncated``,
    newest arrival first. Uses :data:`ARRIVALS_FILTER_SQL` so a superseded or
    dream-cycle memory never re-enters the inventory through its skim.

    ``extra_ids`` adds earlier-window arrivals skimmed by this run's backlog pass
    (``SkimResult.backlog_ids``: their earlier skim call failed), so they reach
    this run's inventory instead of staying invisible to it.
    """
    extra = list(dict.fromkeys(_ids(extra_ids or [])))
    window_sql, window_params = ("(m.created_at >= %s OR m.id = ANY(%s::uuid[]))", (window_start, extra)) \
        if extra else ("m.created_at >= %s", (window_start,))
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT m.id, m.title, m.source_type, m.created_at,
                       s.skim, s.cluster_id, s.truncated
                FROM memory_skim s
                JOIN memories m ON m.id = s.memory_id
                WHERE {window_sql} AND s.model = %s AND s.prompt_version = %s
                  AND {arrivals_filter_sql('m.')}
                ORDER BY m.created_at DESC
                LIMIT %s
                """,
                (*window_params, model, prompt_version, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"])
    return rows


# --------------------------------------------------------------------------- #
# Clusters (Phase 3, optional): existing embeddings, no model calls            #
# --------------------------------------------------------------------------- #
def _parse_vector(text: Any) -> list[float] | None:
    """pgvector's text form ``[0.1,0.2,...]`` → floats (None for NULL / garbage)."""
    if text is None:
        return None
    if isinstance(text, (list, tuple)):
        return [float(x) for x in text]
    s = str(text).strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    try:
        return [float(x) for x in s[1:-1].split(",") if x.strip()]
    except ValueError:
        return None


def fetch_window_centroids(memory_ids: Iterable[Any]) -> dict[str, list[float]]:
    """``{memory_id: vector}`` for the given parent-level memories, using what the
    ingest already embedded: the parent's own ``embedding`` when it has one,
    else the pgvector ``avg()`` of its chunks' embeddings. Two set queries (the
    correlated form took ~14 s on 868 documents; these take ~0.4 s). Reads
    vectors only — no text columns. Ids without any embedding are absent.
    """
    ids = _ids(memory_ids)
    if not ids:
        return {}
    out: dict[str, list[float]] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, embedding::text FROM memories
                WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL
                """,
                (ids,),
            )
            for mid, vec in cur.fetchall():
                parsed = _parse_vector(vec)
                if parsed:
                    out[str(mid)] = parsed
            rest = [i for i in ids if i not in out]
            if rest:
                cur.execute(
                    """
                    SELECT parent_id::text, avg(embedding)::text FROM memories
                    WHERE parent_id = ANY(%s::uuid[]) AND embedding IS NOT NULL
                    GROUP BY parent_id
                    """,
                    (rest,),
                )
                for mid, vec in cur.fetchall():
                    parsed = _parse_vector(vec)
                    if parsed:
                        out[str(mid)] = parsed
    return out


def set_cluster_ids(cluster_ids: dict[str, str | None], model: str, prompt_version: str) -> int:
    """Write ``cluster_id`` (``"c1"``… or None) on the skim rows of the given
    memories for ``(model, prompt_version)``; returns the number of rows updated.
    Cluster ids are relative to one run's window, so a later run over an
    overlapping window simply rewrites them."""
    if not cluster_ids:
        return 0
    ids = list(cluster_ids)
    labels = [cluster_ids[i] for i in ids]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memory_skim s SET cluster_id = v.cluster_id
                FROM (SELECT unnest(%s::uuid[]) AS memory_id, unnest(%s::text[]) AS cluster_id) v
                WHERE s.memory_id = v.memory_id AND s.model = %s AND s.prompt_version = %s
                """,
                (ids, labels, model, prompt_version),
            )
            updated = cur.rowcount or 0
        conn.commit()
    return int(updated)


def run_summary(run_id: str) -> dict:
    """Counts and cost of the skims a run paid for:
    ``records, truncated, input_tokens, output_tokens, cost_usd``."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*),
                       count(*) FILTER (WHERE truncated),
                       COALESCE(sum(input_tokens), 0),
                       COALESCE(sum(output_tokens), 0),
                       COALESCE(sum(cost_usd), 0)
                FROM memory_skim WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
    return {
        "records": int(row[0] or 0),
        "truncated": int(row[1] or 0),
        "input_tokens": int(row[2] or 0),
        "output_tokens": int(row[3] or 0),
        "cost_usd": float(row[4] or 0.0),
    }
