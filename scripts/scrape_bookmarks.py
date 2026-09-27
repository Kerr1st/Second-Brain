#!/usr/bin/env python3
"""Extract Chrome bookmarks and scrape them via Crawlee (public) or
direct fetch with Chrome cookies (internal/authenticated).

Reads Chrome's Bookmarks JSON, filters to scrapable URLs,
deduplicates against Crawlee's knowledge index, and scrapes new URLs.
Internal Amazon links use Chrome cookies for authentication.
"""
import json, os, subprocess, re, sys, tempfile
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


def scrape_with_cookies(name, url):
    """Fetch an internal page using Chrome cookies, save as markdown."""
    try:
        import browser_cookie3, requests
        from html.parser import HTMLParser

        cj = browser_cookie3.chrome(domain_name=urlparse(url).netloc)
        resp = requests.get(url, cookies=cj, timeout=30,
                           headers={"User-Agent": "Mozilla/5.0"})

        if resp.status_code != 200 or "signin" in resp.url.lower():
            return False

        text = html_to_text(resp.text)

        if len(text) < 200:
            return False

        # Write with metadata header
        date = datetime.now().strftime("%Y-%m-%d")
        import hashlib
        filename = f"{slugify(name)}-{hashlib.sha256(url.encode()).hexdigest()[:12]}.md"
        content = render_document(name, {"Source": url, "Type": "web-page", "Date": date}, text)

        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(os.path.join(KNOWLEDGE_DIR, filename), "w") as f:
            f.write(content)
        return True

    except (OSError, ValueError, requests.RequestException) as e:
        print(f"    ✗ Cookie fetch failed: {e}")
        return False


def scrape_with_crawlee(urls):
    """Report only verified index additions as successful public scrapes."""
    if not urls:
        return {"processed": 0, "failed": 0, "skipped": 0}
    before = load_indexed_urls()
    fd, url_file = tempfile.mkstemp(prefix=".sb-urls-", suffix=".txt", dir=CRAWLEE_DIR)
    try:
        with os.fdopen(fd, "w") as handle:
            for _, url in urls:
                handle.write(url + "\n")
        result = subprocess.run(
            ["node", "batch-scrape.js", url_file], cwd=CRAWLEE_DIR,
            timeout=3600, capture_output=True, text=True,
        )
        after = load_indexed_urls()
        requested = {url for _, url in urls}
        skipped = len(requested & before)
        processed = len((requested - before) & after)
        failed = len(requested - after)
        # A nonzero subprocess remains failure even when partial output exists.
        if result.returncode:
            failed = max(1, failed)
        return {"processed": processed, "skipped": skipped, "failed": failed}
    except subprocess.TimeoutExpired:
        return {"processed": 0, "skipped": 0, "failed": len(urls)}
    finally:
        os.unlink(url_file)


def main():
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

    ok = fail = 0

    # Internal links with cookies
    if internal:
        print(f"\n--- Internal links ({len(internal)}) ---")
        for i, (name, url) in enumerate(internal):
            print(f"  [{i+1}/{len(internal)}] {name[:50]}")
            if scrape_with_cookies(name, url):
                ok += 1
            else:
                fail += 1

    # Public links with Crawlee (single batch)
    public_stats = scrape_with_crawlee(public)

    print(f"\nInternal: {ok} scraped, {fail} failed")
    print(f"Public: {public_stats}")
    return int(bool(fail or public_stats["failed"]))


if __name__ == "__main__":
    sys.exit(main())
