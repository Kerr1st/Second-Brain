"""Kiro IDE chat parser — Phase 1 extraction.

Reads .chat JSON files from:
  - ~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/.../*.chat (current)
  - ~/Library/Application Support/Kiro/User/globalStorage/amazonwebservices.aieditoragent/.../*.chat (legacy)

Strips system prompts, tool messages, and boilerplate. Extracts project metadata.
Applies size and content filters. Returns cleaned conversations as markdown strings.

See docs/HYBRID-CHAT-EXTRACTION.md for stripping rules and filter thresholds.
"""

import json
import glob
import os
import re
from datetime import datetime, timezone

from src.project import normalize_project_tag

# Chat source directories (macOS)
IDE_CHAT_DIRS = [
    os.path.expanduser("~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent"),
    os.path.expanduser("~/Library/Application Support/Kiro/User/globalStorage/amazonwebservices.aieditoragent"),
]

# Minimum thresholds for a chat to be worth ingesting
MIN_CONTENT_CHARS = 200
MIN_USER_MESSAGES = 2
MIN_PARAGRAPH_WORDS = 50  # at least one assistant paragraph this long


def find_chat_files():
    """Discover all .chat files across IDE chat directories."""
    files = []
    for base_dir in IDE_CHAT_DIRS:
        if os.path.isdir(base_dir):
            files.extend(glob.glob(os.path.join(base_dir, "**", "*.chat"), recursive=True))
    return files


def extract_project_context(data):
    """Extract project path and workspace info from chat context and metadata."""
    context = data.get("context", [])
    metadata = data.get("metadata", {})

    # Get workspace hash from the file's parent directory
    project = None
    for ctx in context:
        if ctx.get("type") == "fileTree":
            paths = ctx.get("expandedPaths", [])
            if paths:
                # Use the first expanded path to infer project context
                raw_project = paths[0].split("/")[0]
                project = normalize_project_tag(raw_project)

    return {
        "model": metadata.get("modelId", ""),
        "workflow": metadata.get("workflow", ""),
        "start_time": metadata.get("startTime"),
        "end_time": metadata.get("endTime"),
        "project_hint": project,
    }


def strip_messages(chat_messages):
    """Apply structural stripping rules. Returns list of (role, content) tuples."""
    cleaned = []

    for i, msg in enumerate(chat_messages):
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Drop tool messages entirely
        if role == "tool":
            continue

        # Drop system prompt (first human message starting with known patterns)
        if i == 0 and role == "human":
            if content.startswith("# System Prompt") or content.startswith("# Identity"):
                continue

        # Drop empty bot messages
        if role == "bot" and not content.strip():
            continue

        # Drop boilerplate acknowledgment
        if role == "bot" and content.strip() == "I will follow these instructions.":
            continue

        # Strip embedded IDE context blocks from human messages
        if role == "human":
            content = re.sub(r'<EnvironmentContext>.*?</EnvironmentContext>', '', content, flags=re.DOTALL)
            content = re.sub(r'<implicit-rules>.*?</implicit-rules>', '', content, flags=re.DOTALL)
            content = re.sub(r'<implicitInstruction>.*?</implicitInstruction>', '', content, flags=re.DOTALL)
            content = content.strip()

        # Skip if content is now empty after stripping
        if not content.strip():
            continue

        cleaned.append((role, content))

    return cleaned


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
    """Check if conversation contains substantive reasoning, not just mechanical commands."""
    bot_messages = [c for r, c in messages if r == "bot"]

    for content in bot_messages:
        paragraphs = content.split("\n\n")
        for para in paragraphs:
            if len(para.split()) >= MIN_PARAGRAPH_WORDS:
                return True

    return False


def format_as_markdown(filename, messages, meta, file_timestamp):
    """Format cleaned messages as markdown with metadata header."""
    date_str = datetime.fromtimestamp(
        file_timestamp / 1000 if file_timestamp and file_timestamp > 1e12 else file_timestamp or 0,
        tz=timezone.utc
    ).strftime("%Y-%m-%d") if file_timestamp else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# Chat: {filename}",
        "",
        "Source-Type: kiro_ide_chat",
        f"Source-ID: {filename}",
        f"Date: {date_str}",
        f"Model: {meta.get('model', 'unknown')}",
        f"Workflow: {meta.get('workflow', 'unknown')}",
    ]
    if meta.get("project_hint"):
        lines.append(f"Project: {meta['project_hint']}")
    lines.extend(["", "---", ""])

    for role, content in messages:
        label = "**User:**" if role == "human" else "**Assistant:**"
        lines.extend([label, "", content.strip(), ""])

    return "\n".join(lines)


def parse_chat_file(filepath):
    """Parse a single IDE .chat file. Returns (filename, markdown, meta) or None if filtered out."""
    filename = os.path.basename(filepath).replace(".chat", "")

    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    chat_messages = data.get("chat", [])
    if not chat_messages:
        return None

    meta = extract_project_context(data)
    messages = strip_messages(chat_messages)

    if not passes_size_filter(messages):
        return None

    if not passes_content_filter(messages):
        return None

    timestamp = meta.get("start_time")
    markdown = format_as_markdown(filename, messages, meta, timestamp)
    return filename, markdown, meta


def parse_all(already_processed=None):
    """Parse all IDE chat files, applying filters. Yields (filename, markdown, meta) tuples.

    Args:
        already_processed: set of filenames to skip (for deduplication against DB)
    """
    already_processed = already_processed or set()
    files = find_chat_files()

    stats = {"total": len(files), "skipped_dup": 0, "filtered": 0, "passed": 0, "errors": 0}

    for filepath in files:
        filename = os.path.basename(filepath).replace(".chat", "")

        if filename in already_processed:
            stats["skipped_dup"] += 1
            continue

        result = parse_chat_file(filepath)
        if result is None:
            stats["filtered"] += 1
            continue

        stats["passed"] += 1
        yield result

    return stats


if __name__ == "__main__":
    """Quick test: run parser and print stats."""
    total = 0
    filtered = 0
    passed = 0

    for filepath in find_chat_files():
        total += 1
        result = parse_chat_file(filepath)
        if result is None:
            filtered += 1
        else:
            passed += 1

    print(f"Total IDE chats: {total}")
    print(f"Passed filters:  {passed} ({passed*100//total if total else 0}%)")
    print(f"Filtered out:    {filtered} ({filtered*100//total if total else 0}%)")
