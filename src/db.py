"""PostgreSQL connection and memory CRUD operations.

Handles connection management, memory CRUD, relationship management,
vector similarity search, and deduplication helpers. Hybrid search,
ranking, and retrieval reinforcement have been extracted to src/search.py.
"""

import os
import json
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "memory_bank"),
    "user": os.environ.get("DB_USER", "memory_bank"),
    "password": os.environ.get("DB_PASSWORD", "memory_bank"),
}

_pool: SimpleConnectionPool | None = None


@contextmanager
def get_connection():
    """Check out a live connection from the pool (lazily initialized).

    Each connection is probed with SELECT 1 on checkout; dead ones (e.g. after
    a database/container restart) are discarded and replaced, so the long-running
    pool self-heals instead of handing out stale, closed connections.
    """
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(
            int(os.environ.get("DB_POOL_MIN", "1")),
            int(os.environ.get("DB_POOL_MAX", "5")),
            **DB_CONFIG,
        )
    conn = None
    for _ in range(int(os.environ.get("DB_POOL_MAX", "5")) + 1):
        candidate = _pool.getconn()
        try:
            with candidate.cursor() as cur:
                cur.execute("SELECT 1")
            candidate.rollback()
            conn = candidate
            break
        except psycopg2.Error:
            _pool.putconn(candidate, close=True)  # drop dead connection
    if conn is None:
        raise psycopg2.OperationalError("No live database connection available")
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise
    finally:
        _pool.putconn(conn)


def close_pool():
    """Close all connections in the pool and reset to uninitialized state."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def is_reachable():
    """Check if the database is reachable. Returns True/False."""
    try:
        with get_connection() as conn:
            pass
        return True
    except Exception:
        return False


# --- Memory CRUD ---

def create_memory(type, title, content, embedding=None, tags=None, source_url=None,
                   source_type=None, metadata=None, status="active", confidence=1.0,
                   parent_id=None, summary=None, mem_class=None, project=None,
                   encoding_context=None):
    """Insert a new memory. Returns the UUID."""
    with get_connection() as conn:
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
        conn.commit()
        return str(memory_id)


def get_memory(memory_id):
    """Fetch a single memory by ID. Returns dict or None."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM memories WHERE id = %s", (memory_id,))
            return cur.fetchone()


ALLOWED_UPDATE_FIELDS = {
    "title", "content", "summary", "embedding", "tags", "source_url",
    "source_type", "metadata", "status", "confidence", "type",
    "mem_class", "project", "last_accessed_at", "encoding_context",
}


def update_memory(memory_id, **fields):
    """Update specific fields on a memory. Only allowlisted fields accepted."""
    fields = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_FIELDS}
    if not fields:
        return
    if "metadata" in fields and isinstance(fields["metadata"], dict):
        fields["metadata"] = json.dumps(fields["metadata"])
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [memory_id]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE memories SET {set_clause}, updated_at = now() WHERE id = %s", values)
        conn.commit()


def list_memories(type=None, status=None, source_type=None, limit=50, offset=0):
    """List memories with optional filters."""
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

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT %s OFFSET %s", params)
            return cur.fetchall()


# --- Semantic Search ---

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
                SELECT *, 1 - (embedding <=> %s::vector) AS similarity
                FROM memories {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (embedding_str, *filter_params, embedding_str, limit))
            return cur.fetchall()


# --- Relationships ---

def create_relationship(source_id, target_id, relation_type, note=None):
    """Create a relationship between two memories."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memory_relationships (source_id, target_id, relation_type, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET note = EXCLUDED.note
            """, (source_id, target_id, relation_type, note))
        conn.commit()


def get_relationships(memory_id):
    """Get all relationships for a memory (both directions)."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT r.*, m.title, m.type
                FROM memory_relationships r
                JOIN memories m ON m.id = CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END
                WHERE r.source_id = %s OR r.target_id = %s
            """, (memory_id, memory_id, memory_id))
            return cur.fetchall()


# --- Deduplication helpers ---

def get_processed_source_urls(source_type=None):
    """Get set of source_urls already in the database. Used for deduplication."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if source_type:
                cur.execute("SELECT source_url FROM memories WHERE source_type = %s AND source_url IS NOT NULL", (source_type,))
            else:
                cur.execute("SELECT source_url FROM memories WHERE source_url IS NOT NULL")
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





def get_schema_with_constituents(schema_id):
    """Get a schema memory and all its constituent memories via derived_from relationships.

    Returns dict with 'schema' (the schema memory) and 'constituents' (list of
    related memories linked by derived_from).
    """
    schema = get_memory(schema_id)
    if not schema or schema.get("type") != "schema":
        return None

    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT m.*
                FROM memories m
                JOIN memory_relationships r ON r.target_id = m.id
                WHERE r.source_id = %s AND r.relation_type = 'derived_from'
                ORDER BY m.created_at DESC
            """, (schema_id,))
            constituents = cur.fetchall()

    return {"schema": schema, "constituents": constituents}


def find_schemas_for_memory(memory_id):
    """Find all schemas that include a given memory as a constituent.

    Returns list of schema memory dicts.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT m.*
                FROM memories m
                JOIN memory_relationships r ON r.source_id = m.id
                WHERE r.target_id = %s
                  AND r.relation_type = 'derived_from'
                  AND m.type = 'schema'
                ORDER BY m.created_at DESC
            """, (memory_id,))
            return cur.fetchall()
