"""``ingest_batch`` keeps R1's per-item resilience: one bad item never aborts a channel.

R3 narrowed the per-item handler to a tuple of expected types, so a TypeError or
KeyError raised by one malformed item aborted the whole batch (every later item
unprocessed, no failure count). The handler must catch ``Exception`` -- the
failure is logged with its traceback and counted -- while ``BaseException``
subclasses (KeyboardInterrupt, SystemExit, RunInterrupted) still propagate.
"""
import logging

import pytest

import src.ingest as ingest
from src.dream_cycle.orchestrator import RunInterrupted


def _vector(_text=""):
    return [1.0] + [0.0] * 1023


@pytest.fixture
def quiet_ingest(test_db, clean_tables, monkeypatch):
    monkeypatch.setattr(ingest, "generate_embedding", _vector)
    monkeypatch.setattr(ingest, "_discover_relationships", lambda *a: None)
    return ingest


@pytest.mark.parametrize("error", [TypeError("malformed item"), KeyError("missing field"),
                                   AttributeError("bad object"), RuntimeError("unexpected"),
                                   AssertionError("programming error")])
def test_unexpected_exception_on_one_item_is_counted_and_the_batch_continues(quiet_ingest, monkeypatch, caplog, error):
    real = quiet_ingest.ingest_content
    def flaky(content, source_type, source_url, **kw):
        if source_url == "test://bad":
            raise error
        return real(content, source_type, source_url, **kw)
    monkeypatch.setattr(quiet_ingest, "ingest_content", flaky)
    items = [("test://one", "first document body"), ("test://bad", "poison"), ("test://two", "second document body")]
    with caplog.at_level(logging.ERROR, logger="src.ingest"):
        stats = quiet_ingest.ingest_batch(items, "article")
    assert stats == {"processed": 2, "skipped": 0, "failed": 1}
    record = next(r for r in caplog.records if "Failed to ingest source" in r.getMessage())
    assert type(error).__name__ in record.getMessage() and record.exc_info is not None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1), RunInterrupted(15)])
def test_interrupts_are_not_swallowed(quiet_ingest, monkeypatch, interrupt):
    def interrupted(*a, **kw):
        raise interrupt
    monkeypatch.setattr(quiet_ingest, "ingest_content", interrupted)
    with pytest.raises(type(interrupt)):
        quiet_ingest.ingest_batch([("test://one", "body")], "article")
