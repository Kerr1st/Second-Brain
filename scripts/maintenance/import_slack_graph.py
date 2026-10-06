#!/usr/bin/env python3
"""One-shot entry for the Slack social-graph writer (Channel/Person entities).

Replaces scripts/migrate/import_slack_graph.py (8dd3bb7). The hourly run is the
`graph` stage of `python -m src.capture.qd`; this entry exists for a manual or
dry-run import: .venv/bin/python scripts/maintenance/import_slack_graph.py [--dry-run]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.capture.slack_graph import main

if __name__ == "__main__":
    raise SystemExit(main())
