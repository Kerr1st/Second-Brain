"""Queries for capture identity, arrival counts and temporal context."""
import logging

from psycopg2.extras import RealDictCursor
from .pool import get_connection, connection_scope, transaction
from .columns import iso
from src.operations.liveness import CAPTURE_ROWS

logger = logging.getLogger(__name__)

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
# Why a parent left the source selection; stored per row in metadata.capture_retired.
DELETED_UPSTREAM, BELOW_THRESHOLD, RELABELLED = "deleted_upstream", "below_threshold", "relabelled"
RETIRE_REASONS = (DELETED_UPSTREAM, BELOW_THRESHOLD, RELABELLED)
# Present identities the refresh must not touch, by why they stay out.
HELD_KINDS = ("below_threshold_band", "retired_below_threshold_band", "superseded_by", "superseded_elsewhere",
              "reactivation_skipped")
# Mass-retire guard: a namespace may lose at most max(MIN, PERCENT% of its active
# parents) in one run unless an operator explicitly allows more.
MASS_RETIRE_MIN = 25
MASS_RETIRE_PERCENT = 10

# A live superseded_by edge from the parent or one of its chunks: another writer
# (e.g. the dream cycle) has replaced this memory, so it must stay history.
_SUPERSEDED_BY = """EXISTS (SELECT 1 FROM memory_relationships r WHERE r.relation_type = 'superseded_by'
    AND r.expired_at IS NULL AND (r.source_id = {m}.id
        OR r.source_id IN (SELECT c.id FROM memories c WHERE c.parent_id = {m}.id)))"""


class SelectionResult(dict):
    """JSON-safe reconcile counts, plus ``withheld``: selected identities whose stored
    parent stays superseded, which the caller's refresh must therefore skip (a refresh
    would otherwise write active chunks under a superseded parent)."""

    def __init__(self, *args, withheld=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.withheld = frozenset(withheld)


def mass_retire_cap(active):
    """Most parents one run may retire from a namespace holding ``active`` active parents."""
    return max(MASS_RETIRE_MIN, active * MASS_RETIRE_PERCENT // 100)


def reconcile_source_selection(url_prefixes, present, *, upstream_confidence=None, retire_below=None,
                               reactivate_at=None, skip_retirement=None, reactivate=True,
                               allow_mass_retire=False, dry_run=False, now=None):
    """Retire captured parents a snapshot source no longer selects; restore returners.

    ``present`` is the complete set of ``(source_type, source_url)`` identities the
    source selects this run; ``url_prefixes`` bounds which captured parents that
    selection is authoritative for. An active parent under a prefix that is not in
    ``present`` is classified (``relabelled`` when its URL is selected under another
    source type; ``below_threshold`` when ``upstream_confidence`` -- every URL the
    source still holds, selected or not, with its confidence -- still has it;
    otherwise ``deleted_upstream``) and set ``status='superseded'`` together with its
    active chunks. Both carry ``metadata.capture_retired = {reason, at, confidence}``,
    so the change is attributable and reversible: nothing is deleted.

    Hysteresis: a ``below_threshold`` parent is retired only when its confidence is
    below ``retire_below``, and a parent retired ``below_threshold`` is reactivated
    only once its confidence reaches ``reactivate_at``; in between it is held.

    Only rows this function retired are ever reactivated, and never one another
    writer has since superseded (a live ``superseded_by`` edge). Rows superseded by
    other writers are left alone. Selected identities that stay superseded are
    returned in ``.withheld``.

    Guards: a prefix under which ``present`` holds no identity is refused
    (``refused_prefixes``); a prefix that would lose more than
    :func:`mass_retire_cap` parents retires none (``guard[prefix]["refuse"]``)
    unless ``allow_mass_retire``; ``skip_retirement`` (a reason string) disables
    retirement altogether. Reactivation still runs in each case unless
    ``reactivate`` is false (an untrusted source), when returners are held as
    ``reactivation_skipped``. Returns counts only.
    """
    import json
    from datetime import datetime, timezone
    prefixes = tuple(p for p in url_prefixes if isinstance(p, str) and p)
    if not prefixes:
        raise ValueError("url_prefixes must name at least one non-empty prefix")
    present = {(st, url) for st, url in present}
    present_urls = {url for _, url in present}
    upstream = dict(upstream_confidence or {})
    # A prefix with no selected identity is refused, not emptied: an unreadable,
    # truncated or newly created source must never retire a whole namespace.
    refused = [p for p in prefixes if not any(url.startswith(p) for url in present_urls)]
    prefixes = tuple(p for p in prefixes if p not in refused)
    by_reason = dict.fromkeys(RETIRE_REASONS, 0)
    held = dict.fromkeys(HELD_KINDS, 0)
    summary = {"retire": 0, "reactivate": 0, "by_reason": by_reason, "held": held, "guard": {},
               **({"refused_prefixes": refused} if refused else {}),
               **({"retirement_skipped": skip_retirement} if skip_retirement else {}),
               **({"dry_run": True} if dry_run else {})}
    if not prefixes:
        return SelectionResult(summary)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"""SELECT m.id::text, m.source_type, m.source_url, m.status, m.metadata ? %s AS retired,
                m.metadata->%s->>'reason' AS reason, {_SUPERSEDED_BY.format(m='m')} AS superseded_by
            FROM memories m
            WHERE m.parent_id IS NULL AND m.source_url IS NOT NULL
              AND EXISTS (SELECT 1 FROM unnest(%s::text[]) AS p WHERE left(m.source_url, length(p)) = p)
              AND (m.type = 'source' OR m.metadata->>'capture_identity' = 'true' OR m.source_type LIKE 'quick_desktop%%')
              AND COALESCE(m.metadata->>'record_kind', '') NOT IN ('task_memory', 'topic_segment')""",
                    (RETIRED_KEY, RETIRED_KEY, list(prefixes)))
        rows = cur.fetchall()
    candidates = {p: [] for p in prefixes}
    active = dict.fromkeys(prefixes, 0)
    restore, withheld = [], set()
    for pid, st, url, status, retired, reason, superseded_by in rows:
        prefix = next(p for p in prefixes if url.startswith(p))
        identity = (st, url)
        if status == "active":
            active[prefix] += 1
            if identity in present:
                continue
            if url in present_urls:
                why, confidence = RELABELLED, upstream.get(url)
            elif url in upstream:
                why, confidence = BELOW_THRESHOLD, upstream[url]
                if retire_below is not None and confidence is not None and confidence >= retire_below:
                    held["below_threshold_band"] += 1
                    continue
            else:
                why, confidence = DELETED_UPSTREAM, None
            by_reason[why] += 1
            candidates[prefix].append((pid, st, url, why, confidence))
        elif status == "superseded" and identity in present:
            confidence = upstream.get(url)
            if not retired:
                kind = "superseded_elsewhere"
            elif superseded_by:
                kind = "superseded_by"
            elif reason == BELOW_THRESHOLD and reactivate_at is not None and (
                    confidence is None or confidence < reactivate_at):
                kind = "retired_below_threshold_band"
            elif not reactivate:
                kind = "reactivation_skipped"
            else:
                restore.append((pid, st, url))
                continue
            held[kind] += 1
            withheld.add(identity)
    retire = []
    for prefix in prefixes:
        count, cap = len(candidates[prefix]), mass_retire_cap(active[prefix])
        over = count > cap
        summary["guard"][prefix] = {"candidates": count, "active": active[prefix], "cap": cap,
                                    "refuse": over and not allow_mass_retire,
                                    **({"allowed_by_operator": True} if over and allow_mass_retire else {})}
        if over and not allow_mass_retire:
            logger.warning("Selection reconcile REFUSED for %s: %d parents to retire exceeds the cap of %d "
                           "(%d active); refresh continues. Re-run with the mass-retire override after review.",
                           prefix, count, cap, active[prefix])
        elif not skip_retirement:
            retire.extend(candidates[prefix])
    if skip_retirement and any(candidates.values()):
        logger.info("Selection reconcile: retirement skipped (%s)", skip_retirement)
    if dry_run or (not retire and not restore):
        summary.update(retire=len(retire), reactivate=len(restore))
        return SelectionResult(summary, withheld=withheld)
    at = (now or datetime.now(timezone.utc)).isoformat()
    retired = 0
    with transaction() as conn:
        for _, st, url in sorted([r[:3] for r in retire] + restore, key=lambda r: (r[1], r[2])):
            lock_source_identity(st, url, conn=conn)
        with conn.cursor() as cur:
            for pid, _st, _url, why, confidence in retire:
                stamp = json.dumps({RETIRED_KEY: {"reason": why, "at": at, "confidence": confidence}})
                cur.execute("""UPDATE memories SET status='superseded', updated_at=now(), metadata=COALESCE(metadata,'{}'::jsonb) || %s::jsonb
                    WHERE id = %s::uuid AND status='active'""", (stamp, pid))
                if cur.rowcount:
                    retired += 1
                    cur.execute("""UPDATE memories SET status='superseded', updated_at=now(), metadata=COALESCE(metadata,'{}'::jsonb) || %s::jsonb
                        WHERE parent_id = %s::uuid AND status='active'""", (stamp, pid))
            restored = []
            if restore:
                cur.execute(f"""UPDATE memories m SET status='active', updated_at=now(), metadata=m.metadata - %s
                    WHERE m.id = ANY(%s::uuid[]) AND m.status='superseded' AND m.metadata ? %s
                      AND NOT {_SUPERSEDED_BY.format(m='m')} RETURNING m.id::text""",
                            (RETIRED_KEY, [r[0] for r in restore], RETIRED_KEY))
                restored = [row[0] for row in cur.fetchall()]
                if restored:
                    cur.execute("""UPDATE memories SET status='active', updated_at=now(), metadata=metadata - %s
                        WHERE parent_id = ANY(%s::uuid[]) AND status='superseded' AND metadata ? %s""",
                                (RETIRED_KEY, restored, RETIRED_KEY))
            # A returner that lost a race to another writer stays history; never refresh it.
            done = set(restored)
            withheld.update((st, url) for pid, st, url in restore if pid not in done)
    summary.update(retire=retired, reactivate=len(restored))
    return SelectionResult(summary, withheld=withheld)
