"""Read job status records, including stale failures and interrupted wrappers."""
from __future__ import annotations
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path


def inspect_statuses(directory, *, now=None, failure_days=1) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    results = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            value = json.loads(path.read_text())
            state = value["state"]
            result = {"job": value["job"], "state": state, "exit_code": value.get("exit_code"), "healthy": True}
            if state in {"failed", "interrupted"}:
                first = datetime.fromisoformat(value["first_failure_at"])
                age = (now-first).total_seconds()/86400
                result.update(healthy=False, failure_age_days=round(age, 3), persistent=age >= failure_days)
            elif state == "running":
                start = datetime.fromisoformat(value["started_at"])
                age = (now-start).total_seconds()
                if value.get("service"):
                    heartbeat = value.get("heartbeat_at")
                    limit = (float(value.get("health_interval_s", 30)) + float(value.get("health_timeout_s", 3))) * int(value.get("health_failure_limit", 3)) + 60
                    if heartbeat:
                        if (now-datetime.fromisoformat(heartbeat)).total_seconds() > limit:
                            result.update(healthy=False, state="stale_service_heartbeat")
                    elif age > float(value["timeout_s"]) + 60:
                        result.update(healthy=False, state="service_startup_overdue")
                elif age > float(value["timeout_s"]) + 60:
                    result.update(healthy=False, state="overdue_running")
            elif state != "succeeded":
                raise ValueError("unknown job state")
        except (OSError, ValueError, KeyError, TypeError):
            result = {"job":path.stem,"state":"invalid_status","healthy":False}
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=os.environ.get("SECOND_BRAIN_STATUS_DIR", str(Path(__file__).resolve().parents[2]/"logs/jobs")))
    args = parser.parse_args()
    results = inspect_statuses(args.directory)
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return int(any(not result["healthy"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
