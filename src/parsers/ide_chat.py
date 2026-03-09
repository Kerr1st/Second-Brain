"""Kiro IDE chat parser — Phase 1 extraction.

Reads .chat JSON files from:
  - ~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/.../*.chat (current)
  - ~/Library/Application Support/Kiro/User/globalStorage/amazonwebservices.aieditoragent/.../*.chat (legacy)

Strips system prompts, tool messages, and boilerplate. Extracts project path
from context field. Applies size and content filters. Writes cleaned markdown
to staging/chats/.

See docs/HYBRID-CHAT-EXTRACTION.md for stripping rules and filter thresholds.
"""
