"""YouTube capture — self-contained transcript ingestion.

Replaces the external Crawlee/Node path. Uses yt-dlp (+ curl_cffi browser
impersonation) to enumerate the user's intentional-interest playlists
(Liked = ``LL``, Watch Later = ``WL``) and fetch English transcripts, then
feeds them through the standard pipeline (``src.ingest.ingest_content``) as
``source_type="youtube"``.

Engagement signal: each memory records which playlist it came from
(``Engagement:`` header -> metadata) so later phases can weight by interest.

Requires (venv): yt-dlp==2026.3.17, curl_cffi==0.13.0.
Run: python -m src.capture.youtube [--limit N] [--dry-run] [--min-duration 300]

Validated recipe (see docs / session notes): one subtitle track + a request
delay avoids HTTP 429; --skip-download sidesteps the deno/EJS n-challenge.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile

from src.db import get_processed_source_urls, is_reachable
from src.ingest import ingest_content

from src.capture.common import exit_code
from src.capture.framework import run_channel
from src.capture.render import render_document

logger = logging.getLogger(__name__)

# Playlists whose last enumeration returned entries but a non-zero yt-dlp exit.
last_enumeration_partial: set = set()

# Intentional-interest playlists -> engagement-tier label stored on each memory.
PLAYLISTS = {
    "LL": "liked",
    "WL": "watch_later",
}
MIN_DURATION_DEFAULT = 300  # seconds; skip shorts/clips
IMPERSONATE = "chrome"
BROWSER = "chrome"


STDERR_TAIL_CHARS = 300
# yt-dlp errors that mean "this video will never have a fetchable transcript".
# Network errors, HTTP 5xx, timeouts, the "not a bot" sign-in check (an IP/rate
# block) and anything unknown are NOT here: they stay failures so the video is
# retried.
# HTTP 429 is split by WHERE it happens. A 429 on the webpage, player/API or
# playlist request is a real IP rate limit and stays a failure, so the job exits
# non-zero. A 429 on the subtitle track itself ("Unable to download video
# subtitles for 'en': HTTP Error 429") is per-track: the same few videos get it
# every night, at any hour, while fetches seconds before and after succeed, and
# a newer yt-dlp (nightly 2026.10.10) gets the same 429 (probed 2026-10-13).
# It is treated as no_transcript. Nothing is persisted, so the video is still
# retried each night and is picked up if YouTube starts serving the track.
# fetch_transcript raises SubtitleRateLimited for it (not None) so capture can
# count it separately: YouTube rate-limits the timedtext endpoint apart from
# watch/player, and an IP-wide timedtext block would give EVERY video this error.
# capture turns "every fetched video got it" (and at least
# SUBTITLE_429_ALL_FAILED_MIN of them) into a failure instead of a silent zero.
_SUBTITLE_429_RE = re.compile(r"unable to download video subtitles for '[^']*': http error 429", re.I)
SUBTITLE_429_ALL_FAILED_MIN = 3  # the known chronic few must not trip it


class SubtitleRateLimited(Exception):
    """The subtitle (timedtext) request got HTTP 429 after the page/player succeeded.

    Deliberately NOT a RuntimeError: callers count it as no_transcript, not failed.
    """


_PERMANENT_NO_TRANSCRIPT_RE = re.compile(
    r"video unavailable|this video is unavailable|private video|video is private|"
    r"(has been|was) removed|video has been terminated|account associated with this video|"
    r"age[- ]restricted|confirm your age|inappropriate for some users|"
    r"members[- ]only|join this channel|sign in to confirm (?!you.?re not a bot)|"
    r"no subtitles|there are no subtitles|no (automatic )?captions", re.I)
# "Requested format is not available" is deliberately NOT permanent: it comes from
# the yt-dlp player client / JS-challenge setup (see fetch_transcript), and is the
# usual symptom when an extractor change breaks every video at once. It stays a
# failure so a total yt-dlp outage exits non-zero instead of a quiet no_transcript.


def _stderr_tail(stderr: str | None, limit: int = STDERR_TAIL_CHARS) -> str:
    return re.sub(r"\s+", " ", (stderr or "")[-limit:]).strip()


def _ytdlp(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """Invoke the venv's yt-dlp as a module (guarantees curl_cffi is on path)."""
    try:
        return subprocess.run(
            [sys.executable, "-m", "yt_dlp", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out after %ss (%s)", timeout, args[-1] if args else "")
        return subprocess.CompletedProcess(args, returncode=124, stdout="", stderr="timeout")


def list_playlist(playlist_id: str) -> list[dict]:
    """Flat-enumerate a playlist. Returns [{id, title, url, duration, channel}]."""
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    proc = _ytdlp([
        "--flat-playlist", "--dump-json",
        "--cookies-from-browser", BROWSER,
        "--impersonate", IMPERSONATE,
        "--no-warnings",
        url,
    ], timeout=300)
    entries: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = e.get("id")
        if not vid:
            continue
        title = e.get("title") or vid
        if title in ("[Deleted video]", "[Private video]", "[Unavailable video]"):
            continue
        # Canonical watch URL from id -> consistent dedup vs. existing records.
        entries.append({
            "id": vid,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": e.get("duration"),
            "channel": e.get("channel") or e.get("uploader") or "",
        })
    if proc.returncode != 0:
        if not entries:
            # Nothing enumerated and yt-dlp unhappy: that is a failed playlist,
            # never an empty success (F-032).
            raise RuntimeError(f"YouTube enumeration failed (exit {proc.returncode})")
        # yt-dlp exits non-zero when *some* entries of a playlist cannot be
        # extracted (private/removed videos) while still listing the rest. Keep
        # what it listed and say so; discarding the whole playlist would make a
        # single dead liked video hide every other new one, nightly (D N-8).
        logger.warning("YouTube enumeration of %s was partial (yt-dlp exit %d); %d entries kept",
                       playlist_id, proc.returncode, len(entries))
        last_enumeration_partial.add(playlist_id)
    return entries


def vtt_to_text(vtt: str) -> str:
    """Convert WEBVTT subtitles to clean transcript text (strip cues/tags/dups)."""
    out: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT":
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE")):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)   # inline <timestamp>/<c> tags
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if out and out[-1] == line:           # collapse consecutive dups (auto-subs)
            continue
        out.append(line)
    return "\n".join(out)


def fetch_transcript(video_url: str) -> str | None:
    """Fetch ONE English transcript track (manual preferred, else auto). None if absent.

    Raises SubtitleRateLimited for a subtitle-track 429 and RuntimeError for
    transient/unknown yt-dlp failures.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # NOTE: no --cookies-from-browser here. Subtitles are public, and passing
        # cookies makes yt-dlp use the authenticated web_creator/tv player client,
        # which (without the deno JS-challenge solver) returns "Requested format is
        # not available". The anonymous android_vr client still serves subtitles.
        proc = _ytdlp([
            "--impersonate", IMPERSONATE,
            "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "--sleep-requests", "1.5",
            "--no-warnings",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            video_url,
        ], timeout=120)
        vtts = [f for f in os.listdir(tmp) if f.endswith(".vtt")]
        if not vtts:
            if proc.returncode == 0:
                return None
            tail = _stderr_tail(proc.stderr)
            if _SUBTITLE_429_RE.search(tail):
                raise SubtitleRateLimited(f"subtitle track HTTP 429 (yt-dlp exit {proc.returncode})")
            if _PERMANENT_NO_TRANSCRIPT_RE.search(tail):
                # The video will never yield a transcript: count it as
                # no_transcript, not as a nightly failure retried forever.
                logger.info("No transcript for %s (yt-dlp exit %d, permanent): %s",
                            video_url, proc.returncode, tail)
                return None
            logger.warning("yt-dlp transcript fetch failed for %s (exit %d): %s",
                           video_url, proc.returncode, tail)
            raise RuntimeError(f"YouTube transcript fetch failed (exit {proc.returncode})")
        # A subtitle file was written: use it even if yt-dlp exited non-zero
        # (e.g. one of several requested tracks failed).
        vtts.sort(key=lambda f: (".en.vtt" not in f, len(f)))  # prefer plain ".en.vtt"
        with open(os.path.join(tmp, vtts[0]), encoding="utf-8", errors="replace") as fh:
            text = vtt_to_text(fh.read())
        return text or None


def build_markdown(entry: dict, engagement: str, transcript: str) -> str:
    """Build a metadata-headed markdown doc for ingest_content."""
    return render_document(entry["title"], {
        "Source": entry["url"], "Type": "youtube-transcript",
        "Video_ID": entry["id"], "Channel": entry["channel"],
        "Duration": entry.get("duration") or "", "Engagement": engagement,
    }, transcript)


def capture(playlists=None, min_duration: int = MIN_DURATION_DEFAULT,
            limit: int | None = None, dry_run: bool = False) -> dict:
    """Enumerate playlists, fetch transcripts for NEW videos, ingest them."""
    playlists = playlists or list(PLAYLISTS)
    stats = {"processed": 0, "skipped": 0, "failed": 0, "no_transcript": 0, "planned": 0,
             "partial_enumeration": 0, "subtitle_429": 0}
    last_enumeration_partial.clear()

    if not is_reachable():
        logger.error("Database unreachable; aborting.")
        stats["failed"] = 1
        return stats

    already = get_processed_source_urls("youtube")

    attempted = 0  # candidates past the skip filters (includes dry-run planned); drives --limit
    # fetched: videos whose transcript fetch_transcript was actually called for
    # this run. Excludes skipped (already processed / too short), dry-run planned
    # and anything past --limit; includes every outcome of the call (transcript,
    # no_transcript, subtitle 429, failed).
    fetched = 0
    for pid in playlists:
        engagement = PLAYLISTS.get(pid, pid)
        try:
            entries = list_playlist(pid)
        except (OSError, RuntimeError, ValueError):
            logger.exception("YouTube enumeration failed")
            stats["failed"] += 1
            continue
        logger.info("Playlist %s (%s): %d videos enumerated", pid, engagement, len(entries))
        if pid in last_enumeration_partial:
            stats["partial_enumeration"] += 1
        for e in entries:
            if limit is not None and attempted >= limit:
                break
            if e["url"] in already:
                stats["skipped"] += 1
                continue
            dur = e.get("duration")
            if dur is not None and dur < min_duration:
                stats["skipped"] += 1
                continue
            attempted += 1
            if dry_run:
                stats["planned"] += 1
                continue
            fetched += 1
            try:
                transcript = fetch_transcript(e["url"])
            except SubtitleRateLimited as exc:
                logger.warning("No transcript for %s (%s); retried next run", e["url"], exc)
                stats["subtitle_429"] += 1
                stats["no_transcript"] += 1
                continue
            except (OSError, RuntimeError, ValueError):
                logger.exception("Fetch failed: %s", e["url"])
                stats["failed"] += 1
                continue
            if not transcript:
                stats["no_transcript"] += 1
                continue
            # The shared pipeline accounts for falsy ingest results and failures.
            def store(items, source_type):
                result = {"processed": 0, "skipped": 0, "failed": 0}
                for url, md in items:
                    try:
                        saved = ingest_content(md, source_type=source_type, source_url=url)
                    except Exception:  # noqa: BLE001 - one video never aborts the channel (R3 S3-8)
                        logger.exception("YouTube ingest failed")
                        result["failed"] += 1
                    else:
                        result["processed" if saved else "skipped"] += 1
                return result
            outcome = run_channel("youtube", [(e["url"], build_markdown(e, engagement, transcript))], ingest=store)
            for key in ("processed", "skipped", "failed"):
                stats[key] += outcome[key]
            if not outcome["failed"]:
                already.add(e["url"])

    if (stats["processed"] == 0 and stats["subtitle_429"] >= SUBTITLE_429_ALL_FAILED_MIN
            and stats["subtitle_429"] == fetched):
        logger.error("YouTube subtitle endpoint rate-limited for every candidate "
                     "(%d of %d fetched videos got a subtitle HTTP 429)", stats["subtitle_429"], fetched)
        stats["failed"] += 1
    logger.info("Done: %s", stats)
    return stats


def main():
    ap = argparse.ArgumentParser(description="Capture YouTube transcripts (Liked + Watch Later).")
    ap.add_argument("--limit", type=int, default=None, help="max NEW videos to ingest this run")
    ap.add_argument("--min-duration", type=int, default=MIN_DURATION_DEFAULT, help="skip videos shorter than N seconds")
    ap.add_argument("--dry-run", action="store_true", help="enumerate + report, but do not fetch/ingest")
    ap.add_argument("--playlists", nargs="*", default=None, help="playlist IDs (default: LL WL)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    stats = capture(playlists=args.playlists, min_duration=args.min_duration,
            limit=args.limit, dry_run=args.dry_run)
    return exit_code(stats)


if __name__ == "__main__":
    sys.exit(main())
