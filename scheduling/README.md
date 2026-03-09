# Scheduling — macOS launchd plists
#
# To install, symlink into ~/Library/LaunchAgents/:
#   ln -s ~/second-brain/scheduling/com.second-brain.backup.plist ~/Library/LaunchAgents/
#   ln -s ~/second-brain/scheduling/com.second-brain.chat-extract.plist ~/Library/LaunchAgents/
#   ln -s ~/second-brain/scheduling/com.second-brain.ingest.plist ~/Library/LaunchAgents/
#   ln -s ~/second-brain/scheduling/com.second-brain.verify.plist ~/Library/LaunchAgents/
#
# Then load:
#   launchctl load ~/Library/LaunchAgents/com.second-brain.*.plist
#
# Schedule:
#   2:00 AM  — backup (pg_dump + rclone)
#   2:30 AM  — chat extraction (Phase 1)
#   3:00 AM  — Crawlee + staging ingestion (Phase 2)
#   Sunday 3:00 AM — backup verification
