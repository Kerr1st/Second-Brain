"""Kiro CLI chat parser — Phase 1 extraction.

Reads from ~/Library/Application Support/kiro-cli/data.sqlite3,
strips tool markers and tool output, applies size and content filters,
returns cleaned conversations as markdown strings.

See docs/HYBRID-CHAT-EXTRACTION.md for stripping rules and filter thresholds.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from src.project import normalize_project_tag

CLI_DB_PATH = os.path.expanduser("~/Library/Application Support/kiro-cli/data.sqlite3")

# Minimum thresholds (same as IDE parser)
MIN_CONTENT_CHARS = 200
MIN_USER_MESSAGES = 2
MIN_PARAGRAPH_WORDS = 50

# Patterns for tool markers and tool output to strip
TOOL_MARKER_RE = re.compile(r'^\s*\[Tool uses:.*\]$')


def get_conversations():
    """Read all conversations from the CLI SQLite database."""
    if not os.path.exists(CLI_DB_PATH):
        return []

    conn = sqlite3.connect(CLI_DB_PATH)
    rows = conn.execute(
        "SELECT conversation_id, value, created_at FROM conversations_v2 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


def is_tool_output(text):
    """Heuristic: detect tool output that should be stripped."""
    stripped = text.strip()
    # Tool use markers
    if TOOL_MARKER_RE.match(stripped):
        return True
    # Very short tool confirmations embedded in assistant messages aren't tool output
    # But large blocks that look like file contents or directory listings are
    if stripped.startswith("# Total entries:"):
        return True
    return False


def strip_transcript(transcript):
    """Strip tool markers and classify transcript items into user/assistant messages."""
    messages = []

    for item in transcript:
        if not isinstance(item, str):
            continue

        text = item.strip()
        if not text:
            continue

        # Skip pure tool markers
        if TOOL_MARKER_RE.match(text):
            continue

        # Detect role: user messages start with "> "
        if text.startswith("> "):
            content = text[2:].strip()
            # Skip slash commands
            if content.startswith("/"):
                continue
            messages.append(("human", content))
        else:
            # Assistant message — strip inline tool markers
            content = re.sub(r'\[Tool uses:.*?\]', '', text).strip()
            if content:
                messages.append(("bot", content))

    return messages


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


def extract_history_messages(data):
    """Extract (role, text) messages from the newer kiro-cli `history` format.

    Each entry: {"user": {"content": {"Prompt": {"prompt": ...}}}, "assistant":
    {"Response"|"ToolUse": {"content": ...}}}. Tool-result user turns and empty
    assistant content are skipped. Returns [] if no usable history.
    """
    messages = []
    for e in data.get("history") or []:
        if not isinstance(e, dict):
            continue
        u = e.get("user")
        if isinstance(u, dict):
            c = u.get("content")
            p = c.get("Prompt") if isinstance(c, dict) else None
            if isinstance(p, dict) and isinstance(p.get("prompt"), str):
                txt = p["prompt"].strip()
                if txt:
                    messages.append(("human", txt))
        a = e.get("assistant")
        if isinstance(a, dict):
            block = a.get("Response") or a.get("ToolUse")
            if isinstance(block, dict):
                txt = (block.get("content") or "").strip()
                if txt:
                    messages.append(("bot", txt))
    return messages


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


def parse_conversation(conversation_id, value_json, created_at):
    """Parse a single CLI conversation. Returns (conv_id, markdown, project) or None if filtered out."""
    try:
        data = json.loads(value_json)
    except json.JSONDecodeError:
        return None

    # Newer kiro-cli stores turns in structured `history`; older versions used
    # the `transcript` string list. Prefer history, fall back to transcript.
    messages = extract_history_messages(data)
    if not messages:
        messages = strip_transcript(data.get("transcript", []))

    if not messages:
        return None

    # Skip automated --no-interactive sub-agent conversations (dream cycle etc.)
    # so the brain doesn't ingest itself talking to itself.
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
    """Parse all CLI conversations, applying filters. Yields (conv_id, markdown, project) tuples."""
    already_processed = already_processed or set()

    for conversation_id, value_json, created_at in get_conversations():
        if conversation_id in already_processed:
            continue

        result = parse_conversation(conversation_id, value_json, created_at)
        if result:
            yield result


if __name__ == "__main__":
    """Quick test: run parser and print stats."""
    conversations = get_conversations()
    total = len(conversations)
    passed = 0
    filtered = 0

    for cid, value, ts in conversations:
        result = parse_conversation(cid, value, ts)
        if result:
            passed += 1
        else:
            filtered += 1

    print(f"Total CLI chats: {total}")
    print(f"Passed filters:  {passed} ({passed*100//total if total else 0}%)")
    print(f"Filtered out:    {filtered} ({filtered*100//total if total else 0}%)")
