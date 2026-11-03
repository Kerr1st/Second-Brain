"""Kiro CLI v2 (TUI) sessions are captured as ``kiro_cli_chat``.

Live state 2026-10-15: Kiro CLI writes interactive sessions to
``~/.kiro/sessions/<workspace-hash>/sess_<uuid>/`` (session.json + messages.jsonl)
and its sub-agent runs to ``sess_*/sub-executions/``; ``sessions/cli/*.jsonl``
only receives sub-agent sessions. The CLI channel scanned only ``cli/`` and the
IDE channel's ``*/*/session.json`` glob swallowed the v2 sessions as
``kiro_ide_chat``, so ``kiro_cli_chat`` went silent.

2026-10-16: the Kiro IDE writes ``sess_*`` dirs too, so the CLI is recognised by
its prompt history ``sessions/cli/<id>.history`` (or ``_meta.kiro.executionTarget``),
not by the directory prefix.

Synthetic session trees only; the DB inventory and the writer are stubbed.
"""
from __future__ import annotations

import json
import os
from unittest.mock import Mock

import pytest

from src.capture import chats
from src.parsers import kiro_sessions

V2_ID = "sess_11111111-2222-3333-4444-555555555555"
IDE_ID = "66666666-7777-8888-9999-000000000000"
ASSISTANT = "synthetic explanation " * 40


def _records(first_user="How should the synthetic service be structured?"):
    return [
        {"id": "m0", "payload": {"type": "session_start", "content": "SYSTEM_DOC"}},
        {"id": "m1", "payload": {"type": "user", "content": first_user}},
        {"id": "m2", "payload": {"type": "assistant", "operationType": "Reasoning", "content": "PRIVATE_REASONING"}},
        {"id": "m3", "payload": {"type": "tool_call", "content": None}},
        {"id": "m4", "payload": {"type": "tool_result", "content": "SECRET_TOOL_OUTPUT"}},
        {"id": "m5", "payload": {"type": "sub_agent_start", "content": None}},
        {"id": "m6", "payload": {"type": "agent_note", "content": "AGENT_NOTE"}},
        {"id": "m7", "payload": {"type": "assistant", "operationType": "Say", "content": ASSISTANT}},
        {"id": "m8", "payload": {"type": "user", "source": "steer", "content": "Now apply it to the next milestone."}},
        {"id": "m9", "payload": {"type": "assistant", "operationType": "Say", "content": "Applied. " * 10}},
    ]


def _session(root, workspace, dirname, sid, *, stamp, records=None, transcript=True, sub_execution=False,
             history=False, meta_kiro=None):
    """``history=True`` writes the CLI prompt history ``<root>/cli/<dirname>.history``."""
    folder = root / workspace / dirname
    folder.mkdir(parents=True)
    meta = folder / "session.json"
    data = {"id": sid, "schemaVersion": "1.0.0", "title": "Synthetic CLI work",
            "agentMode": "vibe", "modelId": "model", "status": "idle",
            "createdAt": "2026-10-14T08:00:00Z", "lastModifiedAt": "2026-10-14T09:00:00Z",
            "workspacePaths": ["/synthetic/project"]}
    if meta_kiro is not None:
        data["_meta"] = {"kiro": meta_kiro}
    meta.write_text(json.dumps(data))
    if history:
        (root / "cli").mkdir(parents=True, exist_ok=True)
        (root / "cli" / f"{dirname}.history").write_text("synthetic prompt\n")
    if transcript:
        msgs = folder / "messages.jsonl"
        msgs.write_text("\n".join(json.dumps(r) for r in (records or _records())) + "\n")
        os.utime(msgs, (stamp, stamp))
    if sub_execution:
        sub = folder / "sub-executions"
        sub.mkdir()
        (sub / "child.jsonl").write_text(json.dumps(
            {"id": "s1", "payload": {"type": "user", "content": "SUB_AGENT_PROMPT"}}) + "\n")
    return meta


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A synthetic ~/.kiro/sessions: empty v1 cli/, one v2 CLI session, one IDE session."""
    root = tmp_path / "sessions"
    (root / "cli").mkdir(parents=True)
    v2 = _session(root, "abcdef0123456789", V2_ID, V2_ID, stamp=1_790_000_000, sub_execution=True, history=True)
    ide = _session(root, "0123456789abcdef", IDE_ID, IDE_ID, stamp=1_780_000_000)
    monkeypatch.setattr(chats.cli_chat, "SESSIONS_DIR", str(root / "cli"))
    monkeypatch.setattr(chats.kiro_sessions, "SESSIONS_DIR", root)
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    return root, v2, ide


def _inventory(monkeypatch, cli=None, ide=None):
    """Per-channel stub: {channel: {url: fingerprint}}."""
    channels = {"kiro_cli_chat": dict(cli or {}), "kiro_ide_chat": dict(ide or {})}
    monkeypatch.setattr(chats, "_inventory", lambda source: (set(channels[source]), dict(channels[source])))


def _writer(monkeypatch):
    import src.ingest
    writer = Mock(return_value="id")
    monkeypatch.setattr(src.ingest, "ingest_content", writer)
    return writer


def _written(writer):
    return {(c.args[1], c.args[2]): c.args[0] for c in writer.call_args_list}


def test_v2_session_is_captured_as_cli_chat_with_turns_only(store, monkeypatch):
    _, v2, _ = store
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture()
    written = _written(writer)
    url = f"kiro-cli://session/{V2_ID}"
    assert set(written) == {("kiro_cli_chat", url), ("kiro_ide_chat", f"kiro-ide://session/{IDE_ID}")}
    md = written[("kiro_cli_chat", url)]
    assert "kiro_cli_chat" in md and md.count("**User:**") == 2 and md.count("**Assistant:**") == 2
    for noise in ("PRIVATE_REASONING", "SECRET_TOOL_OUTPUT", "AGENT_NOTE", "SYSTEM_DOC", "SUB_AGENT_PROMPT"):
        assert noise not in md
    assert result["kiro_cli_chat"]["processed"] == 1
    assert result["kiro_ide_chat"]["processed"] == 1 and result["kiro_ide_chat"]["cli_v2"] == 1
    cli_call = next(c for c in writer.call_args_list if c.args[1] == "kiro_cli_chat")
    assert cli_call.kwargs["source_metadata"] == {"source_fingerprint": kiro_sessions.fingerprint(v2)}
    assert cli_call.kwargs["refresh"] is True


def test_cli_only_run_still_finds_v2_and_consults_ide_inventory(store, monkeypatch):
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture(ide=False)
    assert set(_written(writer)) == {("kiro_cli_chat", f"kiro-cli://session/{V2_ID}")}
    assert result["kiro_cli_chat"]["candidates"] == 1


def test_v2_session_already_stored_as_ide_is_not_duplicated(store, monkeypatch):
    """Pre-split rows (14 live on 2026-10-15) keep their identity in kiro_ide_chat."""
    _, v2, _ = store
    ide_url = f"kiro-ide://session/{V2_ID}"
    _inventory(monkeypatch, ide={ide_url: "older-fingerprint"})
    writer = _writer(monkeypatch)
    result = chats.capture()
    written = _written(writer)
    assert ("kiro_cli_chat", f"kiro-cli://session/{V2_ID}") not in written
    assert ("kiro_ide_chat", ide_url) in written  # still refreshed where it already lives
    assert result["kiro_cli_chat"]["v2_owned_by_ide"] == 1 and result["kiro_cli_chat"]["processed"] == 0


def test_retagged_row_keeps_its_original_url(store, monkeypatch):
    """After a retag migration the kiro-ide:// URL lives in kiro_cli_chat and is refreshed in place."""
    ide_url = f"kiro-ide://session/{V2_ID}"
    _inventory(monkeypatch, cli={ide_url: "older-fingerprint"})
    writer = _writer(monkeypatch)
    chats.capture(ide=False)
    assert set(_written(writer)) == {("kiro_cli_chat", ide_url)}


def test_unchanged_v2_session_is_not_read_or_written(store, monkeypatch):
    _, v2, _ = store
    url = f"kiro-cli://session/{V2_ID}"
    _inventory(monkeypatch, cli={url: kiro_sessions.fingerprint(v2)})
    monkeypatch.setattr(chats.kiro_sessions, "parse_session", lambda *a, **k: pytest.fail("unchanged session parsed"))
    writer = _writer(monkeypatch)
    result = chats.capture(ide=False)
    assert writer.call_count == 0
    assert result["kiro_cli_chat"]["unchanged"] == 1 and result["kiro_cli_chat"]["candidates"] == 0


def test_automated_v2_conversation_is_filtered_like_v1(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    (root / "cli").mkdir(parents=True)
    _session(root, "abcdef0123456789", V2_ID, V2_ID, stamp=1_790_000_000,
             records=_records(first_user="Run type: nightly synthetic job"), history=True)
    monkeypatch.setattr(chats.cli_chat, "SESSIONS_DIR", str(root / "cli"))
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture(ide=False)
    assert writer.call_count == 0 and result["kiro_cli_chat"]["filtered"] == 1


def test_v2_session_without_transcript_is_incomplete_not_failed(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    (root / "cli").mkdir(parents=True)
    _session(root, "abcdef0123456789", V2_ID, V2_ID, stamp=1_790_000_000, transcript=False, history=True)
    monkeypatch.setattr(chats.cli_chat, "SESSIONS_DIR", str(root / "cli"))
    _inventory(monkeypatch)
    result = chats.capture(ide=False, dry_run=True)
    assert result["kiro_cli_chat"]["incomplete"] == 1 and result["kiro_cli_chat"]["failed"] == 0


def test_sub_executions_are_never_sessions(store):
    root, v2, ide = store
    found = kiro_sessions.find_sessions(root)
    assert set(found) == {v2, ide}
    assert kiro_sessions.is_cli_v2_session(v2) and not kiro_sessions.is_cli_v2_session(ide)


def test_newest_first_across_v1_and_v2_and_bounded(store, monkeypatch):
    root, _, _ = store
    text = lambda t: [{"kind": "text", "data": t}]
    v1 = root / "cli" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    v1.write_text("\n".join(json.dumps(line) for line in [
        {"kind": "Prompt", "data": {"content": text("Explain the synthetic design."), "meta": {"timestamp": 1_700_000_000}}},
        {"kind": "AssistantMessage", "data": {"content": text("Design explanation " * 60)}},
        {"kind": "Prompt", "data": {"content": text("Summarize the steps.")}},
        {"kind": "AssistantMessage", "data": {"content": text("Steps " * 30)}}]) + "\n")
    os.utime(v1, (1_795_000_000, 1_795_000_000))  # newer than the v2 transcript
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture(ide=False, max_sessions=1)
    assert set(_written(writer)) == {("kiro_cli_chat", f"chat://{v1.stem}")}
    assert result["kiro_cli_chat"]["deferred"] == 1 and result["kiro_cli_chat"]["candidates"] == 2


def test_dry_run_plans_without_writing(store, monkeypatch):
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture(dry_run=True)
    assert writer.call_count == 0
    assert result["kiro_cli_chat"]["planned"] == 1 and result["kiro_cli_chat"]["dry_run"] is True


def test_ide_parse_defaults_are_unchanged(store):
    _, _, ide = store
    url, md, fields = kiro_sessions.parse_session(ide)
    assert url == f"kiro-ide://session/{IDE_ID}" and fields["Source_Type"] == "kiro_ide_chat"


# --- Writer signal (2026-10-16): the Kiro IDE writes sess_* dirs too -----------------------

IDE_SESS_ID = "sess_77777777-8888-9999-aaaa-bbbbbbbbbbbb"
V1_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CONT_ID = f"cli_{V1_UUID}_Ab12Cd34"
V1_TURNS = [("human", "Explain the synthetic design."), ("bot", "Design explanation " * 60),
            ("human", "Summarize the steps."), ("bot", "Steps " * 30)]


def _v1_file(root, turns=V1_TURNS, stamp=1_770_000_000):
    text = lambda t: [{"kind": "text", "data": t}]
    path = root / "cli" / f"{V1_UUID}.jsonl"
    path.write_text("\n".join(json.dumps(
        {"kind": "Prompt" if role == "human" else "AssistantMessage", "data": {"content": text(t)}})
        for role, t in turns) + "\n")
    os.utime(path, (stamp, stamp))
    return path


def _continuation_records(turns):
    records = []
    for index, (role, t) in enumerate(turns):
        payload = {"type": "user", "content": t} if role == "human" else \
            {"type": "assistant", "operationType": "Say", "content": t}
        records.append({"id": f"c{index}", "payload": payload})
    return records


def _bare_store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    (root / "cli").mkdir(parents=True)
    monkeypatch.setattr(chats.cli_chat, "SESSIONS_DIR", str(root / "cli"))
    monkeypatch.setattr(chats.kiro_sessions, "SESSIONS_DIR", root)
    monkeypatch.setattr(chats.ide_chat, "find_chat_files", lambda: [])
    return root


@pytest.mark.parametrize("dirname, history, meta_kiro, expected", [
    (V2_ID, True, None, True),                                                  # primary signal
    (V2_ID, True, {"policyPreset": "p", "shellType": "zsh"}, True),             # history wins
    (V2_ID, False, {"executionTarget": {"kind": "local"}}, True),               # secondary signal
    (V2_ID, False, None, False),                                                # IDE, no _meta
    (V2_ID, False, {"policyPreset": "p", "shellType": "zsh"}, False),           # IDE-like
    (V2_ID, False, {"isEmptyWorkspace": True, "policyPreset": "p"}, False),     # IDE _global
    (CONT_ID, True, None, True),                                                # cli_ continuation
    (CONT_ID, False, None, False),
    (IDE_ID, True, {"executionTarget": {"kind": "local"}}, False),              # bare UUID: IDE store
])
def test_cli_writer_signal(tmp_path, dirname, history, meta_kiro, expected):
    root = tmp_path / "sessions"
    meta = _session(root, "abcdef0123456789", dirname, dirname, stamp=1_790_000_000,
                    history=history, meta_kiro=meta_kiro)
    assert kiro_sessions.is_cli_v2_session(meta) is expected


def test_ide_written_sess_dirs_are_captured_as_ide_never_cli(tmp_path, monkeypatch):
    root = _bare_store(tmp_path, monkeypatch)
    _session(root, "212b25e5b3d6c176", IDE_SESS_ID, IDE_SESS_ID, stamp=1_790_000_000,
             meta_kiro={"policyPreset": "p", "shellType": "zsh"})
    no_meta = "sess_99999999-8888-7777-6666-555555555555"
    _session(root, "c8727fd3c8727fd3", no_meta, no_meta, stamp=1_790_000_100)
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    result = chats.capture()
    assert set(_written(writer)) == {("kiro_ide_chat", f"kiro-ide://session/{IDE_SESS_ID}"),
                                     ("kiro_ide_chat", f"kiro-ide://session/{no_meta}")}
    assert result["kiro_cli_chat"]["candidates"] == 0 and "cli_v2" not in result["kiro_ide_chat"]


def test_ide_session_stored_in_cli_channel_is_not_duplicated_into_ide(tmp_path, monkeypatch):
    """A mis-retagged IDE row stays where it is until corrected; no second row appears."""
    root = _bare_store(tmp_path, monkeypatch)
    _session(root, "212b25e5b3d6c176", IDE_SESS_ID, IDE_SESS_ID, stamp=1_790_000_000,
             meta_kiro={"policyPreset": "p", "shellType": "zsh"})
    url = f"kiro-ide://session/{IDE_SESS_ID}"
    _inventory(monkeypatch, cli={url: "older-fingerprint"})
    writer = _writer(monkeypatch)
    result = chats.capture()
    assert writer.call_count == 0
    assert result["kiro_ide_chat"]["owned_by_cli"] == 1 and result["kiro_cli_chat"]["candidates"] == 0


def test_cli_continuation_refreshes_its_v1_row_exactly_once(tmp_path, monkeypatch):
    root = _bare_store(tmp_path, monkeypatch)
    _v1_file(root)
    more = V1_TURNS + [("human", "And the follow-up?"), ("bot", "Follow-up answer " * 20)]
    cont = _session(root, "788584e9f1ebfef0", CONT_ID, CONT_ID, stamp=1_790_000_000,
                    records=_continuation_records(more), history=True)
    v1_url = f"chat://{V1_UUID[:12]}"
    _inventory(monkeypatch, cli={v1_url: "older-v1-fingerprint"})
    writer = _writer(monkeypatch)
    result = chats.capture()
    cli_writes = [c for c in writer.call_args_list if c.args[1] == "kiro_cli_chat"]
    assert len(cli_writes) == 1 and cli_writes[0].args[2] == v1_url
    assert "And the follow-up?" in cli_writes[0].args[0]
    assert cli_writes[0].kwargs["source_metadata"] == {"source_fingerprint": kiro_sessions.fingerprint(cont)}
    assert not [c for c in writer.call_args_list if c.args[1] == "kiro_ide_chat"]
    assert result["kiro_cli_chat"]["v1_continued"] == 1

    # Next run: the continuation is unchanged, and the v1 file must still not be rewritten.
    writer.reset_mock()
    _inventory(monkeypatch, cli={v1_url: kiro_sessions.fingerprint(cont)})
    result = chats.capture()
    assert writer.call_count == 0 and result["kiro_cli_chat"]["candidates"] == 0


def test_cli_continuation_without_its_v1_conversation_gets_its_own_identity(tmp_path, monkeypatch):
    root = _bare_store(tmp_path, monkeypatch)
    _v1_file(root)
    other = [("human", "A different synthetic question?"), ("bot", "Different answer " * 60),
             ("human", "Another one?"), ("bot", "More " * 30)]
    _session(root, "788584e9f1ebfef0", CONT_ID, CONT_ID, stamp=1_790_000_000,
             records=_continuation_records(other), history=True)
    _inventory(monkeypatch)
    writer = _writer(monkeypatch)
    chats.capture()
    assert set(_written(writer)) == {("kiro_cli_chat", f"kiro-cli://session/{CONT_ID}"),
                                     ("kiro_cli_chat", f"chat://{V1_UUID}")}


def test_cli_continuation_owned_by_ide_stays_there(tmp_path, monkeypatch):
    """Ownership rule: the pre-existing kiro_ide_chat row of a continuation keeps refreshing there."""
    root = _bare_store(tmp_path, monkeypatch)
    v1 = _v1_file(root)
    _session(root, "788584e9f1ebfef0", CONT_ID, CONT_ID, stamp=1_790_000_000,
             records=_continuation_records(V1_TURNS + [("human", "Next?")]), history=True)
    ide_url, v1_url = f"kiro-ide://session/{CONT_ID}", f"chat://{V1_UUID[:12]}"
    _inventory(monkeypatch, cli={v1_url: chats._cli_fingerprint(v1)}, ide={ide_url: "older"})
    writer = _writer(monkeypatch)
    result = chats.capture()
    assert set(_written(writer)) == {("kiro_ide_chat", ide_url)}
    assert result["kiro_cli_chat"]["v2_owned_by_ide"] == 1


def test_synthetic_steering_user_turn_is_skipped(tmp_path):
    root = tmp_path / "sessions"
    records = _records()
    records.insert(2, {"id": "s1", "payload": {"type": "user", "content": "STEERING_INJECTION_TEXT",
                                               "_meta": {"kiro": {"syntheticUserMessageReason": "steering-injection"}}}})
    records.insert(3, {"id": "s2", "payload": {"type": "user", "content": None,
                                               "_meta": {"kiro": {"syntheticUserMessageReason": "steering-injection"}}}})
    meta = _session(root, "0123456789abcdef", IDE_ID, IDE_ID, stamp=1_790_000_000, records=records)
    _, md, _ = kiro_sessions.parse_session(meta)
    assert "STEERING_INJECTION_TEXT" not in md and md.count("**User:**") == 2


def test_unreadable_continuation_never_lets_the_v1_file_overwrite_its_row(tmp_path, monkeypatch):
    root = _bare_store(tmp_path, monkeypatch)
    _v1_file(root)
    cont = _session(root, "788584e9f1ebfef0", CONT_ID, CONT_ID, stamp=1_790_000_000,
                    records=_continuation_records(V1_TURNS), history=True)
    with cont.with_name("messages.jsonl").open("a") as handle:
        handle.write('{"id": "partial", "payl')  # transcript mid-write
    v1_url = f"chat://{V1_UUID[:12]}"
    _inventory(monkeypatch, cli={v1_url: "continuation-fingerprint"})
    writer = _writer(monkeypatch)
    result = chats.capture(ide=False)
    assert writer.call_count == 0 and result["kiro_cli_chat"]["failed"] == 1
