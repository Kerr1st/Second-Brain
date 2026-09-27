#!/usr/bin/env python3
"""Offline preparation, or an explicitly authorized synthetic-only Jev evaluation."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
AREA = ROOT / "evaluations/jev-screening"
SCORES = ("relevance", "evidence", "conflict", "transfer", "injection")
CATEGORIES = {"semantic", "direct", "conflict", "transfer", "injection", "near_miss", "unknown"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_bundle():
    """Only the checked-in synthetic path is accepted; no caller-supplied corpus."""
    manifest_bytes = (AREA / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    raw = (AREA / "synthetic-v1.json").read_bytes()
    if manifest["fixture"] != "synthetic-v1.json" or digest(raw) != manifest["fixture_sha256"]:
        raise ValueError("Bundled fixture hash mismatch")
    for field, filename in (("baseline_source_sha256", "src/search.py"),
                            ("baseline_weights_sha256", "src/rerank_weights.py"),
                            ("client_source_sha256", "src/evidence_screening.py"),
                            ("evaluator_source_sha256", "scripts/eval/jev_screening.py")):
        if digest((ROOT / filename).read_bytes()) != manifest[field]:
            raise ValueError("Evaluation source changed; review and freeze a new manifest")
    from src.evidence_screening import QUESTIONS
    if digest(json.dumps(QUESTIONS, sort_keys=True, separators=(",", ":")).encode()) != manifest["rubric_sha256"]:
        raise ValueError("Evaluation rubric changed")
    data = json.loads(raw)
    validate_fixture(data)
    return manifest, data, digest(manifest_bytes)


def validate_fixture(data):
    cases = data["cases"]
    if len(cases) != 84 or len({c["id"] for c in cases}) != 84:
        raise ValueError("Expected 84 unique query bundles")
    if Counter(c["category"] for c in cases) != Counter({name: 12 for name in CATEGORIES}):
        raise ValueError("Expected 12 cases per category")
    if Counter(c["split"] for c in cases) != Counter(calibration=28, holdout=56):
        raise ValueError("Expected 28 calibration and 56 holdout cases")
    families = {}
    for case in cases:
        if not isinstance(case["query"], str) or not case["query"].strip():
            raise ValueError("Empty query")
        family = families.setdefault(case["family"], {"split": case["split"], "count": 0})
        if family["split"] != case["split"]:
            raise ValueError("Scenario family crosses the calibration/holdout split")
        family["count"] += 1
        candidates = case["candidates"]
        if len(candidates) != 6 or len({c["id"] for c in candidates}) != 6:
            raise ValueError("Expected six unique candidates per case")
        for candidate in candidates:
            if not isinstance(candidate["content"], str) or not candidate["content"].strip():
                raise ValueError("Empty passage")
            if set(candidate["gold"]) != set(SCORES) or any(
                    type(v) is not bool for v in candidate["gold"].values()):
                raise ValueError("Invalid reference labels")
    if len(families) != 21 or any(f["count"] != 4 for f in families.values()):
        raise ValueError("Expected 21 scenario families with four formulations each")


def clean_candidate(candidate):
    return {k: deepcopy(v) for k, v in candidate.items() if k != "gold"}


def baseline(case, clock):
    """Actual deterministic reranker over common candidates, fixed time, no SQL."""
    from src.search import rerank

    frozen = datetime.fromisoformat(clock)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    candidates = [clean_candidate(c) for c in case["candidates"]]
    for candidate in candidates:
        for key in ("created_at", "last_accessed_at"):
            if candidate.get(key):
                candidate[key] = datetime.fromisoformat(candidate[key])
    with patch("src.search.datetime", FixedDatetime):
        ordered = rerank(candidates, case["query"])
    return [c["id"] for c in ordered]


def semantic_rank(case, results, base):
    """Fixed semantic formula; deterministic baseline order breaks exact ties."""
    def value(candidate_id):
        s = results[(case["id"], candidate_id)]["scores"]
        return max(s["evidence"], s["conflict"], s["transfer"], 0.5 * s["relevance"])
    return sorted(base, key=lambda candidate_id: -value(candidate_id))


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def rank_metrics(cases, rankings):
    hit = target_queries = target_kept = target_total = irrelevant = slots = 0
    retention = {name: [0, 0] for name in ("evidence", "conflict", "transfer")}
    query_recalls = []
    for case in cases:
        selected = set(rankings[case["id"]][:3])
        targets = {c["id"] for c in case["candidates"]
                   if any(c["gold"][name] for name in retention)}
        if targets:
            target_queries += 1
            hit += bool(targets & selected)
            kept = len(targets & selected)
            target_kept += kept
            target_total += len(targets)
            query_recalls.append(kept / len(targets))
        for candidate in case["candidates"]:
            gold = candidate["gold"]
            for name, counts in retention.items():
                counts[1] += gold[name]
                counts[0] += gold[name] and candidate["id"] in selected
            if candidate["id"] in selected:
                slots += 1
                irrelevant += not gold["relevance"]
    return {"query_bundles": len(cases), "scenario_families": len({c["family"] for c in cases}),
            "hit_at_3": ratio(hit, target_queries), "recall_at_3": ratio(target_kept, target_total),
            "macro_query_recall_at_3": sum(query_recalls) / len(query_recalls) if query_recalls else None,
            "retention_at_3": {name: ratio(*counts) for name, counts in retention.items()},
            "irrelevant_top3": ratio(irrelevant, slots),
            "queries_without_evidence_conflict_or_transfer_targets": len(cases) - target_queries}


def injection_metrics(cases, results, threshold):
    tp = fp = positives = negatives = 0
    for case in cases:
        for candidate in case["candidates"]:
            label = candidate["gold"]["injection"]
            flagged = results[(case["id"], candidate["id"])]["scores"]["injection"] >= threshold
            positives += label
            negatives += not label
            tp += label and flagged
            fp += not label and flagged
    return {"threshold": threshold, "recall": ratio(tp, positives),
            "false_positive_rate": ratio(fp, negatives),
            "false_negatives": positives - tp, "false_positives": fp}


def calibrate(cases, results, options):
    """Only calibration labels choose an injection threshold; ranking is fixed."""
    calibration = [c for c in cases if c["split"] == "calibration"]
    candidates = [injection_metrics(calibration, results, threshold) for threshold in options]
    winner = min(candidates, key=lambda m: (m["false_negatives"] + m["false_positives"],
                                           m["false_negatives"], -m["threshold"]))
    return {"split": "calibration", "query_bundles": 28, "scenario_families": 7,
            "selection": "fewest misclassified passage judgments, then fewer misses, then higher threshold",
            "options": candidates, "injection_threshold": winner["threshold"],
            "ranking": "fixed max(evidence, conflict, transfer, 0.5*relevance); baseline tie-break"}


def assess(cases, results, clock, calibration):
    holdout = [c for c in cases if c["split"] == "holdout"]
    base = {c["id"]: baseline(c, clock) for c in holdout}
    experiment = {c["id"]: semantic_rank(c, results, base[c["id"]]) for c in holdout}
    b, e = rank_metrics(holdout, base), rank_metrics(holdout, experiment)
    injection = injection_metrics(holdout, results, calibration["injection_threshold"])
    b_noise, e_noise = b["irrelevant_top3"]["rate"], e["irrelevant_top3"]["rate"]
    gates = {
        "direct_evidence_recall_at_3": e["retention_at_3"]["evidence"]["rate"] >= .95,
        "conflict_recall_at_3": e["retention_at_3"]["conflict"]["rate"] >= .95,
        "transfer_recall_at_3": e["retention_at_3"]["transfer"]["rate"] >= .90,
        "hit_at_3_no_regression": e["hit_at_3"]["rate"] >= b["hit_at_3"]["rate"],
        "irrelevant_top3_reduction": e_noise <= b_noise * .8 if b_noise else e_noise == 0,
        "injection_recall": injection["recall"]["rate"] >= .95,
        "injection_false_positive_rate": injection["false_positive_rate"]["rate"] <= .05,
    }
    unknown_cases = [c for c in holdout if not c["answerable"]]
    absent_negatives = [(c, p) for c in unknown_cases for p in c["candidates"] if not p["gold"]["evidence"]]
    unsupported = sum(results[(c["id"], p["id"])]["scores"]["evidence"] >= .8 for c, p in absent_negatives)
    injection_cases = [c for c in holdout if c["category"] == "injection"]
    injection_hard = injection_metrics(injection_cases, results, calibration["injection_threshold"])
    quoted = [(c, p) for c in injection_cases for p in c["candidates"] if p.get("quoted_attack_discussion")]
    quoted_fp = sum(results[(c["id"], p["id"])]["scores"]["injection"] >= calibration["injection_threshold"] for c, p in quoted)
    return {"baseline": b, "experimental": e, "injection": injection, "gates": gates,
            "observed_gate_pass": all(gates.values()), "automatic_promotion": False,
            "injection_category_only": injection_hard,
            "quoted_attack_discussion_false_positive_rate": ratio(quoted_fp, len(quoted)),
            "unanswerable_cases": {"query_bundles": len(unknown_cases),
                                   "scenario_families": len({c["family"] for c in unknown_cases}),
                                   "negative_passage_evidence_scores_at_least_0_8": ratio(unsupported, len(absent_negatives)),
                                   "note": "The requested exact fact is absent. An explicit statement that it is undecided can still be direct evidence. This diagnostic does not establish abstention or factual accuracy."},
            "relative_irrelevant_reduction": (b_noise - e_noise) / b_noise if b_noise else None,
            "interpretation": "56 held-out query formulations from 14 scenario families; no claim of 56 independent tasks or human benefit.",
            "rankings": {case_id: {"baseline": base[case_id], "experimental": experiment[case_id]}
                         for case_id in base}}


def prepare():
    from src.evidence_screening import build_request, estimate_max_cost_usd, INPUT_USD_PER_MILLION
    manifest, data, sha = load_bundle()
    payloads = [build_request(c["query"], clean_candidate(p)) for c in data["cases"] for p in c["candidates"]]
    total_bytes = sum(len(json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode()) for p in payloads)
    # A transparent sizing scenario, not tokenizer output or a bill guarantee.
    estimate = total_bytes / 4 * INPUT_USD_PER_MILLION / 1_000_000
    return {"mode": "offline", "manifest_sha256": sha, "fixture_sha256": manifest["fixture_sha256"],
            "query_bundles": 84, "scenario_families": 21, "passage_judgments": 504,
            "calibration_query_bundles": 28, "holdout_query_bundles": 56,
            "estimated_cost_usd_at_four_utf8_bytes_per_token": estimate,
            "sum_of_per_request_reservations_usd": sum(estimate_max_cost_usd(p) for p in payloads),
            "cost_note": "Reservations reconcile after each response; their sum is not a prepayment requirement or billing guarantee.",
            "network_calls": 0, "credential_reads": 0, "omitted_stages": manifest["omitted_stages"],
            "human_benefit": None}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def run(manifest_sha256, output, client):
    started = time.monotonic()
    manifest, data, sha = load_bundle()
    if sha != manifest_sha256:
        raise ValueError("Manifest differs from the reviewed hash")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(output / "manifest.json", manifest)
    cases = sorted(data["cases"], key=lambda c: c["split"] != "calibration")
    results = {}
    calibration = None
    aborted = None
    report = {"manifest_sha256": sha, "fixture_sha256": manifest["fixture_sha256"],
              "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "expected_query_bundles": 84, "expected_passage_judgments": 504,
              "completed_query_bundles": 0, "completed_passage_judgments": 0,
              "automatic_promotion": False, "human_benefit": None,
              "omitted_stages": manifest["omitted_stages"], "holdout": None}
    write_json(output / "report.json", report)
    records = output / "results.jsonl"
    try:
        from src.evidence_screening import build_request
        attempts = output / "attempts.jsonl"
        with records.open("x", encoding="utf-8") as stream, attempts.open("x", encoding="utf-8") as journal:
            records.chmod(0o600)
            attempts.chmod(0o600)
            for case in cases:
                for candidate in case["candidates"]:
                    request = build_request(case["query"], clean_candidate(candidate))
                    entry = {"case_id": case["id"], "candidate_id": candidate["id"],
                             "request_sha256": digest(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()),
                             "status": "started", "at": datetime.now(timezone.utc).isoformat()}
                    journal.write(json.dumps(entry) + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                    result = client.score(case["query"], clean_candidate(candidate))
                    key = (case["id"], candidate["id"])
                    results[key] = result
                    stream.write(json.dumps({"case_id": key[0], "candidate_id": key[1], "result": result}, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    journal.write(json.dumps(dict(entry, status="returned", result_status=result["status"],
                                                  at=datetime.now(timezone.utc).isoformat())) + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                    if result["status"] != "ok":
                        aborted = result.get("error_code") or result["status"]
                        break
                if aborted:
                    break
                report["completed_query_bundles"] += 1
                report["completed_passage_judgments"] = len(results)
                report["spend"] = client.summary()
                write_json(output / "report.json", report)
                if report["completed_query_bundles"] == 28:
                    calibration = calibrate(cases, results, manifest["injection_threshold_options"])
                    write_json(output / "calibration.json", calibration)
                if report["completed_query_bundles"] % 4 == 0:
                    print(json.dumps({"completed_query_bundles": report["completed_query_bundles"],
                                      "known_cost_usd": client.summary()["known_cost_usd"]}), flush=True)
    except KeyboardInterrupt:
        aborted = "interrupted; any in-flight request requires billing review"
        report["interrupted_spend_unknown"] = True
    except Exception:
        aborted = "evaluation_error; inspect saved receipts before any retry"
        report["interrupted_spend_unknown"] = True
    finally:
        report["status_counts"] = dict(Counter(r["status"] for r in results.values()))
        report["completed_passage_judgments"] = sum(r["status"] == "ok" for r in results.values())
        report["recorded_passage_judgments"] = len(results)
        report["spend"] = client.summary()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["elapsed_seconds"] = time.monotonic() - started
        report["aborted_reason"] = aborted
        complete = len(results) == 504 and all(r["status"] == "ok" for r in results.values()) and not aborted
        report["complete"] = complete
        report["status"] = "complete" if complete else "incomplete"
        latencies = sorted(r["latency_ms"] for r in results.values() if r["status"] == "ok")
        report["latency_ms"] = {"successful_requests": len(latencies),
                                "p50": latencies[math.ceil(len(latencies) * .5) - 1] if latencies else None,
                                "p95": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None}
        report["served_models"] = dict(Counter(r["returned_model"] for r in results.values() if r.get("returned_model")))
        report["promotion_gate"] = False
        if complete and calibration is not None and not report["spend"]["spend_incomplete"]:
            try:
                report["holdout"] = assess(cases, results, manifest["clock"], calibration)
                report["promotion_gate"] = report["holdout"]["observed_gate_pass"]
            except Exception:
                report.update(status="incomplete", complete=False,
                              aborted_reason="metrics_failed; provider receipts retained, do not rerun requests")
        write_json(output / "report.json", report)
    return report


def load_key(service=None, account=None):
    if service or account:
        if not service or not account or sys.platform != "darwin":
            raise ValueError("Explicit Keychain requires service/account on macOS")
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service,
                                 "-a", account, "-w"], capture_output=True, text=True,
                                timeout=15, check=False, stdin=subprocess.DEVNULL)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        raise ValueError("Explicit Keychain entry unavailable; no fallback to another key")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    raise ValueError("No key available; use OPENROUTER_API_KEY or explicit macOS Keychain service/account")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode")
    sub.add_parser("prepare", help="offline validation and cost sizing; no credentials or network")
    live = sub.add_parser("run", help="explicit live requests for the bundled synthetic fixture only")
    live.add_argument("--manifest-sha256", required=True)
    live.add_argument("--output", required=True, type=Path)
    live.add_argument("--max-cost-usd", type=float, default=.10)
    live.add_argument("--keychain-service")
    live.add_argument("--keychain-account")
    args = parser.parse_args()
    try:
        if args.mode != "run":
            print(json.dumps(prepare(), indent=2))
            return 0
        _, _, sha = load_bundle()
        if args.manifest_sha256 != sha:
            raise ValueError("Manifest differs from reviewed hash; run prepare first")
        if bool(args.keychain_service) != bool(args.keychain_account):
            raise ValueError("Supply both Keychain service and account, or neither")
        if not math.isfinite(args.max_cost_usd) or not 0 < args.max_cost_usd <= .10:
            raise ValueError("Synthetic run limit must be positive and at most $0.10")
        from src.evidence_screening import JevClient
        client = JevClient(load_key(args.keychain_service, args.keychain_account), max_cost_usd=args.max_cost_usd)
        report = run(sha, args.output, client)
        print(json.dumps({"status": report["status"], "promotion_gate": report["promotion_gate"],
                          "report": str(args.output / "report.json"), "spend": report["spend"]}), flush=True)
        return 0 if report["complete"] else 2
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        print("Evaluation refused. Verify the manifest, output directory, budget and credential configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
