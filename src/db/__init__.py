"""Public database facade; implementation lives in focused repositories."""
from .pool import DB_CONFIG, get_connection, transaction, close_pool, is_reachable
from .columns import MEMORY_SCALAR_COLUMNS, memory_summary, iso
from .memories import (ALLOWED_UPDATE_FIELDS, create_memory, get_memory, get_memories_by_ids, chunk_parent_id,
    update_memory, update_fields, deepen_schema, list_memories, search_similar,
    find_schemas_for_memories, get_memory_provenance)
from .relationships import create_relationship, get_relationships, expire_stale_relationships
from .capture_queries import get_arrivals_summary, get_processed_source_urls, get_capture_fingerprints, find_temporal_neighbors, find_captured_source, lock_source_identity, reconcile_source_selection


def __getattr__(name):
    if name == "_pool":
        from . import pool
        return pool._pool
    raise AttributeError(name)
