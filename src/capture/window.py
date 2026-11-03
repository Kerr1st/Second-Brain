"""Calendar windows use a real time zone, including historical DST rules."""
from __future__ import annotations
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def local_zone() -> ZoneInfo:
    name = os.environ.get("SECOND_BRAIN_TIMEZONE") or os.environ.get("TZ")
    if name:
        return ZoneInfo(name.removeprefix(":"))
    # Both macOS and most Linux distributions point this to a zoneinfo file.
    local = Path("/etc/localtime")
    if local.exists():
        with local.open("rb") as handle:
            return ZoneInfo.from_file(handle)
    return ZoneInfo("UTC")


def completed_days(days_back: int, today: date | None = None) -> list[date]:
    if days_back < 1:
        raise ValueError("days_back must be positive")
    today = today or datetime.now(local_zone()).date()
    return [today - timedelta(days=i) for i in range(days_back, 0, -1)]


def day_bounds(day: date, zone=None) -> tuple[datetime, datetime]:
    zone = zone or local_zone()
    return (datetime.combine(day, time.min, tzinfo=zone),
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone))


def day_bounds_utc(day: date) -> tuple[str, str]:
    return tuple(dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                 for dt in day_bounds(day))
