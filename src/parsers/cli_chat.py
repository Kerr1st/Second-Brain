"""Kiro CLI chat parser — Phase 1 extraction.

Reads Kiro CLI session transcripts from ~/.kiro/sessions/cli/*.jsonl (the store
kiro-cli writes to as of ~Mar 2026), strips tool activity, applies size/content
filters, and returns cleaned conversations as markdown strings.

Each .jsonl line is {version, kind, data}:
  - kind=Prompt          -> user message  (data.content[].data where kind=text)
  - kind=AssistantMessage -> assistant message (text parts; toolUse skipped)
  - kind=ToolResults      -> skipped

See docs/HYBRID-CHAT-EXTRACTION.md for stripping rules and filter thresholds.
"""

import glob
import json
import os
from datetime import datetime, timezone

from src.project import normalize_project_tag

SESSIONS_DIR = os.path.expanduser("~/.kiro/sessions/cli")

# Minimum thresholds (same as IDE parser)
MIN_CONTENT_CHARS = 200
MIN_USER_MESSAGES = 2
MIN_PARAGRAPH_WORDS = 50


def _text_from_content(content):
    """Join the text-kind parts of a message's content list."""
    parts = []
    for item in content or []:
        if isinstance(item, dict) and item.get("kind") == "text":
            t = (item.get("data") or "").strip()
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def extract_session_messages(path):
    """Read a Kiro CLI session .jsonl. Returns (messages, created_at_ms).

    messages: list of (role, text) with role in {human, bot}.
    Prompt -> human, AssistantMessage text -> bot, ToolResults skipped.
    """
    messages = []
    first_ts = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = entry.get("data") or {}
            if first_ts is None:
                ts = (data.get("meta") or {}).get("timestamp")
                if isinstance(ts, (int, float)):
                    first_ts = ts
            kind = entry.get("kind")
            if kind == "Prompt":
                txt = _text_from_content(data.get("content"))
                if txt:
                    messages.append(("human", txt))
            elif kind == "AssistantMessage":
                txt = _text_from_content(data.get("content"))
                if txt:
                    messages.append(("bot", txt))
            # ToolResults and any other kinds are skipped
    created_at_ms = int(first_ts * 1000) if first_ts else None
    return messages, created_at_ms


def get_conversations():
    """Yield (conversation_id, messages, created_at_ms) per CLI session file,
    newest first."""
    if not os.path.isdir(SESSIONS_DIR):
        return
    paths = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    for path in paths:
        conv_id = os.path.basename(path)[:-len(".jsonl")]
        messages, created_at = extract_session_messages(path)
        yield conv_id, messages, created_at


def passes_size_filter(messages):
    """Check if cleaned messages meet minimum size thresholds."""
    user_messages = [c for r, c in messages if r == "human"]
    if len(user_messages) < MIN_USER_MESSAGES:
        return False

    total_chars = sum(len(c) for _, c in messages)
    if total_chars < MIN_CONTENT_CHARS:
        return False

    return True


def passes_content_filter(messages):
    """Check if conversation contains substantive reasoning."""
    bot_messages = [c for r, c in messages if r == "bot"]

    for content in bot_messages:
        paragraphs = content.split("\n\n")
        for para in paragraphs:
            if len(para.split()) >= MIN_PARAGRAPH_WORDS:
                return True

    return False


_AUTOMATED_MARKERS = (
    "Run type:",
    "Please evaluate this candidate insight as the",
)


def is_automated_conversation(messages):
    """True for dream-cycle / --no-interactive sub-agent conversations."""
    humans = [c for r, c in messages if r == "human"]
    if not humans:
        return True
    first = humans[0].lstrip()
    if first.startswith("{"):  # Thinker receives a JSON memory slice
        return True
    return any(m in first[:120] for m in _AUTOMATED_MARKERS)


def format_as_markdown(conversation_id, messages, timestamp_ms, project=None):
    """Format cleaned messages as markdown with metadata header."""
    date_str = datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d") if timestamp_ms else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# Chat: {conversation_id}",
        "",
        "Source-Type: kiro_cli_chat",
        f"Source-ID: {conversation_id}",
        f"Date: {date_str}",
    ]

    if project is not None:
        lines.append(f"Project: {project}")

    lines.extend([
        "",
        "---",
        "",
    ])

    for role, content in messages:
        label = "**User:**" if role == "human" else "**Assistant:**"
        lines.extend([label, "", content.strip(), ""])

    return "\n".join(lines)


def parse_conversation(conversation_id, messages, created_at):
    """Filter and format one session. Returns (conv_id, markdown, project) or None."""
    if not messages:
        return None

    # Skip automated --no-interactive sub-agent conversations (dream cycle etc.)
    if is_automated_conversation(messages):
        return None

    if not passes_size_filter(messages):
        return None

    if not passes_content_filter(messages):
        return None

    project = normalize_project_tag(conversation_id)
    markdown = format_as_markdown(conversation_id, messages, created_at, project=project)
    return conversation_id, markdown, project


def parse_all(already_processed=None):
    """Parse all CLI sessions, applying filters. Yields (conv_id, markdown, project)."""
    already_processed = already_processed or set()

    for conversation_id, messages, created_at in get_conversations():
        if conversation_id in already_processed:
            continue

        result = parse_conversation(conversation_id, messages, created_at)
        if result:
            yield result


if __name__ == "__main__":
    """Quick test: run parser and print stats."""
    conversations = list(get_conversations())
    total = len(conversations)
    passed = sum(1 for cid, msgs, ts in conversations if parse_conversation(cid, msgs, ts))
    filtered = total - passed

    print(f"Total CLI sessions: {total}")
    print(f"Passed filters:  {passed} ({passed * 100 // total if total else 0}%)")
    print(f"Filtered out:    {filtered}")
