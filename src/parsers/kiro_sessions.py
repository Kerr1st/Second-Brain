"""Read native Kiro IDE sessions, separate from legacy .chat and CLI formats.

Observed schema (2026-09-26): sessions/<workspace>/<session>/session.json plus
messages.jsonl envelopes {id,timestamp,payload}. Only explicit user and
assistant/Say payloads are conversation content. Reasoning, tool outputs,
steering, hooks, signatures and system documents are deliberately excluded.
No model call or private source writes occur in this parser.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.parse import quote

from src.capture.render import render_document
from src.parsers.ide_chat import strip_messages, passes_size_filter, passes_content_filter
from src.project import normalize_project_tag

SESSIONS_DIR = Path(os.path.expanduser("~/.kiro/sessions"))
MAX_METADATA_BYTES = 1024 * 1024
MAX_RECORD_CHARS = 8 * 1024 * 1024


def find_sessions(root=None) -> list[Path]:
    """Do not recurse into source snapshots, backups, indexes or CLI sessions."""
    base = Path(root) if root is not None else SESSIONS_DIR
    return sorted(base.glob("*/*/session.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)


def parse_session(metadata_path) -> tuple[str, str, dict] | None:
    path = Path(metadata_path)
    observed = path.stat()
    before = (observed.st_mtime_ns, observed.st_size)
    if observed.st_size > MAX_METADATA_BYTES:
        raise ValueError("Kiro session metadata exceeds size limit")
    meta = json.loads(path.read_text())
    session_id = meta.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Kiro session has no stable id")
    transcript = path.with_name("messages.jsonl")
    before_messages = (transcript.stat().st_mtime_ns, transcript.stat().st_size)
    messages, seen = [], set()
    with transcript.open(encoding="utf-8") as handle:
        while True:
            line = handle.readline(MAX_RECORD_CHARS + 1)
            if not line:
                break
            if len(line) > MAX_RECORD_CHARS:
                raise ValueError("Kiro message exceeds size limit")
            if not line.strip():
                continue
            record = json.loads(line)  # Partial records fail and retry on next cycle.
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Kiro message has invalid payload")
            role = payload.get("type")
            if role != "user" and not (role == "assistant" and payload.get("operationType") == "Say"):
                continue
            content = payload.get("content")
            message_id = record.get("id")
            if not isinstance(content, str) or not isinstance(message_id, str) or not message_id:
                raise ValueError("Kiro conversation message has invalid identity/content")
            if message_id in seen:
                continue
            seen.add(message_id)
            if content.strip():
                messages.append(("human" if role == "user" else "bot", content))
    if before != (path.stat().st_mtime_ns, path.stat().st_size) or before_messages != (transcript.stat().st_mtime_ns, transcript.stat().st_size):
        raise ValueError("Kiro session changed during capture; retry next cycle")
    messages = strip_messages(messages_as_dicts(messages))
    if not passes_size_filter(messages) or not passes_content_filter(messages):
        return None
    url = "kiro-ide://session/" + quote(session_id, safe="")
    paths = meta.get("workspacePaths") or []
    project = normalize_project_tag(Path(paths[0]).name) if paths and isinstance(paths[0], str) else None
    fields = {"Source": url, "Source_Type": "kiro_ide_chat", "Source_ID": session_id,
              "Date": str(meta.get("createdAt") or "")[:10],
              "Updated": meta.get("lastModifiedAt") or "", "Model": meta.get("modelId") or "",
              "Workflow": meta.get("agentMode") or "", "Project": project,
              "Capture_Format": "kiro_session_jsonl"}
    body = "\n\n".join(f"**{'User' if role == 'human' else 'Assistant'}:**\n\n{content}" for role, content in messages)
    return url, render_document(meta.get("title") or "Kiro IDE session", fields, body), fields


def messages_as_dicts(messages):
    return [{"role": role, "content": content} for role, content in messages]
