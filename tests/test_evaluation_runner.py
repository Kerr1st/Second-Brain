"""The evaluation command must distinguish completed, failed, and empty runs."""

import pytest

from scripts.eval import run_evaluation


def test_evaluation_exception_returns_failure(monkeypatch, capsys):
    def unavailable(_limit):
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setitem(run_evaluation.TIER_RUNNERS, "curated", unavailable)
    monkeypatch.setattr("sys.argv", ["evaluation", "--tier", "curated"])
    assert run_evaluation.main() == 1
    assert "failed" in capsys.readouterr().out


@pytest.mark.parametrize("tier,summary", [
    ("golden", {"total_queries": 0}),
    ("curated", {"total": 0}),
    ("cold_warm", {}),
    ("ablation", {"baseline": {"total": 0}}),
    ("consolidation", {"total_insights": 2, "total_queries": 0}),
])
def test_required_tier_without_queries_is_insufficient_evidence(monkeypatch, capsys, tier, summary):
    monkeypatch.setitem(run_evaluation.TIER_RUNNERS, tier, lambda _limit: summary)
    monkeypatch.setattr("sys.argv", ["evaluation", "--tier", tier])
    assert run_evaluation.main() == 2
    assert "insufficient_evidence" in capsys.readouterr().out


@pytest.mark.parametrize("tier,summary", [
    ("golden", {"total_queries": 3}),
    ("curated", {"total": 3}),
    ("cold_warm", {"warm": {"total": 3}, "cold": {"total": 3}}),
    ("ablation", {"baseline": {"total": 3}}),
    ("consolidation", {"total_queries": 3}),
    ("trends", {}),
])
def test_nonempty_tiers_and_history_report_complete(monkeypatch, tier, summary):
    monkeypatch.setitem(run_evaluation.TIER_RUNNERS, tier, lambda _limit: summary)
    monkeypatch.setattr("sys.argv", ["evaluation", "--tier", tier])
    assert run_evaluation.main() == 0


def test_all_tiers_continue_after_failure_and_save_distinct_outcomes(monkeypatch, tmp_path):
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["evaluation"])
    monkeypatch.setattr(run_evaluation, "TIER_RUNNERS", {
        "golden": lambda _: {"error": "provider failed"},
        "curated": lambda _: {"total": 0},
        "cold_warm": lambda _: {"warm": {"total": 2}, "cold": {"total": 2}},
        "ablation": lambda _: {"baseline": {"total": 2}},
        "consolidation": lambda _: {"total_queries": 2},
    })
    assert run_evaluation.main() == 1
    report, = (tmp_path / "evaluations/results").glob("full_eval_*.json")
    results = json.loads(report.read_text())["summary"]
    assert results["golden"]["status"] == "failed"
    assert results["curated"]["status"] == "insufficient_evidence"
    assert results["consolidation"]["evaluated_queries"] == 2
    assert results["consolidation"]["status"] == "completed"
