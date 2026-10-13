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
])
def test_fetch_transcript_permanent_errors_are_no_transcript(stderr, caplog):
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(1, "noise\n" * 200 + stderr)):
        assert youtube.fetch_transcript(_URL) is None


def test_fetch_transcript_subtitle_track_429_raises_subtitle_rate_limited_real_stderr():
    # Verbatim tail of a nightly failure (szDmH_8yQdk): metadata extracts fine,
    # only the subtitle track request gets the 429.
    stderr = (
        "[youtube] szDmH_8yQdk: Downloading m3u8 information\n"
        "[info] szDmH_8yQdk: Downloading subtitles: en\n"
        "[info] szDmH_8yQdk: Downloading 1 format(s): 137+251\n"
        "[info] Writing video subtitles to: /tmp/x/szDmH_8yQdk.en.vtt\n"
        "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests\n"
    )
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(1, "noise\n" * 200 + stderr)):
        with pytest.raises(youtube.SubtitleRateLimited):
            youtube.fetch_transcript(_URL)
    assert not issubclass(youtube.SubtitleRateLimited, (OSError, RuntimeError, ValueError))


@pytest.mark.parametrize("stderr", [
    "ERROR: [youtube] vidX: Unable to download webpage: HTTP Error 429: Too Many Requests",
    "ERROR: [youtube] vidX: Unable to download player API JSON: HTTP Error 429: Too Many Requests",
    "ERROR: [youtube:tab] LL: Unable to download API page: HTTP Error 429: Too Many Requests",
    "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
    "HTTP Error 429: Too Many Requests",
])
def test_fetch_transcript_non_subtitle_429_still_raises(stderr):
    with patch.object(youtube, "_ytdlp", _fake_ytdlp(1, stderr)):
        with pytest.raises(RuntimeError):
            youtube.fetch_transcript(_URL)


def test_capture_counts_subtitle_429_as_no_transcript_but_page_429_as_failed(monkeypatch):
    monkeypatch.setattr(youtube, "is_reachable", lambda: True)
    monkeypatch.setattr(youtube, "get_processed_source_urls", lambda st: set())
    monkeypatch.setattr(youtube, "list_playlist", lambda pid: [
        {"id": "vidX", "title": "T", "url": _URL, "duration": 900, "channel": "C"}])
    monkeypatch.setattr(youtube, "_ytdlp", _fake_ytdlp(
        1, "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"))
    stats = youtube.capture(playlists=["LL"])
    assert stats["failed"] == 0 and stats["no_transcript"] == 1
    monkeypatch.setattr(youtube, "_ytdlp", _fake_ytdlp(
        1, "ERROR: [youtube] vidX: Unable to download webpage: HTTP Error 429: Too Many Requests"))
    stats = youtube.capture(playlists=["LL"])
    assert stats["failed"] == 1 and stats["no_transcript"] == 0


@pytest.mark.parametrize("stderr", [
    "ERROR: [youtube] vidX: Unable to download webpage: HTTP Error 429: Too Many Requests",
    "ERROR: [youtube] vidX: HTTP Error 503: Service Unavailable",
    "ERROR: [youtube] vidX: Unable to download API page: <urlopen error [Errno 8] nodename nor servname provided>",
    "ERROR: [youtube] vidX: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "ERROR: [youtube] vidX: Requested format is not available. Use --list-formats",
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


# --------------------------------------------------------------------------- #
# Silent-zero guard: an IP-wide timedtext block gives every video a subtitle   #
# 429. That must fail the run, while the chronic few must not (review B1).     #
# --------------------------------------------------------------------------- #
_SUB_429 = "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"


def _vid(n):
    return {"id": f"v{n}", "title": f"T{n}", "url": f"https://www.youtube.com/watch?v=v{n}",
            "duration": 900, "channel": "C"}


def _run_capture(monkeypatch, outcomes, already=(), extra_entries=()):
    """outcomes: {video_index: "429" | "none" | "ok" | "fail"} in enumeration order."""
    entries = [_vid(n) for n in outcomes] + list(extra_entries)
    monkeypatch.setattr(youtube, "is_reachable", lambda: True)
    monkeypatch.setattr(youtube, "get_processed_source_urls", lambda st: set(already))
    monkeypatch.setattr(youtube, "list_playlist", lambda pid: entries)
    monkeypatch.setattr(youtube, "run_channel",
                        lambda name, items, ingest: {"processed": len(items), "skipped": 0, "failed": 0})
    by_url = {_vid(n)["url"]: kind for n, kind in outcomes.items()}
    stderr_for = {"429": _SUB_429, "none": "ERROR: [youtube] vX: Private video",
                  "fail": "ERROR: [youtube] vX: HTTP Error 503: Service Unavailable"}

    def ytdlp(args, timeout=180):
        # Real fetch_transcript runs; only the yt-dlp subprocess is faked per URL.
        kind = by_url[args[-1]]
        if kind == "ok":
            return _fake_ytdlp(0, write_vtt=True)(args, timeout)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=stderr_for[kind])

    monkeypatch.setattr(youtube, "_ytdlp", ytdlp)
    return youtube.capture(playlists=["LL"])


def test_subtitle_429_on_two_fetched_videos_only_is_below_threshold(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=youtube.logger.name):
        stats = _run_capture(monkeypatch, {1: "429", 2: "429"})
    assert stats["subtitle_429"] == 2 and stats["no_transcript"] == 2
    assert stats["failed"] == 0 and youtube.exit_code(stats) == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "429" in r.getMessage()]
    assert len(warnings) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_subtitle_429_on_every_one_of_three_fetched_videos_fails(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=youtube.logger.name):
        stats = _run_capture(monkeypatch, {1: "429", 2: "429", 3: "429"})
    assert stats["subtitle_429"] == 3 and stats["no_transcript"] == 3
    assert stats["processed"] == 0 and stats["failed"] == 1 and youtube.exit_code(stats) == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("rate-limited for every candidate" in m for m in errors)


def test_skipped_videos_do_not_count_as_attempted(monkeypatch):
    # 3 fetched, all subtitle 429; 2 already processed and 1 short video are
    # skipped and must not dilute "every candidate" -> still failed.
    old = [_vid(10), _vid(11)]
    short = dict(_vid(12), duration=30)
    stats = _run_capture(monkeypatch, {1: "429", 2: "429", 3: "429"},
                         already={v["url"] for v in old}, extra_entries=old + [short])
    assert stats["skipped"] == 3 and stats["failed"] == 1


def test_subtitle_429s_plus_one_success_exits_zero(monkeypatch):
    stats = _run_capture(monkeypatch, {1: "429", 2: "429", 3: "429", 4: "429", 5: "ok"})
    assert stats["subtitle_429"] == 4 and stats["processed"] == 1
    assert stats["failed"] == 0 and youtube.exit_code(stats) == 0


def test_subtitle_429s_plus_one_permanent_no_transcript_exits_zero(monkeypatch):
    # Not every fetched video got the 429 -> not an endpoint-wide block.
    stats = _run_capture(monkeypatch, {1: "429", 2: "429", 3: "429", 4: "none"})
    assert stats["subtitle_429"] == 3 and stats["no_transcript"] == 4
    assert stats["failed"] == 0 and youtube.exit_code(stats) == 0


def test_todays_nightly_five_chronic_429s_among_57_attempted_exits_zero(monkeypatch):
    # 2026-10-13 production shape: ~57 fetched, the same 5 chronic videos get
    # the subtitle 429, the other 52 are permanent no_transcript (private,
    # removed, no captions ...), nothing new processed. Must still exit 0.
    outcomes = {n: ("429" if n < 5 else "none") for n in range(57)}
    stats = _run_capture(monkeypatch, outcomes)
    assert stats["subtitle_429"] == 5 and stats["no_transcript"] == 57
    assert stats["processed"] == 0 and stats["failed"] == 0
    assert youtube.exit_code(stats) == 0


def test_dry_run_never_trips_the_subtitle_429_guard(monkeypatch):
    monkeypatch.setattr(youtube, "is_reachable", lambda: True)
    monkeypatch.setattr(youtube, "get_processed_source_urls", lambda st: set())
    monkeypatch.setattr(youtube, "list_playlist", lambda pid: [_vid(n) for n in range(5)])
    stats = youtube.capture(playlists=["LL"], dry_run=True)
    assert stats["planned"] == 5 and stats["failed"] == 0
