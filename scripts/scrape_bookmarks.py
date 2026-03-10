#!/usr/bin/env python3
"""Extract Chrome bookmarks and scrape them via Crawlee.

Reads Chrome's Bookmarks JSON, filters to scrapable URLs,
deduplicates against Crawlee's knowledge index, and runs
the scraper on new URLs.
"""
import json, os, subprocess, re

BOOKMARKS_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Bookmarks"
)
CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
INDEX_PATH = os.path.join(CRAWLEE_DIR, "knowledge", "index.md")

# URL patterns to skip
SKIP_PATTERNS = re.compile(
    r"^(chrome|about|javascript|mailto|file|data):|"
    r"(mail\.google|calendar|\.amazon\.com/ap/signin|"
    r"login|signin|auth|accounts\.google|"
    r"localhost|127\.0\.0\.1|\.pdf$|"
    r"w\.amazon\.com|quip-amazon|highspot|broadcast\.amazon|"
    r"atoz\.amazon|quicksight\.aws|amazon\.awsapps|"
    r"ivy-help-center|talent\.a2z|phonetool|"
    r"isengard|conduit\.security|sim\.amazon|"
    r"issues\.amazon|cr\.amazon|code\.amazon)"
)


def extract_urls(node):
    """Recursively extract URLs from Chrome bookmark nodes."""
    if node.get("type") == "url":
        return [(node["name"], node["url"])]
    urls = []
    for child in node.get("children", []):
        urls.extend(extract_urls(child))
    return urls


def load_indexed_urls():
    """Load URLs already in Crawlee's knowledge index."""
    if not os.path.exists(INDEX_PATH):
        return set()
    with open(INDEX_PATH) as f:
        return {m.group(1) for m in re.finditer(r"\((https?://[^)]+)\)", f.read())}


def main():
    with open(BOOKMARKS_PATH) as f:
        data = json.load(f)

    all_urls = []
    for key in data["roots"]:
        if isinstance(data["roots"][key], dict):
            all_urls.extend(extract_urls(data["roots"][key]))

    # Filter
    scrapable = [(name, url) for name, url in all_urls if not SKIP_PATTERNS.search(url)]
    print(f"Total bookmarks: {len(all_urls)}")
    print(f"Scrapable: {len(scrapable)}")

    # Dedup against index
    indexed = load_indexed_urls()
    new = [(name, url) for name, url in scrapable if url not in indexed]
    print(f"Already indexed: {len(scrapable) - len(new)}")
    print(f"New to scrape: {len(new)}")

    if not new:
        print("Nothing new to scrape.")
        return

    # Write URLs to a temp file for Crawlee
    url_file = os.path.join(CRAWLEE_DIR, ".bookmark-urls.txt")
    with open(url_file, "w") as f:
        for name, url in new:
            f.write(url + "\n")

    print(f"\nScraping {len(new)} URLs via Crawlee...")
    for i, (name, url) in enumerate(new):
        print(f"  [{i+1}/{len(new)}] {name[:50]} → {url[:60]}")
        try:
            subprocess.run(
                ["node", "scraper.js", url],
                cwd=CRAWLEE_DIR,
                capture_output=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            print(f"    ⚠ Timeout, skipping")
        except Exception as e:
            print(f"    ✗ {e}")

    os.remove(url_file)
    print(f"\nDone. Scraped {len(new)} bookmarks.")


if __name__ == "__main__":
    main()
