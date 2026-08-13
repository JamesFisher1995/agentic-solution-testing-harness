"""EvaluatorAgent — score how consistent N impact reports are for one CR.

Each dimension is reduced to a consistency score in [0, 1] (1 = identical across
runs). The dimensions are then blended with the configured weights into a single
headline score. Every component is kept in the output so the headline number is
explainable, never a black box.

Dimensions
----------
test_set_stability    mean pairwise Jaccard of the selected test_id sets
action_agreement      per common-test agreement on recommended_action
score_stability       1 − (mean per-test composite_score stdev / penalty)
ranking_stability      mean pairwise Spearman of common-test score rankings
risk_agreement        modal agreement on summary.overall_risk
summary_stability     1 − mean coefficient-of-variation of the headline counts
coverage_gap_stability mean pairwise Jaccard of the coverage-gap component sets

A dimension that is undefined for the data (e.g. ranking with <2 shared tests) is
dropped from the blend and its weight removed from the denominator, rather than
being scored as a misleading 0 or 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .. import metrics
from ..config import EvaluatorConfig, ResponseSchema
from .base import Agent
from .runner import RunResult

_DEFAULT_COUNT_FIELDS = ("must_run", "should_run", "consider", "exclude",
                         "coverage_gaps", "new_test_cases")

_BANDS = ((0.90, "Excellent"), (0.75, "Good"), (0.50, "Fair"), (0.0, "Poor"))


@dataclass
class ConsistencyReport:
    runs_total: int
    runs_evaluated: int
    runs_failed: int
    sufficient: bool                       # need >= 2 successful runs to compare
    overall_score: Optional[float]
    band: Optional[str]
    dimensions: dict[str, Optional[float]] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs_total": self.runs_total,
            "runs_evaluated": self.runs_evaluated,
            "runs_failed": self.runs_failed,
            "sufficient": self.sufficient,
            "overall_score": self.overall_score,
            "band": self.band,
            "dimensions": self.dimensions,
            "weights_used": self.weights_used,
            "details": self.details,
            "notes": self.notes,
        }


class EvaluatorAgent(Agent):
    name = "evaluator"

    def run(self, results: list[RunResult], cfg: EvaluatorConfig, schema: ResponseSchema | None = None) -> ConsistencyReport:
        ok = [r for r in results if r.ok]
        reports = [r.report for r in ok]
        failed = len(results) - len(ok)
        notes: list[str] = []

        if any(r.cache_hit for r in ok):
            notes.append(
                "One or more runs were served from cache — their reports are "
                "identical by construction and inflate consistency. Enable "
                "bypass_cache (default) and ensure no stale completed run shares "
                "the CR's cache key."
            )

        if len(reports) < 2:
            notes.append("Need at least 2 successful runs to measure consistency.")
            return ConsistencyReport(
                runs_total=len(results),
                runs_evaluated=len(reports),
                runs_failed=failed,
                sufficient=False,
                overall_score=None,
                band=None,
                notes=notes,
            )

        schema = schema or ResponseSchema()
        dims, details = self._dimensions(reports, cfg, notes, schema)
        score, weights_used = self._blend(dims, cfg.weights)

        return ConsistencyReport(
            runs_total=len(results),
            runs_evaluated=len(reports),
            runs_failed=failed,
            sufficient=True,
            overall_score=round(score, 4),
            band=_band(score),
            dimensions={k: (round(v, 4) if v is not None else None) for k, v in dims.items()},
            weights_used={k: round(v, 4) for k, v in weights_used.items()},
            details=details,
            notes=notes,
        )

    # ------------------------------------------------------------------
    def _dimensions(
        self, reports: list[dict], cfg: EvaluatorConfig, notes: list[str], schema: ResponseSchema
    ) -> tuple[dict[str, Optional[float]], dict[str, Any]]:
        scored = [_scored_map(r, schema) for r in reports]
        id_sets = [set(m.keys()) for m in scored]
        union = set().union(*id_sets) if id_sets else set()
        common = set(id_sets[0]).intersection(*id_sets[1:]) if id_sets else set()

        details: dict[str, Any] = {
            "union_test_count": len(union),
            "common_test_count": len(common),
        }
        dims: dict[str, Optional[float]] = {}

        # 1. test-set stability
        dims["test_set_stability"] = metrics.mean_pairwise_jaccard(id_sets)

        # 2/3/4 need shared tests
        if common:
            actions = [
                metrics.categorical_agreement([m[tid]["recommended_action"] for m in scored])
                for tid in common
            ]
            dims["action_agreement"] = metrics.mean(actions)

            per_test_stdev = {
                tid: metrics.stdev([m[tid]["composite_score"] for m in scored])
                for tid in common
            }
            mean_stdev = metrics.mean(list(per_test_stdev.values()))
            penalty = cfg.score_stdev_full_penalty or 0.25
            dims["score_stability"] = metrics.clamp01(1.0 - mean_stdev / penalty)
            details["mean_score_stdev"] = round(mean_stdev, 4)
            details["per_test"] = _per_test_detail(common, scored)

            if len(common) >= 2:
                ordered = sorted(common)
                series = [[m[tid]["composite_score"] for tid in ordered] for m in scored]
                dims["ranking_stability"] = metrics.clamp01(
                    (metrics.mean_pairwise_spearman(series) + 1) / 2
                )
            else:
                dims["ranking_stability"] = None
        else:
            dims["action_agreement"] = None
            dims["score_stability"] = None
            dims["ranking_stability"] = None
            if union:
                notes.append(
                    "No test was selected by every run, so per-test agreement / "
                    "score / ranking could not be measured — the churn shows up "
                    "in test_set_stability instead."
                )

        # 5. overall_risk agreement
        risks = [(_summary(r, schema).get(schema.risk_field)) for r in reports]
        dims["risk_agreement"] = metrics.categorical_agreement(risks)
        details["overall_risk_values"] = risks

        # 6. summary count stability
        count_fields = list(schema.count_fields.values()) if schema.count_fields else list(_DEFAULT_COUNT_FIELDS)
        cvs = []
        per_field_cv = {}
        for f in count_fields:
            series = [float(_summary(r, schema).get(f, 0) or 0) for r in reports]
            cv = metrics.coefficient_of_variation(series)
            per_field_cv[f] = round(cv, 4)
            cvs.append(cv)
        dims["summary_stability"] = metrics.clamp01(1.0 - metrics.mean(cvs))
        details["summary_cv"] = per_field_cv

        # 7. coverage-gap stability
        gap_sets = [_gap_components(r, schema) for r in reports]
        dims["coverage_gap_stability"] = metrics.mean_pairwise_jaccard(gap_sets)

        # 8. entity stability — does the story parser extract the same entities every run?
        entity_sets = [_entities(r) for r in reports]
        dims["entity_stability"] = metrics.mean_pairwise_jaccard(entity_sets)
        details["entity_values"] = [sorted(s) for s in entity_sets]

        # 9. generated test-case consistency
        tc_lists = [_generated_test_titles(r) for r in reports]
        tc_counts = [len(tc) for tc in tc_lists]
        # count stability: 1 − normalised stdev of counts
        count_cv = metrics.coefficient_of_variation(tc_counts)
        count_stability = metrics.clamp01(1.0 - count_cv)
        details["generated_tc_counts"] = tc_counts
        details["generated_tc_titles"] = tc_lists
        dims["generated_test_case_consistency"] = count_stability

        # extras (reported, not in the default weighted blend)
        details["blast_radius_values"] = [_classification(r).get("blast_radius") for r in reports]
        details["blast_radius_agreement"] = round(
            metrics.categorical_agreement(details["blast_radius_values"]), 4
        )
        ct_sets = [set(_classification(r).get("change_types") or []) for r in reports]
        details["change_types_stability"] = round(metrics.mean_pairwise_jaccard(ct_sets), 4)
        details["generated_count_stdev"] = round(
            metrics.stdev([len(r.get("generated_test_cases") or []) for r in reports]), 4
        )

        return dims, details

    @staticmethod
    def _blend(
        dims: dict[str, Optional[float]], weights: dict[str, float]
    ) -> tuple[float, dict[str, float]]:
        used: dict[str, float] = {}
        acc = 0.0
        for key, w in weights.items():
            val = dims.get(key)
            if val is None:
                continue
            used[key] = w
            acc += w * val
        total = sum(used.values())
        if total == 0:
            return 0.0, used
        # renormalise so dropped dimensions don't deflate the score
        return acc / total, used


# ---------------------------------------------------------------------------
# extraction helpers — tolerant of missing keys
# ---------------------------------------------------------------------------

def _summary(report: dict, schema: ResponseSchema) -> dict:
    return report.get(schema.summary_field) or {}


def _classification(report: dict) -> dict:
    return report.get("classification") or {}


def _scored_map(report: dict, schema: ResponseSchema) -> dict[str, dict]:
    """Map test_id → {score, action} using the configured field names."""
    out: dict[str, dict] = {}
    for t in report.get(schema.tests_field) or []:
        tid = str(t.get(schema.test_id_field) or "").strip()
        if not tid:
            continue
        out[tid] = {
            "composite_score": float(t.get(schema.score_field) or 0.0),
            "recommended_action": t.get(schema.action_field) or "",
        }
    return out


def _generated_test_titles(report: dict) -> list[str]:
    return [
        str(t.get("title") or t.get("name") or "").strip()
        for t in (report.get("generated_test_cases") or [])
        if (t.get("title") or t.get("name") or "").strip()
    ]


def _entities(report: dict) -> set[str]:
    return {
        str(e).strip()
        for e in (report.get("parsed_cr", {}).get("entities") or [])
        if str(e).strip()
    }


def _gap_components(report: dict, schema: ResponseSchema) -> set[str]:
    return {
        str(g.get(schema.gap_component_field) or "").strip()
        for g in (report.get(schema.gaps_field) or [])
        if (g.get(schema.gap_component_field) or "").strip()
    }


def _per_test_detail(common: set[str], scored: list[dict]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for tid in sorted(common):
        scores = [m[tid]["composite_score"] for m in scored]
        actions = [m[tid]["recommended_action"] for m in scored]
        detail[tid] = {
            "score_mean": round(metrics.mean(scores), 4),
            "score_stdev": round(metrics.stdev(scores), 4),
            "score_min": round(min(scores), 4),
            "score_max": round(max(scores), 4),
            "action_agreement": round(metrics.categorical_agreement(actions), 4),
            "actions": actions,
        }
    return detail


def _band(score: float) -> str:
    for threshold, label in _BANDS:
        if score >= threshold:
            return label
    return "Poor"
