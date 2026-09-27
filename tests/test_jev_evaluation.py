"""Synthetic evaluation invariants; run only through scripts/test_isolated.py."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts.eval import jev_screening as evaluation


def fixture():
    return evaluation.load_bundle()[1]


class ReferenceClient:
    """Test transport replacement using authored labels, never model validation."""
    def __init__(self, data, fail_at=None):
        self.reference = {(case["query"], c["id"]): c["gold"]
                          for case in data["cases"] for c in case["candidates"]}
        self.calls = 0
        self.fail_at = fail_at
        self.spent = 0.0
        self.incomplete = False

    def score(self, query, candidate):
        assert "gold" not in candidate
        self.calls += 1
        if self.calls == self.fail_at:
            self.incomplete = True
            return {"status": "error", "scores": dict.fromkeys(evaluation.SCORES),
                    "cost_usd": None, "latency_ms": 1, "error_code": "unknown_usage",
                    "returned_model": None}
        self.spent += .00001
        return {"status": "ok", "scores": {k: float(v) for k, v in self.reference[(query, candidate["id"])].items()},
                "cost_usd": .00001, "latency_ms": 2, "error_code": None,
                "returned_model": "typesafe/jev-1.13-20260917"}

    def summary(self):
        return {"calls": self.calls, "known_cost_usd": self.spent,
                "spend_incomplete": self.incomplete, "blocked_reason": None,
                "reserved_cost_usd": 0.0, "max_cost_usd": .10}


def test_prepare_does_not_read_credentials_keychain_or_network(monkeypatch):
    monkeypatch.setattr(evaluation, "load_key", Mock(side_effect=AssertionError("No credentials")))
    monkeypatch.setattr(evaluation.subprocess, "run", Mock(side_effect=AssertionError("No Keychain")))
    import src.evidence_screening as screening
    monkeypatch.setattr(screening.JevClient, "__init__", Mock(side_effect=AssertionError("No client")))
    result = evaluation.prepare()
    assert result["network_calls"] == result["credential_reads"] == 0
    assert result["passage_judgments"] == 504
    assert result["estimated_cost_usd_at_four_utf8_bytes_per_token"] > 0


def test_fixture_split_and_tail_evidence():
    data = fixture()
    evaluation.validate_fixture(data)
    groups = {split: {c["family"] for c in data["cases"] if c["split"] == split}
              for split in ("calibration", "holdout")}
    assert len(groups["calibration"]) == 7 and len(groups["holdout"]) == 14
    assert not groups["calibration"] & groups["holdout"]
    long_evidence = {c["id"] for row in data["cases"] for c in row["candidates"]
                     if c["gold"]["evidence"] and len(c["content"]) > 800}
    assert {"handoff-silence-1", "same-owner-other-system-1", "complete-export-1"} <= long_evidence
    corrupted = deepcopy(data)
    corrupted["cases"][0]["candidates"][0]["gold"]["evidence"] = "true"
    with pytest.raises(ValueError, match="labels"):
        evaluation.validate_fixture(corrupted)


def test_baseline_is_real_reranker_without_mutation_or_database(monkeypatch):
    import src.search as search
    monkeypatch.setattr(search, "get_connection", Mock(side_effect=AssertionError("No SQL")))
    data = fixture()
    case = data["cases"][0]
    before = deepcopy(case)
    first = evaluation.baseline(case, data["clock"])
    second = evaluation.baseline(case, data["clock"])
    assert first == second and len(first) == 6
    assert case == before


def test_calibration_never_uses_holdout_labels():
    data = fixture()
    cases = data["cases"]
    results = {(c["id"], p["id"]): {"scores": {key: float(value) for key, value in p["gold"].items()}}
               for c in cases if c["split"] == "calibration" for p in c["candidates"]}
    original = evaluation.calibrate(cases, results, [.5, .8, .95])
    changed = deepcopy(cases)
    for c in changed:
        if c["split"] == "holdout":
            for p in c["candidates"]:
                p["gold"]["injection"] = not p["gold"]["injection"]
    assert evaluation.calibrate(changed, results, [.5, .8, .95]) == original


def test_unknown_spend_persists_partial_receipt_and_stops(tmp_path):
    manifest, data, sha = evaluation.load_bundle()
    client = ReferenceClient(data, fail_at=2)
    report = evaluation.run(sha, tmp_path / "partial", client)
    assert client.calls == 2
    assert report["complete"] is False and report["promotion_gate"] is False
    assert report["holdout"] is None
    assert report["completed_passage_judgments"] == 1
    assert report["spend"]["spend_incomplete"] is True
    receipts = (tmp_path / "partial/results.jsonl").read_text().splitlines()
    assert len(receipts) == 2
    assert json.loads((tmp_path / "partial/report.json").read_text())["status"] == "incomplete"


def test_complete_reference_fixture_reports_denominators_and_no_promotion(tmp_path):
    manifest, data, sha = evaluation.load_bundle()
    report = evaluation.run(sha, tmp_path / "complete", ReferenceClient(data))
    assert report["complete"] and report["completed_query_bundles"] == 84
    assert report["completed_passage_judgments"] == 504
    assert report["automatic_promotion"] is False and report["human_benefit"] is None
    holdout = report["holdout"]
    assert holdout["experimental"]["query_bundles"] == 56
    assert holdout["experimental"]["scenario_families"] == 14
    assert holdout["experimental"]["hit_at_3"]["denominator"] == 56
    assert holdout["experimental"]["hit_at_3"]["rate"] == 1
    assert holdout["injection"]["recall"]["denominator"] == 16
    assert holdout["injection"]["recall"]["rate"] == 1
    assert holdout["unanswerable_cases"]["query_bundles"] == 8
    assert holdout["unanswerable_cases"]["negative_passage_evidence_scores_at_least_0_8"]["numerator"] == 0
    assert holdout["quoted_attack_discussion_false_positive_rate"]["denominator"] == 8
    assert (tmp_path / "complete/calibration.json").is_file()


def test_manifest_mismatch_refuses_before_client_or_output(tmp_path):
    data = fixture()
    client = ReferenceClient(data)
    with pytest.raises(ValueError, match="reviewed hash"):
        evaluation.run("wrong", tmp_path / "refused", client)
    assert client.calls == 0 and not (tmp_path / "refused").exists()


def test_explicit_keychain_wins_and_missing_entry_never_falls_back(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(evaluation.sys, "platform", "darwin")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-test-key")
    lookup = Mock(return_value=SimpleNamespace(returncode=0, stdout="dedicated-test-key\n"))
    monkeypatch.setattr(evaluation.subprocess, "run", lookup)
    assert evaluation.load_key("example-service", "example-account") == "dedicated-test-key"
    lookup.return_value = SimpleNamespace(returncode=1, stdout="")
    with pytest.raises(ValueError, match="no fallback"):
        evaluation.load_key("example-service", "example-account")


def test_metrics_failure_keeps_receipts_without_rerunning(tmp_path, monkeypatch):
    manifest, data, sha = evaluation.load_bundle()
    monkeypatch.setattr(evaluation, "assess", Mock(side_effect=ValueError("bad metric")))
    client = ReferenceClient(data)
    report = evaluation.run(sha, tmp_path / "failed-metrics", client)
    assert client.calls == 504 and report["complete"] is False
    assert report["status"] == "incomplete" and report["promotion_gate"] is False
    assert len((tmp_path / "failed-metrics/results.jsonl").read_text().splitlines()) == 504
    journal = [json.loads(row) for row in (tmp_path / "failed-metrics/attempts.jsonl").read_text().splitlines()]
    assert len(journal) == 1008 and journal[0]["status"] == "started"
    assert journal[1]["status"] == "returned"
