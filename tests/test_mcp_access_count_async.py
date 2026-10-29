"""memory_search must not wait on the access-count write (2026-10-29 MCP latency).

The reinforcement UPDATE is non-HOT on the live corpus (``last_accessed_at`` is
indexed), so each bumped row inserts into every index including HNSW; done
synchronously it added 1-2 s to every full-server search. The bump now runs on a
background writer: the tool result returns first, counts still land, and the
writer flushes on shutdown. The read-only registry never schedules a bump.
"""
import asyncio
import json
import logging
import threading
import time
from unittest.mock import patch

import src.db as db
from src import mcp_readonly, mcp_server, search


def AccessCountWriter(*args, **kwargs):  # resolved per test so the pre-fix run fails per test
    return search.AccessCountWriter(*args, **kwargs)

SLOW_WRITE_S = 2.0


def _hit(mid):
    return {"id": mid, "title": "t", "type": "idea", "content": "c", "rerank_score": 1.0}


def _search_over_protocol(arguments):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            return await client.call_tool("memory_search", arguments)

    result = asyncio.run(go())
    return json.loads(" ".join(c.text for c in result.content if hasattr(c, "text")))


def test_search_result_does_not_wait_for_slow_access_count_write():
    started, release, written = threading.Event(), threading.Event(), []

    def slow_increment(ids):
        started.set()
        release.wait(SLOW_WRITE_S)
        written.append(list(ids))

    hits = [_hit("00000000-0000-0000-0000-00000000000a"), _hit("00000000-0000-0000-0000-00000000000b")]
    writer = getattr(mcp_server, "_access_writer", None)
    with patch.object(mcp_server, "increment_access_count", slow_increment), \
         patch.object(mcp_server, "generate_embedding", return_value=[0.0] * 1024), \
         patch.object(mcp_server, "hybrid_search", return_value=hits), \
         patch.object(mcp_server, "rerank", side_effect=lambda rows, *a, **k: rows), \
         patch.object(mcp_server, "get_memory", return_value=None), \
         patch.object(mcp_server, "find_schemas_for_memories", return_value={}):
        t0 = time.perf_counter()
        payload = _search_over_protocol({"query": "anything", "limit": 5})
        elapsed = time.perf_counter() - t0
        returned_before_write = not written
        release.set()
        assert returned_before_write and elapsed < SLOW_WRITE_S / 2, f"tool result waited {elapsed:.2f}s on the write"
        assert writer.flush(timeout=5)

    assert [r["id"] for r in payload["results"]] == [h["id"] for h in hits]
    assert started.is_set()
    assert written == [sorted(h["id"] for h in hits)]


def test_counts_land_and_repeat_retrievals_coalesce(test_db, clean_tables):
    a = db.create_memory(type="idea", title="A", content="first")
    b = db.create_memory(type="idea", title="B", content="second")
    gate = threading.Event()
    calls = []

    def gated(ids):
        gate.wait(5)
        calls.append(list(ids))
        from src.search import increment_access_count
        increment_access_count(ids)

    writer = AccessCountWriter(gated)
    writer.schedule([a])          # taken by the thread, blocked on the gate
    time.sleep(0.2)
    writer.schedule([a, b])       # coalesced while the first write runs
    writer.schedule([a, a])       # duplicates within one result count once
    gate.set()
    assert writer.close(timeout=5)

    assert db.get_memory(a)["access_count"] == 3
    assert db.get_memory(b)["access_count"] == 1
    assert db.get_memory(a)["last_accessed_at"] is not None
    assert calls == [[a], sorted([a, b]), [a]]


def test_close_flushes_pending_bumps_and_stops_thread():
    written = []

    def slow(ids):
        time.sleep(0.3)
        written.append(list(ids))

    writer = AccessCountWriter(slow)
    writer.schedule(["x"])
    writer.schedule(["y"])
    assert writer.close(timeout=5)
    assert sorted(i for batch in written for i in batch) == ["x", "y"]
    assert not writer._thread.is_alive()
    writer.schedule(["z"])  # after shutdown: dropped, not written, no error
    assert writer.dropped == 1 and len(written) <= 2


def test_server_process_flushes_pending_bump_on_exit(tmp_path):
    """A bump scheduled just before the stdio server exits is still written."""
    import os
    import subprocess
    import sys
    out = tmp_path / "written.txt"
    code = (
        "import time\n"
        "import src.mcp_server as m\n"
        f"def slow(ids):\n    time.sleep(0.5)\n    open({str(out)!r}, 'a').write(','.join(ids))\n"
        "m._access_writer._write = slow\n"
        "m._access_writer.schedule(['x'])\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert out.read_text() == "x"


def test_write_errors_are_logged_not_raised(caplog, capsys):
    def boom(ids):
        raise RuntimeError("db down")

    writer = AccessCountWriter(boom)
    with caplog.at_level(logging.WARNING, logger="src.search"):
        writer.schedule(["x"])
        assert writer.flush(timeout=5)
        ok = []
        writer._write = ok.append
        writer.schedule(["y"])  # thread survived the failure
        assert writer.close(timeout=5)
    assert ok == [["y"]]
    assert any("access-count write failed" in r.getMessage() for r in caplog.records)
    assert capsys.readouterr().out == ""  # stdio transport: nothing on stdout


def test_pending_ids_are_bounded():
    gate = threading.Event()
    written = []
    writer = AccessCountWriter(lambda ids: (gate.wait(5), written.append(list(ids))), max_pending=2)
    writer.schedule(["busy"])
    time.sleep(0.2)
    writer.schedule(["a"])
    writer.schedule(["b", "c"])  # one of these exceeds max_pending=2
    writer.schedule(["a"])  # already pending: still counted
    gate.set()
    assert writer.close(timeout=5)
    assert writer.dropped == 1
    assert written[0] == ["busy"] and len(written[1]) == 2 and "a" in written[1] and written[2] == ["a"]


def test_readonly_search_never_schedules_a_bump():
    with patch.object(mcp_readonly.public, "generate_embedding", return_value=[1.0]), \
         patch.object(mcp_readonly.public, "hybrid_search", return_value=[_hit("00000000-0000-0000-0000-00000000000a")]), \
         patch.object(mcp_readonly.public, "rerank", side_effect=lambda rows, *a, **k: rows), \
         patch.object(mcp_readonly.public, "_access_writer") as writer, \
         patch.object(mcp_readonly.public, "increment_access_count") as reinforce:
        assert len(mcp_readonly.memory_search("test")["results"]) == 1
    writer.schedule.assert_not_called()
    reinforce.assert_not_called()
