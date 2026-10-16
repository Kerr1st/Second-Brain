"""Direct Kiro IDE/CLI capture, replacing the extract-to-staging schedule.

Stable source identities are refreshed atomically by ingestion when sessions grow.
Existing legacy identities are reused, preserving citations during the transition.
Staged leftovers have a separate, explicitly invoked maintenance drain.

Bounded, resumable runs (D N-5)
-------------------------------
Every run is bounded by ``max_sessions`` *ingested* sessions per channel
(``--max-sessions`` / ``SECOND_BRAIN_CHATS_MAX_SESSIONS``, default
``DEFAULT_MAX_SESSIONS``). Selection is by state, not by cursor, so a run that is
killed or fails simply leaves the remainder for the next run:

* a session is a *candidate* when its identity is unknown to the channel or its
  source fingerprint (mtime/size of the session files) differs from the one
  stored with the captured row;
* candidates are examined **newest-first** (most recently modified transcript
  first), so fresh work lands immediately and the historical backlog drains
  behind it over successive nightly runs;
* sessions whose fingerprint matches are skipped without being read, rendered or
  written (``unchanged``); sessions that exist but have no transcript yet are
  ``incomplete`` and neither failed nor counted against the bound.

Legacy ``.chat`` IDE files are a dead channel (newest file 2026-02): only files
whose identity is not yet stored are parsed and ingested, never refreshed.

Kiro CLI v2 sessions (``sessions/<workspace-hash>/sess_<uuid>/``) share the
native session format and tree with the IDE store but belong to
``kiro_cli_chat`` (identity ``kiro-cli://session/<id>``). Those already stored
as ``kiro_ide_chat`` before the split stay there (and keep refreshing) so no
session is duplicated across channels; ``sub-executions/`` sub-agent runs are
never read.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path

from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import ingest_with_metadata, run_channel
from src.parsers import ide_chat, cli_chat, kiro_sessions

logger = logging.getLogger(__name__)

DEFAULT_MAX_SESSIONS = int(os.environ.get("SECOND_BRAIN_CHATS_MAX_SESSIONS", "50"))


def _legacy_url(identity, source, known):
    aliases = ([f"chat://{identity}", f"ide_{identity}.md"] if source == "kiro_ide_chat" else
               [f"chat://{identity[:12]}", f"cli_{identity[:12]}.md"])
    return next((url for url in aliases if url in known), f"chat://{identity}")


def _cli_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _inventory(source_type):
    """Known identities and stored fingerprints for one channel (reads only)."""
    from src.db import get_processed_source_urls, get_capture_fingerprints
    known = set(get_processed_source_urls(source_type))
    fingerprints = get_capture_fingerprints(source_type)
    return known, fingerprints


def _bounded(candidates, parse, items, metadata, max_sessions, counters):
    """Parse newest-first until ``max_sessions`` items are collected.

    ``parse(candidate)`` returns ``(url, markdown)`` or ``None`` (filtered).
    Candidates left unexamined are reported as ``deferred``.
    """
    for index, candidate in enumerate(candidates):
        if len(items) >= max_sessions:
            counters["deferred"] = len(candidates) - index
            break
        try:
            parsed = parse(candidate)
        except kiro_sessions.IncompleteSession:
            counters["incomplete"] += 1
            continue
        except Exception:  # noqa: BLE001 - one malformed session never hides the rest (R3 S3-8)
            logger.warning("Chat session could not be parsed; retry next cycle", exc_info=True)
            items.failed += 1
            continue
        if parsed is None:
            counters["filtered"] += 1
            continue
        url, markdown, fingerprint = parsed
        if url in {existing for existing, _ in items}:
            continue
        items.append((url, markdown))
        metadata[url] = {"source_fingerprint": fingerprint}


def capture(*, dry_run=False, ide=True, cli=True, max_sessions=None) -> dict:
    max_sessions = DEFAULT_MAX_SESSIONS if max_sessions is None else max_sessions
    if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1:
        raise ValueError("max_sessions must be a positive integer")
    result = {}
    ide_known = None
    if ide:
        known, stored = _inventory("kiro_ide_chat")
        ide_known = known
        items = CaptureBatch()
        metadata = {}
        counters = {"unchanged": 0, "incomplete": 0, "filtered": 0, "deferred": 0, "candidates": 0, "legacy_new": 0}
        # Legacy .chat files: dead channel; parse only identities not yet stored.
        for path in ide_chat.find_chat_files():
            name = os.path.basename(path).removesuffix(".chat")
            if any(alias in known for alias in (f"chat://{name}", f"ide_{name}.md")):
                continue
            try:
                parsed = ide_chat.parse_chat_file(path, strict=True)
            except Exception:  # noqa: BLE001
                logger.exception("Legacy IDE chat could not be parsed")
                items.failed += 1
                continue
            if parsed:
                name, md, _ = parsed
                items.append((_legacy_url(name, "kiro_ide_chat", known), md))
                counters["legacy_new"] += 1
        # Native sessions: candidates = unknown identity or changed fingerprint.
        candidates = []
        for path in kiro_sessions.find_sessions():
            try:
                sid = kiro_sessions.session_id(path)
            except Exception:  # noqa: BLE001
                logger.warning("Native IDE session metadata could not be read; retry next cycle", exc_info=True)
                items.failed += 1
                continue
            # A session migrated from the legacy store keeps its existing identity.
            url = next((alias for alias in kiro_sessions.legacy_aliases(sid) if alias in known),
                       kiro_sessions.session_url(sid))
            # Kiro CLI v2 sessions share this tree but belong to kiro_cli_chat. Ones
            # already stored here (before the split) keep their identity and refresh
            # here until retagged, so they are never duplicated across channels.
            if kiro_sessions.is_cli_v2_session(path) and url not in known:
                counters["cli_v2"] = counters.get("cli_v2", 0) + 1  # reported only when non-zero
                continue
            fingerprint = kiro_sessions.fingerprint(path)
            if fingerprint is None:
                counters["incomplete"] += 1
                continue
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            candidates.append((path, url, fingerprint))
        counters["candidates"] = len(candidates)

        def parse_native(candidate):
            path, url, fingerprint = candidate
            parsed = kiro_sessions.parse_session(path)
            if parsed is None:
                return None
            _, md, _ = parsed
            # Replace an older legacy representation of the same session.
            items[:] = [(existing, content) for existing, content in items if existing != url]
            return url, md, fingerprint

        _bounded(candidates, parse_native, items, metadata, max_sessions, counters)
        stats = run_channel("kiro_ide_chat", items, dry_run=dry_run,
                            ingest=ingest_with_metadata(metadata, refresh=True))
        result["kiro_ide_chat"] = {**stats, **counters}
    if cli:
        known, stored = _inventory("kiro_cli_chat")
        items = CaptureBatch()
        metadata = {}
        counters = {"unchanged": 0, "incomplete": 0, "filtered": 0, "deferred": 0, "candidates": 0}
        candidates = []  # (recency_ns, kind, path, url, fingerprint)
        sessions_dir = Path(cli_chat.SESSIONS_DIR)
        paths = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True) \
            if sessions_dir.is_dir() else []
        for path in paths:
            identity = path.stem
            url = _legacy_url(identity, "kiro_cli_chat", known)
            fingerprint = _cli_fingerprint(path)
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            candidates.append((path.stat().st_mtime_ns, "v1", path, url, fingerprint))

        # Kiro CLI v2 store: <sessions>/<workspace-hash>/sess_<uuid>/ next to the v1 cli/ dir.
        v2_root = sessions_dir.parent
        v2_paths = [p for p in kiro_sessions.find_sessions(v2_root) if kiro_sessions.is_cli_v2_session(p)] \
            if v2_root.is_dir() else []
        if v2_paths and ide_known is None:
            ide_known, _ = _inventory("kiro_ide_chat")
        for path in v2_paths:
            try:
                sid = kiro_sessions.session_id(path)
            except Exception:  # noqa: BLE001
                logger.warning("Kiro CLI v2 session metadata could not be read; retry next cycle", exc_info=True)
                items.failed += 1
                continue
            if any(alias in ide_known for alias in [kiro_sessions.session_url(sid), *kiro_sessions.legacy_aliases(sid)]):
                counters["v2_owned_by_ide"] = counters.get("v2_owned_by_ide", 0) + 1  # captured as kiro_ide_chat before the split
                continue
            # A row retagged from kiro_ide_chat keeps its original URL.
            ide_url = kiro_sessions.session_url(sid)
            url = ide_url if ide_url in known else kiro_sessions.cli_v2_url(sid)
            fingerprint = kiro_sessions.fingerprint(path)
            if fingerprint is None:
                counters["incomplete"] += 1
                continue
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            transcript = kiro_sessions.transcript_path(path)
            candidates.append((transcript.stat().st_mtime_ns, "v2", path, url, fingerprint))
        candidates.sort(key=lambda c: c[0], reverse=True)  # newest first across both stores
        counters["candidates"] = len(candidates)

        def parse_cli(candidate):
            _, kind, path, url, fingerprint = candidate
            if kind == "v2":
                parsed = kiro_sessions.parse_session(
                    path, source_type="kiro_cli_chat", url_for=lambda _sid: url,
                    default_title="Kiro CLI session", reject=cli_chat.is_automated_conversation)
                return None if parsed is None else (url, parsed[1], fingerprint)
            messages, created = cli_chat.extract_session_messages(path, strict=True)
            parsed = cli_chat.parse_conversation(path.stem, messages, created)
            if parsed is None:
                return None
            _, md, _ = parsed
            return url, md, fingerprint

        _bounded(candidates, parse_cli, items, metadata, max_sessions, counters)
        stats = run_channel("kiro_cli_chat", items, dry_run=dry_run,
                            ingest=ingest_with_metadata(metadata, refresh=True))
        result["kiro_cli_chat"] = {**stats, **counters}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ide", action="store_true")
    parser.add_argument("--no-cli", action="store_true")
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS,
                        help=f"sessions ingested per channel per run (default {DEFAULT_MAX_SESSIONS}; newest first)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = capture(dry_run=args.dry_run, ide=not args.no_ide, cli=not args.no_cli, max_sessions=args.max_sessions)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
