"""Queries for capture identity, arrival counts and temporal context."""
from psycopg2.extras import RealDictCursor
from .pool import get_connection, connection_scope
from .columns import iso

def get_arrivals_summary(since, samples_per_source=5):
    """What arrived since ``since``: per source, document/memory counts and the newest
    documents (parent-level records, so a chunked article counts once).

    Memories produced by the system itself (dream-cycle syntheses, distilled
    insights without a capture channel) have no source_type; they are labelled
    ``generated:<type>`` so the caller can tell captured life from synthesis.

    Returns a list sorted by document count desc:
        [{"source_type", "documents", "memories", "samples": [{"id","title","type","created_at"}]}]
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT COALESCE(source_type, 'generated:' || type) AS label,
                       count(DISTINCT CASE WHEN parent_id IS NULL THEN
                         CASE WHEN source_type = 'distilled_agent_task' THEN COALESCE(source_url, id::text)
                              ELSE id::text END END) AS documents,
                       count(*) AS memories
                FROM memories WHERE created_at >= %s
                GROUP BY 1 ORDER BY 2 DESC, 3 DESC
                """,
                (since,),
            )
            counts = cur.fetchall()
            cur.execute(
                """
                SELECT id, title, type, created_at, label FROM (
                    SELECT id, title, type, created_at,
                           COALESCE(source_type, 'generated:' || type) AS label,
                           row_number() OVER (PARTITION BY COALESCE(source_type, 'generated:' || type)
                                              ORDER BY created_at DESC) AS rn
                    FROM memories WHERE created_at >= %s AND parent_id IS NULL
                ) t WHERE rn <= %s ORDER BY label, created_at DESC
                """,
                (since, samples_per_source),
            )
            samples = {}
            for r in cur.fetchall():
                samples.setdefault(r["label"], []).append(
                    {"id": str(r["id"]), "title": r["title"], "type": r["type"],
                     "created_at": iso(r["created_at"])})
    return [
        {"source_type": c["label"], "documents": int(c["documents"]), "memories": int(c["memories"]),
         "samples": samples.get(c["label"], [])}
        for c in counts
    ]


def get_processed_source_urls(source_type=None):
    """URL inventory, channel-scoped when ``source_type`` is given.

    R3 keeps the R1 behaviour for ``source_type=None`` (every URL) because the
    unchanged R1 crawlee ingest (``scripts/crawlee_ingest.py``, nightly ``ingest``
    job) still calls it that way; the capture rewrite (R4) makes the argument
    mandatory. A non-empty string is required otherwise.
    """
    if source_type is None:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source_url FROM memories WHERE source_url IS NOT NULL")
            return {row[0] for row in cur.fetchall()}
    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError("source_type must be a non-empty channel label (or None for every URL)")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_url FROM memories WHERE source_type = %s AND source_url IS NOT NULL", (source_type,))
        return {row[0] for row in cur.fetchall()}


def find_temporal_neighbors(memory_id: str, created_at, limit: int = 3) -> list[dict]:
    """Find memories created within ±24 hours of the given timestamp.

    Excludes the specified memory_id. Returns list of dicts with
    id, title, type, created_at fields.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, title, type, created_at
                FROM memories
                WHERE id != %s
                  AND parent_id IS NULL AND status = 'active'
                  AND created_at BETWEEN %s - interval '24 hours' AND %s + interval '24 hours'
                ORDER BY ABS(EXTRACT(EPOCH FROM (created_at - %s)))
                LIMIT %s
            """, (memory_id, created_at, created_at, created_at, limit))
            rows = cur.fetchall()
        return [
            {
                "id": str(row["id"]),
                "title": row["title"],
                "type": row["type"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]



def find_captured_source(source_type, source_url, project=None, *, conn=None):
    """Identity is channel, URL and project, scoped to captured source parents.

    Derived task memories may intentionally share a URL and never match here.
    """
    with connection_scope(conn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, content, metadata FROM memories
                WHERE source_type = %s AND source_url = %s AND project IS NOT DISTINCT FROM %s
                AND (type = 'source' OR metadata->>'capture_identity' = 'true' OR source_type LIKE 'quick_desktop%%')
                AND parent_id IS NULL
                AND COALESCE(metadata->>'record_kind', '') NOT IN ('task_memory', 'topic_segment')
                ORDER BY created_at, id LIMIT 1""", (source_type, source_url, project))
            return cur.fetchone()


def lock_source_identity(source_type, source_url, project, *, conn):
    """Serialize writers of one identity without a corpus-wide unique index."""
    import json
    key = json.dumps([source_type, source_url, project], separators=(',', ':'))
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
