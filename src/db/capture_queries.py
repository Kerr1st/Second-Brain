"""Queries for capture identity, arrival counts and temporal context."""
from psycopg2.extras import RealDictCursor
from .pool import get_connection, connection_scope, transaction
from .columns import iso
from src.operations.liveness import CAPTURE_ROWS

def get_arrivals_summary(since, samples_per_source=5):
    """What arrived since ``since``: per source, document/memory counts and the newest
    documents (parent-level records, so a chunked article counts once).

    Memories produced by the system itself (dream-cycle syntheses, distilled
    insights without a capture channel) have no source_type; they are labelled
    ``generated:<type>`` so the caller can tell captured life from synthesis.

    Index-only chunk rows (Codex ``*_chunk`` children, rechunk-maintenance
    chunks) are not arrivals: they are excluded exactly as liveness excludes them.

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
                FROM memories WHERE created_at >= %s AND """ + CAPTURE_ROWS + """
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
    """Channel-scoped URL inventory. Cross-channel identity is never inferred.

    R3 allowed ``source_type=None`` (every URL) for the R1 crawlee ingest; R4
    replaced that caller with ``src.capture.crawlee``, so the channel label is
    now required.
    """
    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError("source_type is required for a source URL inventory")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_url FROM memories WHERE source_type = %s AND source_url IS NOT NULL", (source_type,))
        return {row[0] for row in cur.fetchall()}


def get_capture_fingerprints(source_type):
    """``{source_url: metadata.source_fingerprint}`` for a channel's captured parents.

    Lets a bounded capture run skip sessions whose source files are byte-for-byte
    the ones already captured (no re-read, no re-render, no DB write). Parents
    captured before fingerprints existed map to ``None`` and are therefore
    re-examined once.
    """
    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError("source_type is required for a fingerprint inventory")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT source_url, metadata->>'source_fingerprint' FROM memories
                       WHERE source_type = %s AND source_url IS NOT NULL AND parent_id IS NULL""", (source_type,))
        return {url: fp for url, fp in cur.fetchall()}


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
    """Identity of a captured source is its channel and URL, scoped to captured
    source parents. ``project`` is accepted for call compatibility but is NOT
    part of the identity: a project tag can be repaired (UUID -> NULL, F-028),
    inferred differently by a newer parser, or absent, and none of that makes
    the same document a new one. Before this rule the first capture run after
    the UUID-project repair would have re-ingested every CLI session (B N1).

    Derived task memories may intentionally share a URL and never match here.
    """
    with connection_scope(conn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, content, metadata, project FROM memories
                WHERE source_type = %s AND source_url = %s
                AND (type = 'source' OR metadata->>'capture_identity' = 'true' OR source_type LIKE 'quick_desktop%%')
                AND parent_id IS NULL
                AND COALESCE(metadata->>'record_kind', '') NOT IN ('task_memory', 'topic_segment')
                ORDER BY created_at, id LIMIT 1""", (source_type, source_url))
            return cur.fetchone()


def lock_source_identity(source_type, source_url, project=None, *, conn):
    """Serialize writers of one identity without a corpus-wide unique index."""
    import json
    key = json.dumps([source_type, source_url], separators=(',', ':'))
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


RETIRED_KEY = "capture_retired"
RETIRED_REASON = "absent_from_source_selection"


def reconcile_source_selection(url_prefixes, present, *, dry_run=False, now=None):
    """Retire captured parents a snapshot source no longer selects; restore returners.

    ``present`` is the complete set of ``(source_type, source_url)`` identities the
    source selects this run; ``url_prefixes`` bounds which captured parents that
    selection is authoritative for. A parent under a prefix that is not in
    ``present`` (deleted upstream, now below the source's own threshold, or
    re-labelled under another source type) is set ``status='superseded'``
    together with its active chunks, and both carry ``metadata.capture_retired``
    so the change is attributable and reversible: nothing is deleted, and only
    rows this function retired are ever reactivated (when their identity is
    selected again). Parents another writer superseded are left alone.

    A prefix under which ``present`` holds no identity is refused and reported
    in ``refused_prefixes``. Returns counts only.
    """
    import json
    from datetime import datetime, timezone
    prefixes = tuple(p for p in url_prefixes if isinstance(p, str) and p)
    if not prefixes:
        raise ValueError("url_prefixes must name at least one non-empty prefix")
    present = {(st, url) for st, url in present}
    # A prefix with no selected identity is refused, not emptied: an unreadable,
    # truncated or newly created source must never retire a whole namespace.
    refused = [p for p in prefixes if not any(url.startswith(p) for _, url in present)]
    prefixes = tuple(p for p in prefixes if p not in refused)
    extra = {**({"refused_prefixes": refused} if refused else {}), **({"dry_run": True} if dry_run else {})}
    if not prefixes:
        return {"retire": 0, "reactivate": 0, **extra}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id::text, source_type, source_url, status, metadata ? %s AS retired FROM memories
            WHERE parent_id IS NULL AND source_url IS NOT NULL
              AND EXISTS (SELECT 1 FROM unnest(%s::text[]) AS p WHERE left(source_url, length(p)) = p)
              AND (type = 'source' OR metadata->>'capture_identity' = 'true' OR source_type LIKE 'quick_desktop%%')
              AND COALESCE(metadata->>'record_kind', '') NOT IN ('task_memory', 'topic_segment')""",
                    (RETIRED_KEY, list(prefixes)))
        rows = cur.fetchall()
    retire = [(i, st, url) for i, st, url, status, _ in rows if status == "active" and (st, url) not in present]
    restore = [(i, st, url) for i, st, url, status, marked in rows
               if status == "superseded" and marked and (st, url) in present]
    if dry_run:
        return {"retire": len(retire), "reactivate": len(restore), **extra}
    if not retire and not restore:
        return {"retire": 0, "reactivate": 0, **extra}
    stamp = json.dumps({RETIRED_KEY: {"reason": RETIRED_REASON,
                        "at": (now or datetime.now(timezone.utc)).isoformat()}})
    retired = reactivated = 0
    with transaction() as conn:
        for _, st, url in sorted(retire + restore, key=lambda r: (r[1], r[2])):
            lock_source_identity(st, url, conn=conn)
        with conn.cursor() as cur:
            if retire:
                ids = [r[0] for r in retire]
                cur.execute("""UPDATE memories SET status='superseded', updated_at=now(), metadata=COALESCE(metadata,'{}'::jsonb) || %s::jsonb
                    WHERE id = ANY(%s::uuid[]) AND status='active'""", (stamp, ids))
                retired = cur.rowcount
                cur.execute("""UPDATE memories SET status='superseded', updated_at=now(), metadata=COALESCE(metadata,'{}'::jsonb) || %s::jsonb
                    WHERE parent_id = ANY(%s::uuid[]) AND status='active'""", (stamp, ids))
            if restore:
                ids = [r[0] for r in restore]
                cur.execute("""UPDATE memories SET status='active', updated_at=now(), metadata=metadata - %s
                    WHERE id = ANY(%s::uuid[]) AND status='superseded' AND metadata ? %s""", (RETIRED_KEY, ids, RETIRED_KEY))
                reactivated = cur.rowcount
                cur.execute("""UPDATE memories SET status='active', updated_at=now(), metadata=metadata - %s
                    WHERE parent_id = ANY(%s::uuid[]) AND status='superseded' AND metadata ? %s""", (RETIRED_KEY, ids, RETIRED_KEY))
    return {"retire": retired, "reactivate": reactivated, **extra}
