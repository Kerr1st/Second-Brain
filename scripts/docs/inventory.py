#!/usr/bin/env python3
"""Generate counted documentation facts directly from source, without importing services.

Writes ``docs/generated/runtime-inventory.{json,md}``. ``tests/test_repo_hygiene.py``
regenerates the snapshot and fails when the committed files drift from source.
Program arguments in the plists are absolute paths on the installed Mac; they are
recorded relative to the repository root so the generated files carry no home path.
"""
from __future__ import annotations

import ast
import json
import plistlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_INSTALLED_ROOT = re.compile(r"^/Users/[^/]+/second-brain/")


def _relative(argument: str) -> str:
    return _INSTALLED_ROOT.sub("", argument)


def snapshot(root: Path = ROOT) -> dict:
    config = tomllib.loads((root / "config/backends.toml").read_text())
    tree = ast.parse((root / "src/backends/resolver.py").read_text())
    adapters: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "DEFAULT_ADAPTERS":
            adapters = [ast.literal_eval(key) for key in node.value.keys]
    schedules = []
    for p in sorted((root / "scheduling").glob("*.plist")):
        doc = plistlib.loads(p.read_bytes())
        env = doc.get("EnvironmentVariables", {})
        schedules.append({
            "file": p.name,
            "label": doc.get("Label"),
            "calendar": doc.get("StartCalendarInterval"),
            "interval_seconds": doc.get("StartInterval"),
            "keep_alive": doc.get("KeepAlive", False),
            "arguments": [_relative(a) for a in doc.get("ProgramArguments", [])],
            "deadline_s": next((int(v) for k, v in env.items() if k.startswith("SECOND_BRAIN_JOB_TIMEOUT_")), None),
            "needs_network": env.get("SECOND_BRAIN_JOB_NEEDS_NETWORK") == "1",
            "exit_timeout_s": doc.get("ExitTimeOut"),
        })
    roles = sorted(config["profiles"][config["default_profile"]])
    cap = re.search(r"SECOND_BRAIN_RUN_COST_CAP_USD:-([0-9.]+)", (root / "scripts/jobs/dream_cycle_scheduled.sh").read_text())
    base = re.search(r"DEFAULT_RUN_COST_CAP_USD = ([0-9.]+)", (root / "src/backends/cost.py").read_text())
    migrations = sorted(p.name for p in (root / "migrations").glob("[0-9]*.sql"))
    return {
        "adapters": sorted(adapters),
        "default_profile": config["default_profile"],
        "profiles": sorted(config["profiles"]),
        "roles": roles,
        "migrations": migrations,
        "migration_high_water": migrations[-1],
        "scheduled_spend_threshold_usd": float(cap.group(1)) if cap else None,
        "library_spend_threshold_usd": float(base.group(1)),
        "schedules": schedules,
    }


def markdown(facts: dict) -> str:
    lines = [
        "# Generated runtime inventory",
        "",
        "Source-derived configuration, not installed or healthy service state. Regenerate with `.venv/bin/python scripts/docs/inventory.py`; `tests/test_repo_hygiene.py` fails on drift.",
        "",
        f"Adapters: {len(facts['adapters'])} ({', '.join(facts['adapters'])}). Profiles: {len(facts['profiles'])} ({', '.join(facts['profiles'])}). Roles per profile: {len(facts['roles'])} ({', '.join(facts['roles'])}). Schedule templates: {len(facts['schedules'])}.",
        f"Default profile: `{facts['default_profile']}`. Migrations: {len(facts['migrations'])} files, high-water `{facts['migration_high_water']}`.",
        f"Scheduled dream-cycle spend threshold: ${facts['scheduled_spend_threshold_usd']:g}; library default: ${facts['library_spend_threshold_usd']:g}. Both stop after a metered response and can overshoot.",
        "",
        "| Schedule | Calendar (launchd local time) / interval | Program (via job_wrapper.sh) | Deadline s | Network preflight | KeepAlive |",
        "|---|---|---|---|---|---|",
    ]
    for s in facts["schedules"]:
        if s["calendar"]:
            cadence = json.dumps(s["calendar"], sort_keys=True)
        elif s["interval_seconds"]:
            cadence = f"{s['interval_seconds']} seconds"
        else:
            cadence = "on demand / RunAtLoad"
        args = s["arguments"]
        wrapper = next((i for i, a in enumerate(args) if a.endswith("job_wrapper.sh")), -1)
        program = " ".join(args[wrapper + 1:])
        deadline = "service" if s["keep_alive"] else (str(s["deadline_s"]) if s["deadline_s"] else "—")
        lines.append(f"| `{s['label']}` | {cadence} | `{program}` | {deadline} | {'yes' if s['needs_network'] else 'no'} | {str(s['keep_alive']).lower()} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    data = snapshot()
    target = ROOT / "docs/generated"
    target.mkdir(exist_ok=True)
    (target / "runtime-inventory.json").write_text(json.dumps(data, indent=2) + "\n")
    (target / "runtime-inventory.md").write_text(markdown(data))
    print(f"wrote {target / 'runtime-inventory.md'}")
