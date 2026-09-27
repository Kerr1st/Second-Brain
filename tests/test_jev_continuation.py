"""Offline reviewed-continuation tests; no credentials or network transports."""
from datetime import datetime, timedelta
import json

import pytest

from scripts.eval import continue_jev_screening as continuation
from scripts.eval import jev_screening as evaluation


class ReceiptClient:
    def __init__(self, data, max_cost_usd=.1, fail_at=None):
        self.labels = {(c["query"], p["id"]): p["gold"] for c in data["cases"] for p in c["candidates"]}
        self.max_cost_usd, self.fail_at = max_cost_usd, fail_at
        self.calls, self.known, self.reserved, self.failed = 0, 0.0, 0.0, False

    def score(self, query, candidate):
        self.calls += 1
        result = {"latency_ms": 1.0, "request_sha256": continuation.request_hash(query, candidate),
                  "returned_model": "typesafe/jev-1.13-20260917", "error_code": None}
        if self.calls == self.fail_at:
            self.failed, self.reserved = True, .000819
            return dict(result, status="error", scores=dict.fromkeys(evaluation.SCORES),
                        usage=None, cost_usd=None, error_code="transport_failure")
        self.known += .00001
        return dict(result, status="ok", scores={k: float(v) for k, v in self.labels[(query, candidate["id"])].items()},
                    usage={"input_tokens": 10, "output_tokens": 5, "cost": .00001}, cost_usd=.00001)

    def summary(self):
        return {"calls": self.calls, "known_cost_usd": self.known, "reserved_cost_usd": self.reserved,
                "spend_incomplete": self.failed, "max_cost_usd": self.max_cost_usd,
                "blocked_reason": "transport_failure" if self.failed else None}


@pytest.fixture
def previous(tmp_path):
    manifest, data, sha = evaluation.load_bundle()
    directory = tmp_path / "original"
    report = evaluation.run(sha, directory, ReceiptClient(data, fail_at=170))
    snapshot = {"before": {"usage": .0001, "verified_at": (datetime.fromisoformat(report["started_at"]) - timedelta(seconds=1)).isoformat()},
                "after": {"usage": .0001 + report["spend"]["known_cost_usd"],
                          "verified_at": (datetime.fromisoformat(report["finished_at"]) + timedelta(seconds=1)).isoformat()},
                "original_report_sha256": evaluation.digest((directory / "report.json").read_bytes()),
                "original_results_sha256": evaluation.digest((directory / "results.jsonl").read_bytes()),
                "confirmed_numerical_match": True, "same_key_confirmed": True,
                "review_note": "Offline test of an explicitly reviewed continuation, not real billing evidence."}
    billing = tmp_path / "billing.json"
    billing.write_text(json.dumps(snapshot))
    return directory, billing, data


def test_reuses_paid_successes_and_preserves_original_failure(previous, tmp_path):
    directory, billing, data = previous
    original_hashes = {p.name: evaluation.digest(p.read_bytes()) for p in directory.iterdir()}
    context = continuation.verify_previous(directory, billing)
    assert len(context["cache"]) == 169
    assert context["new_budget_usd"] == pytest.approx(.1 - .00169 - .000819)
    new_client = ReceiptClient(data, max_cost_usd=context["new_budget_usd"])
    report = continuation.continue_run(context, tmp_path / "continued", new_client)
    assert report["complete"] is True
    assert new_client.calls == 504 - 169
    assert report["spend"]["calls"] == 505
    assert report["spend"]["cached_results_reused"] == 169
    assert report["spend"]["reserved_cost_usd"] == .000819
    assert report["spend"]["historical_status_counts"] == {"ok": 169, "error": 1}
    assert report["continuation"]["previous_spend"]["spend_incomplete"] is True
    assert {p.name: evaluation.digest(p.read_bytes()) for p in directory.iterdir()} == original_hashes
    assert json.loads((directory / "calibration.json").read_text()) == json.loads((tmp_path / "continued/calibration.json").read_text())


def test_mismatched_billing_blocks_before_client(previous):
    directory, billing, data = previous
    snapshot = json.loads(billing.read_text())
    snapshot["after"]["usage"] += .01
    billing.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError, match="reconcile"):
        continuation.verify_previous(directory, billing)


@pytest.mark.parametrize("damage", ["request_hash", "probability", "duplicate"])
def test_corrupted_cache_refused_even_with_refreshed_receipt_hash(previous, damage):
    directory, billing, data = previous
    path = directory / "results.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "request_hash":
        records[0]["result"]["request_sha256"] = "wrong"
    elif damage == "probability":
        records[0]["result"]["scores"]["evidence"] = 1.5
    else:
        records[1] = records[0]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    snapshot = json.loads(billing.read_text())
    snapshot["original_results_sha256"] = evaluation.digest(path.read_bytes())
    billing.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError):
        continuation.verify_previous(directory, billing)


def test_new_unknown_spend_stops_without_retry(previous, tmp_path):
    directory, billing, data = previous
    context = continuation.verify_previous(directory, billing)
    new_client = ReceiptClient(data, max_cost_usd=context["new_budget_usd"], fail_at=1)
    report = continuation.continue_run(context, tmp_path / "failed-again", new_client)
    assert new_client.calls == 1 and report["complete"] is False
    assert report["spend"]["spend_incomplete"] is True
    assert report["spend"]["reserved_cost_usd"] == pytest.approx(.000819 * 2)
    assert report["promotion_gate"] is False
