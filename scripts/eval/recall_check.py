#!/usr/bin/env python3
"""Monitor HNSW recall@10 (indexed vs exact) over a sample of queries.

Catches vector-index degradation (e.g. after a bulk delete, which VACUUM does not
repair) before it silently hurts retrieval. Compares the HNSW-indexed top-10 against
a forced exact (seqscan) top-10 and reports mean overlap. Exits non-zero if mean
recall@10 < threshold so it can drive an alert.

Usage: .venv/bin/python scripts/eval/recall_check.py [--threshold 0.85]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import src.db as db
from src.embeddings import generate_embedding

QUERIES = [
    "agentic SDLC narrative proof points",
    "decision to de-scope S3 backups",
    "dream cycle four agent synthesis pipeline",
    "AWS GenAI certification exam study",
    "context engineering for AI agents",
    "Quick Desktop knowledge graph entity import",
    "what am I spending most of my time working on",
    "personal memory architecture design principles",
    "Kiro adoption playbook",
    "why switch CLI chat parser to JSONL",
]


def _topk(cur, emb, exact, k=10):
    flag = "off" if exact else "on"
    cur.execute(f"SET LOCAL enable_indexscan={flag}")
    cur.execute(f"SET LOCAL enable_bitmapscan={flag}")
    cur.execute("SELECT id FROM memories WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s", (emb, k))
    return {str(r[0]) for r in cur.fetchall()}


def measure():
    total = 0.0
    rows = []
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            for q in QUERIES:
                emb = str(generate_embedding(q))
                exact = _topk(cur, emb, True)
                indexed = _topk(cur, emb, False)
                r = len(exact & indexed) / 10.0
                total += r
                rows.append((r, q))
    return total / len(QUERIES), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    mean, rows = measure()
    if args.verbose:
        for r, q in rows:
            print(f"recall@10={r:.1f}  {q[:48]}")
    print(f"mean recall@10 = {mean:.2f} over {len(QUERIES)} queries (threshold {args.threshold})")
    sys.exit(0 if mean >= args.threshold else 1)


if __name__ == "__main__":
    main()
