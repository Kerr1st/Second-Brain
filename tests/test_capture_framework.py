"""Synthetic capture parity and failure contracts. No private source access."""
import json
from datetime import date, datetime
from unittest.mock import Mock, patch

import pytest

from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.capture.render import render_document, html_to_text
from src.capture.window import day_bounds_utc
from src.parsers import kiro_sessions


def test_pipeline_dedups_validates_and_preserves_fetch_failures():
    writer = Mock(return_value={"processed": 1, "skipped": 0, "failed": 0})
    batch = CaptureBatch([("slack://c/day", "body"), ("slack://c/day", "body"), ("", "body")], failed=2)
    stats = run_channel("slack", batch, ingest=writer)
    assert stats == {"processed": 1, "skipped": 1, "failed": 3}
    assert writer.call_args.args == ([("slack://c/day", "body")], "slack")
    assert exit_code({"slack": stats}) == 1


def test_dry_run_has_planned_counts_and_no_writer():
    writer = Mock(side_effect=AssertionError("must not store"))
    stats = run_channel("slack", [("s://1", "hello")], dry_run=True, ingest=writer)
    assert stats["processed"] == 0 and stats["planned"] == 1
    writer.assert_not_called()


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8),23), (date(2026,11,1),25)])
def test_local_calendar_window_respects_dst(day, hours, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_TIMEZONE", "America/Los_Angeles")
    start, end = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in day_bounds_utc(day))
    assert (end-start).total_seconds() == hours * 3600


def test_headers_cannot_inject_a_body_or_another_metadata_field():
    md = render_document("title\nspoof", {"Video ID": "x\n\n---\nSecret: value", "Thread-replies": 2}, "real body")
    assert "Video_ID: x  --- Secret: value" in md
    assert "Thread_replies: 2" in md
    assert md.count("\n---\n") == 1
    assert html_to_text('<head>secret</head><p>A &amp; B</p><script>bad</script><p>C</p>') == 'A & B\nC'


def native_session(tmp_path, *, session_id="stable-id"):
    root = tmp_path / "workspace" / "session"
    root.mkdir(parents=True)
    meta = root / "session.json"
    meta.write_text(json.dumps({"id":session_id,"schemaVersion":"1","title":"Synthetic decision", "modelId":"model",
                                "createdAt":"2026-09-01T00:00:00Z","lastModifiedAt":"2026-09-02T00:00:00Z", "workspacePaths":["/synthetic/project"]}))
    records = [
        {"id":"u1", "payload":{"type":"user", "content":"How can this synthetic project use the result?"}},
        {"id":"r1", "payload":{"type":"assistant", "operationType":"Reasoning", "content":"PRIVATE_REASONING"}},
        {"id":"a1", "payload":{"type":"assistant", "operationType":"Say", "content":"explanation " * 55}},
        {"id":"u2", "payload":{"type":"user", "content":"Use that evidence in the next milestone."}},
        {"id":"t1", "payload":{"type":"tool_result", "content":"SECRET_TOOL"}},
    ]
    (root / "messages.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return meta, records


def test_native_ide_visible_content_stable_id_and_message_dedup(tmp_path):
    path, records = native_session(tmp_path)
    with path.with_name("messages.jsonl").open("a") as handle:
        handle.write(json.dumps(records[2]) + "\n")
    url, md, fields = kiro_sessions.parse_session(path)
    assert url == "kiro-ide://session/stable-id"
    assert md.count("**Assistant:**") == 1
    assert "PRIVATE_REASONING" not in md and "SECRET_TOOL" not in md
    assert fields["Project"] == "project"
    assert kiro_sessions.find_sessions(tmp_path) == [path]


def test_native_partial_record_fails_instead_of_storing_truncated_session(tmp_path):
    path, _ = native_session(tmp_path)
    with path.with_name("messages.jsonl").open("a") as handle:
        handle.write('{"id":')
    with pytest.raises(ValueError):
        kiro_sessions.parse_session(path)


def test_native_appended_session_keeps_identity(tmp_path):
    path, _ = native_session(tmp_path)
    before = kiro_sessions.parse_session(path)
    with path.with_name("messages.jsonl").open("a") as handle:
        handle.write(json.dumps({"id":"a2","payload":{"type":"assistant","operationType":"Say","content":"New visible outcome"}}) + "\n")
    after = kiro_sessions.parse_session(path)
    assert before[0] == after[0]
    assert before[1] != after[1] and "New visible outcome" in after[1]


def test_youtube_failure_not_empty_success():
    import subprocess
    from src.capture import youtube
    with patch.object(youtube, "_ytdlp", return_value=subprocess.CompletedProcess([],1,"", "no credentials")):
        with pytest.raises(RuntimeError):
            youtube.list_playlist("LL")
        with pytest.raises(RuntimeError):
            youtube.fetch_transcript("https://youtube.com/watch?v=test")


def test_youtube_dry_run_counts_planned_and_honors_limit():
    from src.capture import youtube
    entries = [{"id":str(n),"url":f"https://youtube.com/watch?v={n}","title":"synthetic", "duration":500} for n in range(5)]
    with patch.object(youtube,"is_reachable",return_value=True), patch.object(youtube,"get_processed_source_urls",return_value=set()), patch.object(youtube,"list_playlist",return_value=entries), patch.object(youtube,"fetch_transcript",side_effect=AssertionError("no fetch")):
        result = youtube.capture(playlists=["LL"],dry_run=True,limit=2)
    assert result["processed"] == 0 and result["planned"] == 2


def patch_inventory(monkeypatch, known=(), fingerprints=None):
    """Stand in for the channel's DB inventory (URLs + stored fingerprints)."""
    import src.db
    monkeypatch.setattr(src.db, 'get_processed_source_urls', lambda _: set(known))
    monkeypatch.setattr(src.db, 'get_capture_fingerprints', lambda _: dict(fingerprints or {}))


def test_native_pipeline_routes_refresh_and_dry_run_is_read_only(tmp_path, monkeypatch):
    from src.capture import chats
    import src.ingest
    path, _ = native_session(tmp_path)
    monkeypatch.setattr(chats.ide_chat, 'find_chat_files', lambda: [])
    monkeypatch.setattr(chats.kiro_sessions, 'find_sessions', lambda: [path])
    patch_inventory(monkeypatch)
    writer = Mock(return_value='new-id')
    monkeypatch.setattr(src.ingest, 'ingest_content', writer)
    planned = chats.capture(dry_run=True, cli=False)
    assert planned['kiro_ide_chat']['planned'] == 1
    writer.assert_not_called()
    actual = chats.capture(cli=False)
    assert actual['kiro_ide_chat']['processed'] == 1
    assert writer.call_args.kwargs['refresh'] is True
    assert writer.call_args.kwargs['source_metadata'] == {'source_fingerprint': kiro_sessions.fingerprint(path)}
    assert writer.call_args.args[2] == 'kiro-ide://session/stable-id'


def test_native_parse_failure_produces_failed_capture(tmp_path, monkeypatch):
    from src.capture import chats
    path, _ = native_session(tmp_path)
    path.write_text('not json')
    monkeypatch.setattr(chats.ide_chat,'find_chat_files',lambda: [])
    monkeypatch.setattr(chats.kiro_sessions,'find_sessions',lambda: [path])
    patch_inventory(monkeypatch)
    result = chats.capture(dry_run=True,cli=False)
    assert result['kiro_ide_chat']['failed'] == 1 and exit_code(result) == 1


def test_capture_outcomes_carry_per_item_fetch_failures():
    from src.capture.common import CaptureBatch
    writer=Mock(return_value={'processed':1,'failed':0,'skipped':0})
    results=run_channel('outlook_email',CaptureBatch([('outlook://valid','body')],failed=1),ingest=writer)
    assert results['processed'] == 1 and results['failed'] == 1 and exit_code(results) == 1


def test_one_crawlee_index_reader_handles_shifted_title_columns(tmp_path):
    from src.capture.filters import load_indexed_urls
    index=tmp_path/'index.md'
    index.write_text('| Title | Source | Type | Summary | Date |\n| a | b | [source](https://example.com/article) | web-page | sum | 2026-09-26 |\n')
    assert load_indexed_urls(index) == {'https://example.com/article'}


def test_native_ide_reuses_legacy_identity_when_session_migrated(tmp_path, monkeypatch):
    from src.capture import chats
    import src.ingest
    path,_=native_session(tmp_path)
    monkeypatch.setattr(chats.ide_chat,'find_chat_files',lambda: [])
    monkeypatch.setattr(chats.kiro_sessions,'find_sessions',lambda: [path,path])
    patch_inventory(monkeypatch, known={'chat://stable-id'})
    writer=Mock(return_value='id')
    monkeypatch.setattr(src.ingest,'ingest_content',writer)
    result = chats.capture(cli=False)
    assert writer.call_count == 1 and result['kiro_ide_chat']['processed'] == 1
    assert writer.call_args.args[1:3] == ('kiro_ide_chat', 'chat://stable-id')
