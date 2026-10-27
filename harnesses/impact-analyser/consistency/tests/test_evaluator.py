"""Unit tests for EvaluatorAgent — the consistency maths over fixed reports."""

from harness.agents.evaluator import EvaluatorAgent
from harness.agents.runner import RunResult
from harness.config import EvaluatorConfig
from tests.fixtures import make_report, make_scored_test


def _results(reports):
    return [
        RunResult(index=i, cr_id_used=f"CR#c{i}", run_id=f"r{i}",
                  status="complete", report=rep)
        for i, rep in enumerate(reports)
    ]


def _evaluate(reports):
    ev = EvaluatorAgent(client=None)
    return ev.run(_results(reports), EvaluatorConfig())


def test_identical_reports_score_perfect():
    rep = make_report("CR-1")
    out = _evaluate([rep, rep, rep])
    assert out.sufficient
    assert out.overall_score == 1.0
    assert out.band == "Excellent"
    assert out.dimensions["test_set_stability"] == 1.0
    assert out.dimensions["action_agreement"] == 1.0
    assert out.details["common_test_count"] == 3


def test_disjoint_test_sets_tank_the_score():
    a = make_report("CR-1", scored=[make_scored_test("TC-1", 0.8, "Critical")])
    b = make_report("CR-1", scored=[make_scored_test("TC-9", 0.8, "Critical")])
    out = _evaluate([a, b])
    assert out.dimensions["test_set_stability"] == 0.0
    # no shared test ⇒ per-test dims are undefined and dropped from the blend
    assert out.dimensions["action_agreement"] is None
    assert out.dimensions["score_stability"] is None
    assert "action_agreement" not in out.weights_used
    assert out.overall_score < 0.8


def test_score_drift_lowers_score_stability():
    stable = [make_report("CR-1", scored=[make_scored_test("TC-1", 0.50, "Medium")])
              for _ in range(3)]
    drifty = [
        make_report("CR-1", scored=[make_scored_test("TC-1", 0.10, "Low")]),
        make_report("CR-1", scored=[make_scored_test("TC-1", 0.90, "Critical")]),
    ]
    s = _evaluate(stable)
    d = _evaluate(drifty)
    assert s.dimensions["score_stability"] == 1.0
    assert d.dimensions["score_stability"] < 0.2      # big swing, near-zero stability
    assert d.dimensions["action_agreement"] < 1.0     # Low vs Critical disagree


def test_risk_disagreement_detected():
    out = _evaluate([
        make_report("CR-1", overall_risk="High"),
        make_report("CR-1", overall_risk="High"),
        make_report("CR-1", overall_risk="Low"),
    ])
    assert abs(out.dimensions["risk_agreement"] - 2 / 3) < 1e-3   # reported at 4 dp
    assert out.details["overall_risk_values"] == ["High", "High", "Low"]


def test_single_run_is_insufficient():
    out = _evaluate([make_report("CR-1")])
    assert out.sufficient is False
    assert out.overall_score is None
    assert any("at least 2" in n for n in out.notes)


def test_cache_hit_flagged_in_notes():
    ev = EvaluatorAgent(client=None)
    results = _results([make_report("CR-1"), make_report("CR-1")])
    results[0].cache_hit = True
    out = ev.run(results, EvaluatorConfig())
    assert any("cache" in n.lower() for n in out.notes)
