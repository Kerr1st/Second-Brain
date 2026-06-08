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

logger = logging.getLogger(__name__)

# Intentional-interest playlists -> engagement-tier label stored on each memory.
PLAYLISTS = {
    "LL": "liked",
    "WL": "watch_later",
}
MIN_DURATION_DEFAULT = 300  # seconds; skip shorts/clips
IMPERSONATE = "chrome"
BROWSER = "chrome"


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
    if not entries and proc.returncode != 0:
        logger.warning("Enumeration failed for %s: %s", playlist_id, (proc.stderr or "").strip()[:300])
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
    """Fetch ONE English transcript track (manual preferred, else auto). None if absent."""
    with tempfile.TemporaryDirectory() as tmp:
        # NOTE: no --cookies-from-browser here. Subtitles are public, and passing
        # cookies makes yt-dlp use the authenticated web_creator/tv player client,
        # which (without the deno JS-challenge solver) returns "Requested format is
        # not available". The anonymous android_vr client still serves subtitles.
        _ytdlp([
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
            return None
        vtts.sort(key=lambda f: (".en.vtt" not in f, len(f)))  # prefer plain ".en.vtt"
        with open(os.path.join(tmp, vtts[0]), encoding="utf-8", errors="replace") as fh:
            text = vtt_to_text(fh.read())
        return text or None


def build_markdown(entry: dict, engagement: str, transcript: str) -> str:
    """Build a metadata-headed markdown doc for ingest_content."""
    return "\n".join([
        f"# {entry['title']}",
        "",
        f"Source: {entry['url']}",
        "Type: youtube-transcript",
        f"Video ID: {entry['id']}",
        f"Channel: {entry['channel']}",
        f"Duration: {entry.get('duration') or ''}",
        f"Engagement: {engagement}",
        "",
        "---",
        "",
        transcript,
    ])


def capture(playlists=None, min_duration: int = MIN_DURATION_DEFAULT,
            limit: int | None = None, dry_run: bool = False) -> dict:
    """Enumerate playlists, fetch transcripts for NEW videos, ingest them."""
    playlists = playlists or list(PLAYLISTS)
    stats = {"processed": 0, "skipped": 0, "failed": 0, "no_transcript": 0}

    if not is_reachable():
        logger.error("Database unreachable; aborting.")
        return stats

    already = get_processed_source_urls("youtube")

    for pid in playlists:
        engagement = PLAYLISTS.get(pid, pid)
        entries = list_playlist(pid)
        logger.info("Playlist %s (%s): %d videos enumerated", pid, engagement, len(entries))
        for e in entries:
            if limit is not None and stats["processed"] >= limit:
                break
            if e["url"] in already:
                stats["skipped"] += 1
                continue
            dur = e.get("duration")
            if dur is not None and dur < min_duration:
                stats["skipped"] += 1
                continue
            if dry_run:
                logger.info("[dry-run] would capture: %s (%s)", e["title"][:70], engagement)
                stats["processed"] += 1
                continue
            try:
                transcript = fetch_transcript(e["url"])
            except Exception:
                logger.exception("Fetch failed: %s", e["url"])
                stats["failed"] += 1
                continue
            if not transcript:
                stats["no_transcript"] += 1
                continue
            try:
                ingest_content(build_markdown(e, engagement, transcript),
                               source_type="youtube", source_url=e["url"])
                already.add(e["url"])
                stats["processed"] += 1
                logger.info("Captured: %s (%s)", e["title"][:70], engagement)
            except Exception:
                logger.exception("Ingest failed: %s", e["url"])
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
    capture(playlists=args.playlists, min_duration=args.min_duration,
            limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
