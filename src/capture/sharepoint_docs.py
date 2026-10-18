"""Capture the full text of SharePoint Office documents bookmarked in Chrome.

Part of the Friday bookmarks run (``scripts/scrape_bookmarks.py``). SharePoint
sign-in is Microsoft's, so the cookie/browser paths never fetch
``*.sharepoint.com``; Office documents (``.docx`` / ``.pptx`` / ``.xlsx``) are
read through ``grasp-mcp`` (Amazon-built, Midway-authenticated) instead. Only
read-only grasp tools are called: ``search_sharepoint_content``,
``get_drive_item``, ``download_sharepoint_file`` and, for the library-walk
fallback, ``list_sharepoint_sites``, ``list_sharepoint_site_libraries`` and
``list_library_files``.

Resolution (grasp has no sharing-URL lookup): search by the bookmark's file
name, keep exact-title hits on the same host and ``/sites/<site>/``, then
``get_drive_item`` each (at most ``MAX_CANDIDATES``). That item's ``webUrl`` is
the ``Doc.aspx?sourcedoc={GUID}`` form, so a ``Doc.aspx`` bookmark must match
its GUID; a direct ``/.../<name>.docx`` link must match the full path. Anything
else - none, several - is ``unresolved``: a wrong copy is never captured (three
different "Extended AWOS Framework.xlsx" live in the same site). A ``Doc.aspx``
link without a ``sourcedoc`` GUID is never resolved by name alone (``no_guid``).

Fallback (search misses an item, or a GUID-only bookmark has no ``file=``): a
bounded, read-only walk of the bookmark's site libraries (``MAX_WALK_ITEMS``
items, ``MAX_WALK_DEPTH`` folder levels, once per site per run) looks for the
bookmark's GUID; the item must also carry one of the bookmark's names (its
``file=`` or its Chrome title) exactly, and ``get_drive_item`` must confirm the
GUID. ``no_guid`` / ``no_file_name`` bookmarks that cannot resolve are a
permanent skip: counted and logged once per run, never an advisory.

Identity and change detection: ``source_url`` is ``sharepoint://<driveId>/<itemId>``
(stable across renames, moves and bookmark-URL variants such as ``action=`` or
``wdPreviousSession=``; two bookmarks of one document are one row). The row's
``source_fingerprint`` is ``EXTRACTOR_VERSION:lastModifiedDateTime:size``; an
unchanged document is not downloaded (an item without ``lastModifiedDateTime``
is always re-checked). A changed one is ingested with ``refresh=True`` and
replaces its row; the row keeps its first ``created_at`` (like every other
``refresh=True`` channel), so a changed document is not a new skim arrival.

Budget: every grasp call is bounded by the time left in the pass (``budget``),
captured text is capped per run (``MAX_RUN_CHARS``) and ingest stops at the
deadline; what is left is deferred to the next run.

Files are downloaded into a private 0700 temp dir and deleted right after
extraction; the dir is removed in a ``finally`` (SIGTERM included; stale dirs
of a SIGKILLed run are swept at the next start). Text is extracted locally with
the stdlib (zipfile + expat/ElementTree; worksheets streamed) under zip-bomb
caps; DTDs/entities are refused by expat handlers. Document text is never
logged: names, hosts and counts only.
"""
from __future__ import annotations

import asyncio
import io
import os
import posixpath
import re
import shutil
import signal
import stat
import tempfile
import threading
import time
import sys
import unicodedata
import zipfile
from xml.parsers import expat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree as ET

from src.capture.render import render_document

#: The bookmarks channel (``[channels.web_articles]`` in liveness, ``external`` in skim).
SOURCE_TYPE = "article"
DOC_EXTENSIONS = ("docx", "pptx", "xlsx")
#: Bump when extraction output changes: every document is then re-captured once.
EXTRACTOR_VERSION = "spdoc-v1"

GRASP_CMD = os.environ.get("GRASP_MCP", os.path.expanduser("~/.toolbox/bin/grasp-mcp"))
GRASP_ARGS = ["serve", "--quiet"]
CALL_TIMEOUT_S = float(os.environ.get("SHAREPOINT_CALL_TIMEOUT_S", "120"))
#: Whole pass, also capped by the bookmarks job deadline (caller).
RUN_BUDGET_S = float(os.environ.get("SHAREPOINT_RUN_BUDGET_S", "900"))
#: grasp authenticates through Midway; less session than this defers every document.
MIN_SESSION_S = 30 * 60

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_TOTAL = 50 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
#: XML parts parsed into a tree (everything but worksheets, which are streamed).
MAX_XML_TREE_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 100
MAX_CELLS = 200_000
MAX_SLIDES = 1000
MAX_TEXT_CHARS = 2_000_000
#: Captured text per run (bounds ingest/embedding time); the rest is deferred.
MAX_RUN_CHARS = int(os.environ.get("SHAREPOINT_MAX_RUN_CHARS", str(MAX_TEXT_CHARS)))
#: Less extracted text than this is ``empty`` (not ingested).
MIN_TEXT_CHARS = 20
MAX_CANDIDATES = 10
SEARCH_MAX_RESULTS = 50
#: Library-walk fallback bounds (per site, once per run).
MAX_WALK_ITEMS = 300
MAX_WALK_DEPTH = 3
WALK_PAGE_SIZE = 100
#: grasp's download dir when ``--download-dir`` is ignored (``grasp-mcp serve --help``).
GRASP_DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/.config/grasp-mcp/downloads")
TMP_PREFIX = "sb-spdocs-"
#: A leftover temp dir older than this is from a killed run: swept at start.
STALE_TMP_S = 3600

#: Outcomes that are an advisory (never a strike, never the job's exit).
ADVISORY_OUTCOMES = ("unresolved", "too_large", "failed", "empty", "deferred")
#: Bookmarks that can never resolve (no GUID / no name): counted, logged once, not advisory.
PERMANENT_OUTCOME = "skipped_permanent"
PERMANENT_REASONS = ("no_file_name", "no_guid")
#: The only grasp tools this module calls (all read-only).
READ_ONLY_TOOLS = ("search_sharepoint_content", "get_drive_item", "download_sharepoint_file",
                   "list_sharepoint_sites", "list_sharepoint_site_libraries", "list_library_files")

_SITE_ROOTS = ("sites", "teams", "personal")
_DOC_PAGES = ("doc.aspx", "doc2.aspx")
_GUID_RE = re.compile(r"^\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?$")


# --------------------------------------------------------------------------- #
# Bookmark detection (pure)                                                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DocRef:
    """What a bookmark says about its document. ``name`` is None for a
    ``Doc.aspx`` link without ``file=`` (unresolvable by name)."""
    url: str
    host: str
    site: str                 # "sites/cscoe" (casefolded)
    name: str | None
    ext: str | None
    sourcedoc: str | None     # GUID, upper case, no braces
    path: str | None          # decoded path of a direct link (casefolded), else None


def _norm(text):
    return unicodedata.normalize("NFC", text or "").casefold()


def _site_of_path(path):
    """``sites/<name>`` of a decoded URL path (after an optional ``/:w:/r``), or None."""
    parts = [p for p in path.split("/") if p]
    while parts and (parts[0].startswith(":") or parts[0] in ("r", "s")):
        parts = parts[1:]
    if len(parts) >= 2 and parts[0].lower() in _SITE_ROOTS:
        return _norm(f"{parts[0]}/{parts[1]}")
    return None


def _ext(name):
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return ext if ext in DOC_EXTENSIONS else None


def _guid(value):
    match = _GUID_RE.match((value or "").strip())
    return match.group(1).upper() if match else None


def doc_ref(url):
    """A :class:`DocRef` for a SharePoint Office-document bookmark, else None.

    Recognised: ``.../_layouts/15/Doc.aspx`` (or ``doc2.aspx``) with
    ``sourcedoc=`` and/or ``file=<name>.docx|pptx|xlsx``, and direct links whose
    path ends in such a file. Site homes, library views (``AllItems.aspx``),
    SitePages and video are not documents: None (they stay pre-skipped)."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        path = unquote(parsed.path or "")
        query = parse_qs(parsed.query)
    except ValueError:
        return None
    if parsed.scheme != "https" or not (host == "sharepoint.com" or host.endswith(".sharepoint.com")):
        return None
    site = _site_of_path(path)
    if site is None:
        return None
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last.lower() in _DOC_PAGES and "/_layouts/" in path.lower():
        name = (query.get("file") or [None])[0]
        name = posixpath.basename(name) if name else None
        sourcedoc = _guid((query.get("sourcedoc") or [None])[0])
        if name is None:
            if sourcedoc is None:
                return None
            return DocRef(url, host, site, None, None, sourcedoc, None)
        if _ext(name) is None:
            return None
        return DocRef(url, host, site, name, _ext(name), sourcedoc, None)
    if "/_layouts/" in path.lower() or _ext(last) is None:
        return None
    return DocRef(url, host, site, last, _ext(last), None, _norm(path))


# --------------------------------------------------------------------------- #
# Resolution                                                                   #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Resolved:
    drive_id: str
    item_id: str
    name: str
    size: int | None
    modified: str
    web_url: str

    @property
    def source_url(self):
        return f"sharepoint://{self.drive_id}/{self.item_id}"

    @property
    def fingerprint(self):
        return f"{EXTRACTOR_VERSION}:{self.modified}:{self.size}"

    @property
    def change_detectable(self):
        """False without ``lastModifiedDateTime``: a same-size edit would look unchanged, so always re-check."""
        return bool(self.modified)


class Unresolved(Exception):
    """No single driveItem matches the bookmark (``reason`` says why)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _BudgetExceeded(Exception):
    """The pass deadline was reached before or during a grasp call."""


def _fatal(exc):
    """Errors no per-call handler may swallow: the session is gone, or the time is."""
    return isinstance(exc, _BudgetExceeded) or _is_session_failure(exc)


def bounded_call(call, deadline):
    """``call`` with each grasp call capped by the time left before ``deadline``:
    no call starts after it, and one that runs past it is ``_BudgetExceeded``."""
    async def bounded(tool, params):
        left = deadline - time.monotonic()
        if left <= 0:
            raise _BudgetExceeded(tool)
        limit = max(1.0, min(CALL_TIMEOUT_S, left))
        try:
            return await asyncio.wait_for(call(tool, params), timeout=limit)
        except asyncio.TimeoutError:
            if limit < CALL_TIMEOUT_S:
                raise _BudgetExceeded(tool) from None
            raise
    return bounded


def _url_parts(web_url):
    try:
        parsed = urlparse(web_url or "")
        return ((parsed.hostname or "").lower().rstrip("."), unquote(parsed.path or ""), parse_qs(parsed.query))
    except ValueError:
        return "", "", {}


def search_candidates(ref, results):
    """Search hits with exactly the bookmark's file name on its host and site."""
    keep = []
    for hit in results or ():
        if not isinstance(hit, dict) or not hit.get("id") or not hit.get("driveId"):
            continue
        if _norm(hit.get("title")) != _norm(ref.name):
            continue
        host, path, _ = _url_parts(hit.get("webUrl"))
        if host != ref.host or _site_of_path(path) != ref.site:
            continue
        if ref.path is not None and _norm(path) != ref.path:
            continue
        keep.append(hit)
    return keep


def _item_matches(ref, item, renamed=False):
    """The item is the bookmark's document: same host and site, a file, and the
    bookmark's GUID when it has one. The name must match too, except for a
    renamed file, which needs the GUID."""
    if not isinstance(item, dict) or item.get("isFolder"):
        return False
    if renamed and ref.sourcedoc is None:
        return False
    if not renamed and _norm(item.get("name")) != _norm(ref.name):
        return False
    if _ext(item.get("name")) is None:
        return False
    host, path, query = _url_parts(item.get("webUrl"))
    if host != ref.host or _site_of_path(path) != ref.site:
        return False
    if ref.sourcedoc is not None:
        return _guid((query.get("sourcedoc") or [None])[0]) == ref.sourcedoc
    return True


def _queries(name):
    """Search queries, in order: the file name, then its stem (grasp's search
    sometimes misses a name with its extension, or answers 400 on one)."""
    stem = name.rsplit(".", 1)[0]
    return [name] + ([stem] if stem and stem != name else [])


def _site_hits(ref, results):
    """Any-title hits on the bookmark's host and site (for a GUID-only match of a renamed file)."""
    keep = []
    for hit in results or ():
        if isinstance(hit, dict) and hit.get("id") and hit.get("driveId"):
            host, path, _ = _url_parts(hit.get("webUrl"))
            if host == ref.host and _site_of_path(path) == ref.site:
                keep.append(hit)
    return keep


def _to_resolved(drive_id, item_id, item):
    size = item.get("size")
    return Resolved(drive_id, item_id, str(item.get("name")),
                    int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else None,
                    str(item.get("lastModifiedDateTime") or ""), str(item.get("webUrl") or ""))


def _names(ref, title):
    """The names a bookmark gives its document: ``file=`` and the Chrome title (if an Office file name)."""
    names = {_norm(ref.name)} if ref.name else set()
    if title and _ext(title.strip()):
        names.add(_norm(title.strip()))
    return names


async def resolve(call, ref, *, title=None, walker=None):
    """The one driveItem the bookmark points at; raises :class:`Unresolved`.

    Per query (file name, then stem): exact-title hits on the bookmark's site;
    when none of them is the document and the bookmark carries a ``sourcedoc``
    GUID, the other same-site hits whose item GUID matches (a renamed file). A
    per-call tool error just tries the next query / candidate; a session failure
    (auth, closed transport) or the deadline propagates. Candidates are bounded.
    A GUID bookmark that search does not find goes to ``walker`` (a
    :class:`SiteWalker`: library listing, GUID and exact name mandatory)."""
    if ref.sourcedoc is None and ref.path is None:
        raise Unresolved("no_guid")     # a Doc.aspx link by name alone could be any same-named copy
    if ref.name is None:
        found = await walker.find(call, ref, _names(ref, title)) if walker is not None else None
        if found is None:
            raise Unresolved("no_file_name")
        return found
    matches, tried, errors = {}, set(), 0

    async def check(hits, renamed):
        for hit in hits:
            if hit["id"] in tried:
                continue
            tried.add(hit["id"])
            try:
                item = await call("get_drive_item", {"driveId": hit["driveId"], "itemId": hit["id"]})
            except Exception as exc:  # noqa: BLE001 - a stale search hit (404) is just not a match
                if _fatal(exc):
                    raise
                continue
            if _item_matches(ref, item, renamed=renamed):
                matches[(hit["driveId"], str(item.get("id") or hit["id"]))] = item

    queries = _queries(ref.name)
    for query in queries:
        try:
            reply = await call("search_sharepoint_content", {"query": query, "entityTypes": ["driveItem"],
                                                             "maxResults": SEARCH_MAX_RESULTS})
        except Exception as exc:  # noqa: BLE001 - classified here
            if _fatal(exc):
                raise
            errors += 1
            continue
        results = (reply or {}).get("results") if isinstance(reply, dict) else None
        exact = search_candidates(ref, results)
        if len(exact) > MAX_CANDIDATES:
            raise Unresolved(f"ambiguous ({len(exact)} candidates)")
        await check(exact, renamed=False)
        if not matches and ref.sourcedoc is not None and ref.path is None:
            ids = {h["id"] for h in exact}
            await check([h for h in _site_hits(ref, results) if h["id"] not in ids][:MAX_CANDIDATES], renamed=True)
        if matches:
            break
    if not matches and ref.sourcedoc is not None and walker is not None:
        found = await walker.find(call, ref, _names(ref, title))
        if found is not None:
            return found
    if errors == len(queries):
        raise UnsafeDocument("search failed")
    if len(matches) != 1:
        raise Unresolved("not_found" if not matches else f"ambiguous ({len(matches)} matches)")
    (drive_id, item_id), item = next(iter(matches.items()))
    return _to_resolved(drive_id, item_id, item)


def _site_terms(ref):
    """``list_sharepoint_sites`` search terms for the bookmark's site (grasp finds
    "AmazonQ CSS" but not "AmazonQ-CSS")."""
    name = ref.site.split("/", 1)[1]
    spaced = re.sub(r"[-_.]+", " ", name).strip()
    return [name] + ([spaced] if spaced and spaced != name else [])


class SiteWalker:
    """Read-only library walk of a bookmark's site, at most once per site per run.

    ``list_sharepoint_sites`` -> the one site whose ``webUrl`` is the bookmark's
    host and ``/sites/<x>``; ``list_sharepoint_site_libraries``; then a
    breadth-first ``list_library_files`` over each library (``MAX_WALK_ITEMS``
    items in all, folders to ``MAX_WALK_DEPTH``). Listed files are indexed by the
    ``sourcedoc`` GUID of their ``webUrl``. A tool error ends that site's walk
    (what was listed is kept); a session failure or the deadline propagates."""

    def __init__(self, log=print):
        self._index = {}     # (host, site) -> {GUID: (drive_id, item)}
        self.calls = 0
        self.log = log

    async def find(self, call, ref, names):
        """The :class:`Resolved` item with the bookmark's GUID and one of ``names``, else None."""
        if ref.sourcedoc is None or not names:
            return None
        key = (ref.host, ref.site)
        if key not in self._index:
            self._index[key] = await self._walk(call, ref)
        hit = self._index[key].get(ref.sourcedoc)
        if hit is None or _norm(hit[1].get("name")) not in names:
            return None
        drive_id, listed = hit
        try:
            item = await call("get_drive_item", {"driveId": drive_id, "itemId": listed["id"]})
        except Exception as exc:  # noqa: BLE001 - a vanished item is just not found
            if _fatal(exc):
                raise
            return None
        if not _item_matches(ref, item, renamed=True) or _norm(item.get("name")) not in names:
            return None
        return _to_resolved(drive_id, str(item.get("id") or listed["id"]), item)

    async def _list(self, call, tool, params):
        self.calls += 1
        reply = await call(tool, params)
        return reply if isinstance(reply, dict) else {}

    async def _walk(self, call, ref):
        index = {}
        try:
            site_id = None
            for term in _site_terms(ref):
                reply = await self._list(call, "list_sharepoint_sites", {"searchTerm": term, "maxResults": 25})
                ids = {s.get("id") for s in reply.get("sites") or () if isinstance(s, dict) and s.get("id")
                       and _url_parts(s.get("webUrl"))[0] == ref.host
                       and _site_of_path(_url_parts(s.get("webUrl"))[1]) == ref.site}
                if len(ids) == 1:
                    site_id = ids.pop()
                    break
            if site_id is None:
                return index
            libs = await self._list(call, "list_sharepoint_site_libraries", {"siteId": site_id, "maxResults": 100})
            seen = 0
            for lib in libs.get("libraries") or ():
                if not isinstance(lib, dict) or not lib.get("id"):
                    continue
                queue = [("root", 0)]
                while queue and seen < MAX_WALK_ITEMS:
                    folder, depth = queue.pop(0)
                    token = None
                    while seen < MAX_WALK_ITEMS:
                        params = {"siteId": site_id, "libraryId": lib["id"], "folderId": folder,
                                  "maxResults": max(1, min(WALK_PAGE_SIZE, MAX_WALK_ITEMS - seen))}
                        if token:
                            params["pageToken"] = token
                        page = await self._list(call, "list_library_files", params)
                        for item in page.get("items") or ():
                            if seen >= MAX_WALK_ITEMS:
                                break
                            if not isinstance(item, dict) or not item.get("id"):
                                continue
                            seen += 1
                            if item.get("isFolder"):
                                if depth + 1 <= MAX_WALK_DEPTH:
                                    queue.append((item["id"], depth + 1))
                                continue
                            guid = _guid((_url_parts(item.get("webUrl"))[2].get("sourcedoc") or [None])[0])
                            if guid:
                                index.setdefault(guid, (lib["id"], item))
                        token = page.get("nextPageToken") or page.get("NextPageToken")
                        if not token:
                            break
                if seen >= MAX_WALK_ITEMS:
                    break
        except Exception as exc:  # noqa: BLE001 - a listing error ends this site's walk
            if _fatal(exc):
                raise
            self.log(f"  SharePoint library walk of {ref.site} stopped ({type(exc).__name__})")
        return index


# --------------------------------------------------------------------------- #
# Extraction (pure, stdlib, zip-bomb safe)                                     #
# --------------------------------------------------------------------------- #
class UnsafeDocument(ValueError):
    """The package breaks a safety cap or is not a readable OOXML package."""


_DTD_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


class _Package:
    """Bounded member reads: the declared sizes are checked up front, and the
    bytes actually inflated are counted (a lying header cannot exceed the caps)."""

    def __init__(self, data):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self._zip.infolist()
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise UnsafeDocument(f"not an OOXML package ({type(exc).__name__})") from None
        if len(infos) > MAX_MEMBERS:
            raise UnsafeDocument(f"too many members ({len(infos)})")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_TOTAL:
            raise UnsafeDocument("declared uncompressed size over the cap")
        if any(i.file_size > MAX_MEMBER_BYTES for i in infos):
            raise UnsafeDocument("a member's declared size is over the cap")
        if any(i.flag_bits & 0x1 for i in infos):
            raise UnsafeDocument("encrypted package")
        self._names = {i.filename: i for i in infos}
        self._read_total = 0

    def has(self, name):
        return name in self._names

    def read(self, name):
        info = self._names.get(name)
        if info is None:
            return None
        try:
            with self._zip.open(info) as handle:
                data = handle.read(MAX_MEMBER_BYTES + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as exc:
            raise UnsafeDocument(f"unreadable member ({type(exc).__name__})") from None
        if len(data) > MAX_MEMBER_BYTES:
            raise UnsafeDocument("a member inflates over the cap")
        self._read_total += len(data)
        if self._read_total > MAX_UNCOMPRESSED_TOTAL:
            raise UnsafeDocument("inflated size over the cap")
        return data

    def xml(self, name, required=False):
        data = self.read(name)
        if data is None:
            if required:
                raise UnsafeDocument(f"missing part {name}")
            return None
        if len(data) > MAX_XML_TREE_BYTES:
            raise UnsafeDocument("XML part over the tree-parse cap")
        return parse_xml(data)


def _refuse(*_args):
    raise UnsafeDocument("XML part declares a DTD/entity")


def check_xml(data):
    """Refuse DTDs and entity declarations (OOXML never has them) and non-UTF-8
    parts: a byte regex, then a streaming expat pass whose DOCTYPE / entity
    handlers raise (so the guard does not depend on the regex seeing through
    the part's declared encoding). No tree is built here."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:200]:
        raise UnsafeDocument("non-UTF-8 XML part")
    if _DTD_RE.search(data):
        raise UnsafeDocument("XML part declares a DTD/entity")
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    try:
        for start in range(0, max(len(data), 1), 1 << 20):
            parser.Parse(data[start:start + (1 << 20)], False)
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise UnsafeDocument(f"malformed XML ({exc.code})") from None


def parse_xml(data):
    """ElementTree root of an OOXML part, after :func:`check_xml`."""
    check_xml(data)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UnsafeDocument(f"malformed XML ({exc.code})") from None


def iter_rows(data):
    """Stream the ``<row>`` elements of a worksheet part (after :func:`check_xml`):
    each row is yielded complete, then cleared and detached, so memory stays
    bounded by one row whatever the sheet's size. Stop iterating to stop parsing."""
    check_xml(data)
    pull = ET.XMLPullParser(events=("start", "end"))
    stack = []
    try:
        for start in range(0, max(len(data), 1), 1 << 20):
            pull.feed(data[start:start + (1 << 20)])
            for event, el in pull.read_events():
                if event == "start":
                    stack.append(el)
                    continue
                stack.pop()
                if _local(el.tag) == "row":
                    yield el
                    el.clear()
                    if stack:
                        stack[-1].remove(el)
        pull.close()
    except ET.ParseError as exc:
        raise UnsafeDocument(f"malformed XML ({exc.code})") from None


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(el, local_name):
    """Attribute by local name, any namespace (transitional or strict OOXML)."""
    for key, value in el.attrib.items():
        if _local(key) == local_name:
            return value
    return None


def _rid(el):
    """The relationship id (``r:id``): a *namespaced* ``id`` attribute (``sldId`` also has a plain ``id``)."""
    for key, value in el.attrib.items():
        if key.startswith("{") and _local(key) == "id":
            return value
    return None


def _rels(pkg, rels_path, base_dir):
    """``{Id: (type local name, part path)}`` of a .rels part; external and
    out-of-package targets are dropped."""
    root = pkg.xml(rels_path)
    out = {}
    for rel in root.iter() if root is not None else ():
        if _local(rel.tag) != "Relationship" or (rel.get("TargetMode") or "").lower() == "external":
            continue
        target = rel.get("Target") or ""
        part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base_dir, target))
        if part.startswith("..") or part.startswith("/"):
            continue
        out[rel.get("Id")] = ((rel.get("Type") or "").rsplit("/", 1)[-1], part)
    return out


def _rels_path(part):
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _cell(text):
    return " ".join((text or "").split()).replace("|", "\\|")


# docx ----------------------------------------------------------------------- #
def _docx_para(p):
    parts = []
    for el in p.iter():
        name = _local(el.tag)
        if name == "t":
            parts.append(el.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _docx_heading(p):
    for el in p:
        if _local(el.tag) == "pPr":
            for sub in el:
                if _local(sub.tag) == "pStyle":
                    style = (_attr(sub, "val") or "").replace(" ", "").lower()
                    if style == "title":
                        return 1
                    match = re.fullmatch(r"heading(\d)", style)
                    if match:
                        return int(match.group(1))
    return 0


def _docx_table(tbl):
    rows = []
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        cells = [_cell(" ".join(_docx_para(p) for p in tc.iter() if _local(p.tag) == "p"))
                 for tc in tr if _local(tc.tag) == "tc"]
        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _docx_blocks(el, out):
    for child in el:
        name = _local(child.tag)
        if name == "p":
            text = _docx_para(child)
            if text:
                level = _docx_heading(child)
                out.append(("#" * min(level + 1, 6) + " " + text) if level else text)
        elif name == "tbl":
            table = _docx_table(child)
            if table:
                out.append(table)
        elif name in ("sdt", "sdtContent", "customXml", "ins", "smartTag"):
            _docx_blocks(child, out)


def extract_docx(pkg):
    root = pkg.xml("word/document.xml", required=True)
    body = next((c for c in root if _local(c.tag) == "body"), root)
    out = []
    _docx_blocks(body, out)
    return "\n\n".join(out)


# pptx ----------------------------------------------------------------------- #
_PPTX_SKIP_PH = {"sldNum", "dt", "ftr", "sldImg", "hdr"}


def _ph_type(shape):
    for el in shape.iter():
        if _local(el.tag) == "ph":
            return el.get("type") or "body"
    return None


def _shape_paragraphs(shape):
    out = []
    for p in shape.iter():
        if _local(p.tag) != "p":
            continue
        parts = []
        for el in p.iter():
            name = _local(el.tag)
            if name == "t":
                parts.append(el.text or "")
            elif name == "br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            out.append(text)
    return out


def _slide_texts(root, *, notes=False):
    out = []
    for shape in root.iter():
        if _local(shape.tag) not in ("sp", "graphicFrame"):
            continue
        ph = _ph_type(shape)
        if notes and ph != "body":
            continue
        if ph in _PPTX_SKIP_PH:
            continue
        out.extend(_shape_paragraphs(shape))
    return out


def extract_pptx(pkg):
    pres = pkg.xml("ppt/presentation.xml", required=True)
    rels = _rels(pkg, "ppt/_rels/presentation.xml.rels", "ppt")
    order = [_rid(el) for el in pres.iter() if _local(el.tag) == "sldId"]
    out = []
    for n, rid in enumerate(order[:MAX_SLIDES], start=1):
        kind, part = rels.get(rid, ("", ""))
        if kind != "slide":
            continue
        slide = pkg.xml(part)
        if slide is None:
            continue
        section = [f"## Slide {n}"] + _slide_texts(slide)
        for nkind, npart in _rels(pkg, _rels_path(part), posixpath.dirname(part)).values():
            if nkind == "notesSlide":
                notes_root = pkg.xml(npart)
                notes = _slide_texts(notes_root, notes=True) if notes_root is not None else []
                if notes:
                    section += ["", "Notes:"] + notes
        out.append("\n".join(section))
    if len(order) > MAX_SLIDES:
        out.append(f"[{len(order) - MAX_SLIDES} more slides not extracted]")
    return "\n\n".join(out)


# xlsx ----------------------------------------------------------------------- #
def _shared_strings(pkg):
    root = pkg.xml("xl/sharedStrings.xml")
    strings = []
    for si in root if root is not None else ():
        if _local(si.tag) != "si":
            continue
        parts = []
        for child in si:
            name = _local(child.tag)
            if name == "t":
                parts.append(child.text or "")
            elif name == "r":
                parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
        strings.append("".join(parts))
    return strings


def _col_index(ref):
    letters = re.match(r"[A-Za-z]+", ref or "")
    if not letters:
        return None
    n = 0
    for ch in letters.group(0).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _cell_value(c, strings):
    kind = c.get("t") or "n"
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
    v = next((el.text for el in c if _local(el.tag) == "v"), None)
    if v is None:
        return ""
    if kind == "s":
        try:
            return strings[int(v)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if v.strip() == "1" else "FALSE"
    return v


def extract_xlsx(pkg):
    wb = pkg.xml("xl/workbook.xml", required=True)
    rels = _rels(pkg, "xl/_rels/workbook.xml.rels", "xl")
    strings = _shared_strings(pkg)
    out, cells_left = [], MAX_CELLS
    for sheet in wb.iter():
        if _local(sheet.tag) != "sheet":
            continue
        kind, part = rels.get(_rid(sheet), ("", ""))
        if kind != "worksheet":
            continue
        if cells_left <= 0:
            out.append(f"[sheet {_cell(sheet.get('name') or part)} not extracted: cell cap]")
            continue
        data = pkg.read(part)
        if data is None:
            continue
        lines, rows, truncated = [f"## Sheet: {_cell(sheet.get('name') or part)}"], 0, False
        for row in iter_rows(data):
            values = {}
            for pos, c in enumerate(el for el in row if _local(el.tag) == "c"):
                col = _col_index(c.get("r"))
                col = pos if col is None else col
                if col >= MAX_SHEET_COLS:
                    continue
                value = _cell(_cell_value(c, strings))
                if value:
                    values[col] = value
            if not values:
                continue
            if rows >= MAX_SHEET_ROWS or cells_left <= 0:
                truncated = True
                break        # stop parsing at the caps
            rows += 1
            cells_left -= len(values)
            lines.append("| " + " | ".join(values.get(i, "") for i in range(max(values) + 1)) + " |")
        if truncated:
            lines.append("[further rows not extracted]")
        if rows:
            out.append("\n".join(lines))
    return "\n\n".join(out)


_EXTRACTORS = {"docx": extract_docx, "pptx": extract_pptx, "xlsx": extract_xlsx}


def extract_text(data, ext):
    """Plain/markdown text of a .docx/.pptx/.xlsx package (bytes); raises
    :class:`UnsafeDocument` on a cap breach or a package that is not OOXML."""
    if ext not in _EXTRACTORS:
        raise UnsafeDocument(f"unsupported format {ext!r}")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise UnsafeDocument("file over the download cap")
    text = _EXTRACTORS[ext](_Package(data))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n\n[truncated]"
    return text


def render_markdown(resolved, ref, text, ext, date=None):
    """The captured document in the standard metadata-header format."""
    return render_document(resolved.name, {
        "Source": resolved.source_url,
        "Type": "sharepoint-document",
        "Date": date or datetime.now().strftime("%Y-%m-%d"),
        "Format": ext,
        "Modified": resolved.modified or None,
        "Link": resolved.web_url or None,
        "Bookmark": ref.url,
        "fetched_via": "grasp-mcp",
    }, text)


# --------------------------------------------------------------------------- #
# The pass                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class DocResult:
    url: str
    name: str
    outcome: str = "deferred"
    reason: str | None = None
    size: int | None = None
    chars: int = 0
    seconds: float = 0.0
    source_url: str | None = None
    fingerprint: str | None = None
    markdown: str | None = field(default=None, repr=False)


class _Terminated(SystemExit):
    """SIGTERM during the pass: unwinds through the cleanup ``finally``. A
    ``SystemExit`` (like ``browser_fetch._SigtermExit``) so asyncio re-raises it
    straight out of the loop from whichever task it lands in (the MCP SDK's anyio
    reader/writer tasks included) instead of wrapping it in a ``BaseExceptionGroup``."""

    def __init__(self):
        super().__init__(128 + signal.SIGTERM)


class _Abort(Exception):
    """A session-wide failure (auth / transport): the rest of the pass is deferred."""


def _grasp_env():
    """The SDK's minimal server env with the toolbox and Homebrew on PATH (launchd's PATH lacks them)."""
    from mcp.client.stdio import get_default_environment
    env = dict(get_default_environment())
    env["PATH"] = os.pathsep.join([os.path.expanduser("~/.toolbox/bin"), "/opt/homebrew/bin",
                                   env.get("PATH") or "/usr/bin:/bin"])
    return env


def default_client(download_dir):
    from src.capture.mcp_client import McpClient
    return McpClient(GRASP_CMD, [*GRASP_ARGS, "--download-dir", download_dir],
                     call_timeout=CALL_TIMEOUT_S, env=_grasp_env())


def sweep_stale_dirs(root=None, now=None):
    """Remove this user's ``sb-spdocs-*`` temp dirs left by a killed run."""
    root = root or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(TMP_PREFIX):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime > STALE_TMP_S:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed


def _read_download(download_dir, save_path):
    """The downloaded bytes: a regular file (no symlink) inside the dir, within the cap."""
    path = os.path.join(download_dir, save_path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise UnsafeDocument("download produced no file") from None
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeDocument("download is not a regular file")
    if st.st_size > MAX_DOWNLOAD_BYTES:
        raise UnsafeDocument("downloaded file over the cap")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_DOWNLOAD_BYTES + 1)


def _is_session_failure(exc):
    from src.capture.mcp_client import is_auth_error, is_connection_closed, McpTransportError
    return is_auth_error(exc) or is_connection_closed(exc) or isinstance(exc, McpTransportError)


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _unlink_stray_default_download(save_path, before):
    """grasp ignored ``--download-dir``: remove the file it wrote to its default
    dir, only if that file did not exist before this call."""
    path = os.path.join(GRASP_DEFAULT_DOWNLOAD_DIR, save_path)
    st = _lstat(path)
    if before is None and st is not None and stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
        try:
            os.unlink(path)
        except OSError:
            pass


async def process_docs(call, docs, results, *, download_dir, fingerprints, deadline, log=print, walker=None):
    """Resolve, change-check, download and extract each ``(name, url, ref)``;
    fills ``results`` (url -> DocResult) in place. Every grasp call is bounded by
    ``deadline`` (``reason="budget"``), captured text by ``MAX_RUN_CHARS``. Raises
    :class:`_Abort` on a session-wide failure (the caller defers what is left)."""
    seen = {}
    call = bounded_call(call, deadline)
    walker = walker if walker is not None else SiteWalker(log)
    captured_chars = 0
    for index, (bookmark_name, url, ref) in enumerate(docs, start=1):
        result = results[url]
        if time.monotonic() >= deadline:
            result.reason = "budget"
            continue
        if ref.sourcedoc is None and ref.path is None:
            result.outcome, result.reason = PERMANENT_OUTCOME, "no_guid"   # no call: never resolvable
            continue
        started = time.monotonic()
        try:
            resolved = await resolve(call, ref, title=bookmark_name, walker=walker)
            ext = _ext(resolved.name) or ref.ext       # a renamed file keeps its own format
            save_path = f"{index:03d}.{ext}"
            result.name, result.size = resolved.name, resolved.size
            result.source_url, result.fingerprint = resolved.source_url, resolved.fingerprint
            if resolved.source_url in seen:
                result.outcome, result.reason = "duplicate", f"same document as bookmark {seen[resolved.source_url]}"
            elif resolved.change_detectable and fingerprints.get(resolved.source_url) == resolved.fingerprint:
                result.outcome = "unchanged"
            elif resolved.size is None or resolved.size > MAX_DOWNLOAD_BYTES:
                result.outcome, result.reason = "too_large", "size unknown" if resolved.size is None else "over 25 MB"
            elif captured_chars >= MAX_RUN_CHARS:
                result.reason = "run_char_cap"       # stays deferred: retried next run
            else:
                before = _lstat(os.path.join(GRASP_DEFAULT_DOWNLOAD_DIR, save_path))
                await call("download_sharepoint_file", {"fileId": resolved.item_id, "driveId": resolved.drive_id,
                                                        "savePath": save_path, "maxSizeBytes": MAX_DOWNLOAD_BYTES})
                try:
                    data = _read_download(download_dir, save_path)
                    text = extract_text(data, ext)
                except UnsafeDocument as exc:
                    if str(exc) == "download produced no file":
                        _unlink_stray_default_download(save_path, before)
                    raise
                finally:
                    try:
                        os.unlink(os.path.join(download_dir, save_path))
                    except OSError:
                        pass
                result.chars = len(text)
                if len(text) < MIN_TEXT_CHARS:
                    result.outcome, result.reason = "empty", "no text extracted"
                elif captured_chars and captured_chars + len(text) > MAX_RUN_CHARS:
                    result.reason = "run_char_cap"
                else:
                    captured_chars += len(text)
                    result.outcome = "captured"
                    result.markdown = render_markdown(resolved, ref, text, ext)
            seen.setdefault(resolved.source_url, index)
        except Unresolved as exc:
            if exc.reason in PERMANENT_REASONS:
                result.outcome, result.reason = PERMANENT_OUTCOME, exc.reason
            else:
                result.outcome, result.reason = "unresolved", exc.reason
        except UnsafeDocument as exc:
            result.outcome, result.reason = "failed", str(exc)
        except _BudgetExceeded:
            result.outcome, result.reason = "deferred", "budget"
        except Exception as exc:  # noqa: BLE001 - one document never aborts the pass
            if _is_session_failure(exc):
                result.reason = f"session: {type(exc).__name__}"
                raise _Abort(type(exc).__name__) from None
            result.outcome, result.reason = "failed", type(exc).__name__
        finally:
            result.seconds = time.monotonic() - started
            if result.outcome != PERMANENT_OUTCOME:
                log(f"  SharePoint [{index}/{len(docs)}] {result.name[:60]} ({ref.host}): {result.outcome}"
                    f"{f' ({result.reason})' if result.reason else ''} size={result.size} chars={result.chars} "
                    f"{result.seconds:.1f}s")


def _main_thread():
    return threading.current_thread() is threading.main_thread()


def _install_sigterm(handler=None):
    """SIGTERM -> ``_Terminated`` (or ``handler``); main thread only. True when installed."""
    if not _main_thread():
        return False

    def terminate(signum, frame):
        raise _Terminated()
    try:
        signal.signal(signal.SIGTERM, handler or terminate)
        return True
    except (ValueError, OSError):
        return False


def capture_bookmarked_docs(bookmarks, *, session_left, save=True, budget_s=None, client_factory=None,
                            fingerprints=None, ingest=None, log=print, tmp_root=None):
    """One grasp session over the SharePoint Office-document bookmarks.

    ``bookmarks`` is ``[(name, url)]`` (non-documents are ignored);
    ``session_left`` the Midway session seconds left (None = no session). Returns
    ``{"counts": Counter(outcome), "results": {url: DocResult}, "cause": str|None,
    "ingest": stats|None}``. Never raises an ``Exception``: grasp launch/auth
    failures defer every document (advisory). ``save=False`` reads the stored
    fingerprints only when given and writes nothing (no DB, no files kept)."""
    docs = [(n, u, ref) for n, u in bookmarks for ref in [doc_ref(u)] if ref is not None]
    results = {u: DocResult(u, ref.name or n or "(unnamed)") for n, u, ref in docs}
    summary = {"counts": Counter(), "results": results, "cause": None, "ingest": None}
    if not docs:
        return summary
    # Never resolvable, decided without grasp: no GUID (B1), or no name to match the GUID's item by.
    for n, u, ref in docs:
        if ref.sourcedoc is None and ref.path is None:
            results[u].outcome, results[u].reason = PERMANENT_OUTCOME, "no_guid"
        elif ref.name is None and not _names(ref, n):
            results[u].outcome, results[u].reason = PERMANENT_OUTCOME, "no_file_name"
    pending = [d for d in docs if results[d[1]].outcome != PERMANENT_OUTCOME]
    budget_s = RUN_BUDGET_S if budget_s is None else budget_s
    if not pending:
        pass
    elif session_left is None or session_left < MIN_SESSION_S:
        summary["cause"] = "Midway session missing or under 30 min — run mwinit"
    elif budget_s <= 0:
        summary["cause"] = "no time left in the job deadline"
    if pending and fingerprints is None and summary["cause"] is None:
        if save:
            try:
                from src.db import get_capture_fingerprints
                fingerprints = {u: fp for u, fp in get_capture_fingerprints(SOURCE_TYPE).items()
                                if u and u.startswith("sharepoint://")}
            except Exception as exc:  # noqa: BLE001 - no inventory: defer rather than re-download blindly
                summary["cause"] = f"stored fingerprints unreadable ({type(exc).__name__})"
        else:
            fingerprints = {}
    deadline = time.monotonic() + max(0.0, budget_s)
    if pending and summary["cause"] is None:
        _run_session(pending, results, summary, fingerprints, deadline, client_factory or default_client, log, tmp_root)
    if save and any(r.outcome == "captured" for r in results.values()):
        summary["ingest"] = _ingest(results, ingest, log, deadline)
    skipped = [r for r in results.values() if r.outcome == PERMANENT_OUTCOME]
    if skipped:
        log(f"INFO: {len(skipped)} SharePoint bookmark(s) can never resolve, skipped (not an advisory): "
            + "; ".join(f"{r.name[:60]} ({r.reason})" for r in skipped))
    for r in results.values():
        if r.outcome == "deferred" and r.reason is None:
            r.reason = summary["cause"] or "not reached"
    summary["counts"] = Counter(r.outcome for r in results.values())
    for r in results.values():
        r.markdown = None   # the text lives in the DB only
    return summary


def _run_session(docs, results, summary, fingerprints, deadline, client_factory, log, tmp_root):
    sweep_stale_dirs(tmp_root)
    previous = signal.getsignal(signal.SIGTERM) if _main_thread() else None
    installed = False
    tmp = None
    client = None
    terminated = False
    clean = False
    try:
        installed = _install_sigterm()     # inside the try: a SIGTERM right after still unwinds here
        tmp = tempfile.mkdtemp(prefix=TMP_PREFIX, dir=tmp_root)
        os.chmod(tmp, 0o700)
        client = client_factory(tmp)

        async def run():
            try:
                await client.__aenter__()
            except Exception as exc:  # noqa: BLE001 - launch/auth failure: everything deferred
                summary["cause"] = f"grasp-mcp did not start ({type(exc).__name__})"
                return
            try:
                await process_docs(client.call_json, docs, results, download_dir=tmp,
                                   fingerprints=fingerprints, deadline=deadline, log=log)
            except _Abort as exc:
                summary["cause"] = f"grasp-mcp session failed mid-pass ({exc})"
            finally:
                try:
                    await client.__aexit__(None, None, None)
                except BaseExceptionGroup as group:
                    # A SIGTERM that landed in an SDK (anyio) task already left the loop as
                    # SystemExit; its copy in the task group, re-raised here during shutdown, is noise.
                    if group.subgroup(_Terminated) is None:
                        raise
        asyncio.run(run())
        if any(r.reason == "budget" for r in results.values()):
            summary["cause"] = summary["cause"] or "the SharePoint time budget ran out"
        clean = summary["cause"] is None
    except _Terminated:
        terminated = True
    except Exception as exc:  # noqa: BLE001 - advisory: the bookmarks job's exit is unaffected
        summary["cause"] = f"SharePoint pass error ({type(exc).__name__})"
    finally:
        # A second SIGTERM during cleanup is only recorded: rmtree and the restore always finish.
        late = []
        if installed:
            _install_sigterm(lambda signum, frame: late.append(signum))
        try:
            if not clean:
                _kill_servers(client)     # every abnormal exit (incl. one not caught above)
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            if installed:
                signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
        terminated = terminated or bool(late)
    if terminated:
        summary["cause"] = summary["cause"] or "SIGTERM"
        _chain_sigterm(previous)


def _chain_sigterm(previous):
    """The job's own SIGTERM semantics, after cleanup: a callable previous handler
    is called (it owns termination; if it returns, the pass just ends deferred);
    otherwise flush stdout and ``SystemExit(143)`` (atexit hooks still run)."""
    if callable(previous):
        previous(signal.SIGTERM, None)
        return
    if previous == signal.SIG_IGN:
        return
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    raise SystemExit(128 + signal.SIGTERM)


def _kill_servers(client):
    for pid in getattr(client, "server_pids", None) or ():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _ingest(results, ingest, log, deadline=None):
    """Store the captured documents one at a time; once ``deadline`` has passed
    the rest are deferred (no fingerprint stored: retried next run)."""
    captured = [r for r in results.values() if r.outcome == "captured" and r.markdown]
    if not captured:
        return None
    stats = {"processed": 0, "skipped": 0, "failed": 0}
    try:
        from src.capture.framework import ingest_with_metadata, run_channel
        for n, r in enumerate(captured):
            if deadline is not None and time.monotonic() >= deadline:
                for rest in captured[n:]:
                    rest.outcome, rest.reason = "deferred", "budget"
                log(f"  SharePoint: {len(captured) - n} captured document(s) deferred: ingest budget")
                break
            metadata = {r.source_url: {"source_fingerprint": r.fingerprint, "bookmark_url": r.url}}
            one = run_channel(SOURCE_TYPE, [(r.source_url, r.markdown)],
                              ingest=ingest_with_metadata(metadata, refresh=True, ingest=ingest))
            for key in stats:
                stats[key] += int(one.get(key) or 0)
    except Exception as exc:  # noqa: BLE001 - storage failure is per-run advisory, never the job's exit
        log(f"WARNING: SharePoint documents could not be stored ({type(exc).__name__})")
        stats = {"processed": stats["processed"], "skipped": stats["skipped"],
                 "failed": len(captured) - stats["processed"] - stats["skipped"]}
    if stats.get("failed"):
        # No fingerprint was stored for them, so the next run retries them.
        log(f"WARNING: {stats['failed']} SharePoint document(s) failed to store")
    return stats
