#!/usr/bin/env python3
"""Batch ingest staged chats into the Second Brain.

Reads all markdown files from staging/chats/, runs each through
the ingest pipeline (chunk → embed via Bedrock → store in PostgreSQL),
and moves successfully ingested files to staging/ingested/.
"""
import os, sys, time, json, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import is_reachable
from src.ingest import ingest_content

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

STAGING = os.path.join(os.path.dirname(__file__), "..", "staging", "chats")
INGESTED = os.path.join(os.path.dirname(__file__), "..", "staging", "ingested")
FAILED = os.path.join(os.path.dirname(__file__), "..", "staging", "failed")

def main():
    if not is_reachable():
        log.error("PostgreSQL not reachable"); sys.exit(1)

    os.makedirs(INGESTED, exist_ok=True)
    os.makedirs(FAILED, exist_ok=True)

    files = sorted(f for f in os.listdir(STAGING) if f.endswith(".md"))
    log.info(f"Found {len(files)} staged chats")

    ok = fail = 0
    for i, fname in enumerate(files):
        path = os.path.join(STAGING, fname)
        try:
            with open(path) as f:
                content = f.read()

            # Determine source_type from filename prefix
            source_type = "kiro_cli_chat" if fname.startswith("cli_") else "kiro_ide_chat"
            parent_id = ingest_content(content, source_type=source_type, source_url=fname)

            if parent_id:
                os.rename(path, os.path.join(INGESTED, fname))
                ok += 1
            else:
                os.rename(path, os.path.join(FAILED, fname))
                fail += 1

            if (i + 1) % 100 == 0:
                log.info(f"Progress: {i+1}/{len(files)} (ok={ok} fail={fail})")

        except Exception as e:
            log.error(f"FAIL {fname}: {e}")
            try:
                os.rename(path, os.path.join(FAILED, fname))
            except:
                pass
            fail += 1

    log.info(f"Done: ok={ok} fail={fail} total={len(files)}")

if __name__ == "__main__":
    main()
