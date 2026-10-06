"""R4 chats channel fixes: incomplete native sessions are skipped (fix 1, D N-2),
runs are bounded/newest-first/resumable (fix 2, D N-5), native identity never
duplicates a legacy row (fix 3), and the channel inventory is channel-scoped.

Synthetic session trees only; the DB inventory is stubbed or disposable.
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import Mock

import pytest

from src.capture import chats
from src.capture.common import exit_code
from src.parsers import kiro_sessions
from tests.test_capture_framework import native_session, patch_inventory


# --------------------------------------------------------------------------- #
# Fix 1 — sessions without messages.jsonl are incomplete, not failed (N-2)      #
# --------------------------------------------------------------------------- #
def test_session_without_transcript_is_incomplete_not_an_error(tmp_path):
    path, _ = native_session(tmp_path)
    path.with_name("messages.jsonl").unlink()
    with pytest.raises(kiro_sessions.IncompleteSession):
        kiro_sessions.parse_session(path)
    assert not issubclass(kiro_sessions.IncompleteSession, (ValueError, OSError))
    assert kiro_sessions.fingerprint(path) is None


def test_incomplete_sessions_do_not_fail_the_run(tmp_path, monkeypatch):
    complete, _ = native_session(tmp_path / "a")
    incomplete, _ = native_session(tmp_path / "b", session_id="no-transcript-yet")
    incomplete.with_name("messages.jsonl").unlink()
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "find_sessions", lambda: [incomplete, complete])
    patch_inventory(monkeypatch)
    result = chats.capture(dry_run=True, cli=False)
    ide = result["kiro_ide_chat"]
    assert ide["failed"] == 0 and ide["incomplete"] == 1 and ide["planned"] == 1
    assert exit_code(result) == 0


def test_incomplete_session_is_skipped_even_when_metadata_lists_it_as_a_candidate(tmp_path, monkeypatch):
    """Belt and braces: if the transcript disappears between inventory and parse."""
    path, _ = native_session(tmp_path)
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "find_sessions", lambda: [path])
    monkeypatch.setattr(chats.kiro_sessions, "fingerprint", lambda p: "stale-fp")
    patch_inventory(monkeypatch)
    path.with_name("messages.jsonl").unlink()
    result = chats.capture(dry_run=True, cli=False)
    assert result["kiro_ide_chat"]["failed"] == 0 and result["kiro_ide_chat"]["incomplete"] == 1


# --------------------------------------------------------------------------- #
# Fix 2 — bounded, newest-first, resumable backfill (N-5)                       #
# --------------------------------------------------------------------------- #
def _sessions(tmp_path, n):
    """n native sessions directly under ``tmp_path/<workspace>/<session>/`` (the real layout)."""
    paths = []
    for i in range(n):
        path, _ = native_session(tmp_path / f"stage{i}", session_id=f"sess-{i}")
        target = tmp_path / "workspace" / f"sess-{i}"
        target.parent.mkdir(exist_ok=True)
        path.parent.rename(target)
        path = target / "session.json"
        # Distinct, increasing transcript mtimes: sess-{n-1} is the newest.
        stamp = 1_700_000_000 + i * 60
        os.utime(path.with_name("messages.jsonl"), (stamp, stamp))
        paths.append(path)
    return paths


def test_bounded_run_examines_newest_first_and_defers_the_rest(tmp_path, monkeypatch):
    paths = _sessions(tmp_path, 5)
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "SESSIONS_DIR", tmp_path)
    patch_inventory(monkeypatch)
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    result = chats.capture(cli=False, max_sessions=2)
    ide = result["kiro_ide_chat"]
    assert ide["processed"] == 2 and ide["deferred"] == 3 and ide["candidates"] == 5
    ingested = [call.args[2] for call in writer.call_args_list]
    assert ingested == ["kiro-ide://session/sess-4", "kiro-ide://session/sess-3"]
    assert kiro_sessions.find_sessions(tmp_path) == list(reversed(paths))


def test_resumes_with_the_next_sessions_once_earlier_ones_are_stored(tmp_path, monkeypatch):
    paths = _sessions(tmp_path, 4)
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "SESSIONS_DIR", tmp_path)
    stored = {f"kiro-ide://session/sess-{i}": kiro_sessions.fingerprint(paths[i]) for i in (3, 2)}
    patch_inventory(monkeypatch, known=set(stored), fingerprints=stored)
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    result = chats.capture(cli=False, max_sessions=2)
    ide = result["kiro_ide_chat"]
    assert ide["unchanged"] == 2 and ide["processed"] == 2 and ide["deferred"] == 0
    assert [c.args[2] for c in writer.call_args_list] == ["kiro-ide://session/sess-1", "kiro-ide://session/sess-0"]


def test_unchanged_sessions_are_not_read_rendered_or_written(tmp_path, monkeypatch):
    path, _ = native_session(tmp_path)
    url = "kiro-ide://session/stable-id"
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "find_sessions", lambda: [path])
    patch_inventory(monkeypatch, known={url}, fingerprints={url: kiro_sessions.fingerprint(path)})
    monkeypatch.setattr(chats.kiro_sessions, "parse_session", lambda p: pytest.fail("unchanged session was parsed"))
    import src.ingest
    monkeypatch.setattr(src.ingest, "ingest_content", lambda *a, **k: pytest.fail("unchanged session was written"))
    result = chats.capture(cli=False)
    assert result["kiro_ide_chat"] == {"processed": 0, "skipped": 0, "failed": 0, "unchanged": 1, "incomplete": 0,
                                       "filtered": 0, "deferred": 0, "candidates": 0, "legacy_new": 0}


def test_changed_session_is_refreshed_with_its_new_fingerprint(tmp_path, monkeypatch):
    path, _ = native_session(tmp_path)
    url = "kiro-ide://session/stable-id"
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "find_sessions", lambda: [path])
    patch_inventory(monkeypatch, known={url}, fingerprints={url: "older-fingerprint"})
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    chats.capture(cli=False)
    assert writer.call_args.kwargs["refresh"] is True
    assert writer.call_args.kwargs["source_metadata"] == {"source_fingerprint": kiro_sessions.fingerprint(path)}


def test_cli_sessions_are_bounded_and_fingerprinted_too(tmp_path, monkeypatch):
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    def session(name, stamp):
        p = cli_dir / f"{name}.jsonl"
        text = lambda t: [{"kind": "text", "data": t}]
        lines = [{"kind": "Prompt", "data": {"content": text("Please explain the synthetic architecture in detail."), "meta": {"timestamp": 1_700_000_000}}},
                 {"kind": "AssistantMessage", "data": {"content": text("The architecture explanation " * 60)}},
                 {"kind": "Prompt", "data": {"content": text("Thanks, now summarize the deployment steps.")}},
                 {"kind": "AssistantMessage", "data": {"content": text("Deployment summary " * 30)}}]
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        os.utime(p, (stamp, stamp))
        return p
    paths = [session(f"0000000{i}-aaaa-bbbb-cccc-ddddddddddd{i}", 1_700_000_000 + i) for i in range(3)]
    monkeypatch.setattr(chats.cli_chat, "SESSIONS_DIR", str(cli_dir))
    known_url = f"chat://{paths[0].stem[:12]}"  # legacy identity, unchanged file
    patch_inventory(monkeypatch, known={known_url}, fingerprints={known_url: chats._cli_fingerprint(paths[0])})
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    result = chats.capture(ide=False, max_sessions=1)
    cli = result["kiro_cli_chat"]
    assert cli["unchanged"] == 1 and cli["processed"] == 1 and cli["deferred"] == 1
    assert writer.call_args.args[2] == f"chat://{paths[2].stem}"  # newest first, full id for new sessions
    assert writer.call_args.kwargs["source_metadata"] == {"source_fingerprint": chats._cli_fingerprint(paths[2])}


def test_max_sessions_is_validated_and_defaults_from_env(monkeypatch):
    with pytest.raises(ValueError):
        chats.capture(max_sessions=0, ide=False, cli=False)
    assert chats.DEFAULT_MAX_SESSIONS >= 1


# --------------------------------------------------------------------------- #
# Fix 3 — native identity never duplicates a legacy row of the same session     #
# --------------------------------------------------------------------------- #
def test_legacy_aliases_include_the_hyphen_stripped_uuid():
    sid = "2bf48e2a-1b2c-4d3e-8f90-abcdef123456"
    assert kiro_sessions.legacy_aliases(sid) == [
        f"chat://{sid}", f"ide_{sid}.md",
        "chat://2bf48e2a1b2c4d3e8f90abcdef123456", "ide_2bf48e2a1b2c4d3e8f90abcdef123456.md",
    ]
    assert kiro_sessions.legacy_aliases("sess_x") == ["chat://sess_x", "ide_sess_x.md"]


def test_native_session_matches_legacy_row_keyed_by_32_hex_id(tmp_path, monkeypatch):
    sid = "2bf48e2a-1b2c-4d3e-8f90-abcdef123456"
    path, _ = native_session(tmp_path, session_id=sid)
    legacy = "chat://" + sid.replace("-", "")
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, "find_sessions", lambda: [path])
    patch_inventory(monkeypatch, known={legacy})
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    chats.capture(cli=False)
    assert writer.call_count == 1 and writer.call_args.args[2] == legacy
    assert writer.call_args.kwargs["refresh"] is True


# --------------------------------------------------------------------------- #
# Channel inventory is always channel-scoped                                    #
# --------------------------------------------------------------------------- #
def test_source_url_inventory_requires_a_channel():
    from src.db import get_processed_source_urls, get_capture_fingerprints
    for bad in (None, "", "  "):
        with pytest.raises(ValueError):
            get_processed_source_urls(bad)
        with pytest.raises(ValueError):
            get_capture_fingerprints(bad)


def test_capture_fingerprints_come_from_parent_metadata(test_conn):
    from src import db
    from src.db import get_capture_fingerprints
    parent = db.create_memory(type="source", title="t", content="c", source_url="kiro-ide://session/x",
                              source_type="kiro_ide_chat", metadata={"source_fingerprint": "1:2:3:4"})
    db.create_memory(type="source", title="t", content="c", source_url="kiro-ide://session/x",
                     source_type="kiro_ide_chat", parent_id=parent, metadata={"source_fingerprint": "child"})
    db.create_memory(type="source", title="t", content="c", source_url="chat://legacy", source_type="kiro_ide_chat", metadata={})
    assert get_capture_fingerprints("kiro_ide_chat") == {"kiro-ide://session/x": "1:2:3:4", "chat://legacy": None}
