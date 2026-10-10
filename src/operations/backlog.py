"""Persistent-backlog state a job reports about itself, read by liveness.

A job that legitimately leaves work for its next run (``scrape_bookmarks.py``
defers the URLs its time budget cannot start; exit 0) records how often that
happened in a row in ``<status-dir>/<job>.backlog.json``.  One deferral is
normal; ``consecutive_deferred_runs`` reaching the threshold means the backlog
is not draining.  The job then prints a WARNING and the liveness sweep reports an
advisory (``[backlog.<job>]`` in ``config/liveness.toml``) — never a failure.

The wrapper owns ``<job>.json`` and rewrites it on every run, so the job keeps
this counter in its own sidecar file instead of patching the status record.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUFFIX = ".backlog.json"
DEFAULT_CONSECUTIVE_RUNS = 3


def status_dir(env=None) -> Path:
    """The wrapper's status directory, resolved the way ``jobs.run_job`` does."""
    env = os.environ if env is None else env
    log_dir = Path(env.get("SECOND_BRAIN_LOG_DIR", str(REPO / "logs")))
    return Path(env.get("SECOND_BRAIN_STATUS_DIR", str(log_dir / "jobs")))


def state_path(job: str, directory=None) -> Path:
    return Path(directory or status_dir()) / f"{job}{SUFFIX}"


def read_state(job: str, directory=None) -> dict:
    try:
        value = json.loads(state_path(job, directory).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_run(job: str, deferred: int, directory=None, *, now=None) -> dict:
    """Update the streak after one completed run and return the new state."""
    now = now or datetime.now(timezone.utc)
    previous = read_state(job, directory)
    try:
        streak = int(previous.get("consecutive_deferred_runs") or 0)
    except (TypeError, ValueError):
        streak = 0
    deferred = max(0, int(deferred))
    state = {"schema_version": 1, "job": job, "deferred": deferred,
             "consecutive_deferred_runs": streak + 1 if deferred else 0,
             "first_deferred_at": (previous.get("first_deferred_at") or now.isoformat()) if deferred else None,
             "updated_at": now.isoformat()}
    path = state_path(job, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".backlog-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return state


def threshold(job: str, policy=None) -> int:
    """``[backlog.<job>].consecutive_runs`` from the liveness policy, else the default."""
    try:
        if policy is None:
            from src.operations.liveness import load_policy
            policy = load_policy()
        return int(policy.get("backlog", {}).get(job, {}).get("consecutive_runs", DEFAULT_CONSECUTIVE_RUNS))
    except Exception:  # noqa: BLE001 - an unreadable policy must not stop the job; use the default
        return DEFAULT_CONSECUTIVE_RUNS
