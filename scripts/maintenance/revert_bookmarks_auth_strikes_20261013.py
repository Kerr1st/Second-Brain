#!/usr/bin/env python3
"""Revert the wrong internal ``auth`` strikes of the 2026-10-13 bookmarks run.

That run (15:11:12Z-15:22:31Z) judged every successful Midway round-trip as
``auth`` and gave each internal bookmark one strike (126 entries, 2 strikes each
after the 10-10 run: one more and they cool off for 30 days). This removes that
one strike from internal-host entries (``needs_cookies``) whose last attempt is
an ``auth`` inside the run window. Public Crawlee ``auth`` (403) entries are left.

* attempts 2 -> 1, consecutive_failures -1, last_attempt_at := first_failed_at
  (all such entries had a single prior attempt, which was also their first);
* attempts 1 (first failure was today) -> entry removed;
* attempts > 2 -> not touched, reported (the prior last attempt is unknown).

Idempotent: a reverted entry's last attempt is no longer in the window. Dry run
by default (counts only); ``--apply`` writes through ``Ledger.save`` (atomic,
0600) after copying the original to ``<ledger>.pre-revert-20261013``. Only
domains and counts are printed; the ledger holds no URLs.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.capture.filters import needs_cookies  # noqa: E402
from src.capture.unreachable import Ledger, _parse_time  # noqa: E402

WINDOW = (datetime.fromisoformat("2026-10-13T15:11:12+00:00"), datetime.fromisoformat("2026-10-13T15:22:31+00:00"))


def plan(ledger):
    """[(key, action)] with action "decrement", "remove" or "skip"."""
    actions = []
    for key, entry in ledger.entries.items():
        last = _parse_time(entry.get("last_attempt_at"))
        if entry.get("status") != "auth" or last is None or not (WINDOW[0] <= last <= WINDOW[1]):
            continue
        if not needs_cookies(f"https://{entry.get('domain', '')}/"):
            continue
        attempts = int(entry.get("attempts") or 0)
        actions.append((key, "remove" if attempts <= 1 else "decrement" if attempts == 2 else "skip"))
    return actions


def apply(ledger, actions):
    for key, action in actions:
        if action == "remove":
            del ledger.entries[key]
        elif action == "decrement":
            entry = ledger.entries[key]
            entry["attempts"] = 1
            entry["consecutive_failures"] = max(0, int(entry.get("consecutive_failures") or 0) - 1)
            entry["last_attempt_at"] = entry.get("first_failed_at")
            entry["skip_until"] = None   # streak <= 2 < THRESHOLD


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, help="default: <status-dir>/bookmarks.unreachable.json")
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = parser.parse_args(argv)
    ledger = Ledger.load(args.ledger)
    actions = plan(ledger)
    by_action = Counter(a for _, a in actions)
    by_domain = Counter(ledger.entries[k].get("domain") for k, a in actions if a != "skip")
    print(f"ledger {ledger.path}: {len(ledger.entries)} entries; matched {len(actions)}: {dict(by_action)}")
    for domain, n in by_domain.most_common():
        print(f"  {domain}: {n}")
    if args.apply and by_action.keys() - {"skip"}:
        shutil.copy2(ledger.path, f"{ledger.path}.pre-revert-20261013")
        apply(ledger, actions)
        ledger.save()
        print("applied")
    elif not args.apply:
        print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
