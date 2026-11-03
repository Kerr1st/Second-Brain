"""Capture identity must not depend on the project tag (F-028 / review B N1, D N-1).

Live state 2026-10-05: 267 kiro_cli_chat parents carry their own session UUID as
``project`` (the parser derived it from the filename), 71 carry NULL. The
metadata repair nulls the UUIDs. If identity included ``project`` the first
capture run after the repair would create a second parent (plus chunks and paid
embeddings) for every CLI session still on disk. These tests pin the root-cause
fix (the parser emits no project; UUIDs are never projects) and the identity
rule (channel + URL), before and after the repair.
"""
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src import db
from src.db.maintenance import plan_metadata_repairs, apply_metadata_repairs, digest
from src.ingest import ingest_content, ingest_batch
from src.parsers.cli_chat import parse_conversation
from src.project import normalize_project_tag

SESSION_ID = "3f2a9c1e-7b4d-4c2a-9e1f-0a1b2c3d4e5f"
MESSAGES = [
    ("human", "How should I structure the second brain release so that production stays safe?"),
    ("bot", "Split the refactor into staged releases. " * 12),
    ("human", "What goes first?"),
    ("bot", "Routing and metering first, then the data layer with its migration. " * 12),
]


def _vector(_text=""):
    return [1.0] + [0.0] * 1023


@pytest.fixture
def offline_ingest(monkeypatch):
    import src.ingest as ingest
    embed = Mock(side_effect=_vector)
    monkeypatch.setattr(ingest, "generate_embedding", embed)
    monkeypatch.setattr(ingest, "_discover_relationships", lambda *a: None)
    return embed


def _parents(source_type="kiro_cli_chat"):
    return [m for m in db.list_memories(source_type=source_type, limit=1000) if m["parent_id"] is None]


@pytest.mark.parametrize("raw", [SESSION_ID, SESSION_ID.upper(), "0021dfcf-0435-4bd9-a525-c60ffdf4ee75",
                                 "000d9939b0d50d42818821ef51f93caf", f"/Users/x/.kiro/sessions/cli/{SESSION_ID}"])
def test_opaque_ids_are_never_projects(raw):
    assert normalize_project_tag(raw) is None


@pytest.mark.parametrize("raw", ["second-brain", "/Users/kerrfir/Work/second-brain", "Tools/Crawlee", "abc123"])
def test_real_project_names_still_normalize(raw):
    assert normalize_project_tag(raw) is not None


@pytest.mark.parametrize("conv_id", [SESSION_ID, "0021dfcf-0435-4bd9-a525-c60ffdf4ee75", "not-a-uuid-but-a-bare-id"])
def test_cli_parser_emits_no_project_from_a_bare_session_id(conv_id):
    returned, markdown, project = parse_conversation(conv_id, MESSAGES, 1_700_000_000_000)
    assert returned == conv_id
    assert project is None
    assert "Project:" not in markdown.split("---")[0]


def test_cli_parser_keeps_the_legacy_workspace_path_project():
    _, _, project = parse_conversation("/Users/x/Work/second-brain", MESSAGES, 1_700_000_000_000)
    assert project == "second-brain"


def test_same_session_is_one_parent_before_and_after_metadata_repair(test_db, clean_tables, offline_ingest):
    """Live shape: parent stored with project=<session uuid> (old parser). The fixed
    parser re-captures it (project None, refresh) before and after the UUID repair:
    no new parent, no new embedding."""
    _, markdown, project = parse_conversation(SESSION_ID, MESSAGES, 1_700_000_000_000)
    source_url = f"chat://{SESSION_ID}"
    # Old-parser state, as 267 live rows have it.
    legacy = ingest_content(markdown, "kiro_cli_chat", source_url, project=SESSION_ID)
    assert legacy is not None
    assert db.get_memory(legacy)["project"] is None  # UUID guard already refuses it at write time
    with db.get_connection() as conn, conn.cursor() as cur:  # force the live shape anyway
        cur.execute("UPDATE memories SET project=%s WHERE id=%s OR parent_id=%s", (SESSION_ID, legacy, legacy))
        conn.commit()
    assert db.get_memory(legacy)["project"] == SESSION_ID
    embeds_after_first = offline_ingest.call_count

    # Re-capture BEFORE the repair (project drift: uuid on the row, None from the parser).
    assert ingest_content(markdown, "kiro_cli_chat", source_url, project=project, refresh=True) is None
    assert [m["id"] for m in _parents()] == [legacy]
    assert db.get_memory(legacy)["project"] == SESSION_ID  # a NULL project never erases a tag
    assert offline_ingest.call_count == embeds_after_first

    # Repair nulls the UUID projects (manifest-driven, resumable).
    plan = plan_metadata_repairs()
    assert {op["kind"] for op in plan["operations"]} == {"uuid-project"}
    assert apply_metadata_repairs(plan, digest(plan)) >= 1
    assert db.get_memory(legacy)["project"] is None

    # Re-capture AFTER the repair: still the same single parent, no embedding spent.
    assert ingest_content(markdown, "kiro_cli_chat", source_url, project=project, refresh=True) is None
    assert [m["id"] for m in _parents()] == [legacy]
    assert offline_ingest.call_count == embeds_after_first

    # Batch path uses the same identity.
    stats = ingest_batch([(source_url, markdown)], "kiro_cli_chat", refresh=True)
    assert stats == {"processed": 0, "skipped": 1, "failed": 0}
    assert len(_parents()) == 1


def test_identity_ignores_project_for_every_channel_shape(test_db, clean_tables, offline_ingest):
    """Live source_url shapes (one per channel) are matched regardless of the stored project."""
    shapes = {
        "kiro_cli_chat": "chat://017e7a17-bc7", "kiro_ide_chat": "chat://000d9939b0d50d42818821ef51f93caf",
        "slack": "slack://C016T2N5VV2/2026-09-15", "outlook_email": "outlook://mail/2026-09-14T14:07:16/b9e70f9b725a",
        "outlook_calendar": "outlook://event/2026-09-08T00:00:00/f945eeb968fb", "browser_history": "browser://history/2026-09-08",
        "youtube": "https://www.youtube.com/watch?v=_389pQPLyr8", "article": "https://example.test/doc",
        "quick_desktop_people": "qd://memory/100", "quick_desktop_decision": "qd://entity/122",
        "quick_desktop_feed": "qd-feed://evt_00001", "quick_desktop_chat": "qd-chat://0774d7c5-1e91-42dd-aa8f-cb111f7c7c49",
        "quick_desktop_doc": "qd-doc://10000", "codex_task": "codex://01a07211-6670-7e31-98f2-83fafaeccd64",
        "api_browser": "https://example.test/page", "api_email": "capture://api_email/abc",
    }
    for source_type, url in shapes.items():
        mid = db.create_memory("source", "t", "body", source_url=url, source_type=source_type, project="real-project")
        for stored in (str(uuid4()), None, "other-project"):
            with db.get_connection() as conn, conn.cursor() as cur:
                cur.execute("UPDATE memories SET project=%s WHERE id=%s", (stored, mid)); conn.commit()
            found = db.find_captured_source(source_type, url, None)
            assert found is not None and str(found["id"]) == mid, (source_type, stored)
            assert db.find_captured_source(source_type, url, "yet-another") is not None
        assert db.find_captured_source(source_type, url + "-other", None) is None
        assert db.find_captured_source("slack" if source_type != "slack" else "article", url, None) is None


def test_refresh_adopts_a_real_project_but_never_erases_one(test_db, clean_tables, offline_ingest):
    url = "chat://aaaa-bbbb"
    body = "# T\n\nSource: x\n\n---\n\nbody text\n"
    mid = ingest_content(body, "kiro_ide_chat", url, project="second-brain")
    assert db.get_memory(mid)["project"] == "second-brain"
    assert ingest_content(body, "kiro_ide_chat", url, project=None, refresh=True) is None
    assert db.get_memory(mid)["project"] == "second-brain"
    assert ingest_content(body, "kiro_ide_chat", url, project="renamed", refresh=True) == mid
    assert db.get_memory(mid)["project"] == "renamed"
