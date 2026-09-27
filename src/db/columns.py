"""Canonical scalar projections and safe date/summary serialization."""
from psycopg2 import sql

MEMORY_SCALAR_COLUMNS = (
    "id", "type", "title", "content", "summary", "tags", "source_url", "source_type",
    "metadata", "status", "confidence", "parent_id", "created_at", "updated_at",
    "access_count", "mem_class", "project", "last_accessed_at", "encoding_context",
)


def scalar_columns(alias=None):
    return sql.SQL(", ").join(sql.Identifier(alias, c) if alias else sql.Identifier(c)
                              for c in MEMORY_SCALAR_COLUMNS)


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def memory_summary(row):
    return {"id": str(row["id"]), "type": row.get("type"), "title": row.get("title"),
            "created_at": iso(row.get("created_at")), "source_url": row.get("source_url"),
            "source_type": row.get("source_type"), "project": row.get("project")}
