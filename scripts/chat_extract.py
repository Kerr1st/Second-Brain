"""Daily chat extraction — Phase 1 of hybrid chat ingestion.

Reads from all three chat sources, applies structural stripping and filtering,
writes cleaned markdown to staging/chats/. Runs via launchd at 2:30 AM.

Usage: python scripts/chat_extract.py [--backfill]
  --backfill  Process all chats (initial run), not just new ones.
"""
