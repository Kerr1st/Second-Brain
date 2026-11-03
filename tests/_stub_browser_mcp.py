"""A tiny real stdio MCP server that answers midway-playwright's ``browser_*`` tools.

Tests write :data:`STUB_SOURCE` to a tmp dir and start it with this interpreter
(``sys.executable``: the venv's ``mcp`` package, no network). Like the real
server it leaves a child in its process group (``/bin/sleep`` standing in for
Chromium), so a test can tell whether the whole group was reaped. It writes
``server.pid`` / ``child.pid`` (and ``navigating`` in ``slow`` mode) into
``$STUB_DIR``.

Modes (argv[1]): ``ok`` answers every tool (``/missing`` is a 404, ``/forbidden``
a 403); ``hang`` never reads stdin (initialize hangs); ``slow`` hangs inside
``browser_navigate``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

STUB_SOURCE = r'''
import asyncio, json, os, subprocess, sys, time
from pathlib import Path

mode = sys.argv[1]
d = Path(os.environ["STUB_DIR"])
child = subprocess.Popen(["/bin/sleep", "600"])
(d / "child.pid").write_text(str(child.pid))
(d / "server.pid").write_text(str(os.getpid()))
if mode == "hang":
    while True:
        time.sleep(1)

from mcp.server.fastmcp import FastMCP

app = FastMCP("stub-browser")
STATE = {"url": ""}
TEXT = "Stub page body text. " * 30


@app.tool()
async def browser_navigate(url: str) -> str:
    if mode == "slow":
        (d / "navigating").write_text(url)
        await asyncio.sleep(600)
    STATE["url"] = url
    return f"### Page\n- Page URL: {url}\n- Page Title: Stub\n"


@app.tool()
async def browser_evaluate(function: str) -> str:
    url = STATE["url"]
    status = 404 if "/missing" in url else 403 if "/forbidden" in url else 200
    state = {"u": url, "t": "Stub", "x": TEXT, "f": 0, "s": status}
    return "### Result\n" + json.dumps(json.dumps(state))


@app.tool()
async def browser_snapshot() -> str:
    return "### Snapshot\n```yaml\n- text: stub\n```"


@app.tool()
async def browser_close() -> str:
    return "### Result\nclosed"


app.run()
'''


def write_stub(directory: Path) -> Path:
    path = Path(directory) / "stub_browser_mcp.py"
    path.write_text(STUB_SOURCE)
    return path


def alive(pid: int) -> bool:
    """True when ``pid`` runs (a zombie counts as gone). psutil when available."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Signal 0 also reaches a zombie. Only our own children can be zombies here (an
    # orphan is reaped by launchd), and those waitpid can collect.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped == 0


def wait_for_file(path: Path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path.name} never appeared")


def wait_gone(pids, timeout: float = 10.0) -> list[int]:
    """The pids still alive after ``timeout`` (empty: all gone)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = [p for p in pids if alive(p)]
        if not left:
            return []
        time.sleep(0.1)
    return [p for p in pids if alive(p)]


def stub_pids(directory: Path) -> list[int]:
    return [int(wait_for_file(Path(directory) / name)) for name in ("server.pid", "child.pid")]


def stub_env(directory: Path) -> dict:
    return {"STUB_DIR": str(directory), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(directory))}


PYTHON = sys.executable
