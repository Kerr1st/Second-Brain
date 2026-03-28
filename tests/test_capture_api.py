"""Tests for src/capture_api.py.

Covers: R1 (Generic Capture), R2 (Slack), R3 (Browser), R4 (Email),
R5 (Authentication), R6 (Health Check), R7 (Server Config), R8 (Error Handling).

Uses FastAPI TestClient with mocked generate_embedding (no Bedrock calls).
Property-based tests via Hypothesis for payload validation.
"""

import os
import uuid

# Set token before importing the app module
os.environ.setdefault("CAPTURE_API_TOKEN", "test-token-secret")

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from src.capture_api import app, VALID_TYPES
from tests.conftest import _deterministic_embedding

AUTH = {"Authorization": "Bearer test-token-secret"}
WRONG_AUTH = {"Authorization": "Bearer wrong-token"}


@pytest.fixture()
def client(test_db, clean_tables):
    with patch("src.capture_api.generate_embedding", side_effect=_deterministic_embedding):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# R5: Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_auth_header_returns_401(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c"})
        assert resp.status_code == 422 or resp.status_code == 401
        # FastAPI returns 422 for missing required Header; both are acceptable rejections

    def test_invalid_token_returns_401(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c"}, headers=WRONG_AUTH)
        assert resp.status_code == 401

    def test_valid_token_passes(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c"}, headers=AUTH)
        assert resp.status_code == 201

    def test_no_auth_on_health(self, client):
        resp = client.get("/health")
        assert resp.status_code in (200, 503)


# ---------------------------------------------------------------------------
# R1: Generic Capture Endpoint
# ---------------------------------------------------------------------------

class TestGenericCapture:
    def test_valid_capture_returns_201_with_memory_id(self, client):
        resp = client.post("/capture", json={"title": "Test", "content": "Some content"}, headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert "memory_id" in data
        uuid.UUID(data["memory_id"])  # validates it's a real UUID

    def test_missing_title_returns_422(self, client):
        resp = client.post("/capture", json={"content": "c"}, headers=AUTH)
        assert resp.status_code == 422

    def test_missing_content_returns_422(self, client):
        resp = client.post("/capture", json={"title": "t"}, headers=AUTH)
        assert resp.status_code == 422

    def test_empty_title_returns_422(self, client):
        resp = client.post("/capture", json={"title": "", "content": "c"}, headers=AUTH)
        assert resp.status_code == 422

    def test_empty_content_returns_422(self, client):
        resp = client.post("/capture", json={"title": "t", "content": ""}, headers=AUTH)
        assert resp.status_code == 422

    def test_invalid_type_returns_422(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c", "type": "bogus"}, headers=AUTH)
        assert resp.status_code == 422

    def test_valid_type_accepted(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c", "type": "idea"}, headers=AUTH)
        assert resp.status_code == 201

    def test_default_type_is_research(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "c"}, headers=AUTH)
        assert resp.status_code == 201
        # Verify via DB that the stored memory has type "research"
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["type"] == "research"

    def test_optional_fields_stored(self, client):
        resp = client.post("/capture", json={
            "title": "t", "content": "c", "source_url": "https://example.com",
            "source_type": "manual", "tags": ["test"], "project": "second-brain",
        }, headers=AUTH)
        assert resp.status_code == 201
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["source_url"] == "https://example.com"
        assert mem["source_type"] == "manual"
        assert "test" in mem["tags"]

    def test_content_exceeding_max_length_returns_422(self, client):
        resp = client.post("/capture", json={"title": "t", "content": "x" * 100_001}, headers=AUTH)
        assert resp.status_code == 422

    def test_title_exceeding_max_length_returns_422(self, client):
        resp = client.post("/capture", json={"title": "t" * 501, "content": "c"}, headers=AUTH)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# R2: Slack Capture
# ---------------------------------------------------------------------------

class TestSlackCapture:
    def test_valid_slack_returns_201(self, client):
        resp = client.post("/capture/slack", json={"text": "interesting message"}, headers=AUTH)
        assert resp.status_code == 201
        uuid.UUID(resp.json()["memory_id"])

    def test_missing_text_returns_422(self, client):
        resp = client.post("/capture/slack", json={}, headers=AUTH)
        assert resp.status_code == 422

    def test_title_is_first_80_chars(self, client):
        long_text = "a" * 200
        resp = client.post("/capture/slack", json={"text": long_text}, headers=AUTH)
        assert resp.status_code == 201
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["title"] == "a" * 80

    def test_metadata_includes_slack_fields(self, client):
        resp = client.post("/capture/slack", json={
            "text": "msg", "user_name": "alice", "channel_name": "general", "thread_ts": "123.456",
        }, headers=AUTH)
        assert resp.status_code == 201
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        meta = mem["metadata"]
        assert meta["slack_user"] == "alice"
        assert meta["slack_channel"] == "general"
        assert meta["thread_ts"] == "123.456"

    def test_source_type_is_slack(self, client):
        resp = client.post("/capture/slack", json={"text": "msg"}, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["source_type"] == "slack"

    def test_optional_metadata_omitted_when_not_provided(self, client):
        resp = client.post("/capture/slack", json={"text": "msg"}, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        meta = mem["metadata"]
        assert "slack_user" not in meta
        assert "slack_channel" not in meta
        assert "thread_ts" not in meta


# ---------------------------------------------------------------------------
# R3: Browser Extension
# ---------------------------------------------------------------------------

class TestBrowserCapture:
    def test_valid_browser_returns_201(self, client):
        resp = client.post("/capture/browser", json={
            "url": "https://example.com", "content": "selected text",
        }, headers=AUTH)
        assert resp.status_code == 201
        uuid.UUID(resp.json()["memory_id"])

    def test_missing_url_returns_422(self, client):
        resp = client.post("/capture/browser", json={"content": "c"}, headers=AUTH)
        assert resp.status_code == 422

    def test_missing_content_returns_422(self, client):
        resp = client.post("/capture/browser", json={"url": "https://example.com"}, headers=AUTH)
        assert resp.status_code == 422

    def test_auto_title_when_omitted(self, client):
        resp = client.post("/capture/browser", json={
            "url": "https://example.com", "content": "b" * 200,
        }, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["title"] == "b" * 80

    def test_explicit_title_used(self, client):
        resp = client.post("/capture/browser", json={
            "url": "https://example.com", "content": "c", "title": "My Title",
        }, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["title"] == "My Title"

    def test_source_type_and_url(self, client):
        resp = client.post("/capture/browser", json={
            "url": "https://example.com/page", "content": "c",
        }, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["source_type"] == "browser_extension"
        assert mem["source_url"] == "https://example.com/page"


# ---------------------------------------------------------------------------
# R4: Email Forward
# ---------------------------------------------------------------------------

class TestEmailCapture:
    def test_valid_email_returns_201(self, client):
        resp = client.post("/capture/email", json={
            "subject": "Fwd: Important", "body": "email body text",
        }, headers=AUTH)
        assert resp.status_code == 201
        uuid.UUID(resp.json()["memory_id"])

    def test_missing_subject_returns_422(self, client):
        resp = client.post("/capture/email", json={"body": "b"}, headers=AUTH)
        assert resp.status_code == 422

    def test_missing_body_returns_422(self, client):
        resp = client.post("/capture/email", json={"subject": "s"}, headers=AUTH)
        assert resp.status_code == 422

    def test_title_is_subject(self, client):
        resp = client.post("/capture/email", json={"subject": "My Subject", "body": "b"}, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["title"] == "My Subject"

    def test_metadata_includes_sender(self, client):
        resp = client.post("/capture/email", json={
            "subject": "s", "body": "b", "sender": "<email>",
        }, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["metadata"]["email_sender"] == "<email>"

    def test_source_type_is_email(self, client):
        resp = client.post("/capture/email", json={"subject": "s", "body": "b"}, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert mem["source_type"] == "email"

    def test_sender_omitted_when_not_provided(self, client):
        resp = client.post("/capture/email", json={"subject": "s", "body": "b"}, headers=AUTH)
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert "email_sender" not in mem["metadata"]


# ---------------------------------------------------------------------------
# R6: Health Check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_healthy_when_db_reachable(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy", "database": "connected"}

    def test_unhealthy_when_db_unreachable(self, test_db, clean_tables):
        with patch("src.capture_api.generate_embedding", side_effect=_deterministic_embedding), \
             patch("src.capture_api.is_reachable", return_value=False):
            c = TestClient(app)
            resp = c.get("/health")
        assert resp.status_code == 503
        assert resp.json() == {"status": "unhealthy", "database": "disconnected"}


# ---------------------------------------------------------------------------
# R8: Error Handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_embedding_failure_returns_500(self, test_db, clean_tables):
        with patch("src.capture_api.generate_embedding", side_effect=RuntimeError("Bedrock down")):
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/capture", json={"title": "t", "content": "c"}, headers=AUTH)
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal server error"

    def test_slack_embedding_failure_returns_500(self, test_db, clean_tables):
        with patch("src.capture_api.generate_embedding", side_effect=RuntimeError("Bedrock down")):
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/capture/slack", json={"text": "msg"}, headers=AUTH)
        assert resp.status_code == 500

    def test_browser_embedding_failure_returns_500(self, test_db, clean_tables):
        with patch("src.capture_api.generate_embedding", side_effect=RuntimeError("Bedrock down")):
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/capture/browser", json={"url": "https://x.com", "content": "c"}, headers=AUTH)
        assert resp.status_code == 500

    def test_email_embedding_failure_returns_500(self, test_db, clean_tables):
        with patch("src.capture_api.generate_embedding", side_effect=RuntimeError("Bedrock down")):
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/capture/email", json={"subject": "s", "body": "b"}, headers=AUTH)
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
#
# Patterns followed from existing project tests:
# - deadline=None for all DB-backed property tests (DB I/O is unpredictable)
# - suppress_health_check=[HealthCheck.function_scoped_fixture] when using
#   pytest fixtures (Hypothesis calls the test body N times within one
#   pytest invocation, but the fixture is set up once — this is intentional)
# - whitelist_categories for DB-safe text (explicit allowlist, not denylist)
# - @st.composite for complex strategies
# ---------------------------------------------------------------------------

# DB-safe text: letters, numbers, punctuation, spaces. No NUL, no surrogates,
# no control characters that PostgreSQL text columns reject.
_db_safe_chars = st.characters(whitelist_categories=("L", "N", "P", "Z"))
_db_safe_text = st.text(min_size=1, max_size=500, alphabet=_db_safe_chars)
_db_safe_content = st.text(min_size=1, max_size=1000, alphabet=_db_safe_chars)

valid_type_st = st.sampled_from(sorted(VALID_TYPES))


@st.composite
def capture_payload(draw):
    """Generate a valid generic capture JSON payload."""
    return {
        "title": draw(_db_safe_text),
        "content": draw(_db_safe_content),
        "type": draw(valid_type_st),
    }


@st.composite
def invalid_type_str(draw):
    """Generate a string that is NOT a valid memory type."""
    t = draw(st.text(min_size=1, max_size=50, alphabet=_db_safe_chars))
    if t in VALID_TYPES:
        t = t + "_invalid"
    return t


class TestPropertyGenericCapture:
    """Property: any valid CaptureRequest payload returns 201 with a UUID memory_id."""

    @given(payload=capture_payload())
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_valid_payloads_always_return_201(self, client, payload):
        resp = client.post("/capture", json=payload, headers=AUTH)
        assert resp.status_code == 201
        uuid.UUID(resp.json()["memory_id"])

    @given(bad_type=invalid_type_str())
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_invalid_types_always_return_422(self, client, bad_type):
        resp = client.post("/capture", json={
            "title": "t", "content": "c", "type": bad_type,
        }, headers=AUTH)
        assert resp.status_code == 422


class TestPropertySlackCapture:
    """Property: any non-empty text produces a memory with title ≤ 80 chars."""

    @given(text=_db_safe_content)
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_title_never_exceeds_80_chars(self, client, text):
        resp = client.post("/capture/slack", json={"text": text}, headers=AUTH)
        assert resp.status_code == 201
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert len(mem["title"]) <= 80


class TestPropertyBrowserCapture:
    """Property: auto-title is always ≤ 80 chars when title is omitted."""

    @given(content=_db_safe_content)
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_auto_title_never_exceeds_80_chars(self, client, content):
        resp = client.post("/capture/browser", json={
            "url": "https://example.com", "content": content,
        }, headers=AUTH)
        assert resp.status_code == 201
        from src.db import get_memory
        mem = get_memory(resp.json()["memory_id"])
        assert len(mem["title"]) <= 80


class TestPropertyAuth:
    """Property: any request without valid auth never returns 2xx."""

    @given(title=_db_safe_text, content=_db_safe_content)
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_wrong_token_never_succeeds(self, client, title, content):
        resp = client.post("/capture", json={"title": title, "content": content}, headers=WRONG_AUTH)
        assert resp.status_code == 401
