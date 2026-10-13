"""Capture liveness: channel ages, generic zero-output, dream-cycle freshness and
the scheduled-job status sweep — one policy file, one durable report.

Invoked by ``scripts/jobs/verify_liveness.sh`` as ``python -m src.operations.liveness``.
Reads ``config/liveness.toml`` (override with ``SECOND_BRAIN_LIVENESS_CONFIG``),
runs read-only SELECTs against the DB named by ``DB_HOST/DB_PORT/DB_NAME/DB_USER``
(the installed defaults are production; tests point them at a disposable
cluster), inspects ``logs/jobs/*.json`` and writes every finding to
``logs/jobs/liveness-report.json`` before printing.  Job-written backlog sidecars
(``logs/jobs/<job>.backlog.json``, src.operations.backlog) are read by the
``[backlog.<job>]`` check, never by the status sweep.  Exit 1 iff a *required*
finding is unhealthy.  Nothing here notifies; the shell adapter and the wrapper do.

Every predicate is a pure function over already-fetched rows so it can be tested
with deliberately broken fixtures without a database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / "config" / "liveness.toml"
SEVERITIES = ("required", "advisory", "retired")
NEVER = 99999
#: Job-written backlog sidecars (src.operations.backlog) are not wrapper status records.
BACKLOG_SUFFIX = ".backlog.json"
#: Every job-written sidecar in logs/jobs/ that is not a wrapper status record:
#: backlog streaks and the bookmarks unreachable ledger (src.capture.unreachable).
SIDECAR_SUFFIXES = (BACKLOG_SUFFIX, ".unreachable.json")

#: Rows that are evidence of a capture. Index-only chunk rows are not: the Codex
#: writer's ``*_chunk`` children of Topic Segments / task memories, and any chunk
#: a corpus-maintenance rechunk created (``maintenance_manifest``) -- a rechunk
#: run today must not make a silent channel look fresh.
CAPTURE_ROWS = ("COALESCE(metadata->>'record_kind', '') NOT IN ('topic_segment_chunk', 'task_memory_chunk') "
                "AND NOT COALESCE(metadata ? 'maintenance_manifest', false)")


# --------------------------------------------------------------------------- policy

def load_policy(path=None) -> dict:
    path = Path(path or os.environ.get("SECOND_BRAIN_LIVENESS_CONFIG", DEFAULT_CONFIG))
    policy = tomllib.loads(path.read_text(encoding="utf-8"))
    channels = policy.get("channels") or {}
    if not channels:
        raise ValueError("liveness policy defines no channels")
    for label, channel in channels.items():
        if channel.get("severity") not in SEVERITIES:
            raise ValueError(f"channel {label}: severity must be one of {SEVERITIES}")
        if not isinstance(channel.get("max_age_days"), int) or channel["max_age_days"] <= 0:
            raise ValueError(f"channel {label}: max_age_days must be a positive integer")
        if not str(channel.get("predicate", "")).strip():
            raise ValueError(f"channel {label}: predicate is required")
        if channel["severity"] != "required" and not str(channel.get("reason", "")).strip():
            raise ValueError(f"channel {label}: a non-required channel must state its reason")
    policy.setdefault("zero_output", {})
    policy.setdefault("dream_cycle", {})
    jobs = policy.setdefault("jobs", {})
    jobs.setdefault("expected_interval_hours", {})
    advisory = jobs.setdefault("advisory", {})
    if not isinstance(advisory, dict):
        raise ValueError("[jobs.advisory] must be a table of job name = reason")
    for job, reason in advisory.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"[jobs.advisory] {job}: an advisory job must state its reason")
    backlog = policy.setdefault("backlog", {})
    if not isinstance(backlog, dict):
        raise ValueError("[backlog] must be a table of [backlog.<job>] entries")
    for job, entry in backlog.items():
        if not isinstance(entry, dict):
            raise ValueError(f"[backlog.{job}] must be a table")
        runs = entry.get("consecutive_runs")
        if not isinstance(runs, int) or isinstance(runs, bool) or runs <= 0:
            raise ValueError(f"[backlog.{job}] consecutive_runs must be a positive integer")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"[backlog.{job}]: a backlog advisory must state its reason")
    return policy


def _finding(check, subject, state, *, healthy, severity, message, **extra) -> dict:
    return {"check": check, "subject": subject, "state": state, "healthy": healthy,
            "severity": severity, "message": message, **extra}


# ------------------------------------------------------------------- channel ages

def evaluate_channels(ages: dict, policy: dict) -> list[dict]:
    """``ages``: label → days since the channel's newest row (NEVER when none, None
    when the query failed)."""
    findings = []
    for label, channel in policy["channels"].items():
        age, max_age, severity = ages.get(label), channel["max_age_days"], channel["severity"]
        if age is None:
            findings.append(_finding("channel", label, "query_failed", healthy=False, severity="required",
                                     message=f"{label} QUERY_FAILED: cannot establish capture liveness"))
            continue
        if severity == "retired":
            findings.append(_finding("channel", label, "retired", healthy=True, severity=severity,
                                     message=f"{label} retired — last capture {age}d ago (not monitored): {channel['reason']}",
                                     age_days=age, max_age_days=max_age))
        elif age > max_age:
            state = "never_produced" if age >= NEVER else "silent"
            if severity == "advisory":
                findings.append(_finding("channel", label, state, healthy=False, severity=severity,
                                         message=f"{label} silent {age}d (max {max_age}d) — advisory: {channel['reason']}",
                                         age_days=age, max_age_days=max_age))
            else:
                findings.append(_finding("channel", label, state, healthy=False, severity=severity,
                                         message=f"{label} SILENT — last capture {age}d ago (max {max_age}d)",
                                         age_days=age, max_age_days=max_age))
        else:
            findings.append(_finding("channel", label, "healthy", healthy=True, severity=severity,
                                     message=f"{label} ok ({age}d / max {max_age}d)", age_days=age, max_age_days=max_age))
    return findings


# ---------------------------------------------------------------- zero output

def tolerance_days(days_seen: list[date], floor: int) -> int:
    """Largest observed gap between consecutive capture days, plus one, floored."""
    days = sorted(set(days_seen))
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    return max(floor, (max(gaps) + 1) if gaps else floor)


def zero_output_findings(today: date, last_seen: dict, days_seen: dict, enumerated: set, policy: dict) -> list[dict]:
    """Every source_type that ever produced output.  ``last_seen``: type → date of
    newest row; ``days_seen``: type → capture days inside the look-back window;
    ``enumerated``: types covered by a configured channel (their fixed threshold is
    the ❌ rule, so this generic check is only advisory for them)."""
    cfg = policy.get("zero_output", {})
    ignore = set(cfg.get("ignore", []))
    floor_adv = int(cfg.get("min_silent_days_advisory", 2))
    floor_req = int(cfg.get("min_silent_days_required", 7))
    min_history = int(cfg.get("min_history_days_advisory", 10))
    findings = []
    for source_type in sorted(last_seen):
        if source_type in ignore:
            continue
        covered = source_type in enumerated
        severity = "advisory" if covered else "required"
        floor = floor_adv if covered else floor_req
        history = days_seen.get(source_type, [])
        tolerance = tolerance_days(history, floor)
        age = (today - last_seen[source_type]).days
        if covered and len(set(history)) < min_history:
            # A sparse enumerated type (a handful of capture days in the window) has
            # no learnable cadence; its fixed channel threshold is the only rule.
            findings.append(_finding("zero_output", source_type, "insufficient_history", healthy=True, severity=severity,
                                     message=f"{source_type}: {len(set(history))} capture days in the window — cadence not "
                                             f"learnable, fixed channel threshold applies", age_days=age, enumerated=covered))
            continue
        if age > tolerance:
            findings.append(_finding("zero_output", source_type, "zero_output", healthy=False, severity=severity,
                                     message=(f"{source_type} produced zero rows for {age}d; its own cadence tolerates "
                                              f"{tolerance}d" + (" (enumerated channel — advisory)" if covered
                                                                  else " (NOT an enumerated channel)")),
                                     age_days=age, tolerance_days=tolerance, enumerated=covered))
        else:
            findings.append(_finding("zero_output", source_type, "healthy", healthy=True, severity=severity,
                                     message=f"{source_type} ok ({age}d / tolerates {tolerance}d)",
                                     age_days=age, tolerance_days=tolerance, enumerated=covered))
    return findings


# ---------------------------------------------------------------- dream cycle

def dream_cycle_findings(runs: list[dict], now: datetime, policy: dict) -> list[dict]:
    """``runs``: newest first; each has started_at, completed_at, candidates_generated."""
    cfg = policy.get("dream_cycle", {})
    max_age = int(cfg.get("max_completed_age_days", 7))
    zero_required = int(cfg.get("zero_candidate_runs_required", 2))
    completed = [r for r in runs if r.get("completed_at") is not None]
    findings = []
    if not completed:
        findings.append(_finding("dream_cycle", "completed_at", "never_completed", healthy=False, severity="required",
                                 message="dream_cycle has no completed run"))
        return findings
    newest = completed[0]
    age = (now.date() - newest["completed_at"].date()).days
    if age > max_age:
        findings.append(_finding("dream_cycle", "completed_at", "stalled", healthy=False, severity="required",
                                 message=f"dream_cycle last completed {age}d ago (max {max_age}d)", age_days=age))
    else:
        findings.append(_finding("dream_cycle", "completed_at", "healthy", healthy=True, severity="required",
                                 message=f"dream_cycle completed {age}d ago (max {max_age}d)", age_days=age))
    zero_streak = 0
    for run in completed:
        if (run.get("candidates_generated") or 0) == 0:
            zero_streak += 1
        else:
            break
    if zero_streak >= zero_required:
        findings.append(_finding("dream_cycle", "candidates", "zero_candidates", healthy=False, severity="required",
                                 message=f"dream_cycle: the last {zero_streak} completed runs generated 0 candidates",
                                 zero_candidate_streak=zero_streak))
    elif zero_streak:
        findings.append(_finding("dream_cycle", "candidates", "zero_candidates", healthy=False, severity="advisory",
                                 message=f"dream_cycle: newest completed run generated 0 candidates ({zero_streak} in a row; "
                                         f"❌ at {zero_required})", zero_candidate_streak=zero_streak))
    else:
        findings.append(_finding("dream_cycle", "candidates", "healthy", healthy=True, severity="required",
                                 message=f"dream_cycle newest run generated {newest.get('candidates_generated')} candidates"))
    return findings


# ------------------------------------------------------------ job status sweep

def inspect_statuses(directory, *, now=None, failure_days=1, expected_interval_hours=None,
                     network_failures_required=2, advisory=None) -> list[dict]:
    """Read ``<dir>/*.json`` written by src.operations.jobs.  Flags: a persisted
    non-zero exit (``failed``/``interrupted`` older than ``failure_days``), a running
    batch job past its deadline, a service whose heartbeat went stale, and — new in
    R5 — a status file not updated for longer than the job's expected interval, or
    a configured job with no status file at all (advisory: not yet run once).
    ``network_unavailable`` (exit 68: the preflight never saw DNS come up after a
    dark wake, the job was not started) is infrastructure, not the job: advisory
    with the reason, ❌ once it has happened ``network_failures_required`` scheduled
    runs in a row.  ``advisory``: job → reason (``[jobs.advisory]``); an unhealthy
    status of such a job is reported ⚠ with the reason and never fails the sweep."""
    now = now or datetime.now(timezone.utc)
    expected = dict(expected_interval_hours or {})
    advisory = dict(advisory or {})
    results = []
    seen = set()
    for path in sorted(Path(directory).glob("*.json")):
        if path.name == "liveness-report.json" or path.name.endswith(SIDECAR_SUFFIXES):
            continue
        try:
            value = json.loads(path.read_text())
            state = value["state"]
            job = value["job"]
            seen.add(job)
            result = {"job": job, "state": state, "exit_code": value.get("exit_code"), "healthy": True, "severity": "required"}
            if state == "network_unavailable":
                streak = int(value.get("consecutive_network_failures") or 1)
                persistent = streak >= network_failures_required
                result.update(healthy=False, persistent=persistent, consecutive_network_failures=streak,
                              infrastructure=True, severity="required" if persistent else "advisory",
                              message=f"job {job} not started: network unavailable at launch (exit {value.get('exit_code')}, "
                                      f"probe {value.get('network_probe_host')}, waited {value.get('network_waited_s')}s) — "
                                      f"infrastructure (dark wake before the network was up), {streak} scheduled run(s) in a row"
                                      + ("" if persistent else f"; ❌ at {network_failures_required}"))
            elif state in {"failed", "interrupted"}:
                first = datetime.fromisoformat(value["first_failure_at"])
                age = (now - first).total_seconds() / 86400
                persistent = age >= failure_days
                result.update(healthy=False, failure_age_days=round(age, 3), persistent=persistent,
                              consecutive_failures=value.get("consecutive_failures"),
                              severity="required" if persistent else "advisory",
                              message=f"job {job} last exit {value.get('exit_code')} ({value.get('consecutive_failures')} consecutive, "
                                      f"first failure {age:.1f}d ago)")
            elif state == "running":
                start = datetime.fromisoformat(value["started_at"])
                age = (now - start).total_seconds()
                if value.get("service"):
                    heartbeat = value.get("heartbeat_at")
                    limit = (float(value.get("health_interval_s", 30)) + float(value.get("health_timeout_s", 3))) * int(value.get("health_failure_limit", 3)) + 60
                    if heartbeat:
                        if (now - datetime.fromisoformat(heartbeat)).total_seconds() > limit:
                            result.update(healthy=False, state="stale_service_heartbeat",
                                          message=f"service {job}: no healthy heartbeat for > {limit:.0f}s")
                    elif age > float(value["timeout_s"]) + 60:
                        result.update(healthy=False, state="service_startup_overdue", message=f"service {job}: never became healthy")
                elif age > float(value["timeout_s"]) + 60:
                    result.update(healthy=False, state="overdue_running",
                                  message=f"job {job} 'running' for {age/3600:.1f}h, past its {float(value['timeout_s'])/3600:.1f}h deadline — wrapper lost?")
            elif state != "succeeded":
                raise ValueError("unknown job state")
            if result["healthy"] and job in expected and not value.get("service"):
                last_update = datetime.fromisoformat(value.get("finished_at") or value["started_at"])
                idle_h = (now - last_update).total_seconds() / 3600
                if idle_h > float(expected[job]):
                    result.update(healthy=False, state="stale_status", idle_hours=round(idle_h, 1),
                                  message=f"job {job} has not been started for {idle_h:.0f}h (expected every {expected[job]}h) — is its plist loaded?")
            result.setdefault("message", f"job {job} {result['state']} (exit {result['exit_code']})")
            if not result["healthy"] and job in advisory:
                result.update(severity="advisory", advisory_reason=advisory[job],
                              message=f"{result['message']} — advisory: {advisory[job]}")
        except (OSError, ValueError, KeyError, TypeError):
            result = {"job": path.stem, "state": "invalid_status", "healthy": False, "severity": "required",
                      "message": f"job {path.stem}: unreadable status file"}
        results.append(result)
    for job in sorted(set(expected) - seen):
        results.append({"job": job, "state": "missing", "healthy": True, "severity": "advisory",
                        "message": f"job {job}: no status file yet (never run under the wrapper on this host)"})
    return results


def job_findings(directory, policy: dict, now=None) -> list[dict]:
    cfg = policy.get("jobs", {})
    out = []
    for r in inspect_statuses(directory, now=now, failure_days=int(cfg.get("failure_days", 1)),
                              expected_interval_hours=cfg.get("expected_interval_hours", {}),
                              network_failures_required=int(cfg.get("network_failures_required", 2)),
                              advisory=cfg.get("advisory", {})):
        out.append(_finding("job", r["job"], r["state"], healthy=r["healthy"], severity=r.get("severity", "required"),
                            message=r.get("message", ""), **{k: v for k, v in r.items()
                                                             if k not in {"job", "state", "healthy", "severity", "message"}}))
    return out


def backlog_findings(directory, policy: dict) -> list[dict]:
    """``<dir>/<job>.backlog.json`` (src.operations.backlog) for every ``[backlog.<job>]``:
    ``consecutive_deferred_runs`` at or above ``consecutive_runs`` is an advisory with the
    configured reason; a backlog never fails the sweep.  A missing state file is healthy
    (the job has not completed a run that recorded it yet)."""
    from src.operations import backlog as backlog_state
    findings = []
    for job, entry in sorted((policy.get("backlog") or {}).items()):
        limit = int(entry["consecutive_runs"])
        state = backlog_state.read_state(job, directory)
        if not state:
            findings.append(_finding("backlog", job, "no_state", healthy=True, severity="advisory",
                                     message=f"{job}: no backlog state recorded yet", consecutive_runs=limit))
            continue
        def _count(key):
            # A malformed state file must not crash the sweep (record_run tolerates it too).
            try:
                return max(0, int(state.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        streak = _count("consecutive_deferred_runs")
        deferred = _count("deferred")
        extra = {"consecutive_deferred_runs": streak, "deferred": deferred, "consecutive_runs": limit,
                 "first_deferred_at": state.get("first_deferred_at")}
        cause = state.get("cause") if isinstance(state.get("cause"), str) else None
        if cause:
            extra["cause"] = cause
        if streak >= limit:
            findings.append(_finding("backlog", job, "persistent_backlog", healthy=False, severity="advisory",
                                     message=f"{job}: {deferred} item(s) deferred, {streak} consecutive run(s) with "
                                             f"deferrals (threshold {limit}) — advisory: {entry['reason']}"
                                             f"{f' (last run: {cause})' if cause else ''}",
                                     advisory_reason=entry["reason"], **extra))
        else:
            findings.append(_finding("backlog", job, "healthy", healthy=True, severity="advisory",
                                     message=f"{job} backlog ok ({deferred} deferred; {streak} consecutive / "
                                             f"threshold {limit})", **extra))
    return findings


# ------------------------------------------------------------------- database

def _connect():
    import psycopg2
    return psycopg2.connect(host=os.environ.get("DB_HOST", "127.0.0.1"), port=int(os.environ.get("DB_PORT", "5432")),
                            dbname=os.environ.get("DB_NAME", "memory_bank"), user=os.environ.get("DB_USER", "memory_bank"),
                            password=os.environ.get("DB_PASSWORD", "memory_bank"), connect_timeout=5,
                            options="-c default_transaction_read_only=on -c statement_timeout=30000")


def fetch_channel_ages(conn, policy) -> dict:
    ages = {}
    for label, channel in policy["channels"].items():
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COALESCE(now()::date - MAX(created_at)::date, {NEVER}) FROM memories "
                            f"WHERE ({channel['predicate']}) AND {CAPTURE_ROWS}")
                ages[label] = int(cur.fetchone()[0])
        except Exception:  # a broken predicate must surface as query_failed, not kill the sweep
            conn.rollback()
            ages[label] = None
    return ages


def fetch_output_history(conn, policy) -> tuple[dict, dict, set]:
    history_days = int(policy.get("zero_output", {}).get("history_days", 90))
    predicates = [c["predicate"] for c in policy["channels"].values() if c["severity"] != "retired"]
    with conn.cursor() as cur:
        cur.execute("SELECT source_type, MAX(created_at)::date FROM memories "
                    f"WHERE source_type IS NOT NULL AND source_type <> '' AND {CAPTURE_ROWS} GROUP BY source_type")
        last_seen = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT source_type, created_at::date FROM memories WHERE source_type IS NOT NULL AND source_type <> '' "
                    f"AND {CAPTURE_ROWS} AND created_at >= now() - make_interval(days => %s) GROUP BY 1, 2", (history_days,))
        days_seen: dict = {}
        for source_type, day in cur.fetchall():
            days_seen.setdefault(source_type, []).append(day)
        enumerated = set()
        if predicates and last_seen:
            columns = ", ".join(f"({p})" for p in predicates)
            cur.execute(f"SELECT source_type, ARRAY[{columns}]::bool[] FROM "
                        "(SELECT DISTINCT source_type FROM memories WHERE source_type IS NOT NULL AND source_type <> '') s")
            for source_type, flags in cur.fetchall():
                if any(flags):
                    enumerated.add(source_type)
    return last_seen, days_seen, enumerated


def fetch_runs(conn, limit=5) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT started_at, completed_at, candidates_generated FROM dream_cycle_runs "
                    "ORDER BY started_at DESC LIMIT %s", (limit,))
        return [{"started_at": s, "completed_at": c, "candidates_generated": g} for s, c, g in cur.fetchall()]


# ----------------------------------------------------------------------- main

def run(policy, status_dir, *, conn=None, now=None, skip_db=False) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    findings = []
    if not skip_db:
        own = conn is None
        if own:
            try:
                conn = _connect()
            except Exception as exc:
                findings.append(_finding("database", "connection", "unreachable", healthy=False, severity="required",
                                         message=f"database unreachable for liveness queries: {type(exc).__name__}"))
                conn = None
        if conn is not None:
            try:
                findings += evaluate_channels(fetch_channel_ages(conn, policy), policy)
                last_seen, days_seen, enumerated = fetch_output_history(conn, policy)
                findings += zero_output_findings(now.date(), last_seen, days_seen, enumerated, policy)
                findings += dream_cycle_findings(fetch_runs(conn), now, policy)
            finally:
                if own:
                    conn.close()
    findings += job_findings(status_dir, policy, now=now)
    findings += backlog_findings(status_dir, policy)
    return findings


def write_report(path: Path, findings: list[dict], now: datetime) -> dict:
    report = {"generated_at": now.isoformat(), "healthy": not any(required_failure(f) for f in findings),
              "required_failures": [f["subject"] for f in findings if required_failure(f)],
              "advisories": [f["subject"] for f in findings if not f["healthy"] and f["severity"] != "required"],
              "findings": findings}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, sort_keys=True, default=str, indent=1) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return report


def required_failure(finding: dict) -> bool:
    return not finding["healthy"] and finding["severity"] == "required"


def format_line(finding: dict) -> str:
    if finding["healthy"]:
        mark = "⚠" if finding["state"] in {"retired", "missing"} else "✅"
    else:
        mark = "❌" if finding["severity"] == "required" else "⚠"
    return f"{mark} [{finding['check']}:{finding['subject']}] {finding['message']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None)
    parser.add_argument("--status-dir", default=os.environ.get("SECOND_BRAIN_STATUS_DIR", str(REPO / "logs" / "jobs")))
    parser.add_argument("--report", default=None, help="report path (default <status-dir>/liveness-report.json)")
    parser.add_argument("--skip-db", action="store_true", help="status sweep only (no database)")
    parser.add_argument("--json", action="store_true", help="print JSON findings instead of lines")
    args = parser.parse_args(argv)
    policy = load_policy(args.config)
    now = datetime.now(timezone.utc)
    findings = run(policy, args.status_dir, now=now, skip_db=args.skip_db)
    report = write_report(Path(args.report or Path(args.status_dir) / "liveness-report.json"), findings, now)
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        for finding in findings:
            print(format_line(finding))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
