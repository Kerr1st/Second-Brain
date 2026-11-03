"""Per-URL ledger and bot-wall blocklist for capture-browser's reading scrape.

The reading step sends the pages read in the 2-day window to Crawlee. A page on
a site that blocks headless browsers (queue.acm.org: HTTP 403 "Request blocked",
2026-10-14) failed on every run while it stayed in the window. This module
gives the reading step what the bookmarks job already has:

* **Reading ledger** ``<status-dir>/capture-browser.unreachable.json``: the
  bookmarks ``unreachable.Ledger`` (same strikes, cool-off, URL-hash keys,
  domain-only entries, atomic 0600 writes) under its own file name. The
  ``.unreachable.json`` suffix is in ``liveness.SIDECAR_SUFFIXES``, so the
  job-status sweep does not read it as a job.
* **Bot-walled hosts**: ``BOT_WALLED_SEED`` (curated, see the rule above it;
  host-anchored, subdomains included) plus any *dynamic* host: at least
  ``DYNAMIC_THRESHOLD`` distinct URLs currently cooling off with class ``auth``
  across the reading ledger and the bookmarks ledger (union, URL keys
  deduplicated). Dynamic hosts match exactly (no subdomains) and never include
  a ``NEVER_WALLED`` host: bookmarks ``auth`` also means "private page behind a
  login" (a private repo, a shared doc), which says nothing about the host's
  public pages. The bookmarks ledger is only ever read here. Amazon-internal
  hosts are never bot-walled: an internal 403 is an auth problem.
* **Bad-render guard**: a run where nothing rendered (no ``ok``, at least
  ``BAD_RUN_MIN`` URLs) does not record ``too_short``; that pattern is a broken
  render night, not thin pages, and ``too_short`` would cool the page off for
  30 days on its first hit.
* **Pruning**: on save, entries more than ``PRUNE_AFTER_SKIP`` past their
  ``skip_until``, and entries without ``skip_until`` whose last attempt is
  more than ``PRUNE_IDLE`` old, are dropped.

Locking. ``record_outcomes`` serializes its load/modify/save on a dedicated
``<status-dir>/capture-browser.unreachable.lock`` (``flock`` ``LOCK_EX``,
polled for up to ``LOCK_TIMEOUT_S``; on timeout the write is skipped with a
WARNING). The job wrapper's ``capture-browser.lock`` is not involved, so
whether the caller runs under the wrapper does not matter. The lock file is not
``*.json``, so the liveness job-status sweep never reads it.

Nothing here raises to the caller: a missing or corrupt ledger means "keep
everything" (WARNING), and a failed write is a WARNING.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.capture import unreachable

logger = logging.getLogger(__name__)

JOB = "capture-browser"
FILENAME = f"{JOB}.unreachable.json"
LOCK_NAME = f"{JOB}.unreachable.lock"
#: Seconds ``record_outcomes`` waits for ``LOCK_NAME`` before skipping the write.
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05
#: A run with no ``ok`` and at least this many URLs is a bad render: no ``too_short``.
BAD_RUN_MIN = 3
#: Pruned on save: entries this far past ``skip_until`` ...
PRUNE_AFTER_SKIP = timedelta(days=30)
#: ... and entries without ``skip_until`` whose last attempt is this old.
PRUNE_IDLE = timedelta(days=60)
#: Distinct URLs of one host cooling off with class ``auth`` that make it bot-walled.
DYNAMIC_THRESHOLD = 2

#: Qualification rule for a seed host: at least 2 attempts answered with the
#: bot response HTTP 403 "Request blocked - received 403 status code" (Crawlee
#: log or capture-browser triage), on a public host (not internal), and the host
#: is not in ``NEVER_WALLED``. One blocked URL in one run is not enough: leave
#: such a host to the dynamic rule. Evidence: queue.acm.org (capture-browser
#: 2026-10-14 triage); the others in logs/bookmarks-crawlee-1.log (2026-10-13)
#: with entries in logs/jobs/bookmarks.unreachable.json (ebay.com: 2 URLs, 2
#: blocked attempts each). Matched host-anchored at a label boundary, so www. and
#: other subdomains match and queue.acm.org.evil.io does not.
BOT_WALLED_SEED = frozenset({
    "queue.acm.org",
    "crunchbase.com",
    "glassdoor.com",
    "yelp.com",
    "etsy.com",
    "homedepot.com",
    "saleshacker.com",
    "ebay.com",
})

#: Hosts that wall *private* pages behind a login but serve public pages to
#: headless browsers: an ``auth`` cool-off there means "private", not "bot wall",
#: so the dynamic rule never walls them (host-anchored: subdomains included).
NEVER_WALLED = frozenset({
    "github.com", "gist.github.com", "raw.githubusercontent.com", "gitlab.com", "bitbucket.org",
    "google.com", "docs.google.com", "drive.google.com", "sites.google.com",
    "medium.com", "substack.com", "linkedin.com", "www.linkedin.com",
    "notion.so", "notion.site", "x.com", "twitter.com", "youtube.com", "www.youtube.com",
    "facebook.com", "instagram.com", "reddit.com", "dropbox.com", "figma.com",
    "atlassian.net", "slack.com", "zoom.us", "microsoft.com", "sharepoint.com",
    "office.com", "live.com", "box.com", "airtable.com", "trello.com",
})

#: Amazon-owned (and Amazon-tenant) registrable suffixes: never bot-walled.
INTERNAL_SUFFIXES = (
    "amazon.com", "amazon.dev", "aws.dev", "a2z.com", "amazonaws.com", "amazon.work",
    "aws", "amazon", "aws.amazon.com", "amazon.sharepoint.com", "amazon.awsapps.com",
    "amazon.coupahost.com", "quip-amazon.com", "amazoncognito.com", "awsapps.com",
)


# ------------------------------------------------------------------ helpers

def _status_dir(status_dir) -> Path:
    if status_dir is not None:
        return Path(status_dir)
    from src.operations.backlog import status_dir as resolve
    return resolve()


def ledger_path(status_dir=None) -> Path:
    return _status_dir(status_dir) / FILENAME


def _bookmarks_path(status_dir) -> Path:
    return _status_dir(status_dir) / unreachable.FILENAME


def _now(now):
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _host(value: str) -> str:
    """Lowercase hostname of a URL or a ledger ``domain`` (netloc), no port/userinfo."""
    try:
        text = value if "//" in value else f"//{value}"
        return (urlparse(text).hostname or "").lower().rstrip(".")
    except (ValueError, TypeError, AttributeError):
        return ""


def _under(host: str, suffix: str) -> bool:
    return bool(host) and (host == suffix or host.endswith("." + suffix))


def internal_host(host: str) -> bool:
    return any(_under(host, s) for s in INTERNAL_SUFFIXES)


def _load(path: Path, label: str, *, warn_missing: bool = True):
    """Ledger at ``path``; None (WARNING) when it is missing or unreadable."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        if warn_missing:
            logger.warning("%s ledger missing (%s): nothing skipped from it", label, path.name)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("%s ledger unreadable (%s: %s): nothing skipped from it", label, path.name, type(exc).__name__)
        return None
    entries = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        logger.warning("%s ledger malformed (%s): nothing skipped from it", label, path.name)
        return None
    return unreachable.Ledger(path, {k: v for k, v in entries.items() if isinstance(v, dict)})


def _cooling_auth(ledger, now) -> dict[str, set[str]]:
    """host -> URL keys cooling off with class ``auth``."""
    hosts: dict[str, set[str]] = {}
    if ledger is None:
        return hosts
    for key, entry in ledger.entries.items():
        try:
            if entry.get("status") != "auth":
                continue
            until = unreachable._parse_time(entry.get("skip_until"))
            if until is None or until <= now:
                continue
            host = _host(str(entry.get("domain") or ""))
            if host:
                hosts.setdefault(host, set()).add(key)
        except Exception:  # noqa: BLE001 - one odd entry must not hide the rest
            continue
    return hosts


def never_walled(host: str) -> bool:
    return any(_under(host, h) for h in NEVER_WALLED)


def _bot_walled(reading, bookmarks, now) -> tuple[set[str], set[str]]:
    """(seed hosts, dynamic hosts); internal hosts removed from both."""
    merged: dict[str, set[str]] = {}
    for ledger in (reading, bookmarks):
        for host, keys in _cooling_auth(ledger, now).items():
            merged.setdefault(host, set()).update(keys)
    dynamic = {h for h, keys in merged.items()
               if len(keys) >= DYNAMIC_THRESHOLD and not never_walled(h)}
    seed = set(BOT_WALLED_SEED)
    return ({h for h in seed if not internal_host(h)},
            {h for h in dynamic - seed if not internal_host(h)})


def _matches(host: str, seed: set[str], dynamic: set[str]) -> bool:
    """Seed: host-anchored (subdomains match). Dynamic: exact host only."""
    if not host or internal_host(host):
        return False
    return host in dynamic or any(_under(host, h) for h in seed)


# ------------------------------------------------------------------ public API

def bot_walled_hosts(*, status_dir=None, now=None) -> set[str]:
    """Hosts treated as bot-walled right now (see rules)."""
    try:
        now = _now(now)
        reading = _load(ledger_path(status_dir), "reading", warn_missing=False)
        bookmarks = _load(_bookmarks_path(status_dir), "bookmarks", warn_missing=False)
        seed, dynamic = _bot_walled(reading, bookmarks, now)
        return seed | dynamic
    except Exception as exc:  # noqa: BLE001 - a blocklist must never break the job
        logger.warning("bot-walled hosts unavailable (%s): seed only", type(exc).__name__)
        return {h for h in BOT_WALLED_SEED if not internal_host(h)}


def filter_reading(urls: list[str], *, status_dir=None, now=None) -> tuple[list[str], dict[str, str]]:
    """Return (keep, skipped) where skipped maps url -> reason ("cooling_off" | "bot_walled")."""
    try:
        now = _now(now)
        path = ledger_path(status_dir)
        reading = _load(path, "reading", warn_missing=False)
        if reading is None and not path.exists():
            logger.info("reading ledger not created yet (%s): no cool-offs to apply", path.name)
        bookmarks = _load(_bookmarks_path(status_dir), "bookmarks", warn_missing=False)
        seed, dynamic = _bot_walled(reading, bookmarks, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reading ledger check failed (%s): nothing skipped", type(exc).__name__)
        return list(urls), {}
    keep, skipped = [], {}
    for url in urls:
        try:
            if reading is not None and reading.should_skip(url, now=now):
                skipped[url] = "cooling_off"
                continue
            host = _host(url)
            if _matches(host, seed, dynamic):
                skipped[url] = "bot_walled"
                logger.info("reading skipped (bot_walled): host=%s url=%s", host, url)
                continue
        except Exception:  # noqa: BLE001 - an odd URL is kept, never fatal
            pass
        keep.append(url)
    return keep, skipped


@contextmanager
def _ledger_lock(directory: Path, timeout: float | None = None):
    """Yield True once ``LOCK_NAME`` is held (flock LOCK_EX, polled), False on timeout."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (LOCK_TIMEOUT_S if timeout is None else timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(LOCK_POLL_S)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _prune(ledger, now) -> int:
    """Drop entries long past their cool-off, or idle without one. Returns the count."""
    stale = []
    for key, entry in ledger.entries.items():
        try:
            until = unreachable._parse_time(entry.get("skip_until"))
            if until is not None:
                if now - until > PRUNE_AFTER_SKIP:
                    stale.append(key)
                continue
            last = unreachable._parse_time(entry.get("last_attempt_at") or entry.get("first_failed_at"))
            if last is not None and now - last > PRUNE_IDLE:
                stale.append(key)
        except Exception:  # noqa: BLE001 - an odd entry is kept
            continue
    for key in stale:
        ledger.entries.pop(key, None)
    return len(stale)


def is_bad_render(outcomes: dict[str, str]) -> bool:
    """No ``ok`` among >= ``BAD_RUN_MIN`` URLs: a broken render, not thin pages."""
    values = list(outcomes.values())
    return len(values) >= BAD_RUN_MIN and "ok" not in values


def record_outcomes(outcomes: dict[str, str], *, status_dir=None, now=None,
                    bad_render: bool | None = None, unguarded: dict[str, str] | None = None) -> None:
    """Record Crawlee per-URL classes (ok / too_short / auth / timeout / error) in the reading ledger.

    The bad-render guard (no ``ok`` in >= ``BAD_RUN_MIN`` URLs: ``too_short`` not
    recorded) applies to ``outcomes`` only, judged by ``bad_render`` when given (the
    caller's verdict on the whole Crawlee run) or else on ``outcomes`` itself.
    ``unguarded`` (e.g. the browser route's classes) is recorded under the same lock
    and is neither subject to the guard nor counted by it."""
    try:
        unguarded = dict(unguarded or {})
        if not outcomes and not unguarded:
            return
        now = _now(now)
        values = list(outcomes.values())
        if bad_render is None:
            bad_render = is_bad_render(outcomes)
        if bad_render and "too_short" in values:
            logger.warning("reading ledger: 0 ok of %d URLs: not recording %d too_short "
                           "(looks like a bad render, not thin pages)", len(values), values.count("too_short"))
        directory = _status_dir(status_dir)
        with _ledger_lock(directory) as acquired:
            if not acquired:
                logger.warning("reading ledger not recorded: %s busy for %.0fs", LOCK_NAME, LOCK_TIMEOUT_S)
                return
            path = directory / FILENAME
            ledger = _load(path, "reading", warn_missing=False) or unreachable.Ledger(path)
            for url, cls in outcomes.items():
                if bad_render and cls == "too_short":
                    continue
                ledger.record(url, cls, now=now)
            for url, cls in unguarded.items():
                ledger.record(url, cls, now=now)
            _prune(ledger, now)
            ledger.save()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not change the job's outcome
        logger.warning("could not record the reading ledger: %s", type(exc).__name__)
