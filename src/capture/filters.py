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



def load_indexed_urls(path, *, sources_dir=None) -> set[str]:
    """Return captured identities from the index and optional source headers."""
    from src.parsers.crawlee import build_source_map, parse_index
    urls = {entry["url"] for entry in parse_index(index_path=path)}
    if sources_dir is not None:
        urls.update(build_source_map(sources_dir=sources_dir))
    return urls
