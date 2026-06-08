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
    assert "Video ID: abc123" in md
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
