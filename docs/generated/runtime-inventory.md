# Generated runtime inventory

Source-derived configuration, not installed or healthy service state.

Adapters: 4. Profiles: 4. Roles per profile: 11. Schedule templates: 17.
Default profile: `controlled`. Migration high-water: `016_data_integrity.sql`.
Scheduled spend threshold: $9; library default: $5. Both stop after a metered response and can overshoot.

| Schedule | Calendar (launchd local time) / interval | KeepAlive |
|---|---|---|
| `com.second-brain.backup` | {"Hour": 2, "Minute": 0} | false |
| `com.second-brain.bookmarks` | {"Hour": 1, "Minute": 0, "Weekday": 6} | false |
| `com.second-brain.capture-api` | on demand / RunAtLoad | true |
| `com.second-brain.capture-browser` | {"Hour": 1, "Minute": 15} | false |
| `com.second-brain.capture-outlook` | {"Hour": 0, "Minute": 45} | false |
| `com.second-brain.capture-slack` | {"Hour": 1, "Minute": 0} | false |
| `com.second-brain.chat-extract` | {"Hour": 2, "Minute": 30} | false |
| `com.second-brain.codex-capture` | 21600 seconds | false |
| `com.second-brain.credentials` | {"Hour": 7, "Minute": 0} | false |
| `com.second-brain.distill` | {"Hour": 5, "Minute": 0} | false |
| `com.second-brain.dream-cycle` | [{"Hour": 2, "Minute": 0, "Weekday": 1}, {"Hour": 2, "Minute": 0, "Weekday": 3}, {"Hour": 2, "Minute": 0, "Weekday": 5}] | false |
| `com.second-brain.ingest` | {"Hour": 3, "Minute": 0} | false |
| `com.second-brain.liveness` | {"Hour": 9, "Minute": 0} | false |
| `com.second-brain.qd-sync` | 3600 seconds | false |
| `com.second-brain.verify` | {"Hour": 3, "Minute": 0, "Weekday": 0} | false |
| `com.second-brain.weekly-digest` | {"Hour": 6, "Minute": 0, "Weekday": 1} | false |
| `com.second-brain.youtube` | {"Hour": 1, "Minute": 30} | false |
