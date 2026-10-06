"""Direct Kiro IDE/CLI capture, replacing the extract-to-staging schedule.

Stable source identities are refreshed atomically by ingestion when sessions grow.
Existing legacy identities are reused, preserving citations during the transition.
Staged leftovers have a separate, explicitly invoked maintenance drain.
"""
from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path

from src.capture.common import CaptureBatch, exit_code
from src.capture.framework import run_channel
from src.parsers import ide_chat, cli_chat, kiro_sessions

logger = logging.getLogger(__name__)


def _legacy_url(identity, source, known):
    aliases = ([f"chat://{identity}", f"ide_{identity}.md"] if source == "kiro_ide_chat" else
               [f"chat://{identity[:12]}", f"cli_{identity[:12]}.md"])
    return next((url for url in aliases if url in known), f"chat://{identity}")


def capture(*, dry_run=False, ide=True, cli=True) -> dict:
    from src.db import get_processed_source_urls
    result = {}
    if ide:
        items = CaptureBatch()
        known = set(get_processed_source_urls("kiro_ide_chat")) if not dry_run else set()
        for path in ide_chat.find_chat_files():
            try:
                parsed = ide_chat.parse_chat_file(path, strict=True)
                if parsed:
                    name, md, _ = parsed
                    items.append((_legacy_url(name, "kiro_ide_chat", known), md))
            except (OSError, ValueError, KeyError):
                logger.exception("Legacy IDE chat could not be parsed")
                items.failed += 1
        seen = set()
        for path in kiro_sessions.find_sessions():
            try:
                parsed = kiro_sessions.parse_session(path)
                if parsed:
                    url, md, fields = parsed
                    # If a session migrated from the legacy store, retain its
                    # existing identity and replace the older representation.
                    identity = fields["Source_ID"]
                    aliases = [f"chat://{identity}", f"ide_{identity}.md"]
                    available = known | {existing for existing, _ in items}
                    url = next((alias for alias in aliases if alias in available), url)
                    if url not in seen:
                        seen.add(url)
                        items[:] = [(existing, content) for existing, content in items if existing != url]
                        items.append((url, md))
            except (OSError, ValueError, KeyError):
                logger.warning("Native IDE session could not be parsed; retry next cycle", exc_info=True)
                items.failed += 1
        result["kiro_ide_chat"] = run_channel("kiro_ide_chat", items, dry_run=dry_run, refresh=True)
    if cli:
        items = CaptureBatch()
        known = set(get_processed_source_urls("kiro_cli_chat")) if not dry_run else set()
        # Iterate files independently: one malformed session cannot hide later ones.
        for path in sorted(Path(cli_chat.SESSIONS_DIR).glob("*.jsonl")):
            try:
                identity = path.stem
                messages, created = cli_chat.extract_session_messages(path, strict=True)
                parsed = cli_chat.parse_conversation(identity, messages, created)
                if parsed:
                    _, md, _ = parsed
                    items.append((_legacy_url(identity, "kiro_cli_chat", known), md))
            except (OSError, ValueError, KeyError):
                logger.exception("CLI chat could not be parsed")
                items.failed += 1
        result["kiro_cli_chat"] = run_channel("kiro_cli_chat", items, dry_run=dry_run, refresh=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ide", action="store_true")
    parser.add_argument("--no-cli", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = capture(dry_run=args.dry_run, ide=not args.no_ide, cli=not args.no_cli)
    print(json.dumps(result, sort_keys=True))
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
