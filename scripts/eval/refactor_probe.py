#!/usr/bin/env python3
"""Refactor retrieval benchmark — re-run after each pillar to measure lift.

Exercises the real agent path (generate_embedding -> hybrid_search -> rerank)
over 12 queries spanning the user's use cases, and reports three metrics:
  - distilled_in_top5: queries with a distilled (decision/insight/synthesis...) hit in top-5
  - decision_recall_top3: decision queries whose distilled_chat answer is in top-3
  - near_dup: queries with a near-duplicate in top-10

Trajectory:
  baseline (pre-refactor):     distilled 3/12 | decision 0/4 | near_dup 9/12
  after P1 (distill):          distilled 6/12 | decision 2/4 | near_dup 8/12
  after P2a (retrieval dedup):  distilled 7/12 | decision 3/4 | near_dup 0/12
  after P2b (IDE dedup+vacuum): distilled 8/12 | decision 3/4 | near_dup 0/12
  after HNSW rebuild (m=32/efc=200, ef_search=200): distilled 10/12 | decision 4/4 | near_dup 0/12

Usage: .venv/bin/python scripts/eval/refactor_probe.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.embeddings import generate_embedding
from src.search import hybrid_search, rerank

DISTILLED = {"insight", "synthesis", "decision", "research", "connection"}
QUERIES = {
    "narrative": ["agentic SDLC narrative proof points", "Kiro adoption playbook talking points"],
    "decision_recent": ["decision to de-scope S3 backups in favor of Google Drive",
                        "why switch Kiro CLI chat parser to read JSONL session files",
                        "decision to retire redundant ingest_session_events script",
                        "rationale for per-pipeline Quick Desktop liveness monitoring"],
    "work": ["Quick Desktop knowledge graph entity import", "dream cycle four agent synthesis pipeline"],
    "learning": ["AWS GenAI certification exam study", "context engineering for AI agents"],
    "synthesis": ["personal memory architecture design principles", "what am I spending most of my time working on"],
}


def main():
    n = dist5 = dupq = dec_hit = dec_n = 0
    for cat, qs in QUERIES.items():
        for q in qs:
            res = rerank(hybrid_search(q, generate_embedding(q), limit=10), q)
            n += 1
            if any(r.get("type") in DISTILLED for r in res[:5]):
                dist5 += 1
            pref = [re.sub(r"\s+", " ", (r.get("content") or "").lower()).strip()[:200] for r in res]
            if len(pref) - len(set(pref)) > 0:
                dupq += 1
            if cat == "decision_recent":
                dec_n += 1
                rank = next((i + 1 for i, r in enumerate(res) if r.get("source_type") == "distilled_chat"), None)
                if rank and rank <= 3:
                    dec_hit += 1
    print(f"distilled_in_top5={dist5}/{n} | decision_recall_top3={dec_hit}/{dec_n} | near_dup={dupq}/{n}")


if __name__ == "__main__":
    main()
