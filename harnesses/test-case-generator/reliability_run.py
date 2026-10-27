"""Test Case Generator — Reliability Harness.

Tests the service handles edge cases without crashing or returning empty output:
  - A ticket with no ACs (derives them from description)
  - A very vague/minimal ticket (one line)
  - A ticket referencing a feature not in the knowledge base

A run PASSES if the service returns non-empty structured output with at least
one test case and an Analysis section, regardless of quality.

Usage:
    python3 harnesses/test-case-generator/reliability_run.py
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

EDGE_CASE_FIXTURES = [
    {
        "label": "No ACs supplied",
        "key": "NOAC-001",
        "ticket": "The cookie banner should use the updated Defra approved wording. The current wording is outdated.",
    },
    {
        "label": "Very vague ticket",
        "key": "VAGUE-001",
        "ticket": "Fix the search page.",
    },
    {
        "label": "Feature not in knowledge base",
        "key": "UNKNOWN-001",
        "ticket": "Add a new 'Save my search' feature so users can bookmark their last search result.\n\nAC1: A 'Save search' button appears on the results page.\nAC2: Clicking it saves the current search term and radius to the user's browser.\nAC3: On return visits, the saved search is pre-filled.",
    },
]


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


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


def _is_valid_output(output: str) -> tuple[bool, str]:
    if not output or len(output.strip()) < 100:
        return False, "output too short or empty"
    if not re.search(r"##\s*Analysis", output, re.IGNORECASE):
        return False, "missing Analysis section"
    if not re.search(r"###\s*TC-\d+", output):
        return False, "no test cases generated"
    return True, "ok"


def run_harness(iterations: int | None = None) -> bool:
    cfg = _load_config()
    base_url = cfg["agent_base_url"]
    iters = iterations or cfg["runner"]["iterations"]
    timeout = cfg["runner"]["poll_timeout_sec"]

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root = ROOT / "runs/test-case-generator/reliability"

    overall_pass = 0
    overall_total = 0

    for fixture in EDGE_CASE_FIXTURES:
        label = fixture["label"]
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

        print(f"\nScenario: {label}")
        print(f"  Running {iters} iterations against {base_url} ...")

        pass_count = 0
        run_results = []

        for i in range(iters):
            output = _call_generate(base_url, fixture, timeout)
            valid, reason = _is_valid_output(output or "")
            run_results.append({"pass": valid, "reason": reason})
            if valid:
                pass_count += 1
            else:
                print(f"    [✗] run {i+1}: {reason}")

        overall_pass += pass_count
        overall_total += iters
        grade = "Excellent" if pass_count == iters else "Good" if pass_count >= iters * 0.8 else "Poor"
        print(f"  Runs: {pass_count}/{iters} passed  ({grade})")

        out_dir = out_root / f"{ts}_{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(
            {"scenario": label, "pass_count": pass_count, "iterations": iters,
             "grade": grade, "runs": run_results}, indent=2
        ))

    score = overall_pass / overall_total if overall_total else 0
    grade = "Excellent" if score >= 0.90 else "Good" if score >= 0.80 else "Poor"
    print(f"\nOverall reliability: {overall_pass}/{overall_total}  {score:.3f} ({grade})")
    return overall_pass == overall_total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()
    sys.exit(0 if run_harness() else 1)
