#!/usr/bin/env python3
"""`brief` — the on-demand Express surface (P1).

Composes a briefing from the synthesis the system already produced (dream-cycle
insights, detected contradictions, resurfaced high-value memories, the weekly
digest, open questions), runs the LLM editor pass to rank + headline, and prints
scannable Markdown to stdout. Read-only.

Usage:
  .venv/bin/python scripts/brief.py                # full briefing (LLM-edited)
  .venv/bin/python scripts/brief.py --no-llm       # deterministic ordering, no LLM call
  .venv/bin/python scripts/brief.py --window-days 30
  .venv/bin/python scripts/brief.py --json         # raw composed items as JSON
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import express

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


class _Deterministic:
    """Sentinel invoker that forces the deterministic editor (no LLM call)."""

    def invoke(self, *a, **k):
        raise RuntimeError("--no-llm")


def main():
    ap = argparse.ArgumentParser(description="Print your second-brain briefing.")
    ap.add_argument("--window-days", type=int, default=express.INSIGHT_WINDOW_DAYS,
                    help="How far back to pull dream-cycle insights (default %(default)s).")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip the LLM editor pass; use deterministic ordering.")
    ap.add_argument("--json", action="store_true",
                    help="Emit the composed candidate items as JSON (no editing/rendering).")
    args = ap.parse_args()

    briefing = express.compose_briefing(window_days=args.window_days)

    if args.json:
        # created_at is a datetime; serialize defensively.
        print(json.dumps(briefing, default=str, indent=2))
        return

    briefing = express.edit_briefing(briefing, invoker=_Deterministic() if args.no_llm else None)
    sys.stdout.write(express.render_markdown(briefing))


if __name__ == "__main__":
    main()
