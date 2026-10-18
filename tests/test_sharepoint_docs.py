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

    async def call_json(self, tool, params):
        self.calls.append(tool)
        assert tool in ("search_sharepoint_content", "get_drive_item", "download_sharepoint_file"), tool
        if self.fail_tool and self.fail_tool[0] == tool:
            raise self.fail_tool[1]
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


def test_nameless_link_is_unresolved_without_calls(tmp_path):
    grasp = FakeGrasp(awos_items())
    url = f"{HOST}/:w:/r/sites/CSCOE/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID_A}%7D&action=edit"
    summary, _ = run([("n", url)], grasp, tmp_root=str(tmp_path))
    assert (summary["results"][url].outcome, summary["results"][url].reason) == ("unresolved", "no_file_name")
    assert grasp.calls == []


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
    assert "## Sheet: Data" in text and "| 5 |" in text and "| 6 |" not in text and "[2 more rows not extracted]" in text


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


def test_sigterm_mid_pass_cleans_up_then_terminates(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(sp.os, "kill", lambda pid, sig: killed.append(sig))
    grasp = FakeGrasp(awos_items(), {"id-a": make_xlsx()}, fail_tool=("download_sharepoint_file", sp._Terminated()))
    with pytest.raises(SystemExit):
        run([("a", doc_link(AWOS, GUID_A))], grasp, tmp_root=str(tmp_path))
    assert killed == [sp.signal.SIGTERM] and list(tmp_path.iterdir()) == []
    assert sp.signal.getsignal(sp.signal.SIGTERM) is not None


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
