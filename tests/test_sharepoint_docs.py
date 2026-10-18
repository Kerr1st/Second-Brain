"""Hermetic tests for src.capture.sharepoint_docs (no network, no DB, no grasp).

A fake MCP client stands in for grasp-mcp; Office packages are built in-test.
"""
from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.capture import sharepoint_docs as sp  # noqa: E402
from src.capture.mcp_client import McpAuthError, McpToolError  # noqa: E402

SECRET = "ZEBRA-SECRET-PHRASE"
HOST = "https://amazon.sharepoint.com"
GUID_A, GUID_B, GUID_C = ("69672F84-0853-47AF-8628-8AF34075800B", "66E34BAE-5DB5-4B7E-AB0C-7E21BFAC9BE0",
                          "DA601658-70F9-4147-A0E2-4487143B78D4")
AWOS = "Extended AWOS Framework.xlsx"
DRIVE = "b!drive"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def doc_link(name, guid, site="CSCOE", kind="x"):
    from urllib.parse import quote
    return (f"{HOST}/:{kind}:/r/sites/{site}/_layouts/15/Doc.aspx?sourcedoc=%7B{guid}%7D"
            f"&file={quote(name)}&action=default&mobileredirect=true")


def item_url(name, guid, site="CSCOE"):
    from urllib.parse import quote
    return f"{HOST}/sites/{site}/_layouts/15/Doc.aspx?sourcedoc=%7B{guid}%7D&file={quote(name)}&action=default"


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_docx(secret=SECRET):
    body = (f'<w:document {W}><w:body>'
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Intro</w:t></w:r></w:p>'
            f'<w:p><w:r><w:t>First </w:t></w:r><w:r><w:t>{secret}</w:t></w:r></w:p>'
            f'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell a</w:t></w:r></w:p></w:tc>'
            f'<w:tc><w:p><w:r><w:t>cell|b</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            f'<w:p><w:r><w:delText>deleted</w:delText></w:r></w:p>'
            f'</w:body></w:document>')
    return _zip({"word/document.xml": body})


def make_pptx():
    pres = (f'<p:presentation {P} {R}><p:sldIdLst><p:sldId id="257" r:id="rId9"/>'
            f'<p:sldId id="256" r:id="rId2"/></p:sldIdLst></p:presentation>')
    prels = (f'<Relationships {RELS}><Relationship Id="rId2" Type="{RT}slide" Target="slides/slide1.xml"/>'
             f'<Relationship Id="rId9" Type="{RT}slide" Target="slides/slide2.xml"/></Relationships>')

    def slide(text, num):
        return (f'<p:sld {P} {A}><p:cSld><p:spTree>'
                f'<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'
                f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
                f'<p:txBody><a:p><a:fld><a:t>{num}</a:t></a:fld></a:p></p:txBody></p:sp>'
                f'</p:spTree></p:cSld></p:sld>')
    notes = (f'<p:notes {P} {A}><p:cSld><p:spTree>'
             f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
             f'<p:txBody><a:p><a:r><a:t>speaker note</a:t></a:r></a:p></p:txBody></p:sp>'
             f'</p:spTree></p:cSld></p:notes>')
    srels = (f'<Relationships {RELS}><Relationship Id="rId1" Type="{RT}notesSlide" '
             f'Target="../notesSlides/notesSlide1.xml"/></Relationships>')
    return _zip({"ppt/presentation.xml": pres, "ppt/_rels/presentation.xml.rels": prels,
                 "ppt/slides/slide1.xml": slide("second in order", 99),
                 "ppt/slides/slide2.xml": slide("first in order", 98),
                 "ppt/slides/_rels/slide2.xml.rels": srels, "ppt/notesSlides/notesSlide1.xml": notes})


def make_xlsx():
    wb = (f'<workbook {S} {R}><sheets><sheet name="Plan" sheetId="1" r:id="rId1"/>'
          f'<sheet name="Data" sheetId="2" r:id="rId2"/></sheets></workbook>')
    rels = (f'<Relationships {RELS}><Relationship Id="rId1" Type="{RT}worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{RT}worksheet" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
    sst = f'<sst {S}><si><t>Goal</t></si><si><r><t>Ri</t></r><r><t>ch</t></r></si></sst>'
    s1 = (f'<worksheet {S}><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
          f'<row r="2"><c r="A2"><v>42</v></c><c r="B2" t="b"><v>1</v></c>'
          f'<c r="C2" t="inlineStr"><is><t>inline</t></is></c></row></sheetData></worksheet>')
    s2 = (f'<worksheet {S}><sheetData>'
          + "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 8))
          + '</sheetData></worksheet>')
    return _zip({"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels, "xl/sharedStrings.xml": sst,
                 "xl/worksheets/sheet1.xml": s1, "xl/worksheets/sheet2.xml": s2})


# --------------------------------------------------------------------------- #
# Detection                                                                    #
# --------------------------------------------------------------------------- #
def test_doc_ref_detects_office_links_only():
    ref = sp.doc_ref(doc_link(AWOS, GUID_A.lower()))
    assert ref and ref.name == AWOS and ref.ext == "xlsx" and ref.site == "sites/cscoe" and ref.sourcedoc == GUID_A
    doc2 = sp.doc_ref(f"{HOST}/:x:/r/sites/AmazonQ-CSS/_layouts/15/doc2.aspx?sourcedoc=%7B{GUID_B}%7D&file=2026-P.xlsx")
    assert doc2 and doc2.name == "2026-P.xlsx" and doc2.site == "sites/amazonq-css"
    direct = sp.doc_ref(f"{HOST}/sites/CSCOE/Shared%20Documents/Plans/Kiro%20ROB.docx")
    assert direct and direct.sourcedoc is None and direct.path == "/sites/cscoe/shared documents/plans/kiro rob.docx"
    nameless = sp.doc_ref(f"{HOST}/:w:/r/sites/AmazonQ-CSS/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID_C}%7D&action=edit")
    assert nameless and nameless.name is None
    for skipped in (f"{HOST}/sites/CSCOE", f"{HOST}/sites/CSCOE/Shared%20Documents/Forms/AllItems.aspx?id=x",
                    f"{HOST}/sites/X/SitePages/Page.aspx", f"{HOST}/sites/X/_layouts/15/stream.aspx?id=v.mp4",
                    f"{HOST}/sites/X/Shared%20Documents/a.pdf", doc_link("notes.txt", GUID_A),
                    "https://example.com/sites/X/a.docx", f"http://amazon.sharepoint.com/sites/X/a.docx",
                    "https://evil-sharepoint.com.example.org/sites/X/a.docx"):
        assert sp.doc_ref(skipped) is None, skipped


# --------------------------------------------------------------------------- #
# Fake grasp                                                                   #
# --------------------------------------------------------------------------- #
class FakeGrasp:
    """search_sharepoint_content / get_drive_item / download_sharepoint_file over ``items``."""

    def __init__(self, items, files=None, *, fail_connect=False, fail_tool=None):
        self.items = items              # item id -> dict(name, site, folder, guid, modified, size)
        self.files = files or {}        # item id -> bytes
        self.calls = []
        self.download_dir = None
        self.fail_connect = fail_connect
        self.fail_tool = fail_tool
        self.server_pids = []
        self.closed = False

    def factory(self, download_dir):
        self.download_dir = download_dir
        return self

    async def __aenter__(self):
        if self.fail_connect:
            raise OSError("grasp-mcp: not found")
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def _web(self, it):
        from urllib.parse import quote
        return f"{HOST}/sites/{it['site']}/{quote(it.get('folder', 'Shared Documents'))}/{quote(it['name'])}"

    def _folders(self, site):
        """{folder path within the library: [child folder names]} of ``site``'s items."""
        tree = {"": set()}
        for it in self.items.values():
            if it["site"] != site:
                continue
            parts = it.get("folder", "Shared Documents").split("/")[1:]
            for depth in range(len(parts)):
                tree.setdefault("/".join(parts[:depth]), set()).add(parts[depth])
                tree.setdefault("/".join(parts[:depth + 1]), set())
        return tree

    def _list_library(self, params):
        site = params["siteId"].removeprefix("site:")
        folder = "" if params.get("folderId", "root") == "root" else params["folderId"].removeprefix("f:")
        out = [{"id": f"f:{folder + '/' if folder else ''}{name}", "name": name, "isFolder": True}
               for name in sorted(self._folders(site).get(folder, ()))]
        out += [{"id": i, "name": it["name"], "isFolder": False, "size": it["size"],
                 "lastModifiedDateTime": it["modified"], "webUrl": item_url(it["name"], it["guid"], it["site"])}
                for i, it in self.items.items()
                if it["site"] == site and "/".join(it.get("folder", "Shared Documents").split("/")[1:]) == folder]
        start = int(params.get("pageToken") or 0)
        size = int(params.get("maxResults") or 25)
        page = out[start:start + size]
        return {"items": page, **({"nextPageToken": str(start + size)} if start + size < len(out) else {})}

    async def call_json(self, tool, params):
        self.calls.append(tool)
        assert tool in ("search_sharepoint_content", "get_drive_item", "download_sharepoint_file",
                        "list_sharepoint_sites", "list_sharepoint_site_libraries", "list_library_files"), tool
        assert tool in sp.READ_ONLY_TOOLS, tool
        if self.fail_tool and self.fail_tool[0] == tool:
            raise self.fail_tool[1]
        if tool == "list_sharepoint_sites":
            names = sorted({it["site"] for it in self.items.values()})
            term = params.get("searchTerm", "").lower()
            return {"sites": [{"id": f"site:{n}", "name": n, "webUrl": f"{HOST}/sites/{n}"} for n in names
                              if term in n.lower() or term in n.lower().replace("-", " ")]}
        if tool == "list_sharepoint_site_libraries":
            return {"libraries": [{"id": DRIVE, "name": "Documents",
                                   "webUrl": f"{HOST}/sites/{params['siteId'].removeprefix('site:')}/Shared%20Documents"}]}
        if tool == "list_library_files":
            return self._list_library(params)
        if tool == "search_sharepoint_content":
            return {"results": [{"id": i, "driveId": DRIVE, "title": it["name"], "webUrl": self._web(it),
                                 "lastModified": it["modified"], "size": it["size"]}
                                for i, it in self.items.items() if params["query"].lower() in it["name"].lower()],
                    "totalCount": 0}
        if tool == "get_drive_item":
            it = self.items[params["itemId"]]
            return {"id": params["itemId"], "name": it["name"], "size": it["size"], "isFolder": False,
                    "lastModifiedDateTime": it["modified"], "webUrl": item_url(it["name"], it["guid"], it["site"])}
        save = os.path.join(self.download_dir, params["savePath"])
        with open(save, "wb") as handle:
            handle.write(self.files[params["fileId"]])
        return {"saved": params["savePath"]}


def awos_items():
    return {"id-a": dict(name=AWOS, site="CSCOE", folder="Shared Documents/CS Intelligence", guid=GUID_A,
                         modified="2026-08-03T14:16:47Z", size=1000),
            "id-b": dict(name=AWOS, site="CSCOE", folder="Shared Documents/SOPs", guid=GUID_B,
                         modified="2026-06-05T03:07:43Z", size=1000),
            "id-c": dict(name=AWOS, site="CSCOE", folder="Shared Documents/GPS", guid=GUID_C,
                         modified="2026-01-30T19:52:59Z", size=1000)}


def run(bookmarks, grasp, *, fingerprints=None, session_left=7200, save=False, ingest=None, tmp_root=None, log=None):
    lines = []
    summary = sp.capture_bookmarked_docs(bookmarks, session_left=session_left, save=save, budget_s=60,
                                         client_factory=grasp.factory, fingerprints=fingerprints,
                                         ingest=ingest, log=log or lines.append, tmp_root=tmp_root)
    return summary, lines


# --------------------------------------------------------------------------- #
# Resolution                                                                   #
# --------------------------------------------------------------------------- #
def test_three_same_name_files_pick_the_sourcedoc_match(tmp_path):
    grasp = FakeGrasp(awos_items(), {"id-b": make_xlsx()})
    summary, _ = run([("AWOS", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    result = summary["results"][doc_link(AWOS, GUID_B)]
    assert result.outcome == "captured" and result.source_url == f"sharepoint://{DRIVE}/id-b"
    assert grasp.calls.count("get_drive_item") == 3 and grasp.calls.count("download_sharepoint_file") == 1


def test_no_guid_match_or_other_site_is_unresolved(tmp_path):
    grasp = FakeGrasp(awos_items())
    other = "11111111-2222-3333-4444-555555555555"
    summary, _ = run([("x", doc_link(AWOS, other)), ("y", doc_link(AWOS, GUID_A, site="OtherSite"))],
                     grasp, tmp_root=str(tmp_path))
    assert [r.outcome for r in summary["results"].values()] == ["unresolved", "unresolved"]
    assert "download_sharepoint_file" not in grasp.calls


def test_direct_link_without_guid_needs_exact_path_and_ambiguity_is_unresolved(tmp_path):
    items = awos_items()
    grasp = FakeGrasp(items, {"id-b": make_xlsx()})
    exact = f"{HOST}/sites/CSCOE/Shared%20Documents/SOPs/Extended%20AWOS%20Framework.xlsx"
    summary, _ = run([("d", exact)], grasp, tmp_root=str(tmp_path))
    assert summary["results"][exact].outcome == "captured"
    # Two copies at the identical path (two drives): never guess.
    items["id-d"] = dict(items["id-b"], guid="99999999-2222-3333-4444-555555555555")
    summary, _ = run([("d", exact)], FakeGrasp(items), tmp_root=str(tmp_path))
    assert (summary["results"][exact].outcome, summary["results"][exact].reason[:9]) == ("unresolved", "ambiguous")


def test_nameless_link_without_an_office_title_is_a_permanent_skip_without_calls(tmp_path):
    grasp = FakeGrasp(awos_items())
    url = f"{HOST}/:w:/r/sites/CSCOE/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID_A}%7D&action=edit"
    summary, lines = run([("n", url)], grasp, tmp_root=str(tmp_path))
    assert (summary["results"][url].outcome, summary["results"][url].reason) == ("skipped_permanent", "no_file_name")
    assert grasp.calls == []
    assert [line for line in lines if line.startswith("INFO:")] == [
        "INFO: 1 SharePoint bookmark(s) can never resolve, skipped (not an advisory): n (no_file_name)"]


def test_doc_aspx_without_guid_is_never_resolved_by_name_alone(tmp_path):
    """B1: one same-named search hit elsewhere in the site must not be captured."""
    items = {"other": dict(name="Plan.xlsx", site="cscoe", folder="Shared Documents/Archive", guid=GUID_C,
                           modified="2026-01-01T00:00:00Z", size=100)}
    grasp = FakeGrasp(items, {"other": make_xlsx()})
    url = f"{HOST}/sites/cscoe/_layouts/15/Doc.aspx?file=Plan.xlsx"
    assert sp.doc_ref(url).sourcedoc is None
    summary, lines = run([("Plan.xlsx", url)], grasp, tmp_root=str(tmp_path))
    result = summary["results"][url]
    assert (result.outcome, result.reason, result.source_url) == ("skipped_permanent", "no_guid", None)
    assert grasp.calls == [] and sum(line.startswith("INFO:") for line in lines) == 1
    # resolve() itself refuses too (defence in depth), even with a single same-named hit.
    import asyncio
    with pytest.raises(sp.Unresolved, match="no_guid"):
        asyncio.run(sp.resolve(grasp.call_json, sp.doc_ref(url)))


# --------------------------------------------------------------------------- #
# Change detection and storage                                                 #
# --------------------------------------------------------------------------- #
def test_unchanged_is_skipped_and_changed_recaptured_with_refresh(tmp_path):
    url = doc_link(AWOS, GUID_A)
    stored = {}

    def ingest(content, source_type, source_url, *, refresh, source_metadata):
        assert (source_type, refresh) == ("article", True)
        stored[source_url] = (content, source_metadata)
        return "id"

    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
    summary, _ = run([("a", url)], grasp, fingerprints={}, save=True, ingest=ingest, tmp_root=str(tmp_path))
    assert summary["counts"]["captured"] == 1 and summary["ingest"]["processed"] == 1
    source = f"sharepoint://{DRIVE}/id-a"
    content, meta = stored[source]
    assert meta == {"source_fingerprint": "spdoc-v1:2026-08-03T14:16:47Z:1000", "bookmark_url": url}
    assert content.startswith(f"# {AWOS}\n\nSource: {source}\nType: sharepoint-document\nDate: ")
    assert "## Sheet: Plan" in content and summary["results"][url].markdown is None

    # Same fingerprint: no download, nothing stored.
    stored.clear()
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
    summary, _ = run([("a", url)], grasp, fingerprints={source: meta["source_fingerprint"]}, save=True,
                     ingest=ingest, tmp_root=str(tmp_path))
    assert summary["counts"] == {"unchanged": 1} and "download_sharepoint_file" not in grasp.calls and not stored

    # Modified upstream: re-downloaded and re-stored (refresh=True replaces the row).
    items = awos_items()
    items["id-a"]["modified"] = "2026-10-01T00:00:00Z"
    summary, _ = run([("a", url)], FakeGrasp(items, {"id-a": make_xlsx()}),
                     fingerprints={source: meta["source_fingerprint"]}, save=True, ingest=ingest, tmp_root=str(tmp_path))
    assert summary["counts"] == {"captured": 1}
    assert stored[source][1]["source_fingerprint"] == "spdoc-v1:2026-10-01T00:00:00Z:1000"


def test_duplicate_bookmarks_of_one_document_store_once(tmp_path):
    url1, url2 = doc_link(AWOS, GUID_A), doc_link(AWOS, GUID_A) + "&wdPreviousSession=x"
    calls = []
    summary, _ = run([("a", url1), ("b", url2)], FakeGrasp(awos_items(), {"id-a": make_xlsx()}), fingerprints={},
                     save=True, ingest=lambda *a, **k: calls.append(a[2]) or "id", tmp_root=str(tmp_path))
    assert summary["counts"] == {"captured": 1, "duplicate": 1} and len(calls) == 1


def test_oversize_by_metadata_is_not_downloaded(tmp_path):
    items = awos_items()
    items["id-a"]["size"] = sp.MAX_DOWNLOAD_BYTES + 1
    grasp = FakeGrasp(items)
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"too_large": 1} and "download_sharepoint_file" not in grasp.calls


# --------------------------------------------------------------------------- #
# Extraction                                                                   #
# --------------------------------------------------------------------------- #
def test_extract_docx_paragraphs_headings_tables():
    text = sp.extract_text(make_docx(), "docx")
    assert text.splitlines()[0] == "## Intro"
    assert f"First {SECRET}" in text and "| cell a | cell\\|b |" in text and "deleted" not in text


def test_extract_pptx_slide_order_headers_and_notes():
    text = sp.extract_text(make_pptx(), "pptx")
    assert text.index("## Slide 1\nfirst in order") < text.index("## Slide 2\nsecond in order")
    assert "Notes:\nspeaker note" in text and "98" not in text and "99" not in text


def test_extract_xlsx_sheets_shared_strings_and_row_cap(monkeypatch):
    monkeypatch.setattr(sp, "MAX_SHEET_ROWS", 5)
    text = sp.extract_text(make_xlsx(), "xlsx")
    assert "## Sheet: Plan\n| Goal |  | Rich |\n| 42 | TRUE | inline |" in text
    assert "## Sheet: Data" in text and "| 5 |" in text and "| 6 |" not in text and "[further rows not extracted]" in text


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><w:document/>',
    b'<!ENTITY x SYSTEM "file:///etc/passwd">',
    "<w:document/>".encode("utf-16"),
])
def test_dtd_entities_and_non_utf8_parts_are_rejected(payload):
    with pytest.raises(sp.UnsafeDocument):
        sp.extract_text(_zip({"word/document.xml": payload}), "docx")


def test_zip_bomb_and_member_caps(monkeypatch):
    bomb = _zip({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>'.encode()
                 + b"a" * (2 * 1024 * 1024) + b"</w:t></w:r></w:p></w:body></w:document>"})
    assert len(bomb) < 64 * 1024   # highly compressible
    monkeypatch.setattr(sp, "MAX_UNCOMPRESSED_TOTAL", 1024 * 1024)
    with pytest.raises(sp.UnsafeDocument, match="declared"):
        sp.extract_text(bomb, "docx")
    monkeypatch.setattr(sp, "MAX_UNCOMPRESSED_TOTAL", 50 * 1024 * 1024)
    monkeypatch.setattr(sp, "MAX_MEMBER_BYTES", 1024 * 1024)
    with pytest.raises(sp.UnsafeDocument):
        sp.extract_text(bomb, "docx")
    monkeypatch.setattr(sp, "MAX_MEMBERS", 3)
    with pytest.raises(sp.UnsafeDocument, match="members"):
        sp.extract_text(_zip({f"m{i}": "x" for i in range(4)}), "docx")
    with pytest.raises(sp.UnsafeDocument, match="not an OOXML"):
        sp.extract_text(b"\xd0\xcf\x11\xe0 not a zip", "docx")


def test_lying_member_header_is_capped_by_bytes_actually_inflated(monkeypatch):
    data = _zip({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>'.encode()
                 + b"b" * 200_000 + b"</w:t></w:r></w:p></w:body></w:document>"})
    monkeypatch.setattr(sp, "MAX_MEMBER_BYTES", 100_000)
    pkg_init = sp._Package.__init__

    def lenient_init(self, raw):
        monkeypatch.setattr(sp, "MAX_MEMBER_BYTES", 10 ** 9)   # header checks pass...
        pkg_init(self, raw)
        monkeypatch.setattr(sp, "MAX_MEMBER_BYTES", 100_000)   # ...the inflate count still stops it
    monkeypatch.setattr(sp._Package, "__init__", lenient_init)
    with pytest.raises(sp.UnsafeDocument, match="inflates"):
        sp.extract_text(data, "docx")


def test_corrupt_download_is_failed_not_fatal(tmp_path):
    grasp = FakeGrasp(awos_items(), {"id-a": b"PK\x03\x04garbage"})
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"failed": 1}


# --------------------------------------------------------------------------- #
# Temp files, deferral, session failures, logging                             #
# --------------------------------------------------------------------------- #
def test_temp_dir_is_private_and_removed_even_on_error(tmp_path, monkeypatch):
    seen = {}
    real_read = sp._read_download

    def spy(download_dir, save_path):
        seen["mode"] = stat.S_IMODE(os.stat(download_dir).st_mode)
        seen["file"] = os.path.join(download_dir, save_path)
        real_read(download_dir, save_path)
        raise RuntimeError("boom after download")
    monkeypatch.setattr(sp, "_read_download", spy)
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"failed": 1} and seen["mode"] == 0o700
    assert not os.path.exists(seen["file"]) and not os.path.exists(grasp.download_dir)
    assert list(tmp_path.iterdir()) == []


def test_sigterm_mid_pass_cleans_up_then_exits_143_without_self_kill(tmp_path, monkeypatch):
    """S2: no os.kill under SIG_DFL (it would skip stdout flush and atexit): SystemExit(143)."""
    monkeypatch.setattr(sp.os, "kill", lambda pid, sig: pytest.fail("self-kill"))
    killed = []
    monkeypatch.setattr(sp, "_kill_servers", lambda client: killed.append(client))
    before = sp.signal.signal(sp.signal.SIGTERM, sp.signal.SIG_DFL)
    try:
        grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()},
                          fail_tool=("download_sharepoint_file", sp._Terminated()))
        with pytest.raises(SystemExit) as exc:
            run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
        assert exc.value.code == 143 and killed == [grasp] and list(tmp_path.iterdir()) == []
        assert sp.signal.getsignal(sp.signal.SIGTERM) == sp.signal.SIG_DFL
    finally:
        sp.signal.signal(sp.signal.SIGTERM, before)


def test_sigterm_chains_to_a_callable_previous_handler(tmp_path):
    seen = []
    before = sp.signal.signal(sp.signal.SIGTERM, lambda signum, frame: seen.append(signum))
    try:
        grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()},
                          fail_tool=("download_sharepoint_file", sp._Terminated()))
        summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
        assert seen == [sp.signal.SIGTERM] and summary["counts"] == {"deferred": 1}
        assert list(tmp_path.iterdir()) == []
    finally:
        sp.signal.signal(sp.signal.SIGTERM, before)


def test_second_sigterm_during_cleanup_is_recorded_and_cleanup_finishes(tmp_path, monkeypatch):
    """N2: SIGTERM inside the finally only sets a flag; rmtree and the handler restore complete."""
    real_rmtree = sp.shutil.rmtree

    def rmtree_with_signal(path, **kw):
        os.kill(os.getpid(), sp.signal.SIGTERM)     # delivered to the flag-only cleanup handler
        real_rmtree(path, **kw)
    monkeypatch.setattr(sp.shutil, "rmtree", rmtree_with_signal)
    before = sp.signal.signal(sp.signal.SIGTERM, sp.signal.SIG_DFL)
    try:
        grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
        with pytest.raises(SystemExit) as exc:
            run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
        assert exc.value.code == 143 and list(tmp_path.iterdir()) == []
        assert sp.signal.getsignal(sp.signal.SIGTERM) == sp.signal.SIG_DFL
    finally:
        sp.signal.signal(sp.signal.SIGTERM, before)


_SIGTERM_CHILD = r"""
import asyncio, os, sys, time
sys.path.insert(0, sys.argv[1])
import anyio
from src.capture import sharepoint_docs as sp
URL = sys.argv[2]
killed = []
sp._kill_servers = lambda client: (killed.append(1), print("KILLED", flush=True))

class BusyClient:
    server_pids = []
    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(self._busy)     # like the SDK's stdio reader/writer tasks
        return self
    async def _busy(self):
        while True:
            sum(range(20000))               # busy in the anyio task when SIGTERM lands
            await asyncio.sleep(0)
    async def __aexit__(self, *exc):
        self._tg.cancel_scope.cancel()
        await self._tg.__aexit__(None, None, None)
    async def call_json(self, tool, params):
        print("READY", flush=True)
        await asyncio.sleep(30)

try:
    sp.capture_bookmarked_docs([("a", URL)], session_left=7200, save=False, budget_s=60,
                               client_factory=lambda d: BusyClient(), fingerprints={}, tmp_root=sys.argv[3])
except SystemExit as exc:
    print("EXIT", exc.code, type(exc).__name__, flush=True)
    raise
except BaseException as exc:
    print("ESCAPED", repr(exc), flush=True)
    raise
print("RETURNED", flush=True)
"""


def test_real_sigterm_while_an_anyio_task_is_busy_kills_servers_and_exits_143(tmp_path):
    """S1: a real SIGTERM landing in an anyio task is a SystemExit (not a BaseExceptionGroup)."""
    import signal
    import subprocess
    import time
    root = str(Path(__file__).resolve().parents[1])
    tmp_root = tmp_path / "t"
    tmp_root.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", _SIGTERM_CHILD, root, doc_link(AWOS, GUID_A), str(tmp_root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "ESCAPED" not in out and "BaseExceptionGroup" not in err, (out, err)
    assert "KILLED" in out and "EXIT 143 SystemExit" in out and proc.returncode == 143, (out, err)
    assert list(tmp_root.iterdir()) == []


def test_stale_dirs_of_a_killed_run_are_swept(tmp_path):
    stale = tmp_path / f"{sp.TMP_PREFIX}old"
    stale.mkdir()
    (stale / "001.docx").write_bytes(b"x")
    os.utime(stale, (1, 1))
    fresh = tmp_path / f"{sp.TMP_PREFIX}new"
    fresh.mkdir()
    assert sp.sweep_stale_dirs(str(tmp_path)) == 1 and not stale.exists() and fresh.exists()


@pytest.mark.parametrize("left", [None, 0, 29 * 60])
def test_short_or_missing_midway_session_defers_without_launch(tmp_path, left):
    grasp = FakeGrasp(awos_items())
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, session_left=left, tmp_root=str(tmp_path))
    assert summary["counts"] == {"deferred": 1} and "mwinit" in summary["cause"]
    assert grasp.download_dir is None and grasp.calls == []


def test_grasp_launch_failure_defers_everything(tmp_path):
    grasp = FakeGrasp(awos_items(), fail_connect=True)
    summary, _ = run([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"deferred": 2} and "did not start" in summary["cause"]
    assert list(tmp_path.iterdir()) == []


def test_auth_failure_mid_pass_defers_the_rest(tmp_path):
    grasp = FakeGrasp(awos_items(), fail_tool=("search_sharepoint_content", McpAuthError("expired token")))
    summary, _ = run([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"deferred": 2} and "session failed" in summary["cause"] and grasp.closed


def test_per_doc_tool_error_is_failed_and_pass_continues(tmp_path):
    grasp = FakeGrasp(awos_items(), {"id-b": make_xlsx()},
                      fail_tool=("download_sharepoint_file", McpToolError("download_sharepoint_file: 500")))
    summary, _ = run([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"failed": 2}
    assert grasp.calls.count("search_sharepoint_content") == 2


def test_document_text_never_reaches_the_log(tmp_path, capsys):
    url = f"{HOST}/sites/CSCOE/Shared%20Documents/Plans/Plan.docx"
    items = {"id-x": dict(name="Plan.docx", site="CSCOE", folder="Shared Documents/Plans", guid=GUID_A,
                          modified="2026-01-01T00:00:00Z", size=500)}
    lines = []
    summary, _ = run([("Plan", url)], FakeGrasp(items, {"id-x": make_docx()}), fingerprints={}, save=True,
                     ingest=lambda *a, **k: "id", tmp_root=str(tmp_path), log=lines.append)
    assert summary["counts"] == {"captured": 1} and summary["results"][url].chars > 0
    out = "\n".join(lines) + capsys.readouterr().out + repr(summary)
    assert SECRET not in out and "Plan.docx" in out


# --------------------------------------------------------------------------- #
# scrape_bookmarks wiring                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def sb(tmp_path, monkeypatch):
    import importlib.util
    import json
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("scrape_bookmarks_sp", root / "scripts" / "scrape_bookmarks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    cookie = tmp_path / "midway-cookie"
    cookie.write_text("#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t4102444800\tsession\tv\n")
    monkeypatch.setattr(mod, "MIDWAY_COOKIE_PATH", str(cookie))
    monkeypatch.setattr(mod, "load_indexed_urls", lambda: set())
    monkeypatch.setattr(mod, "browser_fetch_pages", lambda urls, budget_s: pytest.fail("no browser"))
    monkeypatch.setattr(mod, "scrape_with_cookies", lambda name, url: pytest.fail("no cookie fetch"))
    monkeypatch.setattr(mod, "scrape_with_crawlee", lambda *a, **k: pytest.fail("no crawlee"))

    def bookmarks(urls):
        path = tmp_path / "Bookmarks"
        path.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"B{i}", "url": u} for i, u in enumerate(urls)]}}}))
        monkeypatch.setattr(mod, "BOOKMARKS_PATH", str(path))
    mod.test_bookmarks = bookmarks
    return mod


def test_main_routes_docs_to_grasp_and_leaves_other_sharepoint_pre_skipped(sb, tmp_path, monkeypatch, capsys):
    import json
    doc, site = doc_link(AWOS, GUID_A), f"{HOST}/sites/CSCOE/Shared%20Documents/Forms/AllItems.aspx?id=x"
    sb.test_bookmarks([doc, site])
    seen = {}

    def fake_capture(docs, *, session_left, budget_s):
        seen["docs"], seen["session_left"], seen["budget"] = docs, session_left, budget_s
        result = sp.DocResult(doc, AWOS, outcome="unresolved", reason="not_found")
        return {"counts": sp.Counter(unresolved=1), "results": {doc: result}, "cause": None, "ingest": None}
    monkeypatch.setattr(sp, "capture_bookmarked_docs", fake_capture)
    assert sb.main() == 0                      # an unresolved document never fails the job
    out = capsys.readouterr().out
    assert [u for _, u in seen["docs"]] == [doc] and seen["session_left"] > 0 and 0 < seen["budget"] <= sp.RUN_BUDGET_S
    assert "(1 not fetched: non-Midway sign-in)" in out          # the library view stays pre-skipped
    assert "eligible=0" in out and "sharepoint=unresolved:1 -> exit 0" in out
    assert f"WARNING: 1 SharePoint document(s) not captured (advisory, no strike): unresolved: {AWOS} (not_found)" in out
    state = json.loads((tmp_path / "jobs" / "bookmarks-sharepoint.backlog.json").read_text())
    assert state["deferred"] == 1 and "not_found" in state["cause"]
    ledger = tmp_path / "jobs" / "bookmarks.unreachable.json"
    assert not ledger.exists() or doc not in ledger.read_text()


def test_main_sharepoint_pass_error_is_advisory(sb, monkeypatch, capsys):
    sb.test_bookmarks([doc_link(AWOS, GUID_A)])
    monkeypatch.setattr(sp, "capture_bookmarked_docs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert sb.main() == 0
    assert "sharepoint=deferred:1 -> exit 0" in capsys.readouterr().out


def test_main_without_sharepoint_docs_keeps_the_summary_line(sb, monkeypatch, capsys):
    sb.test_bookmarks([f"{HOST}/sites/CSCOE"])
    monkeypatch.setattr(sp, "capture_bookmarked_docs", lambda *a, **k: pytest.fail("no pass"))
    assert sb.main() == 0
    out = capsys.readouterr().out
    assert "sharepoint=" not in out and "SharePoint" not in out


def test_renamed_file_resolves_by_guid_only(tmp_path):
    items = awos_items()
    items["id-b"]["name"] = "Extended AWOS Framework v2.xlsx"     # renamed since bookmarked
    grasp = FakeGrasp(items, {"id-b": make_xlsx()})
    grasp_search = grasp.call_json

    async def stem_search(tool, params):                         # the stem query finds the renamed file
        if tool == "search_sharepoint_content":
            params = dict(params, query=params["query"].removesuffix(".xlsx"))
        return await grasp_search(tool, params)
    grasp.call_json = stem_search
    summary, _ = run([("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    assert summary["results"][doc_link(AWOS, GUID_B)].source_url == f"sharepoint://{DRIVE}/id-b"
    # A direct link (no GUID) never matches a different name.
    exact = f"{HOST}/sites/CSCOE/Shared%20Documents/SOPs/Extended%20AWOS%20Framework.xlsx"
    summary, _ = run([("d", exact)], FakeGrasp(items), tmp_root=str(tmp_path))
    assert summary["results"][exact].outcome == "unresolved"


def test_search_error_falls_back_to_the_stem_query(tmp_path):
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
    real = grasp.call_json
    queries = []

    async def flaky(tool, params):
        if tool == "search_sharepoint_content":
            queries.append(params["query"])
            if params["query"].endswith(".xlsx"):
                raise McpToolError("search_sharepoint_content: 400 BadRequest")
        return await real(tool, params)
    grasp.call_json = flaky
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert queries == [AWOS, AWOS[:-5]] and summary["counts"] == {"captured": 1}


# --------------------------------------------------------------------------- #
# Budget (S3)                                                                  #
# --------------------------------------------------------------------------- #
def test_a_slow_call_is_cut_at_the_remaining_budget(tmp_path, monkeypatch):
    import asyncio
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()})
    real = grasp.call_json

    async def slow(tool, params):
        if tool == "search_sharepoint_content":
            await asyncio.sleep(30)
        return await real(tool, params)
    grasp.call_json = slow
    started = __import__("time").monotonic()
    summary = sp.capture_bookmarked_docs([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))],
                                         session_left=7200, save=False, budget_s=1.5, client_factory=grasp.factory,
                                         fingerprints={}, log=lambda _l: None, tmp_root=str(tmp_path))
    assert __import__("time").monotonic() - started < 10
    assert [(r.outcome, r.reason) for r in summary["results"].values()] == [("deferred", "budget")] * 2
    assert "budget" in summary["cause"]


def test_no_get_drive_item_starts_after_the_deadline(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["t"])
    grasp = FakeGrasp(awos_items())
    real = grasp.call_json

    async def tick(tool, params):
        reply = await real(tool, params)
        if tool == "search_sharepoint_content":
            clock["t"] += 100          # the search used up the budget
        return reply
    grasp.call_json = tick
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert summary["counts"] == {"deferred": 1} and "get_drive_item" not in grasp.calls


def test_captured_text_is_capped_per_run_and_the_rest_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "MAX_RUN_CHARS", 30)
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx(), "id-b": make_xlsx()})
    summary, _ = run([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_path))
    outcomes = [(r.outcome, r.reason) for r in summary["results"].values()]
    assert outcomes == [("captured", None), ("deferred", "run_char_cap")]


def test_ingest_stops_at_the_deadline_and_defers_the_rest(tmp_path, monkeypatch):
    results = {u: sp.DocResult(u, u, outcome="captured", source_url=f"sharepoint://d/{u}", fingerprint="f",
                               markdown=f"# {u}\n\nSource: sharepoint://d/{u}\n\n" + "text " * 20)
               for u in ("u1", "u2")}
    stored = []
    clock = {"t": 0.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["t"])

    def ingest(content, source_type, source_url, **kw):
        stored.append(source_url)
        clock["t"] = 100.0             # the first document used up the budget
        return "id"
    stats = sp._ingest(results, ingest, lambda _l: None, deadline=50.0)
    assert stored == ["sharepoint://d/u1"] and stats["processed"] == 1
    assert (results["u2"].outcome, results["u2"].reason) == ("deferred", "budget")


# --------------------------------------------------------------------------- #
# Change detection without a timestamp (N5), stray default downloads (N6)       #
# --------------------------------------------------------------------------- #
def test_missing_last_modified_is_always_rechecked(tmp_path):
    items = awos_items()
    items["id-a"]["modified"] = ""
    grasp = FakeGrasp(items, {"id-a": make_xlsx()})
    source = f"sharepoint://{DRIVE}/id-a"
    summary, _ = run([("a", doc_link(AWOS, GUID_A))], grasp, fingerprints={source: "spdoc-v1::1000"},
                     tmp_root=str(tmp_path))
    assert summary["counts"] == {"captured": 1} and "download_sharepoint_file" in grasp.calls


def test_a_download_landing_in_grasps_default_dir_is_removed(tmp_path, monkeypatch):
    default = tmp_path / "grasp-downloads"
    default.mkdir()
    monkeypatch.setattr(sp, "GRASP_DEFAULT_DOWNLOAD_DIR", str(default))
    keep = default / "002.xlsx"
    keep.write_bytes(b"pre-existing, not ours")
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx(), "id-b": make_xlsx()})

    async def ignores_download_dir(tool, params):
        grasp.calls.append(tool)
        if tool == "download_sharepoint_file":
            (default / params["savePath"]).write_bytes(b"stray")      # --download-dir ignored
            return {"saved": params["savePath"]}
        grasp.calls.pop()
        return await FakeGrasp.call_json(grasp, tool, params)
    grasp.call_json = ignores_download_dir
    tmp_root = tmp_path / "t"
    tmp_root.mkdir()
    summary, _ = run([("a", doc_link(AWOS, GUID_A)), ("b", doc_link(AWOS, GUID_B))], grasp, tmp_root=str(tmp_root))
    assert [r.reason for r in summary["results"].values()] == ["download produced no file"] * 2
    assert not (default / "001.xlsx").exists()                        # created by this call: removed
    assert keep.read_bytes() == b"stray"                               # existed before: left alone


# --------------------------------------------------------------------------- #
# XML hardening (N1) and bounded parsing (S6)                                  #
# --------------------------------------------------------------------------- #
def test_expat_handlers_refuse_a_dtd_the_regex_misses(monkeypatch):
    monkeypatch.setattr(sp, "_DTD_RE", __import__("re").compile(rb"(?!)"))     # the regex sees nothing
    for payload in (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "x">]><d>&a;</d>',
                    b'<!DOCTYPE d SYSTEM "file:///etc/passwd"><d/>'):
        with pytest.raises(sp.UnsafeDocument, match="DTD/entity"):
            sp.parse_xml(payload)
        with pytest.raises(sp.UnsafeDocument, match="DTD/entity"):
            list(sp.iter_rows(payload))


def test_worksheet_rows_are_streamed_and_detached():
    body = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 2001))
    data = f'<worksheet {S}><sheetData>{body}</sheetData></worksheet>'.encode()
    seen = 0
    for row in sp.iter_rows(data):
        seen += 1
        assert len(row) == 1                 # complete when yielded
    assert seen == 2000
    # Stopping early stops the parse (the generator is just abandoned).
    gen = sp.iter_rows(data)
    assert next(gen).get("r") == "1"
    gen.close()


def test_tree_parsed_parts_have_their_own_cap(monkeypatch):
    monkeypatch.setattr(sp, "MAX_XML_TREE_BYTES", 100)
    with pytest.raises(sp.UnsafeDocument, match="tree-parse cap"):
        sp.extract_text(make_docx(), "docx")


# --------------------------------------------------------------------------- #
# Library-walk fallback (search misses)                                        #
# --------------------------------------------------------------------------- #
class SearchMisses(FakeGrasp):
    async def call_json(self, tool, params):
        if tool == "search_sharepoint_content":
            self.calls.append(tool)
            return {"results": [], "totalCount": 0}
        return await super().call_json(tool, params)


def qe_items(guid=GUID_A, name="Quality engineering Questions.docx", folder="Shared Documents/A/B/C"):
    return {"qe": dict(name=name, site="AmazonQ-CSS", folder=folder, guid=guid,
                       modified="2026-05-01T00:00:00Z", size=500),
            "other": dict(name=name, site="AmazonQ-CSS", folder="Shared Documents/A", guid=GUID_B,
                          modified="2026-05-01T00:00:00Z", size=500)}


def test_fallback_walk_resolves_by_guid_and_exact_name(tmp_path):
    url = doc_link("Quality engineering Questions.docx", GUID_A, site="AmazonQ-CSS", kind="w")
    grasp = SearchMisses(qe_items(), {"qe": make_docx()})
    summary, _ = run([("x", url)], grasp, tmp_root=str(tmp_path))
    result = summary["results"][url]
    assert (result.outcome, result.source_url) == ("captured", f"sharepoint://{DRIVE}/qe")
    assert {"list_sharepoint_sites", "list_sharepoint_site_libraries", "list_library_files"} <= set(grasp.calls)
    assert grasp.calls[-2:] == ["get_drive_item", "download_sharepoint_file"]


def test_fallback_needs_the_guid_and_the_exact_name(tmp_path):
    url = doc_link("Quality engineering Questions.docx", GUID_C, site="AmazonQ-CSS", kind="w")
    summary, _ = run([("x", url)], SearchMisses(qe_items()), tmp_root=str(tmp_path))
    assert summary["results"][url].outcome == "unresolved"                       # no item has the GUID
    url = doc_link("Quality engineering Questions.docx", GUID_A, site="AmazonQ-CSS", kind="w")
    grasp = SearchMisses(qe_items(name="Renamed.docx"))
    summary, _ = run([("x", url)], grasp, tmp_root=str(tmp_path))
    assert summary["results"][url].outcome == "unresolved"                       # GUID ok, name differs
    assert "download_sharepoint_file" not in grasp.calls


def test_guid_only_bookmark_resolves_by_its_office_title(tmp_path):
    url = f"{HOST}/:w:/r/sites/AmazonQ-CSS/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID_A.lower()}%7D&action=edit"
    grasp = SearchMisses(qe_items(), {"qe": make_docx()})
    summary, _ = run([("Quality engineering Questions.docx", url)], grasp, tmp_root=str(tmp_path))
    assert summary["results"][url].outcome == "captured" and "search_sharepoint_content" not in grasp.calls
    # The walk misses it: a permanent skip (never an advisory).
    summary, _ = run([("Quality engineering Questions.docx", url)], SearchMisses(qe_items(guid=GUID_C)),
                     tmp_root=str(tmp_path))
    assert (summary["results"][url].outcome, summary["results"][url].reason) == ("skipped_permanent", "no_file_name")


def test_fallback_walk_is_bounded_and_runs_once_per_site(tmp_path, monkeypatch):
    url = doc_link("Quality engineering Questions.docx", GUID_A, site="AmazonQ-CSS", kind="w")
    deep = qe_items(folder="Shared Documents/A/B/C/D")                          # depth 4: beyond the walk
    grasp = SearchMisses(deep)
    summary, _ = run([("x", url), ("y", doc_link("Quality engineering Questions.docx", GUID_C,
                                                 site="AmazonQ-CSS", kind="w"))], grasp, tmp_root=str(tmp_path))
    assert [r.outcome for r in summary["results"].values()] == ["unresolved", "unresolved"]
    assert grasp.calls.count("list_sharepoint_site_libraries") == 1               # walked once for both
    monkeypatch.setattr(sp, "MAX_WALK_ITEMS", 2)
    many = {f"f{i}": dict(name=f"f{i}.docx", site="AmazonQ-CSS", folder="Shared Documents", guid=GUID_B,
                          modified="m", size=1) for i in range(10)}
    grasp = SearchMisses({**many, **qe_items(folder="Shared Documents")})
    summary, _ = run([("x", url)], grasp, tmp_root=str(tmp_path))
    assert summary["results"][url].outcome == "unresolved"
    assert grasp.calls.count("list_library_files") == 1


def test_walk_listing_error_is_not_found_not_fatal(tmp_path):
    url = doc_link("Quality engineering Questions.docx", GUID_A, site="AmazonQ-CSS", kind="w")
    grasp = SearchMisses(qe_items(), fail_tool=("list_library_files", McpToolError("list_library_files: 429")))
    summary, lines = run([("x", url)], grasp, tmp_root=str(tmp_path))
    assert summary["results"][url].outcome == "unresolved"
    assert any("library walk of sites/amazonq-css stopped (McpToolError)" in line for line in lines)


def test_main_permanent_skips_are_not_fed_to_the_backlog(sb, tmp_path, monkeypatch, capsys):
    """S5: a no_guid / no_file_name bookmark is counted and logged, never an advisory."""
    import json
    nameless = f"{HOST}/:w:/r/sites/CSCOE/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID_A}%7D&action=edit"
    sb.test_bookmarks([nameless])
    monkeypatch.setattr(sp, "default_client", lambda d: pytest.fail("no grasp needed"))
    assert sb.main() == 0
    out = capsys.readouterr().out
    assert "SharePoint document(s) not captured" not in out
    assert out.count("can never resolve") == 1 and "sharepoint=skipped_permanent:1 -> exit 0" in out
    state = json.loads((tmp_path / "jobs" / "bookmarks-sharepoint.backlog.json").read_text())
    assert state["deferred"] == 0
