"""Pure-Python statistics for cross-run consistency.

No numpy/scipy — keeps the harness install to just httpx and makes the maths
auditable. Every function is total: it returns a sensible value for the empty /
singleton / degenerate inputs the evaluator will hand it, so the caller never has
to guard against ZeroDivisionError.

Convention: helpers that express *consistency* return a value in [0, 1] where
1.0 means "identical across all runs".
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Hashable, Sequence


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation. 0.0 for <2 values (nothing varies)."""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    var = sum((v - mu) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def coefficient_of_variation(values: Sequence[float]) -> float:
    """stdev / mean. 0.0 when the mean is 0 (all-zero series is perfectly stable)."""
    mu = mean(values)
    if mu == 0:
        return 0.0
    return stdev(values) / abs(mu)


def jaccard(a: set[Hashable], b: set[Hashable]) -> float:
    """|a ∩ b| / |a ∪ b|. Two empty sets are defined as identical (1.0)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def mean_pairwise_jaccard(sets: Sequence[set[Hashable]]) -> float:
    """Average Jaccard over every unordered pair. 1.0 for a single set."""
    if len(sets) < 2:
        return 1.0
    scores = [jaccard(a, b) for a, b in combinations(sets, 2)]
    return mean(scores)


def categorical_agreement(values: Sequence[Hashable]) -> float:
    """Fraction of values equal to the modal value. 1.0 for <2 values.

    e.g. ["High","High","Medium"] → 2/3 ≈ 0.667.
    """
    if len(values) < 2:
        return 1.0
    counts: dict[Hashable, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.values()) / len(values)


def _ranks(values: Sequence[float]) -> list[float]:
    """Fractional (tie-averaged) ranks, ascending."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, averaged over the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """Pearson correlation. 1.0 when either series has zero variance (both flat
    ⇒ trivially 'in agreement'; one flat ⇒ no ordering signal, treat as agreeing)."""
    n = len(x)
    if n < 2:
        return 1.0
    mx, my = mean(x), mean(y)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    deny = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if denx == 0 or deny == 0:
        return 1.0
    return num / (denx * deny)


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation (Pearson on ranks), in [-1, 1]."""
    if len(x) != len(y):
        raise ValueError("spearman: inputs must be equal length")
    if len(x) < 2:
        return 1.0
    return _pearson(_ranks(x), _ranks(y))


def mean_pairwise_spearman(series: Sequence[Sequence[float]]) -> float:
    """Average Spearman over every pair of equal-length score vectors."""
    if len(series) < 2:
        return 1.0
    scores = [spearman(a, b) for a, b in combinations(series, 2)]
    return mean(scores)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
