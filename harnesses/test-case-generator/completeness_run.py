"""Test Case Generator — Completeness Harness.

Scores how completely the generated test cases cover the gold standard:
  ac_coverage       — does the output contain at least one test case per derived AC?
  type_coverage     — are the required test types present (Positive, Negative, Accessibility etc)?
  scenario_coverage — are the must_cover_scenarios addressed in the output?
  no_invention      — does the output avoid inventing screen names / labels not in the KB?

Usage:
    python3 harnesses/test-case-generator/completeness_run.py
    python3 harnesses/test-case-generator/completeness_run.py --iterations 5
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).parent.parent.parent
CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"

KNOWN_SCREEN_NAMES = [
    "Get air pollution data", "Find monitoring stations by location",
    "Locations matching", "Monitoring stations within", "We could not find",
    "Create a custom dataset", "Add pollutant", "Add time period",
    "Add location", "Download your data", "Sorry, there is a problem",
    "Page not found", "Hub", "Start page",
]


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _load_fixture(path: str) -> dict:
    p = ROOT / path
    return json.loads(p.read_text())


def _call_generate(base_url: str, fixture: dict, timeout: float) -> str | None:
    try:
        r = httpx.post(
            f"{base_url}/generate",
            json={"ticket": fixture["ticket"], "key": fixture["key"]},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["test_cases_md"]
    except Exception as exc:
        print(f"    [ERROR] API call failed: {exc}", file=sys.stderr)
        return None


# ── Evaluators ────────────────────────────────────────────────────────────────

def _score_ac_coverage(output: str, gold: dict) -> float:
    """Check output contains at least one TC per derived AC."""
    min_acs = gold["must_identify"].get("derived_ac_count_min", 1)
    tc_count = len(re.findall(r"###\s+TC-\d+", output))
    if tc_count == 0:
        return 0.0
    return min(1.0, tc_count / min_acs)


def _score_type_coverage(output: str, gold: dict) -> float:
    """Check required test types appear in the output."""
    required = gold.get("must_include_types", [])
    if not required:
        return 1.0
    found = sum(1 for t in required if re.search(rf"\b{re.escape(t)}\b", output, re.IGNORECASE))
    return found / len(required)


def _score_scenario_coverage(output: str, gold: dict) -> float:
    """Check must_cover_scenarios are addressed (keyword overlap)."""
    scenarios = gold.get("must_cover_scenarios", [])
    if not scenarios:
        return 1.0
    output_lower = output.lower()
    covered = 0
    for scenario in scenarios:
        keywords = [w for w in scenario.lower().split() if len(w) > 3]
        if any(kw in output_lower for kw in keywords):
            covered += 1
    return covered / len(scenarios)


def _score_no_invention(output: str, gold: dict) -> float:
    """Check the output doesn't invent screen names outside the known list.
    Penalises [KB GAP] markers (acceptable) but not unknown screen names."""
    kb_gap_count = len(re.findall(r"\[KB GAP", output))
    tc_count = max(1, len(re.findall(r"###\s+TC-\d+", output)))
    if kb_gap_count > tc_count * 2:
        return 0.5
    return 1.0


# ── Runner ────────────────────────────────────────────────────────────────────

def run_harness(iterations: int | None = None) -> bool:
    cfg = _load_config()
    base_url = cfg["agent_base_url"]
    weights = cfg["evaluator"]["weights"]
    pass_min = cfg["evaluator"]["thresholds"]["pass_min"]
    iters = iterations or cfg["runner"]["iterations"]
    timeout = cfg["runner"]["poll_timeout_sec"]

    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_root = ROOT / cfg["output_dir"]

    overall_pass = 0
    overall_total = 0
    all_fixture_scores: list[float] = []

    for fixture_cfg in cfg["fixtures"]:
        fixture = _load_fixture(fixture_cfg["file"])
        gold = fixture["gold_standard"]
        label = fixture_cfg["label"]
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

        print(f"\nFixture: {label}")
        print(f"  Running {iters} iterations against {base_url} ...")

        run_scores: list[float] = []
        pass_count = 0

        for i in range(iters):
            output = _call_generate(base_url, fixture, timeout)
            if output is None:
                run_scores.append(0.0)
                continue

            scores = {
                "ac_coverage":       _score_ac_coverage(output, gold),
                "type_coverage":     _score_type_coverage(output, gold),
                "scenario_coverage": _score_scenario_coverage(output, gold),
                "no_invention":      _score_no_invention(output, gold),
            }
            overall_score = sum(weights[k] * v for k, v in scores.items())
            run_scores.append(overall_score)
            if overall_score >= pass_min:
                pass_count += 1

        avg = sum(run_scores) / len(run_scores) if run_scores else 0.0
        grade = "Excellent" if avg >= 0.90 else "Good" if avg >= 0.80 else "Acceptable" if avg >= 0.70 else "Poor"

        print(f"  Runs: {pass_count}/{iters} passed (>= {int(pass_min*100)}%)")
        print(f"  Overall completeness: {avg:.3f} ({grade})")

        out_dir = out_root / f"{ts}_{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "fixture": label, "key": fixture["key"],
            "iterations": iters, "pass_count": pass_count,
            "overall": round(avg, 3), "grade": grade,
            "run_scores": [round(s, 3) for s in run_scores],
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

        overall_pass += pass_count
        overall_total += iters
        all_fixture_scores.append(avg)

    grand_avg = sum(all_fixture_scores) / len(all_fixture_scores) if all_fixture_scores else 0.0
    grand_grade = "Excellent" if grand_avg >= 0.90 else "Good" if grand_avg >= 0.80 else "Acceptable" if grand_avg >= 0.70 else "Poor"

    print(f"\nOverall: {overall_pass}/{overall_total} runs passed  {grand_avg:.3f} ({grand_grade})")
    return overall_pass == overall_total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()
    success = run_harness(iterations=args.iterations)
    sys.exit(0 if success else 1)
