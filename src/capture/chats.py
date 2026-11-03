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

Kiro CLI v2 sessions (``sessions/<workspace-hash>/<sess_*|cli_*>/`` with a
``sessions/cli/<id>.history`` prompt history, see
:func:`src.parsers.kiro_sessions.is_cli_v2_session`) share the native session
format and tree with the IDE store but belong to ``kiro_cli_chat`` (identity
``kiro-cli://session/<id>``). The IDE writes ``sess_*`` dirs too; those stay
``kiro_ide_chat``. A session already stored in the other channel stays there
(and keeps refreshing) so no session is duplicated across channels;
``sub-executions/`` sub-agent runs are never read. A ``cli_<uuid>_<suffix>``
session that resumes a v1 conversation (and structurally contains it) refreshes
that v1 row instead of creating a second one.
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


def _continues_v1(metadata_path, v1_path: Path) -> bool | None:
    """Whether a ``cli_<uuid>_*`` session's turns contain its v1 conversation, in order.

    ``None`` when the v1 file is absent (nothing to compare). Structural only:
    whitespace-normalised turn texts are compared, nothing is logged.
    """
    if not v1_path.is_file():
        return None
    v1_messages, _ = cli_chat.extract_session_messages(v1_path, strict=True)
    v1_turns = [(role, " ".join(text.split())) for role, text in v1_messages]
    return kiro_sessions.contains_conversation(kiro_sessions.conversation_turns(metadata_path), v1_turns)


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
    cli_inventory = None
    if ide:
        known, stored = _inventory("kiro_ide_chat")
        ide_known = known
        cli_inventory = _inventory("kiro_cli_chat")
        cli_owned = cli_inventory[0]
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
            # The mirror rule: a session whose identity already lives in kiro_cli_chat
            # (e.g. retagged there) is never written a second time here.
            if url not in known and url in cli_owned:
                counters["owned_by_cli"] = counters.get("owned_by_cli", 0) + 1  # reported only when non-zero
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
        known, stored = cli_inventory if cli_inventory is not None else _inventory("kiro_cli_chat")
        items = CaptureBatch()
        metadata = {}
        counters = {"unchanged": 0, "incomplete": 0, "filtered": 0, "deferred": 0, "candidates": 0}
        candidates = []  # (recency_ns, kind, path, url, fingerprint)
        sessions_dir = Path(cli_chat.SESSIONS_DIR)
        paths = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True) \
            if sessions_dir.is_dir() else []
        v1_entries = []  # (path, url, fingerprint); decided after v2 continuations are known
        for path in paths:
            identity = path.stem
            url = _legacy_url(identity, "kiro_cli_chat", known)
            v1_entries.append((path, url, _cli_fingerprint(path)))

        # Kiro CLI v2 store: <sessions>/<workspace-hash>/<sess_*|cli_*>/ next to the v1 cli/ dir.
        v2_root = sessions_dir.parent
        v2_paths = [p for p in kiro_sessions.find_sessions(v2_root) if kiro_sessions.is_cli_v2_session(p)] \
            if v2_root.is_dir() else []
        if v2_paths and ide_known is None:
            ide_known, _ = _inventory("kiro_ide_chat")
        continuations = {}  # v1 url -> (recency_ns, path, fingerprint): newest continuation wins
        held = set()  # v1 urls whose continuation could not be compared this run
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
            v1_identity = kiro_sessions.cli_continuation_of(path)
            if v1_identity is not None and url not in known:
                v1_url = _legacy_url(v1_identity, "kiro_cli_chat", known)
                try:
                    contained = _continues_v1(path, sessions_dir / f"{v1_identity}.jsonl")
                except Exception:  # noqa: BLE001
                    logger.warning("Kiro CLI continuation could not be compared; retry next cycle", exc_info=True)
                    items.failed += 1
                    # Its v1 row may already hold the continuation: never overwrite it
                    # with the shorter v1 file while the comparison is unreadable.
                    held.add(v1_url)
                    continue
                if contained is None and v1_url in known:
                    # The v1 row exists but its file is gone: containment cannot be
                    # verified, and a new identity could duplicate it. Left for review.
                    counters["continuation_unverified"] = counters.get("continuation_unverified", 0) + 1
                    continue
                if contained:
                    recency = int(fingerprint.split(":")[2])
                    previous = continuations.get(v1_url)
                    if previous is not None:
                        counters["continuation_superseded"] = counters.get("continuation_superseded", 0) + 1
                    if previous is None or recency > previous[0]:
                        continuations[v1_url] = (recency, path, fingerprint)
                    continue
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            # Recency from the fingerprint just taken (meta_mtime:meta_size:transcript_mtime:size):
            # a second stat() could race a rotating transcript and abort the whole channel.
            candidates.append((int(fingerprint.split(":")[2]), "v2", path, url, fingerprint))
        # A continuation that contains its v1 conversation refreshes the v1 identity;
        # the v1 file is then not a candidate this run (one write per identity).
        for url, (recency, path, fingerprint) in continuations.items():
            counters["v1_continued"] = counters.get("v1_continued", 0) + 1
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            candidates.append((recency, "v2", path, url, fingerprint))
        for path, url, fingerprint in v1_entries:
            if url in continuations or url in held:
                continue
            if url in known and stored.get(url) == fingerprint:
                counters["unchanged"] += 1
                continue
            candidates.append((int(fingerprint.split(":")[0]), "v1", path, url, fingerprint))
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
