"""Per-URL ledger of bookmark scrape attempts that did not produce a page.

``scrape_bookmarks.py`` re-attempted every unindexed bookmark on every run, so
pages behind a login, pages that render too little text and pages that always
time out were retried weekly forever (and their Crawlee retries consumed the
batch budgets). The ledger in ``<status-dir>/bookmarks.unreachable.json``
records each failed attempt's class and decides when a URL is worth retrying:

* ``auth`` / ``timeout`` / ``error``: after ``THRESHOLD`` strikes in a row the
  URL is ``skipped_unreachable`` for ``COOL_OFF``; one retry after the
  cool-off, and another failure restarts it.
* At most one strike per URL per local calendar day (``last_strike_at``,
  optional; older ledgers fall back to ``last_attempt_at``). A further failure
  on the day of the last strike, e.g. a manual re-run hours after the scheduled
  one, updates ``status`` and ``last_attempt_at`` but not ``attempts`` /
  ``consecutive_failures``: one afternoon's transient outage seen by several
  re-runs is one bad run, not three (2026-10-10 and 10-13: every failure-class
  cool-off in the bookmarks ledger came from same-day manual re-runs,
  second-brain-triage/2026-10-15/bookmarks-gaps-probe.md §4). A calendar day,
  not a rolling window: the jobs run at most once a day on a local-time
  schedule, and a run delayed by sleep (launchd fires on wake) still lands on
  its own day, where a fixed window could swallow it.
* ``too_short``: the page was reached but is empty for our purposes; it is not
  re-scraped for ``COOL_OFF`` (first hit; it also ends a failure streak).
* a success removes the entry.
* ``deferred_runs`` (optional, absent in older ledgers): consecutive runs in
  which the URL sat in a killed Crawlee batch and was never shown attempted.
  It is not a strike; at ``DEFERRAL_LIMIT`` it becomes one ``timeout`` strike.
  Any attempt clears it. Deferral-only entries have no ``status``/``skip_until``.

Entries are keyed by a URL hash and hold only the domain: bookmark URLs are not
otherwise written to the logs, so the ledger does not start doing so. The file
is written atomically with mode 0600 (``tempfile.mkstemp`` + ``os.replace``),
like ``src/operations/backlog.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

FILENAME = "bookmarks.unreachable.json"
THRESHOLD = 3
COOL_OFF = timedelta(days=30)
FAILURE_CLASSES = ("auth", "timeout", "error")
CLASSES = (*FAILURE_CLASSES, "too_short")
#: Consecutive runs a URL may sit unattempted in a killed Crawlee batch
#: (``deferred_runs``) before that counts as one ``timeout`` strike.
DEFERRAL_LIMIT = 3


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def default_path(env=None) -> Path:
    from src.operations.backlog import status_dir
    return status_dir(env) / FILENAME


def _parse_time(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    """The current time (UTC). A function so tests can step the clock across days."""
    return datetime.now(timezone.utc)


def _last_strike(entry: dict):
    """When ``entry``'s last strike was counted. Ledgers written before
    ``last_strike_at`` counted every failed attempt, so there the last attempt
    is the last strike."""
    return _parse_time(entry.get("last_strike_at") or entry.get("last_attempt_at"))


def _same_local_day(earlier, now) -> bool:
    """``earlier`` and ``now`` fall on the same calendar day in this machine's
    local time zone (the zone the jobs are scheduled in)."""
    return earlier is not None and earlier.astimezone().date() == now.astimezone().date()


class Ledger:
    def __init__(self, path=None, entries=None):
        self.path = Path(path) if path is not None else default_path()
        self.entries: dict[str, dict] = entries if entries is not None else {}

    @classmethod
    def load(cls, path=None) -> "Ledger":
        ledger = cls(path)
        try:
            data = json.loads(ledger.path.read_text())
        except (OSError, ValueError):
            return ledger
        entries = data.get("urls") if isinstance(data, dict) else None
        if isinstance(entries, dict):
            ledger.entries = {k: v for k, v in entries.items() if isinstance(v, dict)}
        return ledger

    def skip_until(self, url: str):
        return _parse_time((self.entries.get(url_key(url)) or {}).get("skip_until"))

    def should_skip(self, url: str, now=None) -> bool:
        until = self.skip_until(url)
        return until is not None and until > (now or _now())

    def deferred_runs(self, url: str) -> int:
        try:
            return int((self.entries.get(url_key(url)) or {}).get("deferred_runs") or 0)
        except (TypeError, ValueError):
            return 0

    def record_deferred(self, url: str, now=None) -> dict | None:
        """Count one run in which ``url`` sat in a killed Crawlee batch and was
        never shown as attempted (no strike). After ``DEFERRAL_LIMIT``
        consecutive such runs the URL takes a ``timeout`` strike (and the count
        restarts), so a URL that keeps hanging its batch is bounded by the
        normal strike / cool-off rule. Returns the struck entry, else None."""
        key = url_key(url)
        entry = dict(self.entries.get(key) or {})
        runs = self.deferred_runs(url) + 1
        if runs >= DEFERRAL_LIMIT:
            return self.record(url, "timeout", now=now)   # also resets deferred_runs
        entry.setdefault("domain", urlparse(url).netloc.lower())
        entry["deferred_runs"] = runs
        self.entries[key] = entry
        return None

    def record(self, url: str, outcome: str, now=None) -> dict | None:
        """Record one attempt. ``outcome`` is "ok" or one of ``CLASSES``. Any
        attempt ends a streak of killed-batch deferrals (``deferred_runs``).
        A failure-class outcome is a strike unless one was already counted on
        ``now``'s local calendar day (see the module docstring)."""
        key = url_key(url)
        if outcome == "ok":
            self.entries.pop(key, None)
            return None
        if outcome not in CLASSES:
            outcome = "error"
        now = now or _now()
        entry = dict(self.entries.get(key) or {})
        entry.pop("deferred_runs", None)
        entry["domain"] = urlparse(url).netloc.lower()
        entry["status"] = outcome
        entry.setdefault("first_failed_at", now.isoformat())
        if outcome == "too_short":
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["consecutive_failures"] = 0
            entry.pop("last_strike_at", None)   # the failure streak restarts
            entry["skip_until"] = (now + COOL_OFF).isoformat()
        else:
            streak = int(entry.get("consecutive_failures") or 0)
            if streak <= 0 or not _same_local_day(_last_strike(entry), now):
                # First failure this local day: one strike.
                streak += 1
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry["last_strike_at"] = now.isoformat()
                entry["skip_until"] = (now + COOL_OFF).isoformat() if streak >= THRESHOLD else None
            elif streak >= THRESHOLD and not entry.get("skip_until"):
                entry["skip_until"] = (now + COOL_OFF).isoformat()
            # else: a repeat on the day of the last strike (a manual re-run):
            # the reason and last_attempt_at move, the count and skip_until stay.
            entry["consecutive_failures"] = streak
        entry["last_attempt_at"] = now.isoformat()
        self.entries[key] = entry
        return entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".unreachable-", dir=self.path.parent)  # 0600
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"schema_version": 1, "threshold": THRESHOLD,
                           "cool_off_days": COOL_OFF.days, "urls": self.entries},
                          handle, sort_keys=True, indent=1)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
