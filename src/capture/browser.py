"""Capture browser history and reading into the Second Brain.

Reads Chrome's ``History`` SQLite database (a snapshot copy — the live file is
locked while Chrome runs) and produces two things:

1. One ``browser_history`` document per completed local day: the pages that
   received real attention (dwell >= ATTENTION_SECS, or typed/bookmarked
   navigations), grouped by domain with titles, times and dwell, plus the
   day's search queries. Noise is filtered: auth redirects, mail/chat/calendar
   web apps (captured by their own collectors), consoles and dashboards, and
   the SKIP_PATTERNS the bookmark scraper already maintains.

2. Reading: public pages with dwell >= READ_SECS are handed to the existing
   Crawlee batch scraper, exactly like bookmarks, so the nightly ``ingest`` job
   lands their full text as ``article`` memories. Amazon-internal pages are not
   scraped (cookie/VPN dependent); they still appear in the daily log.

Dedup: ``browser://history/<YYYY-MM-DD>`` per day (ingest_batch skips known
days); reading URLs are deduped against Crawlee's own index.

    python -m src.capture.browser [--days-back N] [--dry-run] [--no-scrape]
"""
from __future__ import annotations

import argparse
import collections
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, parse_qs, urlencode

from src.capture.common import exit_code
from src.capture.framework import run_channel
from src.capture.render import render_header_lines
from src.capture.window import completed_days, day_bounds
from src.capture.filters import SKIP_PATTERNS, INTERNAL_DOMAINS

logger = logging.getLogger(__name__)

SOURCE_TYPE = "browser_history"
CHROME_HISTORY = os.environ.get(
    "CHROME_HISTORY", os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/History"))

ATTENTION_SECS = int(os.environ.get("BROWSER_ATTENTION_SECS", "20"))   # appears in the daily log
READ_SECS = int(os.environ.get("BROWSER_READ_SECS", "60"))             # full text via Crawlee
# Chrome's visit_duration keeps counting while a tab sits open in the background;
# anything past this per visit is not attention.
MAX_VISIT_DWELL_SECS = int(os.environ.get("BROWSER_MAX_VISIT_DWELL_SECS", "1200"))
MAX_READ_URLS_PER_RUN = int(os.environ.get("BROWSER_MAX_READ_URLS", "60"))
# The capture-browser job deadline is 3600 s; the reading scrape must end well
# before it so the day logs are still ingested and the run reports honestly.
CRAWLEE_TIMEOUT_S = int(os.environ.get("BROWSER_CRAWLEE_TIMEOUT_S", "1800"))
MAX_LOG_ENTRIES = 400

# Chrome stores microseconds since 1601-01-01 UTC.
_CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
# Transition core types (low 8 bits) that signal deliberate navigation.
_TYPED, _AUTO_BOOKMARK = 1, 2

# Hosts whose content arrives through a dedicated collector, or that are pure
# plumbing (auth, redirects) or dashboards with no durable content.
_NOISE_HOST_RE = re.compile(
    r"(^|\.)(app\.slack\.com|slack\.com|outlook\.(cloud\.microsoft|office\.com|live\.com)|"
    r"outlook\.office365\.com|teams\.microsoft\.com|mail\.google\.com|calendar\.google\.com|"
    r"midway-auth\.amazon\.com|idp\.federate\.amazon\.com|auth\.openai\.com|awsapps\.com|"
    r"[a-z0-9-]*login[a-z0-9.-]*|accounts\.google\.com|"
    r"console\.aws\.amazon\.com|[a-z0-9-]+\.console\.aws\.amazon\.com|"
    r"[a-z0-9-]*-[a-z0-9]+\.[a-z0-9-]+\.console\.aws\.amazon\.com|"
    r"quicksight\.aws\.amazon\.com|[a-z0-9-]+\.quicksight\.aws\.amazon\.com|"
    r"isengard\.amazon\.com|conduit\.security\.a2z\.com|[a-z0-9.-]*ninjas\.security\.a2z\.com|"
    r"giphy\.com|tenor\.com|localhost|127\.0\.0\.1|newtab|chrome)$", re.I)
# Authenticated web apps: worth logging (what you worked in), impossible to
# scrape without a session, and often confidential. Never sent to Crawlee.
_APP_HOST_RE = re.compile(
    r"(^|\.)(force\.com|salesforce\.com|sharepoint\.com|office\.com|microsoft\.com|"
    r"quip\.com|quip-amazon\.com|atlassian\.net|notion\.so|figma\.com|kudoboard\.com|"
    r"docs\.google\.com|drive\.google\.com|highspot\.com)$", re.I)
_SEARCH_HOST_RE = re.compile(r"(^|\.)(google\.[a-z.]+|bing\.com|duckduckgo\.com)$", re.I)
_CHAT_HOST_RE = re.compile(r"(^|\.)(chatgpt\.com|chat\.openai\.com|claude\.ai|gemini\.google\.com)$", re.I)
# Auth plumbing by path, whatever the host; and personal HR/benefits/finance portals.
_AUTH_PATH_RE = re.compile(
    r"/(oauth2?|oidc|authorize|saml|sso|login|logout|signin|sign-in|callback|auth)(/|$|\?)|"
    r"\b(oidc|auth|login|sso)\.[a-z0-9.-]+/", re.I)
_PRIVATE_HOST_RE = re.compile(
    r"(^|\.)(plusone\.com|fidelity\.com|schwab\.com|workday\.com|adp\.com|chase\.com|"
    r"bankofamerica\.com|wellsfargo\.com|healthcare\.gov|mychart\.[a-z.]+|zoom\.us|"
    r"chargepoint\.com)$", re.I)
# Personal settings pages are login-walled, whatever the site. Anchored to the
# path start or a settings-style segment so /r/Account/<id> (CRM records) is untouched.
_ACCOUNT_PATH_RE = re.compile(r"(^|/)(settings|preferences|billing|account/(settings|security|billing)|profile/edit)(/|$)", re.I)
# Hosts where the content behind the URL is only visible to the logged-in user.
_AUTH_WALLED_HOST_RE = re.compile(r"(^|\.)(linkedin\.com|lnkd\.in|facebook\.com|x\.com|twitter\.com|instagram\.com)$", re.I)
# Query parameters that may carry credentials; stripped from every URL we store.
_SECRET_PARAM_RE = re.compile(
    r"^(token|.*_token|partnertoken|access_token|id_token|code|session.*|sig|signature|"
    r"auth.*|api_?key|key|secret|password|pwd|otp|ticket|sso.*)$", re.I)
_INTERNAL_SUFFIXES = (".amazon.com", ".a2z.com", ".aws.dev", ".amazon.work", ".amazon.dev", ".amazon-corp.com")



# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def chrome_time_to_dt(value: int) -> datetime:
    return _CHROME_EPOCH + timedelta(microseconds=int(value))


def dt_to_chrome_time(dt: datetime) -> int:
    return int((dt.astimezone(timezone.utc) - _CHROME_EPOCH).total_seconds() * 1_000_000)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def classify(url: str) -> str:
    """'skip' | 'search' | 'chat' | 'app' | 'internal' | 'public'."""
    host = host_of(url)
    if not host or not url.lower().startswith(("http://", "https://")):
        return "skip"
    if SKIP_PATTERNS.search(url) or _NOISE_HOST_RE.search(host):
        return "skip"
    if _AUTH_PATH_RE.search(url) or _PRIVATE_HOST_RE.search(host) or _ACCOUNT_PATH_RE.search(urlsplit(url).path):
        return "skip"
    if _SEARCH_HOST_RE.search(host):
        return "search"
    if _CHAT_HOST_RE.search(host):
        return "chat"
    if _APP_HOST_RE.search(host) or _AUTH_WALLED_HOST_RE.search(host):
        return "app"
    if INTERNAL_DOMAINS.search(url) or host.endswith(_INTERNAL_SUFFIXES):
        return "internal"
    return "public"


def canonical_url(url: str) -> str:
    """Drop fragments, tracking parameters and anything that looks like a credential."""
    parts = urlsplit(url)
    if parts.query:
        keep = [(k, v) for k, v in parse_qs(parts.query, keep_blank_values=True).items()
                if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_", "ref", "sxsrf", "ved", "ei", "oq", "gs_l", "sca_esv", "rlz"))
                and not _SECRET_PARAM_RE.match(k)]
        query = urlencode(keep, doseq=True)
    else:
        query = ""
    return parts._replace(query=query, fragment="").geturl()


def search_query(url: str) -> str | None:
    q = parse_qs(urlsplit(url).query).get("q")
    return q[0].strip() if q and q[0].strip() else None



def day_source_url(day: date) -> str:
    return f"browser://history/{day.isoformat()}"


#: Bounded busy timeout for the one sqlite call made on the *private* copy.
SNAPSHOT_SQLITE_TIMEOUT_S = 5.0
#: A torn copy (Chrome mid-write) is retried this many times in total, with
#: backoff 0.5 s, 1 s (capped at 2 s), before the error is raised.
SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_BACKOFF_S = 0.5
SNAPSHOT_BACKOFF_MAX_S = 2.0
_sleep = _time.sleep
_HISTORY_SIDECARS = ("-journal", "-wal")


def snapshot_history(path: str = CHROME_HISTORY) -> str:
    """Copy the (locked) History DB into a private temp dir; caller calls ``discard_snapshot``.

    Chrome holds History locked while it runs. Never open the live file with
    sqlite: ``Connection.backup`` retries SQLITE_BUSY/LOCKED forever, which hung
    the nightly job until its deadline (2026-10-10). A plain file copy never
    takes a SQLite lock. The ``-journal``/``-wal`` sidecars are copied under the
    same basename so SQLite reconciles them when the copy is first opened.

    A copy taken while Chrome is mid-write can be torn ("database disk image is
    malformed").  Each copy is probed (``PRAGMA quick_check(1)``) and the whole
    copy is retried up to ``SNAPSHOT_ATTEMPTS`` times with a short backoff before
    the last error is raised.  A missing/unreadable source (OSError) is not retried.
    """
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        try:
            return _snapshot_once(path)
        except sqlite3.DatabaseError as exc:
            if attempt == SNAPSHOT_ATTEMPTS:
                logger.error("History snapshot failed its integrity probe %d times: %s", attempt, exc)
                raise
            delay = min(SNAPSHOT_BACKOFF_MAX_S, SNAPSHOT_BACKOFF_S * 2 ** (attempt - 1))
            logger.warning("History snapshot attempt %d/%d torn (%s); retrying in %.1fs",
                           attempt, SNAPSHOT_ATTEMPTS, exc, delay)
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _snapshot_once(path: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="sb-history-")
    copy = os.path.join(tmpdir, "History")
    try:
        shutil.copyfile(path, copy)
        for suffix in _HISTORY_SIDECARS:
            if os.path.exists(path + suffix):
                try:
                    shutil.copyfile(path + suffix, copy + suffix)
                except FileNotFoundError:   # Chrome removed it between exists() and copy
                    pass
        # Fold any copied journal/WAL into the copy so later read-only opens are
        # self-contained. The copy is private, so the busy timeout is never hit.
        conn = sqlite3.connect(copy, timeout=SNAPSHOT_SQLITE_TIMEOUT_S)
        try:
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            verdict = conn.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            conn.close()
        if not verdict or verdict[0] != "ok":
            raise sqlite3.DatabaseError("History snapshot failed PRAGMA quick_check")
        return copy
    except (OSError, sqlite3.Error):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def discard_snapshot(snapshot: str) -> None:
    """Remove a snapshot made by ``snapshot_history`` (its whole private dir)."""
    shutil.rmtree(os.path.dirname(snapshot), ignore_errors=True)


def load_visits(db_path: str, day: date) -> list[dict]:
    """Visits on ``day`` (local), joined with url/title, oldest first."""
    start, end = day_bounds(day)
    tz = start.tzinfo
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT v.visit_time, v.visit_duration, v.transition, u.url, u.title "
            "FROM visits v JOIN urls u ON u.id = v.url "
            "WHERE v.visit_time >= ? AND v.visit_time < ? ORDER BY v.visit_time",
            (dt_to_chrome_time(start), dt_to_chrome_time(end))).fetchall()
    finally:
        conn.close()
    return [{"time": chrome_time_to_dt(t).astimezone(tz), "dwell": (d or 0) / 1_000_000,
             "transition": (tr or 0) & 0xFF, "url": u, "title": (title or "").strip()}
            for t, d, tr, u, title in rows]


def summarize_day(visits: list[dict], attention_secs: int = ATTENTION_SECS) -> dict:
    """Aggregate raw visits into page entries, searches and chat sessions."""
    pages: dict[str, dict] = {}
    searches: list[tuple[datetime, str]] = []
    seen_queries: set[str] = set()
    chats: collections.Counter = collections.Counter()
    for v in visits:
        kind = classify(v["url"])
        if kind == "skip":
            continue
        if kind == "search":
            q = search_query(v["url"])
            if q and q.lower() not in seen_queries:
                seen_queries.add(q.lower())
                searches.append((v["time"], q))
            continue
        if kind == "chat":
            chats[host_of(v["url"])] += 1
            continue
        url = canonical_url(v["url"])
        p = pages.setdefault(url, {"url": url, "title": v["title"], "kind": kind, "dwell": 0.0,
                                   "visits": 0, "first": v["time"], "deliberate": False})
        p["dwell"] += min(v["dwell"], MAX_VISIT_DWELL_SECS)
        p["visits"] += 1
        p["title"] = p["title"] or v["title"]
        p["deliberate"] |= v["transition"] in (_TYPED, _AUTO_BOOKMARK)
    attended = [p for p in pages.values() if p["dwell"] >= attention_secs or p["deliberate"]]
    attended.sort(key=lambda p: (-p["dwell"], p["first"]))
    return {"pages": attended[:MAX_LOG_ENTRIES], "searches": searches, "chats": chats,
            "raw_visits": len(visits), "distinct_pages": len(pages)}


def _fmt_dwell(secs: float) -> str:
    return f"{secs/60:.0f}m" if secs >= 90 else f"{secs:.0f}s"


def render_day(day: date, summary: dict) -> tuple[str, str] | None:
    pages, searches, chats = summary["pages"], summary["searches"], summary["chats"]
    if not pages and not searches:
        return None
    total_dwell = sum(p["dwell"] for p in pages)
    by_host: dict[str, list[dict]] = collections.defaultdict(list)
    for p in pages:
        by_host[host_of(p["url"])].append(p)
    hosts = sorted(by_host.items(), key=lambda kv: -sum(p["dwell"] for p in kv[1]))
    header = [
        f"# Browsing: {day.isoformat()}",
        "",
        f"Source: {day_source_url(day)}",
        "Type: browser-history",
        f"Date: {day.isoformat()}",
        f"Pages: {len(pages)}",
        f"Searches: {len(searches)}",
        f"Attention: {_fmt_dwell(total_dwell)}",
        f"Raw visits: {summary['raw_visits']}",
    ]
    body: list[str] = []
    if searches:
        body.append("## Searches")
        body += [f"- {t:%H:%M} {q}" for t, q in searches]
        body.append("")
    if chats:
        body.append("## AI assistant sessions")
        body += [f"- {h}: {n} page loads" for h, n in chats.most_common()]
        body.append("")
    body.append("## Pages read (by site, most attention first)")
    for host, plist in hosts:
        body.append(f"### {host} — {_fmt_dwell(sum(p['dwell'] for p in plist))}")
        for p in sorted(plist, key=lambda p: -p["dwell"]):
            title = p["title"] or p["url"]
            flags = {"internal": " (internal)", "app": " (app)"}.get(p["kind"], "")
            body.append(f"- {p['first']:%H:%M} [{_fmt_dwell(p['dwell'])}] {title}{flags}\n  {p['url']}")
        body.append("")
    return day_source_url(day), render_header_lines(header, "\n".join(body).rstrip() + "\n")


def reading_urls(summary: dict, read_secs: int = READ_SECS) -> list[tuple[str, str]]:
    """(title, url) of public pages that were actually read — candidates for full-text capture."""
    return [(p["title"], p["url"]) for p in summary["pages"]
            if p["kind"] == "public" and p["dwell"] >= read_secs and not p["url"].lower().endswith(".pdf")]


# --------------------------------------------------------------------------- #
# Reading (full text via Crawlee)                                              #
# --------------------------------------------------------------------------- #
#: logs/capture-browser-crawlee-<batch>.log (mode 0600). The bookmarks job keeps
#: logs/bookmarks-crawlee-<batch>.log; each run clears only ``<its name>-*.log``,
#: so neither job overwrites or deletes the other's logs.
CRAWLEE_LOG_NAME = "capture-browser-crawlee"
#: Per-URL classes ``scrape_with_crawlee`` writes into ``outcomes`` besides "ok".
_READING_CLASSES = ("auth", "timeout", "error", "too_short")


def _select_reading(fresh: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reading URLs to send to Crawlee this run. Returns them unchanged for now;
    integration point for ``src.capture.reading_ledger.filter_reading``."""
    return fresh


def _record_reading(outcomes: dict[str, str]) -> None:
    """Persist this run's per-URL reading outcomes. No-op for now; integration
    point for ``src.capture.reading_ledger.record_outcomes``."""


def _report_reading(fresh: list[tuple[str, str]], scrape_stats: dict, outcomes: dict[str, str]) -> dict:
    """Log one line per failed/too_short URL (URL only, never page text) and one
    summary line; return the advisory counts and the real (batch-level) failures.

    Exit rule: an individual URL that fails (auth/timeout/error) or is too short
    never fails the job. Real failures are a broken Crawlee (``crawlee_broken``),
    a batch that exited non-zero naming no failed URL (``failed_batches``) and a
    batch killed at its timeout (``timed_out_batches``)."""
    for _, url in fresh:
        cls = outcomes.get(url)
        if cls in _READING_CLASSES:
            (logger.info if cls == "too_short" else logger.warning)("reading %s: %s", cls, url)
    classes = collections.Counter(outcomes.values())
    broken = scrape_stats.get("crawlee_broken", 0)
    failed_batches = scrape_stats.get("failed_batches", 0)
    timed_out = scrape_stats.get("timed_out_batches", 0)
    real = failed_batches + timed_out + broken
    failed, too_short = scrape_stats.get("failed", 0), scrape_stats.get("too_short", 0)
    if real or failed or too_short:
        logger.warning(
            "reading summary — sent=%d ok=%d auth=%d timeout=%d error=%d too_short=%d failed=%d "
            "deferred=%d failed_batches=%d timed_out_batches=%d crawlee_broken=%d -> %s",
            len(fresh), scrape_stats.get("processed", 0), classes.get("auth", 0), classes.get("timeout", 0),
            classes.get("error", 0), too_short, failed, scrape_stats.get("deferred", 0),
            failed_batches, timed_out, broken,
            "reading scrape failed" if real else "per-URL outcomes are advisory, not a job failure")
    return {"failed": failed, "too_short": too_short, "broken": real}


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def capture(days_back: int = 2, dry_run: bool = False, scrape: bool = True,
            history_path: str = CHROME_HISTORY) -> dict:
    from src.db import get_processed_source_urls
    from src.ingest import ingest_batch

    if not os.path.exists(history_path):
        logger.error("Chrome History not found: %s", history_path)
        return {"processed": 0, "skipped": 0, "failed": 1, "reading": 0}

    already = set(get_processed_source_urls(SOURCE_TYPE))
    days = completed_days(days_back)
    snap = snapshot_history(history_path)
    items: list[tuple[str, str]] = []
    to_read: list[tuple[str, str]] = []
    try:
        for day in days:
            summary = summarize_day(load_visits(snap, day))
            reads = reading_urls(summary)
            # Reading is deduped against Crawlee's index (successful scrapes only), not
            # against the day log, so a failed scrape is retried while the day is in window.
            to_read += reads
            if day_source_url(day) in already:
                continue
            rendered = render_day(day, summary)
            if rendered:
                items.append(rendered)
            logger.info("%s: %d raw visits -> %d attended pages, %d searches, %d read",
                        day, summary["raw_visits"], len(summary["pages"]), len(summary["searches"]), len(reads))
    finally:
        discard_snapshot(snap)

    # Full text of read pages via the bookmark pipeline (dedup against Crawlee's index).
    reading_sent = 0
    reading = {"failed": 0, "too_short": 0, "broken": 0}
    if scrape and to_read:
        from scripts.scrape_bookmarks import (  # type: ignore
            BATCH_SCRAPE_ENV, load_indexed_urls, scrape_with_crawlee)
        indexed = load_indexed_urls()
        fresh, seen = [], set()
        for title, url in to_read:
            if url in indexed or url in seen:
                continue
            seen.add(url)
            fresh.append((title, url))
        fresh = _select_reading(fresh)
        fresh = fresh[:MAX_READ_URLS_PER_RUN]
        reading_sent = len(fresh)
        if dry_run:
            for _, url in fresh:
                logger.info("[DRY] read -> crawlee: %s", url)
        elif fresh:
            outcomes: dict[str, str] = {}
            scrape_stats = scrape_with_crawlee(fresh, timeout=CRAWLEE_TIMEOUT_S, outcomes=outcomes,
                                               log_name=CRAWLEE_LOG_NAME, env=BATCH_SCRAPE_ENV)
            _record_reading(outcomes)
            reading = _report_reading(fresh, scrape_stats, outcomes)
            reading_sent = scrape_stats["processed"]

    stats = run_channel(SOURCE_TYPE, items, dry_run=dry_run, ingest=ingest_batch)
    stats["reading_planned" if dry_run else "reading"] = reading_sent
    # Per-URL reading outcomes are advisory; only a broken scrape is a real failure.
    stats["reading_failed"] = reading["failed"]
    stats["reading_too_short"] = reading["too_short"]
    stats["failed"] += reading["broken"]
    logger.info("%s: %s", SOURCE_TYPE, stats)
    return stats


def main():
    ap = argparse.ArgumentParser(description="Capture browser history + reading into the Second Brain.")
    ap.add_argument("--days-back", type=int, default=2, help="completed local days to capture (default 2)")
    ap.add_argument("--dry-run", action="store_true", help="summarize + list, do not ingest or scrape")
    ap.add_argument("--no-scrape", action="store_true", help="skip full-text capture of read pages")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    stats = capture(days_back=args.days_back, dry_run=args.dry_run, scrape=not args.no_scrape)
    logger.info("captures_this_cycle: browser=%d reading_sent=%d",
                stats.get("processed", 0), stats.get("reading", 0))
    return exit_code(stats)


if __name__ == "__main__":
    sys.exit(main())
