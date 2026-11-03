"""Relationship lifecycle: live neighbours and permanent provenance links."""
from psycopg2.extras import RealDictCursor
from .pool import connection_scope
from src.validation import RELATION_TYPES, one_of, uuid_or_error

ALLOWED_RELATIONSHIP_TYPES = RELATION_TYPES
PERMANENT_RELATION_TYPES = frozenset({"derived_from", "superseded_by"})
SYMMETRIC_RELATION_TYPES = frozenset({"related_to", "contradicts"})


def create_relationship(source_id, target_id, relation_type, note=None, *, conn=None):
    source_id, target_id = uuid_or_error(source_id, "source_id"), uuid_or_error(target_id, "target_id")
    one_of(relation_type, RELATION_TYPES, "relation_type")
    if source_id == target_id:
        raise ValueError("A relationship needs two distinct memories")
    if relation_type in SYMMETRIC_RELATION_TYPES:
        source_id, target_id = sorted((source_id, target_id))
    with connection_scope(conn, write=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memory_relationships (source_id, target_id, relation_type, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_id, target_id, relation_type)
                DO UPDATE SET note = COALESCE(EXCLUDED.note, memory_relationships.note), expired_at = NULL
            """, (source_id, target_id, relation_type, note))


def get_relationships(memory_id, *, include_expired=False, conn=None):
    """Return live edges with neighbour IDs, collapsing only symmetric reverse pairs."""
    with connection_scope(conn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (m.id, r.relation_type,
                    CASE WHEN r.relation_type IN ('related_to', 'contradicts') THEN NULL::uuid ELSE r.source_id END) r.*,
                       m.id AS neighbor_id, m.title, m.type
                FROM memory_relationships r
                JOIN memories m ON m.id = CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END
                WHERE (r.source_id = %s OR r.target_id = %s)
                  AND (%s OR r.expired_at IS NULL)
                ORDER BY m.id, r.relation_type,
                    CASE WHEN r.relation_type IN ('related_to', 'contradicts') THEN NULL::uuid ELSE r.source_id END,
                    r.expired_at NULLS FIRST, r.created_at DESC, r.source_id, r.target_id
            """, (memory_id, memory_id, memory_id, include_expired))
            return cur.fetchall()


def expire_stale_relationships(days_threshold=90, *, conn=None):
    """Expire old association links only after an age and endpoint access grace."""
    if isinstance(days_threshold, bool) or not isinstance(days_threshold, int) or days_threshold < 1:
        raise ValueError("days_threshold must be a positive integer")
    with connection_scope(conn, write=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE memory_relationships mr SET expired_at = now()
                WHERE mr.expired_at IS NULL
                  AND mr.relation_type NOT IN ('derived_from', 'superseded_by')
                  AND mr.created_at < now() - make_interval(days => %s)
                  AND NOT EXISTS (
                    SELECT 1 FROM memories m WHERE m.id IN (mr.source_id, mr.target_id)
                    AND m.last_accessed_at >= now() - make_interval(days => %s))
            """, (days_threshold, days_threshold))
            return cur.rowcount
