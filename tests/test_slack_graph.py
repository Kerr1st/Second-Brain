"""Tests for Slack social graph import into the knowledge graph.

Ported from 8dd3bb7 (scripts/migrate/import_slack_graph.py → src/capture/slack_graph.py).
User decision 2026-10-06: the hourly knowledge-graph writer stays until R7, so the
`graph` stage of src.capture.qd runs it between feed and chats — exactly where
qd_sync.sh at 8dd3bb7 called the script. Runs on the isolated runner only.
"""

import json
import os
import tempfile
import pytest
from hypothesis import given, strategies as st, settings



# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_CHANNELS = [
    {"id": "C0000000001", "name": "tfc-ngde", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": False, "is_member": True, "num_members": 1632,
     "topic": "NextGen Developer Experience", "purpose": "TFC Members and Aspiring AoDs"},
    {"id": "C0000000002", "name": "genaiml-specialists-ww", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": False, "is_member": True, "num_members": 425,
     "topic": "GenAI Specialist Org", "purpose": "The WW GenAI Specialist Org"},
    {"id": "C0000000003", "name": "tech-summit-san-francisco", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": True, "is_member": True, "num_members": 558,
     "topic": "", "purpose": ""},
]

SAMPLE_USERS = [
    {"id": "W0000000001", "name": "alice", "real_name": "Alice Example",
     "display_name": "alice", "is_bot": False, "is_admin": False, "deleted": False},
    {"id": "U0000000002", "name": "bsample", "real_name": "Bob Sample",
     "display_name": "bsample", "is_bot": False, "is_admin": False, "deleted": False},
    {"id": "U0000000003", "name": "ctest", "real_name": "Carol Test",
     "display_name": "ctest", "is_bot": False, "is_admin": False, "deleted": False},
]


@pytest.fixture
def channels_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for ch in SAMPLE_CHANNELS:
            f.write(json.dumps(ch) + "\n")
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def users_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for u in SAMPLE_USERS:
            f.write(json.dumps(u) + "\n")
        path = f.name
    yield path
    os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests: Parsing
# ---------------------------------------------------------------------------

class TestParseChannels:
    def test_parse_channels_from_jsonl(self, channels_file):
        from src.capture.slack_graph import parse_channels
        channels = parse_channels(channels_file)
        assert len(channels) == 3
        assert channels[0]["id"] == "C0000000001"
        assert channels[0]["name"] == "tfc-ngde"

    def test_parse_channels_skips_empty_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(SAMPLE_CHANNELS[0]) + "\n")
            f.write("\n")
            f.write(json.dumps(SAMPLE_CHANNELS[1]) + "\n")
            path = f.name
        try:
            from src.capture.slack_graph import parse_channels
            channels = parse_channels(path)
            assert len(channels) == 2
        finally:
            os.unlink(path)


class TestParseUsers:
    def test_parse_users_from_jsonl(self, users_file):
        from src.capture.slack_graph import parse_users
        users = parse_users(users_file)
        assert len(users) == 3
        assert users[0]["real_name"] == "Alice Example"

    def test_parse_users_skips_bots(self):
        data = [
            {"id": "B01", "name": "bot1", "real_name": "Bot One", "display_name": "bot1",
             "is_bot": True, "is_admin": False, "deleted": False},
            {"id": "U02", "name": "human", "real_name": "Human One", "display_name": "human",
             "is_bot": False, "is_admin": False, "deleted": False},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for u in data:
                f.write(json.dumps(u) + "\n")
            path = f.name
        try:
            from src.capture.slack_graph import parse_users
            users = parse_users(path)
            assert len(users) == 1
            assert users[0]["name"] == "human"
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Unit Tests: Entity building
# ---------------------------------------------------------------------------

class TestBuildEntities:
    def test_channel_entity_structure(self):
        from src.capture.slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[0])
        assert entity["category"] == "Channel"
        assert entity["name"] == "tfc-ngde"
        assert entity["source_type"] == "slack_cache"
        assert entity["properties"]["slack_id"] == "C0000000001"
        assert entity["properties"]["is_private"] is True
        assert entity["properties"]["num_members"] == 1632

    def test_channel_entity_includes_topic_and_purpose(self):
        from src.capture.slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[0])
        assert "NextGen Developer Experience" in entity["summary"]
        assert "TFC Members" in entity["summary"]

    def test_channel_entity_archived_flag(self):
        from src.capture.slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[2])
        assert entity["properties"]["is_archived"] is True

    def test_person_entity_structure(self):
        from src.capture.slack_graph import build_person_entity
        entity = build_person_entity(SAMPLE_USERS[0])
        assert entity["category"] == "Person"
        assert entity["name"] == "Alice Example"
        assert entity["source_type"] == "slack_cache"
        assert entity["properties"]["slack_id"] == "W0000000001"
        assert entity["properties"]["alias"] == "alice"

    def test_person_entity_summary(self):
        from src.capture.slack_graph import build_person_entity
        entity = build_person_entity(SAMPLE_USERS[0])
        assert "alice" in entity["summary"]


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {"Person", "Organization", "DefinedTerm", "CreativeWork",
                    "Channel", "Product", "Action", "Event", "Project", "Observation"}


@given(name=st.text(min_size=1, max_size=80).filter(lambda x: x.strip()),
       num_members=st.integers(min_value=0, max_value=100000))
@settings(max_examples=50)
def test_channel_entity_name_always_nonempty(name, num_members):
    from src.capture.slack_graph import build_channel_entity
    ch = {"id": "C001", "name": name, "is_private": False, "is_im": False,
          "is_mpim": False, "is_archived": False, "is_member": True,
          "num_members": num_members, "topic": "", "purpose": ""}
    entity = build_channel_entity(ch)
    assert entity["name"]
    assert entity["category"] in VALID_CATEGORIES


@given(real_name=st.text(min_size=1, max_size=80).filter(lambda x: x.strip()),
       alias=st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("Ll", "Nd"))))
@settings(max_examples=50)
def test_person_entity_name_always_nonempty(real_name, alias):
    from src.capture.slack_graph import build_person_entity
    u = {"id": "U001", "name": alias, "real_name": real_name,
         "display_name": alias, "is_bot": False, "is_admin": False, "deleted": False}
    entity = build_person_entity(u)
    assert entity["name"]
    assert entity["category"] in VALID_CATEGORIES


# ---------------------------------------------------------------------------
# E2E Tests (require test DB)
# ---------------------------------------------------------------------------

class TestE2EImport:
    def test_import_creates_entities(self, test_db, channels_file, users_file):
        from src.capture.slack_graph import run_import
        import src.db as db

        stats = run_import(channels_path=channels_file, users_path=users_file)
        assert stats["channels_upserted"] == 3
        assert stats["persons_upserted"] == 3

        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache' AND category = 'Channel'")
                assert cur.fetchone()[0] == 3
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache' AND category = 'Person'")
                assert cur.fetchone()[0] == 3

    def test_import_is_idempotent(self, test_db, channels_file, users_file):
        from src.capture.slack_graph import run_import
        import src.db as db

        run_import(channels_path=channels_file, users_path=users_file)
        run_import(channels_path=channels_file, users_path=users_file)

        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache'")
                assert cur.fetchone()[0] == 6  # 3 channels + 3 persons, no duplicates

    def test_dry_run_creates_nothing(self, test_db, channels_file, users_file):
        from src.capture.slack_graph import run_import
        import src.db as db

        # Clean slate
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM entities WHERE source_type = 'slack_cache'")
            conn.commit()

        stats = run_import(channels_path=channels_file, users_path=users_file, dry_run=True)
        assert stats["channels_upserted"] == 3
        assert stats["persons_upserted"] == 3

        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache'")
                assert cur.fetchone()[0] == 0

    def test_properties_updated_on_conflict(self, test_db, channels_file, users_file):
        from src.capture.slack_graph import run_import
        import src.db as db

        run_import(channels_path=channels_file, users_path=users_file)

        # Modify a channel's num_members
        modified = SAMPLE_CHANNELS[0].copy()
        modified["num_members"] = 9999
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(modified) + "\n")
            mod_path = f.name

        try:
            run_import(channels_path=mod_path, users_path=users_file)
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT properties FROM entities WHERE category = 'Channel' AND name = 'tfc-ngde'")
                    props = cur.fetchone()[0]
                    if isinstance(props, str):
                        props = json.loads(props)
                    assert props["num_members"] == 9999
        finally:
            os.unlink(mod_path)


# ---------------------------------------------------------------------------
# Hourly job wiring: the writer is the `graph` stage of src.capture.qd (until R7)
# ---------------------------------------------------------------------------

class TestQdSyncKeepsTheGraphWriter:
    def _stub_stages(self, monkeypatch, calls, graph=None):
        from src.capture import quick_desktop, qd_eventlog, qd_chats, qd_tags, slack_graph

        def stage(name):
            def run(*, dry_run=False):
                calls.append((name, dry_run))
                return {"processed": 0, "failed": 0}
            return run
        monkeypatch.setattr(quick_desktop, "capture", stage("facts"))
        monkeypatch.setattr(qd_tags, "enrich_memories", stage("tags"))
        monkeypatch.setattr(qd_eventlog, "ingest_eventlog", stage("feed"))
        monkeypatch.setattr(qd_chats, "capture", stage("chats"))
        monkeypatch.setattr(slack_graph, "run_import", graph or stage("graph"))
        monkeypatch.setattr(qd_eventlog, "INTERACTIONS_PATH", "/nonexistent/interactions.jsonl")

    def test_graph_runs_between_feed_and_chats_with_the_job_dry_run_flag(self, monkeypatch):
        from src.capture import qd
        calls = []
        self._stub_stages(monkeypatch, calls)
        result = qd.capture(dry_run=True)
        assert [name for name, _ in calls] == ["facts", "tags", "feed", "graph", "chats"]
        assert all(dry_run is True for _, dry_run in calls)
        assert "graph" in result

    def test_graph_failure_is_counted_and_never_hides_the_other_stages(self, monkeypatch):
        from src.capture import qd
        from src.capture.common import exit_code
        calls = []

        def broken(*, dry_run=False):
            calls.append(("graph", dry_run))
            raise RuntimeError("slack cache unreadable")
        self._stub_stages(monkeypatch, calls, graph=broken)
        result = qd.capture(dry_run=False)
        assert result["graph"] == {"failed": 1}
        assert [name for name, _ in calls] == ["facts", "tags", "feed", "graph", "chats"]
        assert exit_code(result) == 1

    def test_graph_stage_is_the_8dd3bb7_writer(self, monkeypatch):
        """The stage calls run_import with only the job's dry_run flag — default paths."""
        from src.capture import qd, slack_graph
        seen = {}
        self._stub_stages(monkeypatch, [], graph=lambda **kw: seen.update(kw) or {"channels_upserted": 0, "persons_upserted": 0})
        assert qd.capture(dry_run=True)["graph"] == {"channels_upserted": 0, "persons_upserted": 0}
        assert seen == {"dry_run": True}
        assert slack_graph.SOURCE_TYPE == "slack_cache"

    def test_maintenance_entry_delegates_to_the_writer_main(self, monkeypatch):
        import runpy
        from src.capture import slack_graph
        monkeypatch.setattr(slack_graph, "main", lambda: 0)
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path("scripts/maintenance/import_slack_graph.py", run_name="__main__")
        assert excinfo.value.code == 0

    def test_writer_main_dry_run_writes_nothing_and_exits_zero(self, test_db, channels_file, users_file, monkeypatch):
        from src.capture import slack_graph
        import src.db as db
        monkeypatch.setattr(slack_graph, "CHANNELS_PATH", channels_file)
        monkeypatch.setattr(slack_graph, "USERS_PATH", users_file)
        monkeypatch.setattr("sys.argv", ["import_slack_graph.py", "--dry-run"])
        assert slack_graph.main() == 0
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache'")
                assert cur.fetchone()[0] == 0
