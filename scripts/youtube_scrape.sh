#!/bin/bash
# Nightly YouTube watch history scrape via Crawlee.
# Runs at 1:30 AM via launchd, before the chat extraction pipeline.
#
# Scrapes YouTube watch history via browser cookies, filtering to videos
# ≥5 minutes (likely watched to completion, not shorts/clips).
# Crawlee writes transcripts to knowledge/sources/ with metadata headers
# and registers them in knowledge/index.md. The Second Brain ingestion
# pipeline picks them up from there.

set -uo pipefail

CRAWLEE_DIR="/Users/kerrfir/Work/Tools/Crawlee"
LOGFILE="/Users/kerrfir/second-brain/logs/youtube-$(date +%Y%m%d).log"
MIN_DURATION=300  # 5 minutes in seconds

mkdir -p "$(dirname "$LOGFILE")"
log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOGFILE"; }

# Pre-flight: check yt-dlp is available
if ! command -v yt-dlp &>/dev/null; then
  log "ABORT: yt-dlp not found. Install with: brew install yt-dlp"
  exit 1
fi

cd "$CRAWLEE_DIR"

log "=== YouTube Watch History Scrape (min ${MIN_DURATION}s) ==="

# Get watch history video URLs, filtering to ≥ MIN_DURATION.
# --no-flat-playlist fetches metadata so --match-filter can check duration.
VIDEOS=$(yt-dlp --print url --match-filter "duration>=${MIN_DURATION}" \
  --cookies-from-browser chrome --no-download \
  "https://www.youtube.com/playlist?list=HL" 2>>"$LOGFILE") || true

if [ -z "$VIDEOS" ]; then
  log "No videos matched filter. Done."
  exit 0
fi

PROCESSED=0
SKIPPED=0
FAILED=0

while IFS= read -r VIDEO_URL; do
  [ -z "$VIDEO_URL" ] && continue

  log "Processing: $VIDEO_URL"
  node --input-type=module -e "
    const yt = await import('./youtube.js');
    const ks = await import('./knowledge-store.js');
    const md = yt.extractTranscript('$VIDEO_URL', { lang: 'en' });
    if (md.includes('No transcript available')) {
      process.stderr.write('No subtitles\\n');
      process.exit(2);
    }
    ks.ensureKnowledgeStore('$CRAWLEE_DIR/knowledge');
    const title = (md.match(/^# (.+)$/m) || [])[1] || 'Untitled';
    const videoId = (md.match(/^Video ID: (.+)$/m) || [])[1]?.trim() || '';
    const filename = yt.youtubeFilename(title, videoId);
    ks.writeSource('$CRAWLEE_DIR/knowledge', filename, md);
    const date = new Date().toISOString().split('T')[0];
    const entry = { title, url: '$VIDEO_URL', type: 'youtube-transcript', filename: filename.replace('.md', ''), date };
    ks.updateIndex('$CRAWLEE_DIR/knowledge', entry);
    ks.updateTypeIndex('$CRAWLEE_DIR/knowledge', 'youtube-transcript', entry);
  " >> "$LOGFILE" 2>&1

  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 0 ]; then
    PROCESSED=$((PROCESSED + 1))
  elif [ $EXIT_CODE -eq 2 ]; then
    SKIPPED=$((SKIPPED + 1))
  else
    FAILED=$((FAILED + 1))
  fi
done <<< "$VIDEOS"

log "=== Done: processed=$PROCESSED skipped=$SKIPPED failed=$FAILED ==="
