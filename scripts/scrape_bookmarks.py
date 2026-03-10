#!/usr/bin/env python3
"""Extract Chrome bookmarks and scrape them via Crawlee (public) or
direct fetch with Chrome cookies (internal/authenticated).

Reads Chrome's Bookmarks JSON, filters to scrapable URLs,
deduplicates against Crawlee's knowledge index, and scrapes new URLs.
Internal Amazon links use Chrome cookies for authentication.
"""
import json, os, subprocess, re, sys
from urllib.parse import urlparse
from datetime import datetime

BOOKMARKS_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Bookmarks"
)
CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
INDEX_PATH = os.path.join(CRAWLEE_DIR, "knowledge", "index.md")
KNOWLEDGE_DIR = os.path.join(CRAWLEE_DIR, "knowledge", "sources")

# URLs to always skip (not useful content)
SKIP_PATTERNS = re.compile(
    r"^(chrome|about|javascript|mailto|file|data):|"
    r"(mail\.google|accounts\.google|"
    r"localhost|127\.0\.0\.1|\.pdf$|"
    r"login|signin|/ap/signin|"
    r"quicksight\.aws|phonetool|isengard|"
    r"conduit\.security|sim\.amazon|"
    r"issues\.amazon|cr\.amazon|code\.amazon)"
)

# Internal Amazon domains that need cookies
INTERNAL_DOMAINS = re.compile(
    r"(w\.amazon\.com|quip-amazon\.com|highspot|"
    r"broadcast\.amazon|atoz\.amazon|amazon\.awsapps|"
    r"ivy-help-center|talent\.a2z|docs\.hub\.amazon)"
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

        # Simple HTML to text extraction
        text = re.sub(r"<script[^>]*>.*?</script>", "", resp.text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) < 200:
            return False

        # Write with metadata header
        date = datetime.now().strftime("%Y-%m-%d")
        filename = f"{slugify(name)}.md"
        content = f"# {name}\n\nSource: {url}\nType: web-page\nDate: {date}\n\n---\n\n{text}\n"

        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(os.path.join(KNOWLEDGE_DIR, filename), "w") as f:
            f.write(content)
        return True

    except Exception as e:
        print(f"    ✗ Cookie fetch failed: {e}")
        return False


def scrape_with_crawlee(name, url):
    """Scrape a public page via Crawlee."""
    try:
        result = subprocess.run(
            ["node", "scraper.js", url],
            cwd=CRAWLEE_DIR, capture_output=True, timeout=60,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"    ⚠ Timeout")
        return False
    except Exception as e:
        print(f"    ✗ {e}")
        return False


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

    # Public links with Crawlee
    if public:
        print(f"\n--- Public links ({len(public)}) ---")
        for i, (name, url) in enumerate(public):
            print(f"  [{i+1}/{len(public)}] {name[:50]}")
            if scrape_with_crawlee(name, url):
                ok += 1
            else:
                fail += 1

    print(f"\nDone: {ok} scraped, {fail} failed")


if __name__ == "__main__":
    main()
