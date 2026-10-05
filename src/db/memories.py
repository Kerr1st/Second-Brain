"""Memory repository. Every mutation can join a caller-owned transaction."""
import json
import uuid
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from .pool import get_connection, connection_scope
from .columns import MEMORY_SCALAR_COLUMNS, scalar_columns

ALLOWED_UPDATE_FIELDS = {'tags', 'summary', 'embedding', 'metadata', 'content', 'source_type', 'encoding_context', 'title', 'project', 'confidence', 'mem_class', 'last_accessed_at', 'source_url', 'type', 'status'}

_LIST_COLUMNS = ", ".join(MEMORY_SCALAR_COLUMNS)

def create_memory(type, title, content, embedding=None, tags=None, source_url=None,
                   source_type=None, metadata=None, status="active", confidence=1.0,
                   parent_id=None, summary=None, mem_class=None, project=None,
                   encoding_context=None, *, conn=None):
    """Insert a new memory. Returns the UUID."""
    from src.validation import require_mapping
    require_mapping(metadata, "metadata")
    with connection_scope(conn, write=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memories (type, title, content, summary, embedding, tags,
                    source_url, source_type, metadata, status, confidence, parent_id,
                    mem_class, project, encoding_context)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (type, title, content, summary, embedding, tags or [],
                  source_url, source_type, json.dumps(metadata or {}),
                  status, confidence, parent_id, mem_class, project,
                  encoding_context))
            memory_id = cur.fetchone()[0]
        return str(memory_id)


def get_memory(memory_id, *, conn=None):
    """Fetch a single memory by ID. Returns dict or None."""
    with connection_scope(conn, write=False) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM memories WHERE id = %s", (memory_id,))
            return cur.fetchone()


def get_memories_by_ids(memory_ids):
    """Fetch several memories in one query, preserving the requested order.

    Returns the scalar columns only (no ``embedding`` / ``search_vector``); ids
    that don't exist are simply absent from the result.
    """
    ids = [str(uuid.UUID(str(mid))) for mid in memory_ids]
    if not ids:
        return []
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {_LIST_COLUMNS} FROM memories WHERE id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            by_id = {str(r["id"]): r for r in cur.fetchall()}
    return [by_id[i] for i in ids if i in by_id]


def update_memory(memory_id, *, conn=None, **fields):
    """Update specific fields on a memory. Only allowlisted fields accepted."""
    return update_fields(memory_id, {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_FIELDS}, conn=conn)


def list_memories(type=None, status=None, source_type=None, limit=50, offset=0, created_after=None):
    """List memories with optional filters, newest first. Scalar columns only
    (no embedding / search_vector — a listing never needs a 1024-dim vector per row)."""
    conditions = []
    params = []
    if type:
        conditions.append("type = %s")
        params.append(type)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if source_type:
        conditions.append("source_type = %s")
        params.append(source_type)
    if created_after is not None:
        conditions.append("created_at >= %s")
        params.append(created_after)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {_LIST_COLUMNS} FROM memories {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params,
            )
            return cur.fetchall()


def search_similar(embedding, limit=10, type=None, status=None):
    """Find memories most similar to the given embedding vector."""
    conditions = ["embedding IS NOT NULL"]
    filter_params = []
    if type:
        conditions.append("type = %s")
        filter_params.append(type)
    if status:
        conditions.append("status = %s")
        filter_params.append(status)

    where = "WHERE " + " AND ".join(conditions)
    embedding_str = str(embedding)

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT {_LIST_COLUMNS}, 1 - (embedding <=> %s::vector) AS similarity
                FROM memories {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (embedding_str, *filter_params, embedding_str, limit))
            return cur.fetchall()


def find_schemas_for_memories(memory_ids):
    """Batched find_schemas_for_memory: one query for many constituents.

    Returns {memory_id (str): [schema dicts, newest first]}. Only constituents that
    belong to at least one schema appear as keys.
    """
    if not memory_ids:
        return {}
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql.SQL("""
                SELECT r.target_id AS constituent_id, {columns}
                FROM memories m
                JOIN memory_relationships r ON r.source_id = m.id
                WHERE r.target_id = ANY(%s::uuid[])
                  AND r.relation_type = 'derived_from'
                  AND m.type = 'schema'
                ORDER BY m.created_at DESC
            """).format(columns=scalar_columns("m")), (list(memory_ids),))
            grouped = {}
            for row in cur.fetchall():
                constituent_id = str(row.pop("constituent_id"))
                grouped.setdefault(constituent_id, []).append(row)
            return grouped


def get_memory_provenance(memory_ids):
    """Lightweight provenance for many memories: id, type, source_type, created_at,
    parent_id, project. No content, no vectors. Ids that don't exist (or are not
    valid UUIDs) are absent from the result. Used by the dream-cycle attention ledger.
    """
    import uuid as _uuid
    ids = []
    for m in memory_ids:
        try:
            ids.append(str(_uuid.UUID(str(m))))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return []
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, type, source_type, created_at, parent_id, project
                FROM memories WHERE id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            return [dict(r) for r in cur.fetchall()]



def update_fields(memory_id, fields, *, metadata_patch=None, conn=None):
    """Atomically update scalar fields and merge a JSON object without lost keys."""
    from src.validation import require_mapping
    unknown = set(fields) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ValueError("Unknown memory fields: " + ", ".join(sorted(unknown)))
    if metadata_patch is not None:
        require_mapping(metadata_patch, "metadata_patch")
        if "metadata" in fields:
            raise ValueError("metadata and metadata_patch cannot be combined")
    assignments = []
    values = []
    for key, value in fields.items():
        assignments.append(sql.SQL("{} = %s").format(sql.Identifier(key)))
        values.append(json.dumps(value) if key == "metadata" and isinstance(value, dict) else value)
    if metadata_patch is not None:
        assignments.append(sql.SQL("metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb"))
        values.append(json.dumps(metadata_patch))
    if not assignments:
        return False
    with connection_scope(conn, write=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("UPDATE memories SET {}, updated_at = now() WHERE id = %s").format(sql.SQL(", ").join(assignments)), [*values, memory_id])
            return cur.rowcount == 1


def deepen_schema(schema_id, constituent_ids, *, conn=None):
    """Link constituents to an active schema in one caller-owned transaction."""
    from .relationships import create_relationship
    with connection_scope(conn, write=True) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, type, status FROM memories WHERE id = %s FOR UPDATE", (schema_id,))
            row = cur.fetchone()
            if not row or row['type'] != 'schema' or row['status'] != 'active':
                raise ValueError("An active schema is required")
        for mid in sorted(set(map(str, constituent_ids))):
            create_relationship(schema_id, mid, 'derived_from', conn=conn)
        update_fields(schema_id, {}, metadata_patch={'schema_updated': True}, conn=conn)
