#!/usr/bin/env python3
"""Run the dream cycle pipeline with fixture capture enabled.

Captures raw agent inputs/outputs to tests/fixtures/golden_run/ for
building the golden-path replay test.

Usage:
    python scripts/run_golden_capture.py
"""

import logging
import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

# Suppress noisy AWS SDK logging
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from src.dream_cycle import DreamCycleOrchestrator

orch = DreamCycleOrchestrator()
result = orch.run(run_type="user_triggered", capture_dir="tests/fixtures/golden_run")

print(f"\n{'='*60}")
print(f"Run ID:    {result.run_id}")
print(f"Generated: {result.candidates_generated}")
print(f"Accepted:  {result.candidates_accepted}")
print(f"Rejected:  {result.candidates_rejected}")
print(f"Aborted:   {result.aborted_early}")
print(f"Digest:    {result.digest_path}")
print(f"{'='*60}")

sys.exit(0 if not result.aborted_early else 1)
