"""PostgreSQL connection and memory CRUD operations.

Handles connection management, memory CRUD, relationship management,
vector similarity search, and deduplication helpers. Hybrid search,
ranking, and retrieval reinforcement have been extracted to src/search.py.
"""

import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "memory_bank"),
    "user": os.environ.get("DB_USER", "memory_bank"),
    "password": os.environ.get("DB_PASSWORD", "memory_bank"),
}


def get_connection():
    """Get a database connection."""
    return psycopg2.connect(**DB_CONFIG)


def is_reachable():
    """Check if the database is reachable. Returns True/False."""
    try:
        conn = get_connection()
        conn.close()
        return True
    except psycopg2.OperationalError:
        return False


# --- Memory CRUD ---

def create_memory(type, title, content, embedding=None, tags=None, source_url=None,
                   source_type=None, metadata=None, status="active", confidence=1.0,
                   parent_id=None, summary=None):
    """Insert a new memory. Returns the UUID."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memories (type, title, content, summary, embedding, tags,
                    source_url, source_type, metadata, status, confidence, parent_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (type, title, content, summary, embedding, tags or [],
                  source_url, source_type, json.dumps(metadata or {}),
                  status, confidence, parent_id))
            memory_id = cur.fetchone()[0]
        conn.commit()
        return str(memory_id)
    finally:
        conn.close()


def get_memory(memory_id):
    """Fetch a single memory by ID. Returns dict or None."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM memories WHERE id = %s", (memory_id,))
            return cur.fetchone()
    finally:
        conn.close()


ALLOWED_UPDATE_FIELDS = {
    "title", "content", "summary", "embedding", "tags", "source_url",
    "source_type", "metadata", "status", "confidence", "type",
}


def update_memory(memory_id, **fields):
    """Update specific fields on a memory. Only allowlisted fields accepted."""
    fields = {k: v for k, v in fields.items() if k in ALLOWED_UPDATE_FIELDS}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [memory_id]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE memories SET {set_clause}, updated_at = now() WHERE id = %s", values)
        conn.commit()
    finally:
        conn.close()


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

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT %s OFFSET %s", params)
            return cur.fetchall()
    finally:
        conn.close()


# --- Semantic Search ---

def search_similar(embedding, limit=10, type=None, status=None):
    """Find memories most similar to the given embedding vector."""
    conditions = ["embedding IS NOT NULL"]
    params = [str(embedding)]
    if type:
        conditions.append("type = %s")
        params.append(type)
    if status:
        conditions.append("status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT *, 1 - (embedding <=> %s::vector) AS similarity
                FROM memories {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (params[0], params[0], *params[1:]))
            return cur.fetchall()
    finally:
        conn.close()


# --- Relationships ---

def create_relationship(source_id, target_id, relation_type, note=None):
    """Create a relationship between two memories."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO memory_relationships (source_id, target_id, relation_type, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_id, target_id, relation_type) DO UPDATE SET note = EXCLUDED.note
            """, (source_id, target_id, relation_type, note))
        conn.commit()
    finally:
        conn.close()


def get_relationships(memory_id):
    """Get all relationships for a memory (both directions)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT r.*, m.title, m.type
                FROM memory_relationships r
                JOIN memories m ON m.id = CASE WHEN r.source_id = %s THEN r.target_id ELSE r.source_id END
                WHERE r.source_id = %s OR r.target_id = %s
            """, (memory_id, memory_id, memory_id))
            return cur.fetchall()
    finally:
        conn.close()


# --- Deduplication helpers ---

def get_processed_source_urls(source_type=None):
    """Get set of source_urls already in the database. Used for deduplication."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if source_type:
                cur.execute("SELECT source_url FROM memories WHERE source_type = %s AND source_url IS NOT NULL", (source_type,))
            else:
                cur.execute("SELECT source_url FROM memories WHERE source_url IS NOT NULL")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

