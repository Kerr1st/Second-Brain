"""Session-start scheduling policy, separate from storage."""
from datetime import datetime, timezone

def should_run_briefing(db, *, now=None):
    last = db.get_last_briefing_time()
    if last is None:
        return True
    if ((now or datetime.now(timezone.utc)) - last).total_seconds() < 86400:
        return False
    return db.has_activity_since(last)
