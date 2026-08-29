#!/usr/bin/env python3
"""Review, approve, and publish Second Brain steering guidance."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.steering import (
    SteeringProposal,
    approve_steering_candidate,
    evaluate_steering_proposal,
)
from src.steering_publisher import preview_agents_rule, publish_agents_rule


def _json_object(value: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="run the four-member Dream Cycle panel")
    review.add_argument("--title", required=True)
    review.add_argument("--wording", required=True)
    review.add_argument("--source-memory-id", action="append", required=True)
    review.add_argument("--proposed-scope", choices=("project", "personal", "system"))
    review.add_argument("--applicability", type=_json_object, default={})
    review.add_argument("--supersedes-rule-id")

    approve = commands.add_parser("approve", help="activate one accepted candidate")
    approve.add_argument("candidate_id")
    approve.add_argument("--wording", required=True)
    approve.add_argument("--scope", required=True, choices=("project", "personal", "system"))
    approve.add_argument("--applicability", type=_json_object, required=True)

    preview = commands.add_parser("preview-agents", help="print a reviewable AGENTS.md diff")
    preview.add_argument("rule_id")
    preview.add_argument("--path", type=Path, default=Path("AGENTS.md"))

    publish = commands.add_parser("publish-agents", help="apply an already reviewed AGENTS.md diff")
    publish.add_argument("rule_id")
    publish.add_argument("--path", type=Path, default=Path("AGENTS.md"))
    publish.add_argument("--expected-current-digest", required=True)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "review":
        result = evaluate_steering_proposal(
            SteeringProposal(
                title=args.title,
                wording=args.wording,
                source_memory_ids=tuple(args.source_memory_id),
                proposed_authority_scope=args.proposed_scope,
                proposed_applicability=args.applicability,
                supersedes_rule_id=args.supersedes_rule_id,
            )
        )
        print(json.dumps(asdict(result), sort_keys=True))
        return 0 if result.final_verdict in {"ACCEPTED", "DUPLICATE"} else 1
    if args.command == "approve":
        result = approve_steering_candidate(
            args.candidate_id,
            wording=args.wording,
            authority_scope=args.scope,
            applicability=args.applicability,
        )
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    if args.command == "preview-agents":
        result = preview_agents_rule(args.rule_id, args.path)
        print(result.current_digest)
        print(result.diff, end="")
        return 0
    result = publish_agents_rule(
        args.rule_id,
        args.path,
        expected_current_digest=args.expected_current_digest,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
