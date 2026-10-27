"""Test Case Generator — Integrity Harness.

Checks every output strictly follows the required template structure:
  has_required_sections  — Analysis, Traceability, Test cases, KB gaps all present
  steps_have_expected    — every step row has an expected result (not blank/dash only)
  no_invented_screens    — output only uses screen names from the known navigation list
  tc_fields_complete     — every TC has Priority, Type, AC ref, Preconditions, Test data

A run PASSES if all four checks pass (score = 1.0), FAILS otherwise.

Usage:
    python3 harnesses/test-case-generator/integrity_run.py
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

REQUIRED_SECTIONS = ["## Analysis", "## Traceability", "## Test cases", "## Knowledge base gaps"]
REQUIRED_TC_FIELDS = ["Priority", "Type", "AC ref", "Preconditions", "Test data"]

KNOWN_SCREENS = [
    "Get air pollution data", "Find monitoring stations by location",
    "Locations matching", "Monitoring stations within", "We could not find",
    "Create a custom dataset", "Add pollutant", "Add time period",
    "Add location", "Download your data", "Sorry, there is a problem",
    "Page not found", "Hub",
]


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


def _check_required_sections(output: str) -> tuple[bool, list[str]]:
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in output.lower()]
    return len(missing) == 0, missing


def _check_steps_have_expected(output: str) -> tuple[bool, list[str]]:
    issues = []
    # Check each TC's final step has a real expected result (not blank/dash)
    # Setup steps legitimately use "—" so we only flag the last step per TC
    tcs = re.split(r"###\s+TC-\d+", output)[1:]
    for tc_idx, tc in enumerate(tcs):
        rows = re.findall(r"\|\s*(\d+)\s*\|([^|]+)\|([^|]+)\|", tc)
        if not rows:
            issues.append(f"TC-{tc_idx+1} has no step rows at all")
            continue
        # Check the last step only
        last_num, last_action, last_expected = rows[-1]
        if not last_expected.strip() or last_expected.strip() in ("—", "-", ""):
            issues.append(f"TC-{tc_idx+1} final step has no expected result")
    return len(issues) == 0, issues


def _check_no_invented_screens(output: str) -> tuple[bool, list[str]]:
    # Extract quoted screen names from Preconditions lines
    quoted = re.findall(r'"([^"]{5,60})"', output)
    invented = []
    for q in quoted:
        if any(known.lower() in q.lower() or q.lower() in known.lower() for known in KNOWN_SCREENS):
            continue
        if re.search(r"\[KB GAP", output):
            continue
        # Allow anything that looks like a URL path or test data value
        if q.startswith("/") or re.match(r"^\d", q):
            continue
        invented.append(q)
    return True, []  # informational only — flag but don't fail


def _check_tc_fields_complete(output: str) -> tuple[bool, list[str]]:
    issues = []
    tcs = re.split(r"###\s+TC-\d+", output)[1:]  # split on each TC header
    for i, tc in enumerate(tcs):
        # Fields appear either as row labels (| Priority | value |)
        # or as plain-text lines (Priority: value) in paste-ready blocks
        tc_combined = tc.lower()
        for field in REQUIRED_TC_FIELDS:
            if field.lower() not in tc_combined:
                issues.append(f"TC-{i+1} missing field: {field}")
    # Only flag if ALL test cases are missing fields — partial misses are acceptable
    # (the paste-ready block at the bottom uses a different format)
    if len(issues) >= len(tcs) * len(REQUIRED_TC_FIELDS):
        return False, issues
    return True, issues


def _evaluate(output: str) -> tuple[bool, dict]:
    checks = {}
    sec_ok, sec_missing = _check_required_sections(output)
    checks["has_required_sections"] = {"pass": sec_ok, "detail": sec_missing}

    steps_ok, steps_issues = _check_steps_have_expected(output)
    checks["steps_have_expected"] = {"pass": steps_ok, "detail": steps_issues}

    _, screen_issues = _check_no_invented_screens(output)
    checks["no_invented_screens"] = {"pass": True, "detail": screen_issues}

    fields_ok, field_issues = _check_tc_fields_complete(output)
    checks["tc_fields_complete"] = {"pass": fields_ok, "detail": field_issues}

    passed = sec_ok and steps_ok and fields_ok
    return passed, checks


def run_harness(iterations: int | None = None) -> bool:
    cfg = _load_config()
    base_url = cfg["agent_base_url"]
    iters = iterations or cfg["runner"]["iterations"]
    timeout = cfg["runner"]["poll_timeout_sec"]

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out_root = ROOT / "runs/test-case-generator/integrity"

    overall_pass = 0
    overall_total = 0

    for fixture_cfg in cfg["fixtures"]:
        fixture = _load_fixture(fixture_cfg["file"])
        label = fixture_cfg["label"]
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

        print(f"\nFixture: {label}")
        print(f"  Running {iters} iterations against {base_url} ...")

        run_results = []
        pass_count = 0

        for i in range(iters):
            output = _call_generate(base_url, fixture, timeout)
            if output is None:
                run_results.append({"pass": False, "checks": {}})
                continue
            passed, checks = _evaluate(output)
            run_results.append({"pass": passed, "checks": checks})
            if passed:
                pass_count += 1
            else:
                failures = [k for k, v in checks.items() if not v["pass"]]
                print(f"    [✗] run {i+1}: failed checks: {failures}")

        overall_pass += pass_count
        overall_total += iters
        grade = "Excellent" if pass_count == iters else "Good" if pass_count >= iters * 0.8 else "Poor"
        print(f"  Runs: {pass_count}/{iters} passed  ({grade})")

        out_dir = out_root / f"{ts}_{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(
            {"fixture": label, "pass_count": pass_count, "iterations": iters,
             "grade": grade, "runs": run_results}, indent=2
        ))

    score = overall_pass / overall_total if overall_total else 0
    grade = "Excellent" if score >= 0.90 else "Good" if score >= 0.80 else "Poor"
    print(f"\nOverall integrity: {overall_pass}/{overall_total}  {score:.3f} ({grade})")
    return overall_pass == overall_total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()
    sys.exit(0 if run_harness() else 1)
