#!/bin/bash
# YouTube capture — self-contained yt-dlp + curl_cffi path.
#
# Enumerates the user's Liked + Watch Later playlists (cookies) and ingests
# English transcripts (anonymous fetch + browser impersonation) via
# src.capture.youtube. Replaces the former Crawlee/Node scrape (see git history).
# Invoked nightly by launchd through job_wrapper.sh; args pass through for
# manual runs, e.g.:  scripts/jobs/youtube_scrape.sh --dry-run --limit 5
set -uo pipefail
cd "$(dirname "$0")/../.."          # repo root, so `-m src.capture.youtube` resolves
exec .venv/bin/python -m src.capture.youtube "$@"
