"""memory_search must not wait on the access-count write (2026-10-29 MCP latency).

The reinforcement UPDATE is non-HOT on the live corpus (``last_accessed_at`` is
indexed), so each bumped row inserts into every index including HNSW; done
synchronously it added 1-2 s to every full-server search. The bump now runs on a
background writer: the tool result returns first, counts still land, and the
writer flushes on a clean exit or SIGTERM (driven through a real stdio client
below). The read-only registry never schedules a bump.
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

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
    assert writer.flush(timeout=5)  # drain bumps from earlier full-server searches in the session
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
    gate, taken = threading.Event(), threading.Event()
    calls = []

    def gated(ids):
        taken.set()
        gate.wait(5)
        calls.append(list(ids))
        from src.search import increment_access_count
        increment_access_count(ids)

    writer = AccessCountWriter(gated)
    writer.schedule([a])          # taken by the thread, blocked on the gate
    assert taken.wait(5)
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


#: Runs the real ``src.mcp_server`` ``__main__`` block (as ``python -m`` does) with
#: the write replaced by a slow file append, plus a ``bump`` tool that schedules one
#: bump. No DB: importing the server opens no connection and the write is fake.
_STDIO_WRAPPER = """
import runpy, sys, time
repo, out, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
sys.path.insert(0, repo)
import src.search as search
from mcp.server.fastmcp import FastMCP

def slow(ids):
    time.sleep(delay)
    with open(out, "a") as f:
        f.write(",".join(ids))

search.increment_access_count = slow  # bound by mcp_server's `from src.search import ...`
_run = FastMCP.run

def run(self, *args, **kwargs):
    writer = sys.modules["__main__"]._access_writer
    @self.tool()
    def bump() -> str:
        writer.schedule(["x"])
        return "ok"
    return _run(self, *args, **kwargs)

FastMCP.run = run
runpy.run_module("src.mcp_server", run_name="__main__", alter_sys=True)
"""


@pytest.mark.parametrize("delay", [0.5, 3.0], ids=["stdin-eof", "sigterm"])
def test_stdio_client_close_still_writes_pending_bump(tmp_path, delay):
    """The bump of the last search lands after a real stdio client closes the server.

    The client closes stdin, waits 2 s, sends SIGTERM, then SIGKILL 2 s later. A
    0.5 s write finishes in the atexit flush after stdin EOF. A 3.0 s write is still
    running at SIGTERM: Python's default action used to drop it; the ``__main__``
    handler now flushes before exiting.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client import stdio

    wrapper, out, errlog = tmp_path / "wrapper.py", tmp_path / "written.txt", tmp_path / "server.err"
    wrapper.write_text(_STDIO_WRAPPER)
    params = StdioServerParameters(
        command=sys.executable, args=[str(wrapper), os.getcwd(), str(out), str(delay)], cwd=os.getcwd(),
        env={**os.environ, "DB_HOST": "/nonexistent", "DB_PORT": "1"})

    async def go():
        with open(errlog, "w") as err:
            async with stdio.stdio_client(params, errlog=err) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("bump", {})
                    t0 = time.monotonic()
        return result, time.monotonic() - t0

    result, close_s = asyncio.run(go())
    assert result.content[0].text == "ok"
    if delay > stdio.PROCESS_TERMINATION_TIMEOUT:
        assert close_s >= stdio.PROCESS_TERMINATION_TIMEOUT  # the SIGTERM path was taken
    assert "Traceback" not in errlog.read_text()
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
    gate, taken = threading.Event(), threading.Event()
    written = []
    writer = AccessCountWriter(lambda ids: (taken.set(), gate.wait(5), written.append(list(ids))), max_pending=2)
    writer.schedule(["busy"])
    assert taken.wait(5)
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
