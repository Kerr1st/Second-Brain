"""Tests for Slack social graph import into the knowledge graph."""

import json
import os
import tempfile
import pytest
from hypothesis import given, strategies as st, settings

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_CHANNELS = [
    {"id": "C000EXAMPLE1", "name": "platform-engineering", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": False, "is_member": True, "num_members": 1632,
     "topic": "Developer Experience", "purpose": "Developer experience discussion"},
    {"id": "C000EXAMPLE2", "name": "ai-specialists", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": False, "is_member": True, "num_members": 425,
     "topic": "AI Specialist Group", "purpose": "The WW AI Specialist Group"},
    {"id": "C000EXAMPLE3", "name": "tech-summit", "is_private": True, "is_im": False,
     "is_mpim": False, "is_archived": True, "is_member": True, "num_members": 558,
     "topic": "", "purpose": ""},
]

SAMPLE_USERS = [
    {"id": "U000EXAMPLE1", "name": "sampleuser1", "real_name": "Sample User One",
     "display_name": "sampleuser1", "is_bot": False, "is_admin": False, "deleted": False},
    {"id": "U000EXAMPLE2", "name": "sampleuser2", "real_name": "Sample User Two",
     "display_name": "sampleuser2", "is_bot": False, "is_admin": False, "deleted": False},
    {"id": "U000EXAMPLE3", "name": "sampleuser3", "real_name": "Sample User Three",
     "display_name": "sampleuser3", "is_bot": False, "is_admin": False, "deleted": False},
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
        from scripts.migrate.import_slack_graph import parse_channels
        channels = parse_channels(channels_file)
        assert len(channels) == 3
        assert channels[0]["id"] == "C000EXAMPLE1"
        assert channels[0]["name"] == "platform-engineering"

    def test_parse_channels_skips_empty_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(SAMPLE_CHANNELS[0]) + "\n")
            f.write("\n")
            f.write(json.dumps(SAMPLE_CHANNELS[1]) + "\n")
            path = f.name
        try:
            from scripts.migrate.import_slack_graph import parse_channels
            channels = parse_channels(path)
            assert len(channels) == 2
        finally:
            os.unlink(path)


class TestParseUsers:
    def test_parse_users_from_jsonl(self, users_file):
        from scripts.migrate.import_slack_graph import parse_users
        users = parse_users(users_file)
        assert len(users) == 3
        assert users[0]["real_name"] == "Sample User One"

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
            from scripts.migrate.import_slack_graph import parse_users
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
        from scripts.migrate.import_slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[0])
        assert entity["category"] == "Channel"
        assert entity["name"] == "platform-engineering"
        assert entity["source_type"] == "slack_cache"
        assert entity["properties"]["slack_id"] == "C000EXAMPLE1"
        assert entity["properties"]["is_private"] is True
        assert entity["properties"]["num_members"] == 1632

    def test_channel_entity_includes_topic_and_purpose(self):
        from scripts.migrate.import_slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[0])
        assert "Developer Experience" in entity["summary"]
        assert "Developer experience" in entity["summary"]

    def test_channel_entity_archived_flag(self):
        from scripts.migrate.import_slack_graph import build_channel_entity
        entity = build_channel_entity(SAMPLE_CHANNELS[2])
        assert entity["properties"]["is_archived"] is True

    def test_person_entity_structure(self):
        from scripts.migrate.import_slack_graph import build_person_entity
        entity = build_person_entity(SAMPLE_USERS[0])
        assert entity["category"] == "Person"
        assert entity["name"] == "Sample User One"
        assert entity["source_type"] == "slack_cache"
        assert entity["properties"]["slack_id"] == "U000EXAMPLE1"
        assert entity["properties"]["alias"] == "sampleuser1"

    def test_person_entity_summary(self):
        from scripts.migrate.import_slack_graph import build_person_entity
        entity = build_person_entity(SAMPLE_USERS[0])
        assert "sampleuser1" in entity["summary"]


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {"Person", "Organization", "DefinedTerm", "CreativeWork",
                    "Channel", "Product", "Action", "Event", "Project", "Observation"}


@given(name=st.text(min_size=1, max_size=80).filter(lambda x: x.strip()),
       num_members=st.integers(min_value=0, max_value=100000))
@settings(max_examples=50)
def test_channel_entity_name_always_nonempty(name, num_members):
    from scripts.migrate.import_slack_graph import build_channel_entity
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
    from scripts.migrate.import_slack_graph import build_person_entity
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
        from scripts.migrate.import_slack_graph import run_import
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
        from scripts.migrate.import_slack_graph import run_import
        import src.db as db

        run_import(channels_path=channels_file, users_path=users_file)
        run_import(channels_path=channels_file, users_path=users_file)

        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE source_type = 'slack_cache'")
                assert cur.fetchone()[0] == 6  # 3 channels + 3 persons, no duplicates

    def test_dry_run_creates_nothing(self, test_db, channels_file, users_file):
        from scripts.migrate.import_slack_graph import run_import
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
        from scripts.migrate.import_slack_graph import run_import
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
                    cur.execute("SELECT properties FROM entities WHERE category = 'Channel' AND name = 'platform-engineering'")
                    props = cur.fetchone()[0]
                    if isinstance(props, str):
                        props = json.loads(props)
                    assert props["num_members"] == 9999
        finally:
            os.unlink(mod_path)
