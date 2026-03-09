"""Ingestion pipeline — the single path from content to PostgreSQL.

All source types (Crawlee markdown, CLI chats, IDE chats, notes) are
normalized to markdown with metadata headers before reaching this module.

Pipeline: read markdown → chunk by topic → embed → extract metadata → store.
"""
