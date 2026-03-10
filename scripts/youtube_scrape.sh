#!/bin/bash
# Nightly YouTube watch history scrape via Crawlee.
# Runs at 1:30 AM via launchd, before the chat extraction pipeline.
#
# Two modes:
#   1. Playlist mode (default): scrapes a curated "Second Brain Queue" playlist
#   2. History mode (--history): scrapes full YouTube watch history via browser cookies
#
# Crawlee writes transcripts to knowledge/sources/ with metadata headers
# and registers them in knowledge/index.md. The Second Brain ingestion
# pipeline picks them up from there.

set -uo pipefail

CRAWLEE_DIR="/Users/kerrfir/Work/Tools/Crawlee"
LOGFILE="/Users/kerrfir/second-brain/logs/youtube-$(date +%Y%m%d).log"
MODE="${1:-playlist}"

mkdir -p "$(dirname "$LOGFILE")"
log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOGFILE"; }

# Pre-flight: check yt-dlp is available
if ! command -v yt-dlp &>/dev/null; then
  log "ABORT: yt-dlp not found. Install with: brew install yt-dlp"
  exit 1
fi

cd "$CRAWLEE_DIR"

if [ "$MODE" = "history" ]; then
  # Full watch history via browser cookies
  # Note: requires Chrome to have an active YouTube session
  log "=== YouTube Watch History Scrape ==="
  PLAYLIST_URL="https://www.youtube.com/playlist?list=HL"
  node -e "
    import('./youtube.js').then(yt => {
      yt.extractPlaylist('$PLAYLIST_URL', {
        output: '$CRAWLEE_DIR/knowledge',
        lang: 'en',
        maxVideos: 20,
        ytDlpArgs: ['--cookies-from-browser', 'chrome']
      });
    });
  " >> "$LOGFILE" 2>&1
else
  # Curated playlist mode — set YOUTUBE_PLAYLIST_URL in environment or here
  PLAYLIST_URL="${YOUTUBE_PLAYLIST_URL:-}"
  if [ -z "$PLAYLIST_URL" ]; then
    log "SKIP: No YOUTUBE_PLAYLIST_URL set. Create a playlist and set the env var."
    exit 0
  fi
  log "=== YouTube Playlist Scrape: $PLAYLIST_URL ==="
  node -e "
    import('./youtube.js').then(yt => {
      yt.extractPlaylist('$PLAYLIST_URL', {
        output: '$CRAWLEE_DIR/knowledge',
        lang: 'en'
      });
    });
  " >> "$LOGFILE" 2>&1
fi

EXIT_CODE=$?
log "=== Done (exit code $EXIT_CODE) ==="
