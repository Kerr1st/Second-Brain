"""Kiro CLI chat parser — Phase 1 extraction.

Reads from ~/Library/Application Support/kiro-cli/data.sqlite3,
strips tool markers and tool output, applies size and content filters,
writes cleaned markdown to staging/chats/.

See docs/HYBRID-CHAT-EXTRACTION.md for stripping rules and filter thresholds.
"""
