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
from src.capture.filters import SKIP_PATTERNS, INTERNAL_DOMAINS, load_indexed_urls as _load_indexed_urls
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
    skipped_auth) or "failed" (genuine fetch error)."""
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
            return "failed"

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
        return "failed"


def _run_crawlee_batch(batch, timeout):
    """Run one Crawlee batch; returns (returncode or None on timeout)."""
    fd, url_file = tempfile.mkstemp(prefix=".sb-urls-", suffix=".txt", dir=CRAWLEE_DIR)
    try:
        with os.fdopen(fd, "w") as handle:
            for _, url in batch:
                handle.write(url + "\n")
        try:
            result = subprocess.run(
                ["node", "batch-scrape.js", url_file], cwd=CRAWLEE_DIR,
                timeout=timeout, capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired:
            return None
        return result.returncode
    finally:
        os.unlink(url_file)


def scrape_with_crawlee(urls, timeout=None, batch_size=None, batch_timeout=None):
    """Scrape public URLs via Crawlee in bounded batches.

    ``timeout`` is the wall-clock budget for the whole call (default: the job
    deadline less a margin); each batch also has its own ``batch_timeout``.
    Only verified index additions count as processed, also after a timeout, so
    partial progress is kept. URLs never attempted because the budget ran out
    are ``deferred`` (not failed; they are retried on the next run).
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0, "timed_out_batches": 0, "deferred": 0}
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
        rc = _run_crawlee_batch(batch, this_timeout)
        # Measure progress from the index whatever happened, so partial
        # successes in a timed-out or failed batch still count.
        after = load_indexed_urls()
        processed = len((requested - before) & after)
        failed = len(requested - after)
        if rc is None:
            stats["timed_out_batches"] += 1
            print(f"Crawlee batch {n}/{len(batches)} TIMED OUT after {this_timeout:.0f}s: "
                  f"{processed} of {len(requested) - len(skipped_here)} new URL(s) indexed, {failed} failed")
        elif rc:
            failed = max(1, failed)  # a nonzero exit stays a failure even with partial output
        stats["processed"] += processed
        stats["skipped"] += len(skipped_here)
        stats["failed"] += failed
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

    internal = [(n, u) for n, u in new if INTERNAL_DOMAINS.search(u)]
    public = [(n, u) for n, u in new if not INTERNAL_DOMAINS.search(u)]

    print(f"Total: {len(all_urls)}, Scrapable: {len(scrapable)}, New: {len(new)}")
    print(f"  Internal (cookies): {len(internal)}")
    print(f"  Public (Crawlee): {len(public)}")

    ok = fail = skipped_auth = 0

    # Internal links with cookies
    if internal:
        print(f"\n--- Internal links ({len(internal)}) ---")
        for i, (name, url) in enumerate(internal):
            print(f"  [{i+1}/{len(internal)}] {name[:50]}")
            outcome = scrape_with_cookies(name, url)
            if outcome is True or outcome == "ok":
                ok += 1
            elif outcome == "auth":
                skipped_auth += 1
            else:
                fail += 1

    # Public links with Crawlee: bounded batches, total kept under the job deadline.
    budget = job_deadline_s() - (time.monotonic() - started) - DEADLINE_MARGIN_S
    public_stats = scrape_with_crawlee(public, timeout=max(0.0, budget)) if public else \
        {"processed": 0, "skipped": 0, "failed": 0, "timed_out_batches": 0, "deferred": 0}

    print(f"\nInternal: {ok} scraped, {skipped_auth} skipped_auth, {fail} failed")
    print(f"Public: {public_stats}")
    report_backlog(public_stats.get("deferred", 0))
    return int(bool(fail or public_stats["failed"]))


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
