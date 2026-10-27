"""Test Case Generator — Consistency Harness.

Scores how consistently the generator produces the same output structure
across multiple runs of the same ticket:

  ac_count_stability    — does the number of test cases stay the same?
  type_stability        — do the same test types appear every run?
  traceability_stability — does every run include a traceability table?
  structure_stability   — does every run follow the template sections?

Usage:
    python3 harnesses/test-case-generator/consistency_run.py
    python3 harnesses/test-case-generator/consistency_run.py --iterations 5
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys

import httpx

ROOT = pathlib.Path(__file__).parent.parent.parent
CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _load_fixture(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


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
        print(f"    [ERROR] {exc}", file=sys.stderr)
        return None


def _extract_features(output: str) -> dict:
    tc_count = len(re.findall(r"###\s+TC-\d+", output))
    types_found = set(re.findall(r"\|\s*Type\s*\|\s*([^\|]+)\|", output))
    types_found = {t.strip() for t in types_found if t.strip()}
    has_traceability = bool(re.search(r"##\s*Traceability", output, re.IGNORECASE))
    has_analysis = bool(re.search(r"##\s*Analysis", output, re.IGNORECASE))
    has_kb_gaps = bool(re.search(r"##\s*Knowledge base gaps", output, re.IGNORECASE))
    return {
        "tc_count": tc_count,
        "types": types_found,
        "has_traceability": has_traceability,
        "has_analysis": has_analysis,
        "has_kb_gaps": has_kb_gaps,
    }


def _score_consistency(runs: list[dict]) -> dict[str, float]:
    if not runs:
        return {}

    # ac_count_stability — how consistent is the TC count?
    counts = [r["tc_count"] for r in runs]
    max_count = max(counts) if counts else 1
    count_variance = sum(abs(c - counts[0]) for c in counts) / len(counts)
    ac_count_stability = max(0.0, 1.0 - (count_variance / max(max_count, 1)))

    # type_stability — do the same types appear in every run?
    all_types = [r["types"] for r in runs]
    common_types = set.intersection(*all_types) if all_types else set()
    all_types_union = set.union(*all_types) if all_types else set()
    type_stability = len(common_types) / len(all_types_union) if all_types_union else 1.0

    # traceability_stability — always present?
    traceability_stability = sum(1 for r in runs if r["has_traceability"]) / len(runs)

    # structure_stability — analysis + kb_gaps always present?
    structure_stability = (
        sum(1 for r in runs if r["has_analysis"] and r["has_kb_gaps"]) / len(runs)
    )

    return {
        "ac_count_stability":    round(ac_count_stability, 3),
        "type_stability":        round(type_stability, 3),
        "traceability_stability": round(traceability_stability, 3),
        "structure_stability":   round(structure_stability, 3),
    }


def run_harness(iterations: int | None = None) -> bool:
    cfg = _load_config()
    base_url = cfg["agent_base_url"]
    iters = iterations or cfg["runner"]["iterations"]
    timeout = cfg["runner"]["poll_timeout_sec"]
    pass_min = 0.80

    weights = {
        "ac_count_stability":    0.35,
        "type_stability":        0.25,
        "traceability_stability": 0.25,
        "structure_stability":   0.15,
    }

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root = ROOT / "runs/test-case-generator/consistency"

    all_scores: list[float] = []
    all_pass = 0
    all_total = 0

    for fixture_cfg in cfg["fixtures"]:
        fixture = _load_fixture(fixture_cfg["file"])
        label = fixture_cfg["label"]
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

        print(f"\nFixture: {label}")
        print(f"  Running {iters} iterations against {base_url} ...")

        run_features: list[dict] = []
        for _ in range(iters):
            output = _call_generate(base_url, fixture, timeout)
            if output:
                run_features.append(_extract_features(output))

        scores = _score_consistency(run_features)
        overall = sum(weights[k] * scores.get(k, 0) for k in weights)
        grade = "Excellent" if overall >= 0.90 else "Good" if overall >= 0.80 else "Acceptable" if overall >= 0.70 else "Poor"
        passed = overall >= pass_min
        all_pass += 1 if passed else 0
        all_total += 1
        all_scores.append(overall)

        print(f"  Overall consistency: {overall:.3f} ({grade})")
        for k, v in scores.items():
            print(f"  {k:<28}: {v:.3f}")

        out_dir = out_root / f"{ts}_{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(
            {"fixture": label, "overall": round(overall, 3), "grade": grade, "scores": scores}, indent=2
        ))

    grand = sum(all_scores) / len(all_scores) if all_scores else 0.0
    grand_grade = "Excellent" if grand >= 0.90 else "Good" if grand >= 0.80 else "Acceptable" if grand >= 0.70 else "Poor"
    print(f"\nOverall: {all_pass}/{all_total} fixtures passed  {grand:.3f} ({grand_grade})")
    return all_pass == all_total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()
    sys.exit(0 if run_harness(iterations=args.iterations) else 1)
