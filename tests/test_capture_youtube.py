"""Tests for the YouTube capture source (pure logic + mocked enumeration)."""

import json
import subprocess
from unittest.mock import patch

from src.capture import youtube


def test_vtt_to_text_strips_cues_tags_and_dups():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
Hello there
Hello there

00:00:03.000 --> 00:00:05.000
You know <00:00:04.000><c>the</c> rules
"""
    out = youtube.vtt_to_text(vtt)
    assert "-->" not in out
    assert "WEBVTT" not in out
    assert "<c>" not in out and "<00:" not in out
    assert out.count("Hello there") == 1          # consecutive duplicate collapsed
    assert "You know the rules" in out             # inline tags stripped


def test_build_markdown_has_metadata_header():
    md = youtube.build_markdown(
        {"id": "abc123", "title": "My Vid",
         "url": "https://www.youtube.com/watch?v=abc123",
         "channel": "Chan", "duration": 600},
        "liked", "transcript body")
    assert md.startswith("# My Vid")
    assert "Source: https://www.youtube.com/watch?v=abc123" in md
    assert "Engagement: liked" in md
    assert "Video_ID: abc123" in md
    assert md.strip().endswith("transcript body")


def _fake_proc(stdout, rc=0):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


def test_list_playlist_skips_deleted_and_builds_canonical_url():
    lines = "\n".join([
        json.dumps({"id": "vid1", "title": "Real Video", "duration": 600}),
        json.dumps({"id": "vid2", "title": "[Deleted video]", "duration": None}),
        json.dumps({"id": "vid3", "title": "[Private video]"}),
        json.dumps({"id": "vid4", "title": "Another", "channel": "C"}),
    ])
    with patch.object(youtube, "_ytdlp", return_value=_fake_proc(lines)):
        entries = youtube.list_playlist("LL")
    assert [e["id"] for e in entries] == ["vid1", "vid4"]        # deleted/private skipped
    assert entries[0]["url"] == "https://www.youtube.com/watch?v=vid1"  # canonical url


# --------------------------------------------------------------------------- #
# fetch_transcript exit-code policy (2026-10-10: permanent no-transcript       #
# videos were counted failed nightly and retried forever)                      #
# --------------------------------------------------------------------------- #
import logging
import os

import pytest

_URL = "https://www.youtube.com/watch?v=vidX"
_VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello world\n"


def _fake_ytdlp(rc, stderr="", write_vtt=False):
    def run(args, timeout=180):
        if write_vtt:
            out_dir = os.path.dirname(args[args.index("-o") + 1])
            with open(os.path.join(out_dir, "vidX.en.vtt"), "w") as fh:
                fh.write(_VTT)
        return subprocess.CompletedProcess(args, rc, stdout="", stderr=stderr)
    return run


def test_fetch_transcript_reads_vtt_even_when_ytdlp_exits_non_zero():
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(1, "ERROR: one track failed", write_vtt=True)):
        assert youtube.fetch_transcript(_URL) == "hello world"


def test_fetch_transcript_clean_exit_without_vtt_is_no_transcript():
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(0)):
        assert youtube.fetch_transcript(_URL) is None


@pytest.mark.parametrize("stderr", [
    "ERROR: [youtube] vidX: Video unavailable. This video has been removed by the uploader",
    "ERROR: [youtube] vidX: Private video. Sign in if you've been granted access to this video",
    "ERROR: [youtube] vidX: Sign in to confirm your age. This video may be inappropriate for some users.",
    "ERROR: [youtube] vidX: Join this channel to get access to members-only content like this video",
    "WARNING: There are no subtitles for the requested languages",
    "ERROR: [youtube] vidX: Requested format is not available. Use --list-formats",
])
def test_fetch_transcript_permanent_errors_are_no_transcript(stderr, caplog):
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(1, "noise\n" * 200 + stderr)):
        assert youtube.fetch_transcript(_URL) is None


@pytest.mark.parametrize("stderr", [
    "ERROR: [youtube] vidX: Unable to download webpage: HTTP Error 429: Too Many Requests",
    "ERROR: [youtube] vidX: HTTP Error 503: Service Unavailable",
    "ERROR: [youtube] vidX: Unable to download API page: <urlopen error [Errno 8] nodename nor servname provided>",
    "ERROR: [youtube] vidX: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "timeout",
    "something nobody has seen before",
])
def test_fetch_transcript_transient_or_unknown_errors_raise_and_log_stderr_tail(stderr, caplog):
    long_stderr = "x" * 1000 + stderr
    with caplog.at_level(logging.WARNING, logger=youtube.logger.name), \
            patch.object(youtube, "_ytdlp", _fake_ytdlp(1, long_stderr)):
        with pytest.raises(RuntimeError):
            youtube.fetch_transcript(_URL)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert stderr[-60:] in logged
    assert "x" * 400 not in logged                     # only the bounded tail is logged


def test_capture_counts_permanent_errors_as_no_transcript_not_failed(monkeypatch):
    monkeypatch.setattr(youtube, "is_reachable", lambda: True)
    monkeypatch.setattr(youtube, "get_processed_source_urls", lambda st: set())
    monkeypatch.setattr(youtube, "list_playlist", lambda pid: [
        {"id": "vidX", "title": "T", "url": _URL, "duration": 900, "channel": "C"}])
    monkeypatch.setattr(youtube, "_ytdlp", _fake_ytdlp(1, "ERROR: Video unavailable"))
    stats = youtube.capture(playlists=["LL"])
    assert stats["failed"] == 0 and stats["no_transcript"] == 1
    monkeypatch.setattr(youtube, "_ytdlp", _fake_ytdlp(1, "HTTP Error 429: Too Many Requests"))
    stats = youtube.capture(playlists=["LL"])
    assert stats["failed"] == 1 and stats["no_transcript"] == 0
