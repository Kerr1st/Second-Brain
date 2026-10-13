"""Shared capture URL policies and Crawlee index identity reader."""
from __future__ import annotations
import re

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
    r"ivy-help-center|talent\.a2z|docs\.hub\.amazon|"
    r"meetings\.amazon|inside\.amazon|pathfinder\.legal\.amazon|"
    r"idp\.federate\.amazon|amazon-mentoring|"
    r"console\.harmony\.a2z)"
)

# Corporate sites behind SSO that Crawlee (no session) can only fail on: 403s,
# sign-in redirects and "Execution context was destroyed" (2026-10-10 run).
# They take the cookie path (skipped_auth when the session does not carry),
# never Crawlee. Generic hosts (cloudfront.net, amplifyapp.com — public apps exist there too) are left to the unreachable ledger.
AUTH_REQUIRED = re.compile(
    r"^https?://([^/?#]*\.)?("
    r"sharepoint\.com|"                      # Microsoft 365 SSO
    r"aws\.dev|"                             # internal AWS apps (awsinsights, awsentral, ...)
    r"amazon\.dev|"                          # internal apps (hyperbadge, sage, my.day1, agentspaces, ...)
    r"a2z\.com|"                             # internal (gather, ...)
    r"([a-z0-9-]+-)?corp\.amazon\.com|"      # corp.amazon.com, collaborate-corp.amazon.com
    r"coupahost\.com|"                       # Coupa procurement (SSO)
    r"studio\.[a-z0-9-]+\.prod\.workshops\.aws"  # Workshop Studio (authoring) - catalog is public
    r")(:\d+)?([/?#]|$)", re.I)


def needs_cookies(url: str) -> bool:
    """Internal or SSO-only: fetched with Chrome cookies, never handed to Crawlee."""
    return bool(INTERNAL_DOMAINS.search(url) or AUTH_REQUIRED.search(url))


def is_private_host(url: str) -> bool:
    """Bookmarks only: LAN / loopback hosts (192.168.x, 10.x, 172.16-31.x, 127.x,
    link-local, localhost, *.local) are skipped entirely — nothing to index, and
    an unreachable LAN IP burns a 30 s navigation timeout per attempt. Kept out
    of the shared SKIP_PATTERNS, which browser history also uses (it has its own
    private-host rule)."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local



def load_indexed_urls(path, *, sources_dir=None) -> set[str]:
    """Return captured identities from the index and optional source headers."""
    from src.parsers.crawlee import build_source_map, parse_index
    urls = {entry["url"] for entry in parse_index(index_path=path)}
    if sources_dir is not None:
        urls.update(build_source_map(sources_dir=sources_dir))
    return urls
