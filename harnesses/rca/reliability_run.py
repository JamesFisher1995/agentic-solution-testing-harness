"""RCA Reliability Harness — asserts the RCA correctly identifies the failure
type and cause category for known failure scenarios.

Design intent
-------------
For each scenario we submit the same fixture N times and assert that every run:
  • returns status "complete"
  • identifies the correct cause_category (e.g. "Code change")
  • identifies the correct failure_type (e.g. "Build" or "Test")
  • mentions at least one required keyword in primary_cause

If the solution misidentifies the failure type, on-call engineers get the wrong
triage signal.  This harness quantifies that risk.

Pass condition per run
----------------------
  ALL of the following must hold:
  • status == "complete"
  • rca.cause_category in scenario.expected_cause_categories
  • summary.failure_type in scenario.expected_failure_types (if defined)
  • at least one keyword from scenario.primary_cause_keywords appears in
    rca.primary_cause (case-insensitive)

Usage
-----
    python3 harnesses/rca/reliability_run.py
    python3 harnesses/rca/reliability_run.py --iterations 10
    python3 harnesses/rca/reliability_run.py --check-health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = _HERE / "reliability_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "agent_base_url": "http://localhost:8000",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "output_dir": "runs/rca/reliability",
    "runner": {
        "iterations": 5,
        "poll_interval_sec": 3.0,
        "poll_timeout_sec": 300.0,
    },
    "scenarios": [
        {
            "label": "TypeScript compilation error",
            "file": "harnesses/rca/fixtures/build_334_typescript_error.json",
            "expected_cause_categories": ["Code change"],
            "expected_failure_types": ["Build"],
            "primary_cause_keywords": ["TypeScript", "compilation", "error"],
        },
        {
            "label": "Jest unit test failures",
            "file": "harnesses/rca/fixtures/build_failure_test_failures.json",
            "expected_cause_categories": ["Code change"],
            "expected_failure_types": ["Test"],
            "primary_cause_keywords": ["test", "fail", "API", "500", "TypeError"],
        },
    ],
}


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config(path: Optional[str], iterations_override: Optional[int]) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        cfg_path = _REPO_ROOT / cfg_path
    if cfg_path.exists():
        with open(cfg_path) as f:
            _deep_merge(cfg, json.load(f))
    if iterations_override is not None:
        cfg["runner"]["iterations"] = iterations_override
    return cfg


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_DONE = {"complete", "cache_hit"}
_FAILED = {"failed"}


async def _submit(
    client: httpx.AsyncClient,
    base: str,
    tenant_id: str,
    fixture: dict[str, Any],
    nonce_ado_run_id: str,
) -> dict[str, Any]:
    failure_data = {
        k: v for k, v in fixture.items()
        if k not in {"label", "gold_standard", "pipeline_id", "pipeline_name", "ado_run_id"}
    }
    body = {
        "tenant_id":     tenant_id,
        "pipeline_id":   fixture["pipeline_id"],
        "ado_run_id":    nonce_ado_run_id,
        "pipeline_name": fixture.get("pipeline_name", ""),
        "failure_data":  failure_data,
    }
    res = await client.post(f"{base}/analyse/pipeline-failure", json=body, timeout=30.0)
    res.raise_for_status()
    return res.json()


async def _poll(
    client: httpx.AsyncClient,
    base: str,
    tenant_id: str,
    run_id: str,
    poll_interval: float,
    poll_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + poll_timeout
    while True:
        res = await client.get(
            f"{base}/analyse/pipeline-failure/{run_id}",
            params={"tenant_id": tenant_id},
        )
        res.raise_for_status()
        row = res.json()
        status = row.get("status", "unknown")
        if status in _DONE or status in _FAILED:
            return row
        if time.monotonic() >= deadline:
            row["status"] = "timeout"
            return row
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Reliability check
# ---------------------------------------------------------------------------

def _check(row: dict, scenario: dict) -> list[str]:
    """Return violation strings. Empty = pass."""
    violations: list[str] = []
    status = row.get("status", "unknown")

    if status not in _DONE:
        violations.append(
            f"status={status!r} — run did not complete "
            f"(error: {row.get('error_message', '?')})"
        )
        return violations  # no report to check

    report = row.get("report") or {}
    rca = report.get("rca") or {}
    summary = report.get("summary") or {}

    # cause_category
    cause_cat = rca.get("cause_category", "")
    expected_cats = scenario.get("expected_cause_categories", [])
    if expected_cats and cause_cat not in expected_cats:
        violations.append(
            f"cause_category={cause_cat!r} — expected one of {expected_cats}"
        )

    # failure_type
    failure_type = summary.get("failure_type", "")
    expected_types = scenario.get("expected_failure_types", [])
    if expected_types and failure_type not in expected_types:
        violations.append(
            f"failure_type={failure_type!r} — expected one of {expected_types}"
        )

    # primary_cause keyword
    primary_cause = (rca.get("primary_cause") or "").lower()
    keywords = scenario.get("primary_cause_keywords", [])
    if keywords and not any(kw.lower() in primary_cause for kw in keywords):
        violations.append(
            f"primary_cause missing all expected keywords {keywords} "
            f"(got: {primary_cause[:120]!r})"
        )

    return violations


# ---------------------------------------------------------------------------
# Run one iteration
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    index: int
    run_id: Optional[str] = None
    status: str = "pending"
    passed: bool = False
    violations: list[str] = field(default_factory=list)
    cause_category: Optional[str] = None
    failure_type: Optional[str] = None
    primary_cause_snippet: Optional[str] = None
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "index":                self.index,
            "run_id":               self.run_id,
            "status":               self.status,
            "passed":               self.passed,
            "violations":           self.violations,
            "cause_category":       self.cause_category,
            "failure_type":         self.failure_type,
            "primary_cause_snippet": self.primary_cause_snippet,
            "elapsed_sec":          self.elapsed_sec,
            "error":                self.error,
        }


async def _run_once(
    index: int,
    client: httpx.AsyncClient,
    cfg: dict,
    fixture: dict[str, Any],
    scenario: dict,
    log: logging.Logger,
) -> RunResult:
    res = RunResult(index=index)
    t0 = time.monotonic()
    runner = cfg["runner"]
    nonce_ado_run_id = f"{fixture['ado_run_id']}#{uuid.uuid4().hex[:8]}"

    try:
        submitted = await _submit(
            client, cfg["agent_base_url"], cfg["tenant_id"], fixture, nonce_ado_run_id
        )
        run_id = submitted.get("run_id")
        if not run_id:
            raise RuntimeError(f"no run_id in submit response: {submitted!r}")
        res.run_id = run_id
        log.info("run %d submitted — run_id=%s", index + 1, run_id)

        row = await _poll(
            client, cfg["agent_base_url"], cfg["tenant_id"], run_id,
            runner["poll_interval_sec"], runner["poll_timeout_sec"],
        )
        res.status = row.get("status", "unknown")

        report = (row.get("report") or {})
        rca = report.get("rca") or {}
        summary = report.get("summary") or {}
        res.cause_category = rca.get("cause_category")
        res.failure_type = summary.get("failure_type")
        res.primary_cause_snippet = (rca.get("primary_cause") or "")[:100]

        violations = _check(row, scenario)
        res.violations = violations
        res.passed = len(violations) == 0

    except Exception as exc:
        res.status = "error"
        res.error = f"{type(exc).__name__}: {exc}"
        res.passed = False
        res.violations = [f"run error: {exc}"]
        log.error("run %d error: %s", index + 1, exc)
    finally:
        res.elapsed_sec = round(time.monotonic() - t0, 2)

    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _band(score: float) -> str:
    if score == 1.0:
        return "Excellent"
    if score >= 0.8:
        return "Good"
    if score >= 0.6:
        return "Fair"
    return "Poor"


def _write_report(
    out_dir: Path,
    scenario_label: str,
    results: list[RunResult],
    scenario: dict,
    elapsed_total: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "results.json", "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)

    n = len(results)
    passed = sum(1 for r in results if r.passed)
    score = passed / n if n else 0.0
    band = _band(score)

    lines = [
        "# RCA Reliability Report",
        "",
        f"**Scenario:** {scenario_label}  ",
        f"**Expected cause_category:** {scenario.get('expected_cause_categories')}  ",
        f"**Expected failure_type:** {scenario.get('expected_failure_types')}  ",
        f"**Runs:** {passed}/{n} passed  ",
        f"**Reliability score:** {score:.3f} ({band})  ",
        f"**Elapsed:** {elapsed_total:.1f}s  ",
        "",
        "## Per-run results",
        "",
        "| Run | Status | cause_category | failure_type | primary_cause (snippet) | Pass |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        tick = "✓" if r.passed else "✗"
        lines.append(
            f"| {r.index + 1} | {r.status} "
            f"| {r.cause_category or '—'} "
            f"| {r.failure_type or '—'} "
            f"| {(r.primary_cause_snippet or '—')[:60]} "
            f"| {tick} |"
        )

    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## Violations", ""]
        for r in failed:
            lines.append(f"**Run {r.index + 1}:**")
            for v in r.violations:
                lines.append(f"- {v}")

    with open(out_dir / "summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _check_health(cfg: dict) -> int:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(f"{cfg['agent_base_url']}/health")
            print(f"OK  {cfg['agent_base_url']}/health -> {res.status_code}")
            return 0
        except Exception as exc:
            print(f"FAIL {cfg['agent_base_url']}/health -> {exc}")
            return 1


async def _run_scenario(
    scenario: dict,
    cfg: dict,
    out_root: Path,
    log: logging.Logger,
) -> tuple[list[RunResult], Path]:
    fixture_path = _REPO_ROOT / scenario["file"]
    with open(fixture_path) as f:
        fixture = json.load(f)

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [
            _run_once(i, client, cfg, fixture, scenario, log)
            for i in range(cfg["runner"]["iterations"])
        ]
        results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0

    results = list(results)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = scenario["label"].lower().replace(" ", "_").replace("—", "").replace(",", "")[:40]
    out_dir = out_root / f"{ts}_{slug}"
    _write_report(out_dir, scenario["label"], results, scenario, elapsed)

    return results, out_dir


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rca-reliability-harness", description=__doc__)
    p.add_argument("--config", help="path to reliability_config.json")
    p.add_argument("--iterations", type=int)
    p.add_argument("--agent-url")
    p.add_argument("--tenant-id")
    p.add_argument("--check-health", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("rca-reliability")

    cfg = load_config(args.config, args.iterations)
    if args.agent_url:
        cfg["agent_base_url"] = args.agent_url.rstrip("/")
    if args.tenant_id:
        cfg["tenant_id"] = args.tenant_id

    if args.check_health:
        return asyncio.run(_check_health(cfg))

    out_root = _REPO_ROOT / cfg["output_dir"]
    all_passed = 0
    all_total = 0
    overall_ok = True

    for scenario in cfg["scenarios"]:
        print(f"\nScenario: {scenario['label']}")
        print(f"  Expected: cause_category={scenario.get('expected_cause_categories')}  "
              f"failure_type={scenario.get('expected_failure_types')}")
        print(f"  Running {cfg['runner']['iterations']} iterations ...")

        try:
            results, out_dir = asyncio.run(_run_scenario(scenario, cfg, out_root, log))
        except Exception as exc:
            log.error("scenario '%s' failed: %s", scenario["label"], exc)
            overall_ok = False
            continue

        n = len(results)
        passed = sum(1 for r in results if r.passed)
        score = passed / n if n else 0.0
        all_passed += passed
        all_total += n

        print(f"  Runs: {passed}/{n} passed  ({_band(score)})")
        for r in results:
            tick = "✓" if r.passed else "✗"
            print(f"    [{tick}] run {r.index + 1}: cause_category={r.cause_category!r}  "
                  f"failure_type={r.failure_type!r}")
            for v in r.violations:
                print(f"         ! {v}")

        print(f"  Artifacts: {out_dir}/summary.md")

        if passed < n:
            overall_ok = False

    if all_total:
        overall = all_passed / all_total
        print(f"\nOverall reliability: {all_passed}/{all_total}  {overall:.3f} ({_band(overall)})")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
