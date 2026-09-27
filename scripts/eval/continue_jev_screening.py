#!/usr/bin/env python3
"""Manually reviewed continuation using verified successful synthetic receipts.

This never retries automatically. An explicit billing snapshot must reconcile the
previous attempt, whose outstanding reservation remains a conservative buffer.
The frozen evaluator, fixture, client and original result directory are unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.eval import jev_screening as evaluation
from src.evidence_screening import ALLOWED_RETURNED_MODELS, JevClient, build_request


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def request_hash(query, candidate):
    payload = build_request(query, evaluation.clean_candidate(candidate))
    return evaluation.digest(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def verify_previous(previous, billing_path):
    """Read only frozen synthetic files and an explicitly reviewed billing record."""
    manifest, data, sha = evaluation.load_bundle()
    previous, billing_path = Path(previous), Path(billing_path)
    report_bytes = (previous / "report.json").read_bytes()
    receipts_bytes = (previous / "results.jsonl").read_bytes()
    report, billing = json.loads(report_bytes), json.loads(billing_path.read_bytes())
    if (report["manifest_sha256"] != sha or report["complete"] is not False
            or evaluation.digest((previous / "manifest.json").read_bytes()) != sha):
        raise ValueError("Original run does not match the incomplete frozen evaluation")
    if (billing.get("original_report_sha256") != evaluation.digest(report_bytes)
            or billing.get("original_results_sha256") != evaluation.digest(receipts_bytes)
            or billing.get("confirmed_numerical_match") is not True
            or billing.get("same_key_confirmed") is not True
            or not isinstance(billing.get("review_note"), str) or not billing["review_note"].strip()):
        raise ValueError("Billing review must identify and explicitly confirm this run")
    cases = sorted(data["cases"], key=lambda c: c["split"] != "calibration")
    expected = [(c, p) for c in cases for p in c["candidates"]]
    records = [json.loads(line) for line in receipts_bytes.decode().splitlines()]
    if not 0 < len(records) < 504:
        raise ValueError("Expected a partial original run")
    cache, successes, known = {}, {}, 0.0
    seen = set()
    for index, record in enumerate(records):
        case, candidate = expected[index]
        key = (record["case_id"], record["candidate_id"])
        if key in seen or key != (case["id"], candidate["id"]):
            raise ValueError("Duplicate, reordered or unrecognized original receipt")
        seen.add(key)
        result = record["result"]
        fingerprint = request_hash(case["query"], candidate)
        if result.get("request_sha256") != fingerprint:
            raise ValueError("Cached request differs from current frozen payload")
        if result.get("cost_usd") is not None:
            if not number(result["cost_usd"]):
                raise ValueError("Invalid original cost")
            known += result["cost_usd"]
        if result["status"] != "ok":
            if index != len(records) - 1:
                raise ValueError("Original run continued after failure")
            continue
        scores, usage = result.get("scores"), result.get("usage")
        if (not isinstance(scores, dict) or set(scores) != set(evaluation.SCORES)
                or any(not number(v) or v > 1 for v in scores.values())
                or not isinstance(usage, dict) or not number(usage.get("cost"))
                or usage["cost"] != result["cost_usd"]
                or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens"))
                or result.get("returned_model") not in ALLOWED_RETURNED_MODELS
                or not number(result.get("latency_ms"))):
            raise ValueError("Invalid cached successful response")
        if fingerprint in cache:
            raise ValueError("Repeated cached request fingerprint")
        cache[fingerprint], successes[key] = result, result
    original = report["spend"]
    if (not number(original.get("known_cost_usd")) or not number(original.get("reserved_cost_usd"))
            or not number(original.get("max_cost_usd")) or original["max_cost_usd"] > .10
            or not math.isclose(known, original["known_cost_usd"], rel_tol=0, abs_tol=1e-12)
            or original["calls"] != len(records)
            or report["status_counts"] != dict(Counter(r["result"]["status"] for r in records))):
        raise ValueError("Original report and receipt accounting differ")
    before, after = billing["before"], billing["after"]
    if (not number(before.get("usage")) or not number(after.get("usage"))
            or not math.isclose(after["usage"] - before["usage"], known, rel_tol=0, abs_tol=1e-12)
            or datetime.fromisoformat(before["verified_at"]) > datetime.fromisoformat(report["started_at"])
            or datetime.fromisoformat(after["verified_at"]) < datetime.fromisoformat(report["finished_at"])):
        raise ValueError("Account usage or snapshot times do not reconcile")
    if (previous / "calibration.json").exists():
        expected_calibration = evaluation.calibrate(cases, successes, manifest["injection_threshold_options"])
        if json.loads((previous / "calibration.json").read_text()) != expected_calibration:
            raise ValueError("Original calibration differs from its cached scores")
    budget = original["max_cost_usd"] - known - original["reserved_cost_usd"]
    if budget <= 0:
        raise ValueError("No budget remains after known spend and the unresolved reservation")
    return {"sha": sha, "cache": cache, "original": report, "billing": billing,
            "new_budget_usd": budget, "previous_directory": str(previous.resolve()),
            "billing_snapshot_sha256": evaluation.digest(billing_path.read_bytes())}


class CachedClient:
    def __init__(self, context, new_client):
        self.context, self.new_client, self.cache_hits = context, new_client, 0
        if not math.isclose(new_client.max_cost_usd, context["new_budget_usd"], rel_tol=0, abs_tol=1e-12):
            raise ValueError("Continuation client must use the reduced remaining budget")

    def score(self, query, candidate):
        cached = self.context["cache"].get(request_hash(query, candidate))
        if cached is not None:
            self.cache_hits += 1
            return deepcopy(cached)
        return self.new_client.score(query, candidate)

    def summary(self):
        old, new = self.context["original"]["spend"], self.new_client.summary()
        return dict(new, calls=old["calls"] + new["calls"],
                    known_cost_usd=old["known_cost_usd"] + new["known_cost_usd"],
                    reserved_cost_usd=old["reserved_cost_usd"] + new["reserved_cost_usd"],
                    max_cost_usd=old["max_cost_usd"], cached_results_reused=self.cache_hits,
                    new_calls=new["calls"], historical_spend_incomplete=old["spend_incomplete"],
                    historical_status_counts=self.context["original"]["status_counts"],
                    billing_review="Observed account delta matched prior known receipts; prior reservation retained for delayed billing.")


def continue_run(context, output, new_client):
    client = CachedClient(context, new_client)
    report = evaluation.run(context["sha"], output, client)
    record = {"previous_directory": context["previous_directory"],
              "billing_snapshot_sha256": context["billing_snapshot_sha256"],
              "billing_review": context["billing"], "previous_spend": context["original"]["spend"],
              "previous_status_counts": context["original"]["status_counts"],
              "cached_successes": len(context["cache"]), "new_calls": new_client.summary()["calls"],
              "new_run_budget_usd": context["new_budget_usd"],
              "continuation_source_sha256": evaluation.digest(Path(__file__).read_bytes()),
              "omissions": "Reuses frozen judgments; account snapshot cannot rule out later billing. No private corpus transfer or runtime activation."}
    report["continuation"] = record
    evaluation.write_json(Path(output) / "continuation.json", record)
    evaluation.write_json(Path(output) / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--billing-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true", help="send only missing judgments; default is offline validation")
    parser.add_argument("--keychain-service")
    parser.add_argument("--keychain-account")
    args = parser.parse_args()
    try:
        context = verify_previous(args.previous, args.billing_snapshot)
        if not args.run:
            print(json.dumps({"mode": "offline", "cached_successes": len(context["cache"]),
                              "remaining_judgments": 504 - len(context["cache"]),
                              "new_budget_usd": context["new_budget_usd"], "manifest_sha256": context["sha"]}))
            return 0
        if args.output is None:
            raise ValueError("A new output directory is required")
        key = evaluation.load_key(args.keychain_service, args.keychain_account)
        report = continue_run(context, args.output, JevClient(key, max_cost_usd=context["new_budget_usd"]))
        print(json.dumps({"status": report["status"], "spend": report["spend"],
                          "promotion_gate": report["promotion_gate"], "report": str(args.output / "report.json")}))
        return 0 if report["complete"] else 2
    except (ValueError, OSError, KeyError, TypeError):
        print("Continuation refused. Verify frozen receipts, the explicit billing review, output and credential configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
