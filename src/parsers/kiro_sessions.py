"""Read native Kiro IDE sessions, separate from legacy .chat and CLI formats.

Observed schema (2026-09-26, re-checked 2026-10-05 against the live store):
``sessions/<workspace>/<session>/session.json`` (schemaVersion "1.0.0", keys
id/title/agentMode/workspacePaths/createdAt/lastModifiedAt/modelId/...) plus
``messages.jsonl`` envelopes ``{id,timestamp,payload}``. Only explicit ``user``
payloads (``source`` chat/steer/absent — all human-authored) and
``assistant``/``Say`` payloads are conversation content. Reasoning, tool calls
and results, turn markers, steering, hooks, sub-agent markers, usage summaries
and system documents are deliberately excluded. A session directory that has
``session.json`` but no ``messages.jsonl`` yet (two such exist in the live
store) is *incomplete*, not broken: it is skipped, not failed.

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
URL_PREFIX = "kiro-ide://session/"


class IncompleteSession(Exception):
    """``session.json`` exists but the transcript does not (yet).

    Deliberately *not* a ``ValueError``/``OSError``: callers treat those as a
    failed parse, whereas an incomplete session is simply not capturable this
    cycle and must not turn the nightly job red (D N-2).
    """


def find_sessions(root=None) -> list[Path]:
    """Do not recurse into source snapshots, backups, indexes or CLI sessions.

    Newest first by transcript modification time (falls back to the metadata
    file when the transcript is missing), so a bounded run captures the most
    recent sessions first.
    """
    base = Path(root) if root is not None else SESSIONS_DIR

    def recency(meta: Path) -> int:
        transcript = meta.with_name("messages.jsonl")
        try:
            return transcript.stat().st_mtime_ns
        except OSError:
            return meta.stat().st_mtime_ns

    return sorted(base.glob("*/*/session.json"), key=recency, reverse=True)


def transcript_path(metadata_path) -> Path:
    return Path(metadata_path).with_name("messages.jsonl")


def fingerprint(metadata_path) -> str | None:
    """Cheap change detector for a session: mtime_ns and size of both files.

    ``None`` when the transcript is missing (incomplete session). Stored in the
    captured row's metadata as ``source_fingerprint`` so later runs can skip
    unchanged sessions without re-reading or re-rendering them.
    """
    meta = Path(metadata_path)
    transcript = transcript_path(meta)
    try:
        m, t = meta.stat(), transcript.stat()
    except FileNotFoundError:
        return None
    return f"{m.st_mtime_ns}:{m.st_size}:{t.st_mtime_ns}:{t.st_size}"


def session_id(metadata_path) -> str:
    """The stable native id from ``session.json`` (raises ValueError if absent)."""
    path = Path(metadata_path)
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("Kiro session metadata exceeds size limit")
    meta = json.loads(path.read_text())
    sid = meta.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("Kiro session has no stable id")
    return sid


def session_url(sid: str) -> str:
    return URL_PREFIX + quote(sid, safe="")


def legacy_aliases(sid: str) -> list[str]:
    """Identities the same session could already be stored under.

    The legacy ``.chat`` store keyed chats by a 32-hex basename
    (``chat://<32hex>``, staged as ``ide_<32hex>.md``) while native sessions use
    hyphenated UUIDs, so the hyphen-stripped id is checked as well. Verified
    read-only on 2026-10-05: 0 of 309 native ids match a legacy row either way,
    but the check is what guarantees a migrated session is never duplicated.
    """
    forms = [sid]
    stripped = sid.replace("-", "")
    if stripped != sid:
        forms.append(stripped)
    aliases = []
    for form in forms:
        aliases.extend([f"chat://{form}", f"ide_{form}.md"])
    return aliases


def parse_session(metadata_path) -> tuple[str, str, dict] | None:
    path = Path(metadata_path)
    observed = path.stat()
    before = (observed.st_mtime_ns, observed.st_size)
    if observed.st_size > MAX_METADATA_BYTES:
        raise ValueError("Kiro session metadata exceeds size limit")
    meta = json.loads(path.read_text())
    sid = meta.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("Kiro session has no stable id")
    transcript = transcript_path(path)
    if not transcript.exists():
        raise IncompleteSession(f"Kiro session {path.parent.name} has no messages.jsonl yet")
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
    url = session_url(sid)
    paths = meta.get("workspacePaths") or []
    project = normalize_project_tag(Path(paths[0]).name) if paths and isinstance(paths[0], str) else None
    fields = {"Source": url, "Source_Type": "kiro_ide_chat", "Source_ID": sid,
              "Date": str(meta.get("createdAt") or "")[:10],
              "Updated": meta.get("lastModifiedAt") or "", "Model": meta.get("modelId") or "",
              "Workflow": meta.get("agentMode") or "", "Project": project,
              "Capture_Format": "kiro_session_jsonl"}
    body = "\n\n".join(f"**{'User' if role == 'human' else 'Assistant'}:**\n\n{content}" for role, content in messages)
    return url, render_document(meta.get("title") or "Kiro IDE session", fields, body), fields


def messages_as_dicts(messages):
    return [{"role": role, "content": content} for role, content in messages]
