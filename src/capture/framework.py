"""Shared capture pipeline: collect, validate identity, ingest, report outcome.

Adapters own preflight and source windows; asynchronous adapters hand over their
collected batch. All writes use the ingestion service, not source-specific SQL.
"""
from __future__ import annotations
from src.capture.common import CaptureBatch, exit_code


def run_channel(source_type: str, items=None, *, collect=None, preflight=None,
                dry_run=False, ingest=None, refresh=False, normalize_headers=True) -> dict:
    if preflight is not None:
        preflight()
    batch = collect() if collect is not None else items
    if batch is None:
        batch = []
    stats = {"processed": 0, "skipped": 0, "failed": getattr(batch, "failed", 0)}
    from src.capture.render import normalize_document_headers
    clean, seen = [], set()
    for url, markdown in batch:
        if not isinstance(url, str) or not url.strip() or not isinstance(markdown, str) or not markdown.strip():
            stats["failed"] += 1
        elif url in seen:
            stats["skipped"] += 1
        else:
            seen.add(url)
            clean.append((url, normalize_document_headers(markdown) if normalize_headers else markdown))
    if dry_run:
        return {**stats, "planned": len(clean), "dry_run": True}
    if clean:
        if ingest is None:
            from src.ingest import ingest_batch
            ingest = ingest_batch
        outcome = ingest(clean, source_type, **({"refresh": True} if refresh else {}))
        for name in ("processed", "skipped", "failed"):
            stats[name] += outcome.get(name, 0)
    return stats


def run_records(records, *, dry_run=False, refresh=False, ingest=None) -> dict:
    """Capture trusted source mappings while retaining their typed provenance.

    These are parser-owned fields, never unconstrained model arguments.
    """
    from collections import defaultdict
    from psycopg2 import Error as DatabaseError
    if ingest is None:
        from src.ingest import ingest_content
        ingest = ingest_content
    groups = defaultdict(CaptureBatch)
    mappings = {}
    for record in records:
        source = record["source_type"]
        url = record["source_url"]
        # Retain the source's body exactly; standard headers live in metadata when
        # the upstream record is already typed instead of being a document.
        groups[source].append((url, record["content"]))
        mappings[(source, url)] = record
    summary = {}
    for source, items in groups.items():
        def store(batch, source_type, **_kwargs):
            result = {"processed": 0, "skipped": 0, "failed": 0}
            for url, content in batch:
                record = mappings[(source_type, url)]
                fields = {k: record[k] for k in ("type", "title", "confidence", "tags", "mem_class") if k in record}
                try:
                    saved = ingest(content, source_type, url, refresh=refresh,
                                   source_metadata=record.get("metadata") or {}, memory_fields=fields, parse_header=False)
                except (DatabaseError, OSError, ValueError, RuntimeError):
                    import logging
                    logging.getLogger(__name__).exception("Capture record storage failed")
                    result["failed"] += 1
                else:
                    result["processed" if saved else "skipped"] += 1
            return result
        summary[source] = run_channel(source, items, dry_run=dry_run, ingest=store, normalize_headers=False)
    return summary
