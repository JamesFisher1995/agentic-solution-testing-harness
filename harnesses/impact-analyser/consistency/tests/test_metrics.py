"""Unit tests for the pure-Python statistics."""

from harness import metrics


def test_jaccard_basic():
    assert metrics.jaccard({1, 2, 3}, {2, 3, 4}) == 2 / 4
    assert metrics.jaccard(set(), set()) == 1.0          # both empty ⇒ identical
    assert metrics.jaccard({1}, set()) == 0.0
    assert metrics.jaccard({1, 2}, {1, 2}) == 1.0


def test_mean_pairwise_jaccard():
    assert metrics.mean_pairwise_jaccard([{1, 2}, {1, 2}, {1, 2}]) == 1.0
    assert metrics.mean_pairwise_jaccard([{1, 2}]) == 1.0   # single set
    # two pairs at 1.0 and one disjoint pair at 0.0
    val = metrics.mean_pairwise_jaccard([{1}, {1}, {2}])
    assert abs(val - (1.0 + 0.0 + 0.0) / 3) < 1e-9


def test_stdev_and_cv():
    assert metrics.stdev([5]) == 0.0
    assert metrics.stdev([2, 2, 2]) == 0.0
    assert abs(metrics.stdev([0.0, 1.0]) - 0.5) < 1e-9     # population stdev
    assert metrics.coefficient_of_variation([0, 0, 0]) == 0.0


def test_categorical_agreement():
    assert metrics.categorical_agreement(["High", "High", "High"]) == 1.0
    assert abs(metrics.categorical_agreement(["High", "High", "Low"]) - 2 / 3) < 1e-9
    assert metrics.categorical_agreement(["x"]) == 1.0     # singleton


def test_spearman():
    assert abs(metrics.spearman([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-9
    assert abs(metrics.spearman([1, 2, 3], [3, 2, 1]) + 1.0) < 1e-9   # perfectly reversed
    # a flat series carries no ordering signal ⇒ treated as agreeing (1.0)
    assert metrics.spearman([1, 1, 1], [1, 2, 3]) == 1.0


def test_clamp01():
    assert metrics.clamp01(-0.5) == 0.0
    assert metrics.clamp01(1.5) == 1.0
    assert metrics.clamp01(0.3) == 0.3
