"""grasp-mcp (Microsoft Graph) throttling and the SharePoint library walk.

Hermetic: no network, no DB, no MCP server process. grasp answers Graph
throttling as a tool error like "Rate limited by Microsoft Graph API (HTTP 429)
... Wait 16 seconds ...", which McpClient must back off on (bounded), and the
AmazonQ-CSS site (4 libraries, folders 4 deep, ~156 items in Documents) must be
walkable within the caps; the walk index is kept across runs in a private sidecar.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.capture import mcp_client as mc  # noqa: E402
from src.capture import sharepoint_docs as sp  # noqa: E402
from src.capture.mcp_client import (  # noqa: E402
    McpClient, McpRateLimited, McpToolError, classify_error, is_rate_limited, rate_limit_retry_after,
)

GRASP_429 = ("Error: Rate limited by Microsoft Graph API (HTTP 429). Too many requests in a short period. "
             "Wait 16 seconds before retrying this tool.")
HOST = "https://amazon.sharepoint.com"
GUID = "548392D4-60B8-462C-93C9-38E1BCAF4338"
OTHER = "4D879818-28E5-41DA-A83A-99D3684A5F24"
SECRET = "ZEBRA-SECRET-PHRASE"


# --------------------------------------------------------------------------- #
# McpClient: grasp's 429 wording                                               #
# --------------------------------------------------------------------------- #
def _reply(text, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=is_error)


class FakeSession:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    async def call_tool(self, tool, params):
        self.calls.append((tool, params))
        item = self.script.pop(0) if self.script else _reply('{"ok": true}')
        if callable(item):
            item = item(tool, params)
        if isinstance(item, BaseException):
            raise item
        return item


def _client(session, **kw):
    client = McpClient("/nonexistent/server", [], reconnect_backoff=0, **kw)
    client._session = session
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(mc.asyncio, "sleep", fake_sleep)
    return slept


def test_grasp_graph_429_is_recognised_with_its_wait():
    assert is_rate_limited(GRASP_429)
    assert rate_limit_retry_after(GRASP_429) == 16.0
    assert is_rate_limited("Request failed: HTTP 429") and is_rate_limited("429 Too Many Requests")
    assert rate_limit_retry_after("Retry-After: 16") == 16.0
    assert rate_limit_retry_after("please wait for 3 seconds") == 3.0
    assert rate_limit_retry_after("Wait 2 minutes") == 120.0
    assert rate_limit_retry_after("Retry after 38s.") == 38.0          # slack-mcp, unchanged
    assert rate_limit_retry_after("Retry after 250ms") == 0.25
    assert rate_limit_retry_after("waiting on the server") is None
    assert not is_rate_limited("rate limited")                           # free-form text: ordinary tool error
    assert classify_error(McpRateLimited(GRASP_429, 16.0)) == "error"     # never transport/auth


def test_grasp_429_is_waited_out_then_retried(no_sleep):
    session = FakeSession(_reply(GRASP_429, is_error=True), _reply('{"items": []}'))
    client = _client(session, rate_limit_retries=3, rate_limit_max_wait=60)
    assert asyncio.run(client.call_json("list_library_files")) == {"items": []}
    assert no_sleep == [16.0] and client.rate_limit_hits == 1 and len(session.calls) == 2


def test_one_wait_is_capped(no_sleep):
    session = FakeSession(_reply("Rate limited by Microsoft Graph API (HTTP 429). Wait 600 seconds.", True),
                          _reply('{"ok": 1}'))
    client = _client(session, rate_limit_retries=3, rate_limit_max_wait=60)
    asyncio.run(client.call_json("get_drive_item"))
    assert no_sleep == [60.0]


def test_total_wait_per_call_is_capped_and_resets_per_call(no_sleep):
    throttled = _reply("Rate limited by Microsoft Graph API (HTTP 429). Wait 50 seconds.", True)
    session = FakeSession(throttled, throttled, throttled, _reply('{"late": 1}'))
    client = _client(session, rate_limit_retries=5, rate_limit_max_wait=60, rate_limit_call_budget=120)
    with pytest.raises(McpRateLimited) as exc:
        asyncio.run(client.call_json("list_library_files"))
    assert isinstance(exc.value, McpToolError) and exc.value.retry_after == 50.0
    assert no_sleep == [50.0, 50.0] and client.rate_limit_waited == 100.0   # a third 50 s would pass 120 s
    session.script = [throttled, _reply('{"ok": 1}')]
    assert asyncio.run(client.call_json("list_library_files")) == {"ok": 1}  # the next call has its own budget
    assert no_sleep == [50.0, 50.0, 50.0]


def test_session_budget_still_bounds_all_waits(no_sleep):
    throttled = _reply("Rate limited by Microsoft Graph API (HTTP 429). Wait 40 seconds.", True)
    client = _client(FakeSession(*[throttled] * 10), rate_limit_retries=5, rate_limit_max_wait=60,
                     rate_limit_call_budget=120, rate_limit_wait_budget=100)
    with pytest.raises(McpRateLimited):
        asyncio.run(client.call_json("a"))
    with pytest.raises(McpRateLimited):
        asyncio.run(client.call_json("b"))
    assert sum(no_sleep) <= 100


def test_default_client_does_not_retry_so_outlook_keeps_its_semantics(no_sleep):
    """capture-outlook uses the defaults (no retries): a grasp 429 is now an
    McpRateLimited -- still an McpToolError, so its per-item ``except McpToolError``
    counts it as failed exactly as before -- and is counted in rate_limit_hits."""
    session = FakeSession(_reply(GRASP_429, is_error=True))
    client = _client(session)
    with pytest.raises(McpToolError) as exc:
        asyncio.run(client.call_json("get_email_details"))
    assert isinstance(exc.value, McpRateLimited) and no_sleep == [] and client.rate_limit_hits == 1


def test_sharepoint_client_opts_into_bounded_rate_limit_retries(tmp_path):
    client = sp.default_client(str(tmp_path))
    assert client._rate_limit_retries == sp.RATE_LIMIT_RETRIES >= 1
    assert client._rate_limit_max_wait == sp.RATE_LIMIT_MAX_WAIT_S <= 60
    assert client._rate_limit_call_budget == sp.RATE_LIMIT_CALL_BUDGET_S
    assert client._rate_limit_wait_budget == sp.RATE_LIMIT_SESSION_BUDGET_S < sp.RUN_BUDGET_S
    assert (sp.MAX_WALK_DEPTH, sp.MAX_WALK_ITEMS, sp.MAX_WALK_CALLS) == (6, 2000, 250)
    assert sp.WALK_PACE_S >= 0.5


def test_bounded_call_leaves_room_for_rate_limit_waits_but_not_past_the_deadline(monkeypatch):
    monkeypatch.setattr(sp, "CALL_TIMEOUT_S", 1.0)          # bounded_call never cuts under 1 s
    monkeypatch.setattr(sp, "RATE_LIMIT_CALL_BUDGET_S", 0.5)

    def slow(seconds):
        async def call(tool, params):
            await asyncio.sleep(seconds)
            return {"ok": 1}
        return call

    far = time.monotonic() + 60
    assert asyncio.run(_run(sp.bounded_call(slow(1.2), far))) == {"ok": 1}   # > call timeout, < cap
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run(sp.bounded_call(slow(3), far)))                    # cut at the 1.5 s cap
    with pytest.raises(sp._BudgetExceeded):
        asyncio.run(_run(sp.bounded_call(slow(3), time.monotonic() + 1.2)))  # cut at the deadline


async def _run(bounded):
    return await bounded("list_library_files", {})


# --------------------------------------------------------------------------- #
# Site walk: AmazonQ-CSS shape, pacing, deadline                               #
# --------------------------------------------------------------------------- #
def item_url(name, guid, site="AmazonQ-CSS"):
    return f"{HOST}/sites/{site}/_layouts/15/Doc.aspx?sourcedoc=%7B{guid}%7D&file={name}&action=default"


class Grasp:
    """A site with ``libraries`` -> {folder path: [children]}; files carry GUIDs."""

    def __init__(self, files, libraries=("Documents", "Lib2", "Lib3", "Lib4"), *, drive_item=None, fail=None):
        self.files = files            # item id -> dict(name, guid, lib, folder)
        self.libraries = libraries
        self.calls = []
        self.stamps = []
        self.drive_item = drive_item  # override for get_drive_item
        self.fail = fail or {}        # tool -> exception
        self.server_pids = []
        self.download_dir = None

    def factory(self, download_dir):
        self.download_dir = download_dir
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def _children(self, lib, folder):
        prefix = "" if folder == "root" else folder.removeprefix("f:") + "/"
        folders, files = set(), []
        for item_id, it in self.files.items():
            if it["lib"] != lib:
                continue
            path = it["folder"]
            if not (path + "/").startswith(prefix) and prefix:
                continue
            rest = path[len(prefix):] if prefix else path
            if rest:
                folders.add(prefix + rest.split("/")[0])
            else:
                files.append(item_id)
        out = [{"id": f"f:{f}", "name": f.rsplit("/", 1)[-1], "isFolder": True} for f in sorted(folders)]
        out += [{"id": i, "name": self.files[i]["name"], "isFolder": False,
                 "webUrl": item_url(self.files[i]["name"], self.files[i]["guid"])} for i in files]
        return out

    async def call_json(self, tool, params):
        assert tool in sp.READ_ONLY_TOOLS, tool
        self.calls.append(tool)
        self.stamps.append(time.monotonic())
        if tool in self.fail:
            raise self.fail[tool]
        if tool == "list_sharepoint_sites":
            return {"sites": [{"id": "site1", "webUrl": f"{HOST}/sites/AmazonQ-CSS"}]}
        if tool == "list_sharepoint_site_libraries":
            return {"libraries": [{"id": lib, "name": lib} for lib in self.libraries]}
        if tool == "list_library_files":
            items = self._children(params["libraryId"], params["folderId"])
            start = int(params.get("pageToken") or 0)
            size = int(params["maxResults"])
            return {"items": items[start:start + size],
                    **({"nextPageToken": str(start + size)} if start + size < len(items) else {})}
        if tool == "get_drive_item":
            if self.drive_item is not None:
                return self.drive_item(params)
            it = self.files[params["itemId"]]
            return {"id": params["itemId"], "name": it["name"], "size": len(SECRET), "isFolder": False,
                    "lastModifiedDateTime": "2026-05-01T00:00:00Z", "webUrl": item_url(it["name"], it["guid"])}
        raise AssertionError(f"unexpected {tool}")


def amazonq_css(target_folder="A/B/C/D"):
    """~156 items in Documents with 91-ish folders, 4 levels deep, plus three small libraries."""
    files = {}
    n = 0
    for a in range(6):
        for b in range(3):
            for c in range(2):
                for d in range(2):
                    files[f"x{n}"] = dict(name=f"f{n}.docx", guid=f"00000000-0000-0000-0000-{n:012d}",
                                          lib="Documents", folder=f"A{a}/B{b}/C{c}/D{d}")
                    n += 1
    for lib in ("Lib2", "Lib3", "Lib4"):
        files[f"{lib}-1"] = dict(name="g.xlsx", guid=f"11111111-0000-0000-0000-{n:012d}", lib=lib, folder="")
        n += 1
    files["qe"] = dict(name="Quality engineering Questions.docx", guid=GUID, lib="Documents",
                       folder=target_folder)
    return files


def guid_only(guid=GUID):
    return f"{HOST}/:w:/r/sites/AmazonQ-CSS/_layouts/15/Doc.aspx?sourcedoc=%7B{guid.lower()}%7D&action=edit"


TITLE = "Quality engineering Questions.docx"


def resolve(grasp, walker, guid=GUID, title=TITLE, deadline_s=60):
    call = sp.bounded_call(grasp.call_json, time.monotonic() + deadline_s)
    return asyncio.run(sp.resolve(call, sp.doc_ref(guid_only(guid)), title=title, walker=walker))


@pytest.fixture
def no_pace(monkeypatch):
    monkeypatch.setattr(sp, "WALK_PACE_S", 0.0)


def test_four_level_site_with_four_libraries_completes_within_the_caps(no_pace):
    grasp = Grasp(amazonq_css())
    walker = sp.SiteWalker(log=lambda _l: None)
    found = resolve(grasp, walker)
    assert found.item_id == "qe" and found.drive_id == "Documents"
    stats = walker.stats[f"amazon.sharepoint.com/sites/amazonq-css"]
    assert stats["complete"] is True and stats["depth"] == 4 and stats["libraries"] == 4
    assert stats["walk_calls"] <= sp.MAX_WALK_CALLS and stats["items"] <= sp.MAX_WALK_ITEMS
    # A complete walk that does not list a GUID is a definite "not there" (not walk_incomplete).
    with pytest.raises(sp.Unresolved, match="no_file_name"):
        resolve(grasp, walker, guid=OTHER)


def test_walk_calls_are_paced(monkeypatch):
    monkeypatch.setattr(sp, "WALK_PACE_S", 0.05)
    files = {"qe": dict(name=TITLE, guid=GUID, lib="Documents", folder="A/B")}
    grasp = Grasp(files, libraries=("Documents",))
    assert resolve(grasp, sp.SiteWalker(log=lambda _l: None)).item_id == "qe"
    listing = [t for tool, t in zip(grasp.calls, grasp.stamps) if tool.startswith("list_")]
    assert len(listing) >= 4
    assert min(b - a for a, b in zip(listing, listing[1:])) >= 0.045


def test_a_paced_walk_still_stops_at_the_pass_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "WALK_PACE_S", 0.2)
    grasp = Grasp(amazonq_css())
    started = time.monotonic()
    summary = sp.capture_bookmarked_docs([(TITLE, guid_only())], session_left=7200, save=False, budget_s=1.0,
                                         client_factory=grasp.factory, fingerprints={}, log=lambda _l: None,
                                         tmp_root=str(tmp_path))
    assert time.monotonic() - started < 3
    result = summary["results"][guid_only()]
    assert (result.outcome, result.reason) == ("deferred", "budget")


def test_a_429_mid_walk_is_backed_off_and_the_walk_completes(no_sleep, no_pace):
    """End to end through McpClient: grasp throttles one listing call; the client
    waits the 16 s it asked for (faked) and the walk still completes."""
    grasp = Grasp(amazonq_css(target_folder="A"))
    throttled = {"done": False}

    async def answer(tool, params):
        if tool == "list_library_files" and not throttled["done"]:
            throttled["done"] = True
            return _reply(GRASP_429, is_error=True)
        return _reply(json.dumps(await grasp.call_json(tool, params)))

    session = FakeSession()
    session.call_tool = answer
    client = _client(session, rate_limit_retries=3, rate_limit_max_wait=60, rate_limit_call_budget=120)
    walker = sp.SiteWalker(log=lambda _l: None)
    call = sp.bounded_call(client.call_json, time.monotonic() + 60)
    found = asyncio.run(sp.resolve(call, sp.doc_ref(guid_only()), title=TITLE, walker=walker))
    assert found.item_id == "qe" and no_sleep == [16.0] and client.rate_limit_hits == 1
    assert walker.stats["amazon.sharepoint.com/sites/amazonq-css"]["complete"] is True


def test_rate_limit_exhausted_mid_walk_is_walk_incomplete(no_pace):
    grasp = Grasp(amazonq_css(), fail={"list_library_files": McpRateLimited(GRASP_429, 16.0)})
    with pytest.raises(sp.Unresolved, match="walk_incomplete"):
        resolve(grasp, sp.SiteWalker(log=lambda _l: None))


# --------------------------------------------------------------------------- #
# Cross-run walk index sidecar                                                 #
# --------------------------------------------------------------------------- #
def _cache(path, now=None):
    return sp.WalkIndexCache(path, log=lambda _l: None, **({"now": now} if now else {}))


def test_second_run_costs_one_get_drive_item(no_pace, tmp_path):
    path = tmp_path / "jobs" / sp.WALK_INDEX_FILENAME
    first = Grasp(amazonq_css())
    assert resolve(first, sp.SiteWalker(log=lambda _l: None, cache=_cache(path))).item_id == "qe"
    assert "list_library_files" in first.calls
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert not path.name.endswith(".json")                    # liveness reads logs/jobs/*.json as job records
    assert [p.name for p in path.parent.iterdir()] == [path.name]   # atomic: no temp file left
    data = json.loads(path.read_text())
    entry = data["sites"]["amazon.sharepoint.com/sites/amazonq-css"][GUID]
    assert set(entry) == {"drive_id", "item_id", "name", "seen"}
    assert (entry["drive_id"], entry["item_id"], entry["name"]) == ("Documents", "qe", TITLE)
    assert SECRET not in path.read_text()
    second = Grasp(amazonq_css())
    walker = sp.SiteWalker(log=lambda _l: None, cache=_cache(path))
    found = resolve(second, walker)
    assert found.item_id == "qe" and second.calls == ["get_drive_item"]
    assert walker.stats["amazon.sharepoint.com/sites/amazonq-css"]["cache"] == "hit"


def test_a_stale_hint_falls_back_to_a_fresh_walk(no_pace, tmp_path):
    path = tmp_path / sp.WALK_INDEX_FILENAME
    cache = _cache(path)
    cache.remember("amazon.sharepoint.com/sites/amazonq-css", GUID, "Documents", "moved-away", TITLE)
    cache.save()

    grasp = Grasp(amazonq_css())
    real = grasp.call_json

    async def call(tool, params):
        if tool == "get_drive_item" and params["itemId"] == "moved-away":   # now another document
            grasp.calls.append(tool)
            return {"id": "moved-away", "name": TITLE, "isFolder": False, "size": 1,
                    "webUrl": item_url(TITLE, OTHER)}
        return await real(tool, params)
    grasp.call_json = call
    walker = sp.SiteWalker(log=lambda _l: None, cache=_cache(path))
    assert resolve(grasp, walker).item_id == "qe"
    assert grasp.calls[0] == "get_drive_item" and "list_library_files" in grasp.calls
    assert walker.stats["amazon.sharepoint.com/sites/amazonq-css"]["cache"] == "stale"
    assert json.loads(path.read_text())["sites"]["amazon.sharepoint.com/sites/amazonq-css"][GUID]["item_id"] == "qe"


def test_a_vanished_hint_walks_and_a_throttled_hint_is_kept(no_pace, tmp_path):
    path = tmp_path / sp.WALK_INDEX_FILENAME
    cache = _cache(path)
    cache.remember("amazon.sharepoint.com/sites/amazonq-css", GUID, "Documents", "qe", TITLE)
    cache.save()
    gone = Grasp({}, fail={"get_drive_item": McpToolError("get_drive_item: 404 itemNotFound")})
    with pytest.raises(sp.Unresolved, match="no_file_name"):    # walked afresh: not in the (empty) site
        resolve(gone, sp.SiteWalker(log=lambda _l: None, cache=_cache(path)))
    assert gone.calls[0] == "get_drive_item" and "list_sharepoint_sites" in gone.calls
    assert _cache(path).lookup("amazon.sharepoint.com/sites/amazonq-css", GUID) is None
    cache = _cache(path)
    cache.remember("amazon.sharepoint.com/sites/amazonq-css", GUID, "Documents", "qe", TITLE)
    cache.save()
    throttled = Grasp(amazonq_css(), fail={"get_drive_item": McpRateLimited(GRASP_429, 16.0)})
    with pytest.raises(sp.Unresolved, match="rate_limited"):
        resolve(throttled, sp.SiteWalker(log=lambda _l: None, cache=_cache(path)))
    assert throttled.calls == ["get_drive_item"]          # no walk into a throttled API
    assert _cache(path).lookup("amazon.sharepoint.com/sites/amazonq-css", GUID) == ("Documents", "qe", TITLE)


def test_expired_entries_are_ignored(no_pace, tmp_path):
    path = tmp_path / sp.WALK_INDEX_FILENAME
    old = time.time() - sp.WALK_INDEX_TTL_S - 60
    cache = _cache(path, now=lambda: old)
    cache.remember("amazon.sharepoint.com/sites/amazonq-css", GUID, "Documents", "qe", TITLE)
    cache.save()
    assert _cache(path).lookup("amazon.sharepoint.com/sites/amazonq-css", GUID) is None
    grasp = Grasp(amazonq_css())
    assert resolve(grasp, sp.SiteWalker(log=lambda _l: None, cache=_cache(path))).item_id == "qe"
    assert grasp.calls[0] == "list_sharepoint_sites"


@pytest.mark.parametrize("content", [b"not json", b'{"version": 9, "sites": {}}', b"[1, 2]",
                                     json.dumps({"version": 1, "sites": {"k": {GUID: {"drive_id": 5}},
                                                                         "x": "y"}}).encode()])
def test_a_corrupt_sidecar_is_ignored(no_pace, tmp_path, content):
    path = tmp_path / sp.WALK_INDEX_FILENAME
    path.write_bytes(content)
    grasp = Grasp(amazonq_css())
    assert resolve(grasp, sp.SiteWalker(log=lambda _l: None, cache=_cache(path))).item_id == "qe"
    assert json.loads(path.read_text())["version"] == 1          # rewritten cleanly


def test_a_symlinked_sidecar_is_not_followed(no_pace, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text(json.dumps({"version": 1, "sites": {"amazon.sharepoint.com/sites/amazonq-css": {
        GUID: {"drive_id": "Documents", "item_id": "qe", "name": TITLE, "seen": int(time.time())}}}}))
    path = tmp_path / sp.WALK_INDEX_FILENAME
    path.symlink_to(target)
    assert _cache(path).lookup("amazon.sharepoint.com/sites/amazonq-css", GUID) is None
    grasp = Grasp(amazonq_css())
    resolve(grasp, sp.SiteWalker(log=lambda _l: None, cache=_cache(path)))
    assert not path.is_symlink() and json.loads(target.read_text())["sites"]   # replaced the link, target untouched
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


def test_an_unwritable_dir_never_raises(no_pace, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        lines = []
        cache = sp.WalkIndexCache(ro / sp.WALK_INDEX_FILENAME, log=lines.append)
        assert resolve(Grasp(amazonq_css()), sp.SiteWalker(log=lambda _l: None, cache=cache)).item_id == "qe"
        assert any("walk index not saved" in line for line in lines)
    finally:
        os.chmod(ro, 0o700)


def test_capture_uses_the_status_dir_sidecar_only_when_saving(no_pace, tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    grasp = Grasp(amazonq_css())
    sp.capture_bookmarked_docs([(TITLE, guid_only())], session_left=7200, save=False, budget_s=60,
                               client_factory=grasp.factory, fingerprints={}, log=lambda _l: None,
                               tmp_root=str(tmp_path))
    assert not (tmp_path / "jobs").exists()                       # save=False writes nothing
    probe = tmp_path / "probe.cache"
    sp.capture_bookmarked_docs([(TITLE, guid_only())], session_left=7200, save=False, budget_s=60,
                               client_factory=Grasp(amazonq_css()).factory, fingerprints={},
                               log=lambda _l: None, tmp_root=str(tmp_path), walk_index=probe)
    assert probe.exists() and not (tmp_path / "jobs").exists()
    assert sp.default_walk_index_path() == tmp_path / "jobs" / sp.WALK_INDEX_FILENAME


def test_a_complete_walk_replaces_the_site_and_entries_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "MAX_WALK_ITEMS", 3)
    path = tmp_path / sp.WALK_INDEX_FILENAME
    cache = _cache(path)
    key = "amazon.sharepoint.com/sites/amazonq-css"
    cache.remember(key, OTHER, "d", "gone", "gone.docx")
    index = {f"00000000-0000-0000-0000-{i:012d}": ("d", {"id": f"i{i}", "name": f"n{i}.docx"}) for i in range(5)}
    cache.update(key, index, complete=False)
    assert len(cache._load()[key]) == 3                           # capped
    cache.update(key, {GUID: ("d", {"id": "qe", "name": TITLE})}, complete=True)
    assert set(cache._load()[key]) == {GUID}                      # a complete walk drops what it did not list
