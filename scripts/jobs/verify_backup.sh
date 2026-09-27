#!/bin/bash
# Verify the newest encrypted bundle, including a full restore in a disposable cluster.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REMOTE="${SECOND_BRAIN_BACKUP_REMOTE:-gdrive:memory-bank-backups}"
AREA="$(mktemp -d "${TMPDIR:-/tmp}/sb-cloud-verify.XXXXXX")" || exit 1
trap 'rm -rf "$AREA"' EXIT
LIST="$AREA/list"
if ! rclone lsf "$REMOTE" --files-only --include 'second-brain_*.tar.gpg' > "$LIST"; then exit 1; fi
LATEST="$(sort "$LIST" | tail -1)"
if [[ -z "$LATEST" || "$LATEST" == */* || "$LATEST" != second-brain_*.tar.gpg ]]; then
  echo 'No valid encrypted bundle found'; exit 1
fi
if ! rclone copyto "$REMOTE/$LATEST" "$AREA/backup.tar.gpg"; then exit 1; fi
"$ROOT/.venv/bin/python" "$ROOT/scripts/maintenance/backup_bundle.py" verify "$AREA/backup.tar.gpg" --restore "$@"
