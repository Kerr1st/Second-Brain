#!/usr/bin/env python3
"""Extract Chrome bookmarks and send public pages to Crawlee.

Authenticated or internal URLs are skipped. This public version deliberately does
not read browser cookies or scrape private sites.
"""
import json, os, subprocess, re
from urllib.parse import urlparse

BOOKMARKS_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Bookmarks"
)
CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
INDEX_PATH = os.path.join(CRAWLEE_DIR, "knowledge", "index.md")

SKIP_PATTERNS = re.compile(
    r"^(chrome|about|javascript|mailto|file|data):|"
    r"(mail\.google|accounts\.google|"
    r"localhost|127\.0\.0\.1|\.pdf$|"
    r"login|signin|/ap/signin)"
)

AUTHENTICATED_DOMAINS = re.compile(
    r"(intranet|internal|corp|sso|idp|auth|admin|console)"
)


def extract_urls(node):
    if node.get("type") == "url":
        return [(node["name"], node["url"])]
    urls = []
    for child in node.get("children", []):
        urls.extend(extract_urls(child))
    return urls


def load_indexed_urls():
    if not os.path.exists(INDEX_PATH):
        return set()
    with open(INDEX_PATH) as f:
        return {m.group(1) for m in re.finditer(r"\((https?://[^)]+)\)", f.read())}


def scrape_with_crawlee(urls):
    if not urls:
        return
    url_file = os.path.join(CRAWLEE_DIR, ".bookmark-urls.txt")
    with open(url_file, "w") as f:
        for _, url in urls:
            f.write(url + "\n")

    print(f"\nScraping {len(urls)} public URLs via Crawlee (batch)...")
    try:
        result = subprocess.run(
            ["node", "batch-scrape.js", url_file],
            cwd=CRAWLEE_DIR, timeout=3600, capture_output=True, text=True,
        )
        ok = result.stderr.count("✓")
        fail = result.stderr.count("✗")
        skip = result.stderr.count("⚠")
        print(f"  Crawlee batch: {ok} scraped, {fail} failed, {skip} skipped")
        print(result.stderr[-500:] if len(result.stderr) > 500 else result.stderr)
    except subprocess.TimeoutExpired:
        print("  Batch scrape timed out after 1 hour")
    finally:
        os.remove(url_file) if os.path.exists(url_file) else None


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

    authenticated = [(n, u) for n, u in new if AUTHENTICATED_DOMAINS.search(urlparse(u).netloc)]
    public = [(n, u) for n, u in new if not AUTHENTICATED_DOMAINS.search(urlparse(u).netloc)]

    print(f"Total: {len(all_urls)}, Scrapable: {len(scrapable)}, New: {len(new)}")
    print(f"  Skipped authenticated/internal-looking URLs: {len(authenticated)}")
    print(f"  Public Crawlee URLs: {len(public)}")

    scrape_with_crawlee(public)


if __name__ == "__main__":
    main()
