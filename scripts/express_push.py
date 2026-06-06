#!/usr/bin/env python3
"""express_push — the proactive Gmail push (P2), gated high.

Chained after the noon dream cycle. Sends an email ONLY when the latest dream
cycle produced a cross-project synthesis or a contradiction (see should_push);
otherwise logs why and exits 0. A run-id state file prevents re-sending the same
run if the job runs more than once.

Usage:
  scripts/express_push.py             # gated send
  scripts/express_push.py --dry-run   # compose + render + print, never send (proves composition)
  scripts/express_push.py --force     # ignore the already-pushed guard
  scripts/express_push.py --no-llm     # deterministic headlines (skip the editor LLM)

Email config (never committed): EXPRESS_EMAIL_TO, EXPRESS_EMAIL_FROM, GMAIL_APP_PASSWORD.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import express

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("express_push")

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", ".express_last_push"
)


def _read_last_pushed() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_last_pushed(run_id: str) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(run_id)
    except OSError as exc:
        log.warning("Could not persist last-push state: %s", exc)


class _DeterministicInvoker:
    def invoke(self, *a, **k):
        raise RuntimeError("--no-llm")


def main() -> int:
    ap = argparse.ArgumentParser(description="Proactive Express email (gated).")
    ap.add_argument("--dry-run", action="store_true", help="Compose + print, never send.")
    ap.add_argument("--force", action="store_true", help="Ignore the already-pushed guard.")
    ap.add_argument("--no-llm", action="store_true", help="Skip the editor LLM pass.")
    args = ap.parse_args()

    last = None if args.force else _read_last_pushed()
    decision = express.should_push(last_pushed_run_id=last)
    if not decision["push"]:
        log.info("Nothing worth sending (%s).", decision["reason"])
        return 0

    log.info("Push triggered for run %s — %s", decision["run_id"][:8], decision["reason"])
    briefing = express.compose_briefing()
    briefing = express.edit_briefing(briefing, invoker=_DeterministicInvoker() if args.no_llm else None)
    email = express.render_email(briefing)

    if args.dry_run:
        log.info("[DRY-RUN] would send: %s", email["subject"])
        sys.stdout.write("\n" + email["text"] + "\n")
        return 0

    if not express.email_configured():
        log.info("Email not configured yet (set %s / %s / %s); composed but not sending.",
                 express.ENV_TO, express.ENV_FROM, express.ENV_PASSWORD)
        return 0

    try:
        express.send_email(email["subject"], email["html"], email["text"])
    except RuntimeError as exc:
        log.error("Send failed: %s", exc)
        return 1
    _write_last_pushed(decision["run_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
