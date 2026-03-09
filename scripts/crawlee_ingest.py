"""Daily Crawlee ingestion — reads new content from Crawlee's knowledge store.

Reads knowledge/index.md, compares against PostgreSQL, ingests new entries
through the shared pipeline (chunk → embed → store).

Usage: python scripts/crawlee_ingest.py
"""
