#!/usr/bin/env python3
"""`brief` — the on-demand Express surface, and the controls that shape it.

Composes a briefing from what the system has already synthesized (dream-cycle
insights, detected contradictions, resurfaced high-value memories, the weekly
digest, open questions), ranks it with an LLM editor, and prints scannable
Markdown. Read-only unless you pass a feedback flag.

VIEW
  brief                      full briefing (LLM-edited headlines)
  brief --no-llm             fast deterministic ranking, no LLM call
  brief --window-days 30     widen the insight window (default 14)
  brief --json               raw composed items as JSON

SHAPE WHAT IT SURFACES (delivery preferences — a gradient)
  brief --useful <target>    boost this (rank it higher in future briefings)
  brief --less   <target>    soft down-weight (show it, but lower)
  brief --mute   <target>    hard hide (never surface it)
  brief --unmute <target>    clear any preference for a target
  brief --prefs              list your current preferences

  <target> is one of:
    • an item id   — the `#abcd1234` shown next to each briefing item
    • a kind       — insight | contradiction | resurface | digest | question
    • a topic      — any project/topic name (e.g. a project tag)

  Examples:
    brief --mute resurface             # stop surfacing "worth revisiting" items
    brief --useful contradiction       # I want more contradiction-spotting
    brief --less kiro                  # less about the "kiro" topic
    brief --mute 1a2b3c4d              # hide one specific item
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


def _print_prefs():
    prefs = express.list_feedback()
    if not prefs:
        print("No preferences set. Shape the briefing with --useful / --less / --mute <target>.")
        return
    print("Current Express preferences:")
    for p in prefs:
        print(f"  {p['signal']:7s} {p['target_type']:5s}  {p['target_key']}")


def main():
    ap = argparse.ArgumentParser(
        description="Print your second-brain briefing, and shape what it surfaces.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--window-days", type=int, default=express.INSIGHT_WINDOW_DAYS,
                    help="How far back to pull dream-cycle insights (default %(default)s).")
    ap.add_argument("--no-llm", action="store_true", help="Skip the LLM editor pass.")
    ap.add_argument("--json", action="store_true", help="Emit composed candidate items as JSON.")
    # Feedback (delivery preferences)
    ap.add_argument("--useful", metavar="TARGET", help="Boost an item id / kind / topic.")
    ap.add_argument("--less", metavar="TARGET", help="Soft down-weight an item id / kind / topic.")
    ap.add_argument("--mute", metavar="TARGET", help="Hard-hide an item id / kind / topic.")
    ap.add_argument("--unmute", metavar="TARGET", help="Clear any preference for a target.")
    ap.add_argument("--prefs", action="store_true", help="List current preferences.")
    args = ap.parse_args()

    # Feedback actions short-circuit (perform + confirm, don't render).
    signal_map = {"useful": args.useful, "less": args.less, "mute": args.mute}
    for signal, target in signal_map.items():
        if target:
            res = express.record_feedback(target, signal)
            print(f"OK — {signal} {res['target_type']} '{res['target_key']}'. "
                  f"Future briefings will reflect this.")
            return
    if args.unmute:
        n = express.remove_feedback(args.unmute)
        print(f"Cleared {n} preference(s) for '{args.unmute}'." if n
              else f"No preference was set for '{args.unmute}'.")
        return
    if args.prefs:
        _print_prefs()
        return

    briefing = express.compose_briefing(window_days=args.window_days)
    if args.json:
        print(json.dumps(briefing, default=str, indent=2))
        return
    briefing = express.edit_briefing(briefing, invoker=_Deterministic() if args.no_llm else None)
    sys.stdout.write(express.render_markdown(briefing))


if __name__ == "__main__":
    main()
