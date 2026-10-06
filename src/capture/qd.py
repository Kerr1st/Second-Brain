"""One QD capture job, preserving per-channel outcomes on partial failures."""
from __future__ import annotations
import argparse
import json
import logging
import os
from src.capture.common import exit_code


def capture(*, dry_run=False):
    from src.capture import quick_desktop, qd_eventlog, qd_chats, qd_tags
    stages = {"facts": quick_desktop.capture, "tags": qd_tags.enrich_memories,
              "feed": qd_eventlog.ingest_eventlog, "chats": qd_chats.capture}
    # QD document export is a retired channel. Its replay adapter remains available
    # as python -m src.capture.qd_docs, not an hourly false-health signal.
    results = {}
    for name, run in stages.items():
        try:
            results[name] = run(dry_run=dry_run)
        except Exception:  # noqa: BLE001 - one stage never hides the others' outcome (R3 S3-8)
            logging.getLogger(__name__).exception("QD %s capture failed", name)
            results[name] = {"failed": 1}
    if not dry_run and not exit_code(results.get("feed", {})):
        try:
            if os.path.isfile(qd_eventlog.INTERACTIONS_PATH):
                results["interactions"] = qd_eventlog.enrich_with_interactions()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("QD interaction enrichment failed")
            results["interactions"] = {"failed": 1}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = capture(dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
