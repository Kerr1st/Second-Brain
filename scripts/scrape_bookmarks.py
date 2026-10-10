#!/usr/bin/env python3
"""Extract Chrome bookmarks and scrape them via Crawlee (public) or
direct fetch with Chrome cookies (internal/authenticated).

Reads Chrome's Bookmarks JSON, filters to scrapable URLs,
deduplicates against Crawlee's knowledge index, and scrapes new URLs.
Internal Amazon links use Chrome cookies for authentication.
"""
import json, os, subprocess, re, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.capture.filters import SKIP_PATTERNS, needs_cookies, load_indexed_urls as _load_indexed_urls
from src.capture.render import html_to_text, render_document
from urllib.parse import urlparse
from datetime import datetime

BOOKMARKS_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Bookmarks"
)
CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
INDEX_PATH = os.path.join(CRAWLEE_DIR, "knowledge", "index.md")
KNOWLEDGE_DIR = os.path.join(CRAWLEE_DIR, "knowledge", "sources")


def extract_urls(node):
    if node.get("type") == "url":
        return [(node["name"], node["url"])]
    urls = []
    for child in node.get("children", []):
        urls.extend(extract_urls(child))
    return urls


def load_indexed_urls():
    return _load_indexed_urls(INDEX_PATH, sources_dir=KNOWLEDGE_DIR)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "page"


#: Crawlee is driven in batches, each with its own timeout, so one slow page
#: cannot consume the whole run and partial progress is always measured.
CRAWLEE_BATCH_SIZE = int(os.environ.get("CRAWLEE_BATCH_SIZE", "50"))
CRAWLEE_BATCH_TIMEOUT_S = float(os.environ.get("CRAWLEE_BATCH_TIMEOUT_S", "1200"))
#: Do not start a batch with less than this much budget left.
CRAWLEE_MIN_BATCH_S = 120.0
#: Headroom kept between the Crawlee budget and the job's wall-clock deadline.
DEADLINE_MARGIN_S = 900.0

# Internal fetches that land on a sign-in page or are refused for auth reasons
# are a session problem, not a broken bookmark.
_AUTH_STATUS = {401, 403, 407}
_AUTH_URL_RE = re.compile(
    r"signin|sign-in|sign_in|/login|login\.|midway-auth\.|idp\.federate\.|/sso/|/saml|/oauth2?/|authorize", re.I)


def job_deadline_s():
    """The bookmarks job's wall-clock deadline (plist value, else the runner floor)."""
    try:
        from src.operations.jobs import resolve_timeout
        return resolve_timeout("bookmarks", os.environ)[0]
    except Exception:  # noqa: BLE001 - a bad env value must not stop the scrape; use the floor
        return 3 * 3600.0


def _is_auth_response(resp):
    if resp.status_code in _AUTH_STATUS:
        return True
    # Any redirect target, or the final URL, pointing at sign-in. The originally
    # requested URL (history[0]) is not judged.
    redirects = [getattr(r, "url", "") or "" for r in (getattr(resp, "history", None) or [])][1:]
    return any(_AUTH_URL_RE.search(u) for u in [*redirects, resp.url or ""])


def scrape_with_cookies(name, url):
    """Fetch an internal page using Chrome cookies, save as markdown.

    Returns "ok", "auth" (redirected to sign-in / refused for auth: counted as
    skipped_auth), "too_short" (reached, but too little text to index),
    "timeout" or "failed" (genuine fetch error)."""
    try:
        import browser_cookie3, requests
        from html.parser import HTMLParser

        cj = browser_cookie3.chrome(domain_name=urlparse(url).netloc)
        resp = requests.get(url, cookies=cj, timeout=30,
                           headers={"User-Agent": "Mozilla/5.0"})

        if _is_auth_response(resp):
            return "auth"
        if resp.status_code != 200:
            return "failed"

        text = html_to_text(resp.text)

        if len(text) < 200:
            return "too_short"

        # Write with metadata header
        date = datetime.now().strftime("%Y-%m-%d")
        import hashlib
        filename = f"{slugify(name)}-{hashlib.sha256(url.encode()).hexdigest()[:12]}.md"
        content = render_document(name, {"Source": url, "Type": "web-page", "Date": date}, text)

        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(os.path.join(KNOWLEDGE_DIR, filename), "w") as f:
            f.write(content)
        return "ok"

    except Exception as e:  # noqa: BLE001 - one bookmark never aborts the batch (and `requests` may be unbound on ImportError)
        print(f"    ✗ Cookie fetch failed: {type(e).__name__}")
        return "timeout" if "Timeout" in type(e).__name__ else "failed"


# batch-scrape.js (stderr) prints one line per finished request:
#   "✓ <title>"   indexed (detected through the index, as before)
#   "✗ <url>"     failed after Crawlee's retries
#   "⚠ Skip (too short): <url>"
# Crawlee's own log lines carry {"url": ...} and say why a request failed.
_TOO_SHORT_RE = re.compile(r"^\s*⚠ Skip \(too short\):\s*(\S+)\s*$")
_FAILED_RE = re.compile(r"^\s*✗\s+(\S+)\s*$")
_LOG_URL_RE = re.compile(r'"url":"([^"]+)"')
_LOG_AUTH_RE = re.compile(r"\b(401|403|407)\b status code|status code:? (401|403|407)\b", re.I)
_LOG_TIMEOUT_RE = re.compile(r"timed? ?out|TimeoutError", re.I)


def parse_crawlee_output(text):
    """Map URLs to "too_short" / "auth" / "timeout" / "error" from batch-scrape.js output."""
    hints, outcomes = {}, {}
    for line in (text or "").splitlines():
        match = _LOG_URL_RE.search(line)
        if match:
            if _LOG_AUTH_RE.search(line):
                hints[match.group(1)] = "auth"
            elif _LOG_TIMEOUT_RE.search(line):
                hints.setdefault(match.group(1), "timeout")
            continue
        match = _TOO_SHORT_RE.match(line)
        if match:
            outcomes[match.group(1)] = "too_short"
            continue
        match = _FAILED_RE.match(line)
        if match and match.group(1).startswith("http"):
            outcomes[match.group(1)] = "fail"
    return {url: (hints.get(url, "error") if cls == "fail" else cls) for url, cls in outcomes.items()}


def _run_crawlee_batch(batch, timeout):
    """Run one Crawlee batch; returns (returncode or None on timeout, output text).

    Output goes to a temporary file rather than a pipe, so what was printed
    before a timeout kill is still readable."""
    fd, url_file = tempfile.mkstemp(prefix=".sb-urls-", suffix=".txt", dir=CRAWLEE_DIR)
    log_fd, log_file = tempfile.mkstemp(prefix=".sb-out-", suffix=".log")
    try:
        with os.fdopen(fd, "w") as handle:
            for _, url in batch:
                handle.write(url + "\n")
        with os.fdopen(log_fd, "w") as log:
            try:
                result = subprocess.run(
                    ["node", "batch-scrape.js", url_file], cwd=CRAWLEE_DIR,
                    timeout=timeout, stdout=log, stderr=subprocess.STDOUT, text=True,
                )
                rc = result.returncode
            except subprocess.TimeoutExpired:
                rc = None
        try:
            with open(log_file, errors="replace") as log:
                output = log.read()
        except OSError:
            output = ""
        return rc, output
    finally:
        os.unlink(url_file)
        if os.path.exists(log_file):
            os.unlink(log_file)


def scrape_with_crawlee(urls, timeout=None, batch_size=None, batch_timeout=None, outcomes=None):
    """Scrape public URLs via Crawlee in bounded batches.

    ``timeout`` is the wall-clock budget for the whole call (default: the job
    deadline less a margin); each batch also has its own ``batch_timeout``.
    Only verified index additions count as processed, also after a timeout, so
    partial progress is kept. URLs never attempted because the budget ran out
    are ``deferred`` (not failed; they are retried on the next run). Pages
    Crawlee reached but skipped as too short are ``too_short`` (not failed).
    When given, ``outcomes`` is filled with url -> "ok" or a ledger class.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0, "too_short": 0, "timed_out_batches": 0, "deferred": 0}
    outcomes = {} if outcomes is None else outcomes
    if not urls:
        return stats
    batch_size = batch_size or CRAWLEE_BATCH_SIZE
    batch_timeout = batch_timeout or CRAWLEE_BATCH_TIMEOUT_S
    budget = timeout if timeout is not None else job_deadline_s() - DEADLINE_MARGIN_S
    started = time.monotonic()
    before = load_indexed_urls()
    batches = [urls[i:i + batch_size] for i in range(0, len(urls), batch_size)]
    for n, batch in enumerate(batches, start=1):
        remaining = budget - (time.monotonic() - started)
        if remaining < min(CRAWLEE_MIN_BATCH_S, batch_timeout):
            stats["deferred"] = sum(len(b) for b in batches[n - 1:])
            print(f"Crawlee: budget of {budget:.0f}s exhausted; {stats['deferred']} URL(s) "
                  f"in {len(batches) - n + 1} batch(es) deferred to the next run")
            break
        this_timeout = min(batch_timeout, remaining)
        requested = {url for _, url in batch}
        skipped_here = requested & before
        rc, output = _run_crawlee_batch(batch, this_timeout)
        parsed = parse_crawlee_output(output)
        # Measure progress from the index whatever happened, so partial
        # successes in a timed-out or failed batch still count.
        after = load_indexed_urls()
        processed = len((requested - before) & after)
        missing = requested - after
        for url in requested - before:
            outcomes[url] = "ok" if url in after else parsed.get(url, "timeout" if rc is None else "error")
        too_short = sum(1 for url in missing if outcomes.get(url) == "too_short")
        failed = len(missing) - too_short
        if rc is None:
            stats["timed_out_batches"] += 1
            print(f"Crawlee batch {n}/{len(batches)} TIMED OUT after {this_timeout:.0f}s: "
                  f"{processed} of {len(requested) - len(skipped_here)} new URL(s) indexed, {failed} failed")
        elif rc:
            failed = max(1, failed)  # a nonzero exit stays a failure even with partial output
        stats["processed"] += processed
        stats["skipped"] += len(skipped_here)
        stats["failed"] += failed
        stats["too_short"] += too_short
        before = after
    return stats


def main():
    started = time.monotonic()
    with open(BOOKMARKS_PATH) as f:
        data = json.load(f)

    all_urls = []
    for key in data["roots"]:
        if isinstance(data["roots"][key], dict):
            all_urls.extend(extract_urls(data["roots"][key]))

    scrapable = [(n, u) for n, u in all_urls if not SKIP_PATTERNS.search(u)]
    indexed = load_indexed_urls()
    new = [(n, u) for n, u in scrapable if u not in indexed]

    ledger = _load_ledger()
    unreachable = [(n, u) for n, u in new if ledger is not None and ledger.should_skip(u)]
    skip = {u for _, u in unreachable}
    pending = [(n, u) for n, u in new if u not in skip]
    internal = [(n, u) for n, u in pending if needs_cookies(u)]
    public = [(n, u) for n, u in pending if not needs_cookies(u)]

    print(f"Total: {len(all_urls)}, Scrapable: {len(scrapable)}, New: {len(new)}")
    print(f"  skipped_unreachable (ledger cool-off): {len(unreachable)}")
    print(f"  Internal (cookies): {len(internal)}")
    print(f"  Public (Crawlee): {len(public)}")

    ok = fail = skipped_auth = too_short = 0
    outcomes = {}

    # Internal links with cookies
    if internal:
        print(f"\n--- Internal links ({len(internal)}) ---")
        for i, (name, url) in enumerate(internal):
            print(f"  [{i+1}/{len(internal)}] {name[:50]}")
            outcome = scrape_with_cookies(name, url)
            if outcome is True or outcome == "ok":
                ok += 1
                outcomes[url] = "ok"
            elif outcome == "auth":
                skipped_auth += 1
                outcomes[url] = "auth"
            elif outcome == "too_short":
                too_short += 1
                outcomes[url] = "too_short"
            else:
                fail += 1
                outcomes[url] = "timeout" if outcome == "timeout" else "error"

    # Public links with Crawlee: bounded batches, total kept under the job deadline.
    budget = job_deadline_s() - (time.monotonic() - started) - DEADLINE_MARGIN_S
    public_stats = scrape_with_crawlee(public, timeout=max(0.0, budget), outcomes=outcomes) if public else \
        {"processed": 0, "skipped": 0, "failed": 0, "too_short": 0, "timed_out_batches": 0, "deferred": 0}

    print(f"\nInternal: {ok} scraped, {skipped_auth} skipped_auth, {fail} failed, {too_short} too_short")
    print(f"Public: {public_stats}")
    record_unreachable(ledger, outcomes, len(unreachable))
    report_backlog(public_stats.get("deferred", 0))
    return int(bool(fail or public_stats["failed"]))


def _load_ledger():
    try:
        from src.capture.unreachable import Ledger
        return Ledger.load()
    except Exception as exc:  # noqa: BLE001 - a broken ledger means "retry everything", never a crash
        print(f"WARNING: could not read the bookmarks unreachable ledger: {type(exc).__name__}")
        return None


def record_unreachable(ledger, outcomes, skipped_unreachable):
    """Persist this run's per-URL outcomes and print the ledger summary (counts only)."""
    if ledger is None:
        return None
    from collections import Counter
    counts = Counter(cls for cls in outcomes.values() if cls != "ok")
    newly = 0
    try:
        for url, cls in outcomes.items():
            was_skipping = ledger.skip_until(url) is not None
            entry = ledger.record(url, cls)
            if entry and entry.get("skip_until") and not was_skipping:
                newly += 1
        ledger.save()
    except Exception as exc:  # noqa: BLE001 - ledger bookkeeping must not change the job's outcome
        print(f"WARNING: could not record the bookmarks unreachable ledger: {type(exc).__name__}")
        return None
    print(f"Unreachable: {skipped_unreachable} skipped_unreachable, {newly} newly cooling off, "
          f"attempt classes {dict(sorted(counts.items()))}, ledger {len(ledger.entries)} URL(s)")
    return counts


def report_backlog(deferred):
    """Track consecutive runs that deferred URLs; warn once the backlog persists.

    A single deferral is normal (the rest is retried next run) and never fails
    the job; the liveness sweep turns a persistent streak into an advisory."""
    try:
        from src.operations import backlog
        state = backlog.record_run("bookmarks", deferred)
        limit = backlog.threshold("bookmarks")
    except Exception as exc:  # noqa: BLE001 - backlog bookkeeping must not change the job's outcome
        print(f"WARNING: could not record the bookmarks backlog state: {type(exc).__name__}")
        return None
    streak = state["consecutive_deferred_runs"]
    if streak >= limit:
        print(f"WARNING: bookmarks backlog persists — {deferred} URL(s) deferred, {streak} consecutive "
              f"run(s) with deferrals (threshold {limit}); the Crawlee budget is not draining the queue")
    return state


if __name__ == "__main__":
    sys.exit(main())
