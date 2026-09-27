"""Manual metered probe for the revision step: ask the Thinker backend to repair a candidate
given the panel's blocking/advisory issues, then write the revised candidates as a
--candidate-file for scripts/panel_replay.py.

    AWS_PROFILE=second-brain-jobs .venv/bin/python scripts/revise_probe.py \\
        --replay logs/panel_replay/<v2 run>.jsonl --candidate <id> [--candidate <id>] \\
        --out /tmp/revised.json --profile controlled

Phase 1 experiment (docs/PLAN-panel-acceptance.md): does removing the defects the
Astra seats named turn a rejected candidate into an accepted one?
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.panel_replay import candidate_from_row, fetch_candidates, sources_text_for  # noqa: E402
from src.backends.resolver import default_resolver, resolve_stage
from src.backends.cost import SpendTracker, invoke_metered  # noqa: E402

log = logging.getLogger("revise_probe")

REVISION_PROMPT = """\
You are repairing a candidate insight for a personal knowledge system so that it is \
grounded in its cited sources. You are given the candidate (JSON), the full text of its \
cited source memories, and the issues four evaluators raised.

Rules:
- Remove or correct every BLOCKING issue: delete any statement, number, name, channel or \
quotation that does not appear in the cited sources; fix any claim the sources contradict; \
attribute wording to whoever the source shows produced it (a person, a system, an agent's \
summary of an email is not the email's author); drop conclusions that rest on uncited material.
- Address ADVISORY issues by narrowing: state each claim at the scope the sources support and \
quantify ("in the two captured notices") instead of generalising ("every", "never").
- Keep what is supported. Do not add new claims or new sources. Keep the WHAT / EVIDENCE / \
WHY IT MATTERS structure; end EVIDENCE with one sentence beginning "claim_scope:".
- Set confidence by the evidence: 1 source = low; 2 sources or several from one day/conversation \
= medium; 3+ independent sources = high.
- Rewrite the title so it makes no claim the body does not support.

Return ONLY the revised candidate as a JSON object with the same fields as the input \
(title, type, operation, target_memory_id, supersedes_reason, schema_operation, schema_note, \
confidence, confidence_reasoning, content, source_memories, relationships, strategy_that_found_it)."""

REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "type": {"type": "string"}, "operation": {"type": "string"},
        "target_memory_id": {"type": ["string", "null"]}, "supersedes_reason": {"type": ["string", "null"]},
        "schema_operation": {"type": "string"}, "schema_note": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence_reasoning": {"type": "string"}, "content": {"type": "string"},
        "source_memories": {"type": "array", "items": {"type": "string"}},
        "relationships": {"type": "array", "items": {"type": "object"}},
        "strategy_that_found_it": {"type": "string"},
    },
    "required": ["title", "content", "confidence", "source_memories"],
}


def issues_for(replay_path: str, candidate_id: str) -> tuple[list[str], list[str]]:
    blocking, advisory = [], []
    for line in open(replay_path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("candidate_id") != candidate_id:
            continue
        blocking += [f"[{r['seat']}] {b}" for b in (r.get("blocking_issues") or [])]
        advisory += [f"[{r['seat']}] {a}" for a in (r.get("advisory_issues") or [])]
    return blocking, advisory


def revise(candidate, sources_text: str, blocking: list[str], advisory: list[str], invoker, effort: str, *, spec, tracker: SpendTracker) -> dict:
    user = json.dumps({
        "candidate": asdict(candidate),
        "cited_sources": sources_text,
        "blocking_issues": blocking,
        "advisory_issues": advisory,
    }, default=str)
    result = invoke_metered(invoker, spec, tracker, system_prompt=REVISION_PROMPT, user_message=user, effort=effort,
                            stage="revise_probe", schema=REVISION_SCHEMA)
    out = result.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    usage = result.get("usage") or {}
    log.info("revised %r -> %r | usage in=%s out=%s", candidate.title[:50], str(out.get("title", ""))[:60],
             usage.get("input_tokens") or usage.get("inputTokens"), usage.get("output_tokens") or usage.get("outputTokens"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True, help="panel_replay JSONL (standard variant) with issue lists")
    ap.add_argument("--candidate", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="controlled")
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    os.environ["SECOND_BRAIN_PROFILE"] = args.profile
    resolver = default_resolver()
    invoker, spec = resolve_stage("thinker", resolver=resolver)
    tracker = SpendTracker.from_env()

    revised: dict[str, dict] = {}
    for row in fetch_candidates(None, args.candidate, None):
        cand = candidate_from_row(row)
        blocking, advisory = issues_for(args.replay, row["id"])
        if not blocking and not advisory:
            log.warning("%s: no issues recorded in %s; skipping", row["id"], args.replay)
            continue
        out = revise(cand, sources_text_for(cand), blocking, advisory, invoker, args.effort, spec=spec, tracker=tracker)
        if out.get("title") and out.get("content"):
            revised[row["id"]] = {k: out[k] for k in ("title", "content", "confidence", "confidence_reasoning") if k in out}
    json.dump(revised, open(args.out, "w"), indent=2)
    log.info("wrote %d revised candidate(s) to %s", len(revised), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
