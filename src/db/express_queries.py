"""Express data access. Public services own connection scopes; callers get rows."""
from psycopg2.extras import RealDictCursor
from .pool import connection_scope


def _truncate(text, n=600):
    text=(text or '').strip()
    return text if len(text)<=n else text[:n-1].rstrip()+'…'

def _gather_insights(cur, window_days: int) -> list[dict]:
    """Recent accepted dream-cycle insights (identified by the `dream-cycle` tag)."""
    cur.execute(
        """
        SELECT id, type, title, content, created_at, metadata, project
        FROM memories
        WHERE tags @> ARRAY['dream-cycle'] AND status = 'active'
          AND created_at > now() - (%s || ' days')::interval
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (window_days,),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    # Resolve source-memory projects/titles in one batched query → cross-project signal.
    all_src: set[str] = set()
    for r in rows:
        for sid in (r["metadata"] or {}).get("source_memories", []) or []:
            all_src.add(sid)
    src_info: dict[str, dict] = {}
    if all_src:
        cur.execute(
            "SELECT id, title, project FROM memories WHERE id = ANY(%s::uuid[])",
            (list(all_src),),
        )
        for s in cur.fetchall():
            src_info[str(s["id"])] = {"title": s["title"], "project": s["project"]}

    items = []
    for r in rows:
        meta = r["metadata"] or {}
        src_ids = meta.get("source_memories", []) or []
        projects = {
            src_info[s]["project"]
            for s in src_ids
            if s in src_info and src_info[s]["project"]
        }
        source_titles = [src_info[s]["title"] for s in src_ids if s in src_info]
        cross_project = (
            meta.get("strategy") == "cross_project_collision" or len(projects) >= 2
        )
        items.append(
            {
                "kind": "insight",
                "id": str(r["id"]),
                "title": r["title"],
                "detail": _truncate(r["content"]),
                "created_at": r["created_at"],
                "meta": {
                    "strategy": meta.get("strategy"),
                    "confidence": meta.get("confidence"),
                    "cross_project": cross_project,
                    "projects": sorted(projects),
                    "topics": sorted(projects),
                    "source_titles": source_titles[:5],
                },
            }
        )
    return items


def _gather_contradictions(cur, limit: int) -> list[dict]:
    """Active `contradicts` edges — the 'you're reversing a past decision' signal.

    Deduped to one entry per source memory (a memory may contradict several others;
    showing it once, newest-first, keeps the briefing varied).
    """
    cur.execute(
        """
        SELECT r.source_id, r.target_id, r.note, r.created_at,
               a.title AS a_title, b.title AS b_title
        FROM memory_relationships r
        JOIN memories a ON a.id = r.source_id
        JOIN memories b ON b.id = r.target_id
        WHERE r.relation_type = 'contradicts' AND r.expired_at IS NULL
        ORDER BY r.created_at DESC
        LIMIT %s
        """,
        (limit * 4,),
    )
    items = []
    seen_sources: set = set()
    for r in cur.fetchall():
        if r["source_id"] in seen_sources:
            continue
        seen_sources.add(r["source_id"])
        note = (r["note"] or "").strip()
        detail = note or "These two memories make opposing claims."
        detail += f"\n\n• «{r['a_title']}»\n• «{r['b_title']}»"
        items.append(
            {
                "kind": "contradiction",
                "id": f"{r['source_id']}:{r['target_id']}",
                "title": r["a_title"],
                "detail": detail,
                "created_at": r["created_at"],
                "meta": {"a_title": r["a_title"], "b_title": r["b_title"], "note": note},
            }
        )
        if len(items) >= limit:
            break
    return items


def _gather_resurfaced(cur, limit: int, suppress_days: int, min_age_days: int) -> list[dict]:
    """High-value, long-unaccessed memories (desirable difficulty; Bjork 1992).

    Value signal is `access_count` (proven prior retrieval = high storage strength)
    on distilled types; depth_score is only populated on dream-cycle memories so it
    is used only as a tiebreaker. "Forgotten" = old + not accessed recently.
    """
    cur.execute(
        """
        SELECT id, type, title, content, created_at, access_count, project,
               COALESCE((metadata->>'depth_score')::float, 0) AS depth
        FROM memories
        WHERE status = 'active'
          AND type IN ('insight', 'decision', 'synthesis')
          AND NOT (tags @> ARRAY['dream-cycle'])
          AND created_at < now() - (%s || ' days')::interval
          AND (last_accessed_at IS NULL OR last_accessed_at < now() - (%s || ' days')::interval)
          AND (
                metadata->>'express_last_surfaced' IS NULL
                OR (metadata->>'express_last_surfaced')::timestamptz < now() - (%s || ' days')::interval
              )
        ORDER BY access_count DESC, depth DESC, created_at ASC
        LIMIT %s
        """,
        (min_age_days, min_age_days, suppress_days, limit),
    )
    items = []
    for r in cur.fetchall():
        items.append(
            {
                "kind": "resurface",
                "id": str(r["id"]),
                "title": r["title"],
                "detail": _truncate(r["content"]),
                "created_at": r["created_at"],
                "meta": {"depth": round(r["depth"], 2), "access_count": r["access_count"],
                         "topics": [r["project"]] if r["project"] else []},
            }
        )
    return items


def _gather_digest(cur) -> list[dict]:
    """The latest weekly digest ('what you're working on / learning')."""
    cur.execute(
        """
        SELECT id, title, content, created_at, project
        FROM memories
        WHERE source_type = 'weekly_digest'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    r = cur.fetchone()
    if not r:
        return []
    return [
        {
            "kind": "digest",
            "id": str(r["id"]),
            "title": r["title"],
            "detail": _truncate(r["content"]),
            "created_at": r["created_at"],
            "meta": {"topics": [r["project"]] if r["project"] else []},
        }
    ]


def _gather_questions(cur, limit: int) -> list[dict]:
    """Active open questions (currently none in the store; future-proofed)."""
    cur.execute(
        """
        SELECT id, title, content, created_at, project
        FROM memories
        WHERE type = 'question' AND status = 'active'
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "kind": "question",
            "id": str(r["id"]),
            "title": r["title"],
            "detail": _truncate(r["content"]),
            "created_at": r["created_at"],
            "meta": {"topics": [r["project"]] if r["project"] else []},
        }
        for r in cur.fetchall()
    ]


def _distinct_projects(cur, source_ids) -> list[str]:
    """Distinct non-empty projects among the given source memory UUIDs."""
    ids = [s for s in (source_ids or []) if s]
    if not ids:
        return []
    cur.execute(
        "SELECT DISTINCT project FROM memories "
        "WHERE id = ANY(%s::uuid[]) AND project IS NOT NULL AND project <> ''",
        (ids,),
    )
    return [row["project"] for row in cur.fetchall()]


def store_feedback(target_type,target_key,signal,weight, *, conn=None):
    with connection_scope(conn,write=True) as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO express_feedback(target_type,target_key,signal,weight)
            VALUES(%s,%s,%s,%s) ON CONFLICT(target_type,target_key) DO UPDATE
            SET signal=EXCLUDED.signal,weight=EXCLUDED.weight,updated_at=now()""",(target_type,target_key,signal,weight))


def delete_feedback(target_type,target_key, *, conn=None):
    with connection_scope(conn,write=True) as conn,conn.cursor() as cur:
        cur.execute('DELETE FROM express_feedback WHERE target_type=%s AND target_key=%s',(target_type,target_key))
        return cur.rowcount


def list_feedback(*,conn=None):
    with connection_scope(conn) as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT target_type,target_key,signal,weight,updated_at FROM express_feedback ORDER BY updated_at DESC')
        return [dict(row) for row in cur.fetchall()]


def read_briefing(window_days,suppress_days,*,contradiction_limit,resurface_limit,resurface_min_age,question_limit):
    with connection_scope() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        items=_gather_insights(cur,window_days)
        items+=_gather_contradictions(cur,contradiction_limit)
        items+=_gather_resurfaced(cur,resurface_limit,suppress_days,resurface_min_age)
        items+=_gather_digest(cur)
        items+=_gather_questions(cur,question_limit)
        return items,list_feedback(conn=conn)


def latest_accepted_candidates():
    with connection_scope() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cur.execute('SELECT id FROM dream_cycle_runs WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1')
        run=cur.fetchone()
        if not run:return None,[]
        cur.execute("SELECT candidate_json FROM dream_cycle_candidates WHERE run_id=%s AND final_verdict='ACCEPTED'",(str(run['id']),))
        candidates=[dict(row['candidate_json'] or {}) for row in cur.fetchall()]
        for candidate in candidates:
            candidate['source_projects']=_distinct_projects(cur,candidate.get('source_memories'))
        return str(run['id']),candidates
