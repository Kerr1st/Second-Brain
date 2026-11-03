"""Shared capture pipeline: collect, validate identity, ingest, report outcome.

Adapters own preflight and source windows; asynchronous adapters hand over their
collected batch. All writes use the ingestion service, not source-specific SQL.
"""
from __future__ import annotations
import logging
from src.capture.common import CaptureBatch, exit_code

logger = logging.getLogger(__name__)


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
            # One odd header line fails that item, never the whole channel (D N-9).
            try:
                rendered = normalize_document_headers(markdown) if normalize_headers else markdown
            except ValueError:
                logger.warning("Capture item header could not be normalized; counted as failed", exc_info=True)
                stats["failed"] += 1
                continue
            seen.add(url)
            clean.append((url, rendered))
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


def ingest_with_metadata(metadata_by_url: dict, *, refresh=False, ingest=None):
    """An ``ingest`` callable for :func:`run_channel` that attaches per-item
    source metadata (for example a source fingerprint) without widening the
    document header. Mirrors ``ingest_batch``: one bad item is counted, never
    fatal; ``BaseException`` propagates.
    """
    if ingest is None:
        from src.ingest import ingest_content
        ingest = ingest_content

    def store(items, source_type, **_kwargs):
        result = {"processed": 0, "skipped": 0, "failed": 0}
        for url, content in items:
            try:
                saved = ingest(content, source_type, url, refresh=refresh,
                               source_metadata=metadata_by_url.get(url) or None)
            except Exception:  # noqa: BLE001 - per-item resilience (R3 S3-8)
                logger.error("Failed to ingest source: %s", url[:120], exc_info=True)
                result["failed"] += 1
            else:
                result["processed" if saved else "skipped"] += 1
        return result
    return store


def run_records(records, *, dry_run=False, refresh=False, ingest=None) -> dict:
    """Capture trusted source mappings while retaining their typed provenance.

    These are parser-owned fields, never unconstrained model arguments.
    """
    from collections import defaultdict
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
        if dry_run:
            # A real preview: classify each record against the stored row (reads
            # only) so the first run after a deploy is predictable, not a guess.
            summary[source] = _preview_records(source, items)
            continue

        def store(batch, source_type, **_kwargs):
            result = {"processed": 0, "skipped": 0, "failed": 0}
            for url, content in batch:
                record = mappings[(source_type, url)]
                fields = {k: record[k] for k in ("type", "title", "confidence", "tags", "mem_class") if k in record}
                try:
                    saved = ingest(content, source_type, url, refresh=refresh,
                                   source_metadata=record.get("metadata") or {}, memory_fields=fields, parse_header=False)
                except Exception:  # noqa: BLE001 - one bad record never aborts the channel (R3 S3-8)
                    logger.exception("Capture record storage failed")
                    result["failed"] += 1
                else:
                    result["processed" if saved else "skipped"] += 1
            return result
        summary[source] = run_channel(source, items, dry_run=dry_run, ingest=store, normalize_headers=False)
    return summary


def _preview_records(source_type, items) -> dict:
    from src.db import find_captured_source
    stats = run_channel(source_type, items, dry_run=True, normalize_headers=False)
    stats.update({"new": 0, "changed": 0, "unchanged": 0})
    seen = set()
    for url, content in items:
        if not isinstance(url, str) or not url.strip() or url in seen:
            continue
        seen.add(url)
        existing = find_captured_source(source_type, url)
        if existing is None:
            stats["new"] += 1
        elif existing["content"] == content:
            stats["unchanged"] += 1
        else:
            stats["changed"] += 1
    return stats
