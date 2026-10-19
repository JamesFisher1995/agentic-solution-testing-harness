"""RCA Integrity Harness — asserts the RCA does NOT hallucinate when no task
data is available (pipeline failed at validation before any tasks ran).

Pass condition per run
----------------------
  • run reaches status "failed" (not "complete")
  • response carries an error_message containing the expected phrases
  • response does NOT contain a "report" with rca/summary content (no hallucination)

Usage
-----
    python3 harnesses/rca/integrity_run.py
    python3 harnesses/rca/integrity_run.py --iterations 10
    python3 harnesses/rca/integrity_run.py --check-health
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

_DEFAULT_CONFIG = _HERE / "integrity_config.json"

DEFAULT_CONFIG = {
    "agent_base_url": "http://localhost:8000",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "output_dir": "runs/rca/integrity",
    "runner": {
        "iterations": 5,
        "poll_interval_sec": 3.0,
        "poll_timeout_sec": 120.0,
    },
    "scenarios": [
        {
            "label": "Validation failure — no tasks executed",
            "file": "harnesses/rca/fixtures/validation_failure_no_tasks.json",
        }
    ],
}


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


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# HTTP helpers — same pattern as consistency harness
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
        if k not in {"label", "integrity_assertions", "description",
                     "pipeline_id", "pipeline_name", "ado_run_id"}
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
# Integrity check
# ---------------------------------------------------------------------------

def _check(row: dict, assertions: dict) -> list[str]:
    """Return list of violation strings. Empty = pass."""
    violations: list[str] = []
    status = row.get("status", "unknown")

    expected_status = assertions.get("expected_status", "failed")
    if status != expected_status:
        violations.append(
            f"status={status!r} — expected {expected_status!r} "
            f"(hallucination: RCA completed instead of rejecting empty data)"
        )

    # If it completed, check whether there's an actual report (hallucination)
    if assertions.get("must_not_have_report") and row.get("report"):
        report = row["report"]
        rca = report.get("rca") or {}
        primary_cause = rca.get("primary_cause", "")
        if primary_cause:
            violations.append(
                f"hallucinated primary_cause: {primary_cause[:120]!r}"
            )

    # Error message should contain expected phrases
    error_msg = (row.get("error_message") or "").lower()
    for phrase in assertions.get("error_message_must_contain", []):
        if phrase.lower() not in error_msg:
            violations.append(
                f"error_message missing expected phrase {phrase!r} "
                f"(got: {row.get('error_message', '(none)')!r})"
            )

    # Error message must NOT contain hallucinated content
    for phrase in assertions.get("must_not_contain", []):
        if phrase.lower() in error_msg:
            violations.append(
                f"error_message contains forbidden phrase {phrase!r} — possible hallucination"
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
    error_message: Optional[str] = None
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "index":         self.index,
            "run_id":        self.run_id,
            "status":        self.status,
            "passed":        self.passed,
            "violations":    self.violations,
            "error_message": self.error_message,
            "elapsed_sec":   self.elapsed_sec,
            "error":         self.error,
        }


async def _run_once(
    index: int,
    client: httpx.AsyncClient,
    cfg: dict,
    fixture: dict[str, Any],
    assertions: dict[str, Any],
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
        res.error_message = row.get("error_message")

        violations = _check(row, assertions)
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

def _write_report(
    out_dir: Path,
    scenario_label: str,
    results: list[RunResult],
    elapsed_total: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "results.json", "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)

    n = len(results)
    passed = sum(1 for r in results if r.passed)
    score = passed / n if n else 0.0
    band = "Excellent" if score == 1.0 else ("Good" if score >= 0.8 else ("Fair" if score >= 0.6 else "Poor"))

    lines = [
        "# RCA Integrity Report",
        "",
        f"**Scenario:** {scenario_label}  ",
        f"**Runs:** {passed}/{n} passed  ",
        f"**Integrity score:** {score:.3f} ({band})  ",
        f"**Elapsed:** {elapsed_total:.1f}s  ",
        "",
        "## Per-run results",
        "",
        "| Run | Status | RCA status | Error message | Pass |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        err_snippet = (r.error_message or "—")[:80]
        tick = "✓" if r.passed else "✗"
        lines.append(
            f"| {r.index + 1} | {r.status} | {r.status} | {err_snippet} | {tick} |"
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

    assertions = fixture.get("integrity_assertions", {})
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [
            _run_once(i, client, cfg, fixture, assertions, log)
            for i in range(cfg["runner"]["iterations"])
        ]
        results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - t0
    results = list(results)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = scenario["label"].lower().replace(" ", "_").replace("—", "").replace(",", "")[:40]
    out_dir = out_root / f"{ts}_{slug}"
    _write_report(out_dir, scenario["label"], results, elapsed)

    return results, out_dir


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rca-integrity-harness", description=__doc__)
    p.add_argument("--config", help="path to integrity_config.json")
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
    log = logging.getLogger("rca-integrity")

    cfg = load_config(args.config, args.iterations)
    if args.agent_url:
        cfg["agent_base_url"] = args.agent_url.rstrip("/")
    if args.tenant_id:
        cfg["tenant_id"] = args.tenant_id

    if args.check_health:
        return asyncio.run(_check_health(cfg))

    out_root = _REPO_ROOT / cfg["output_dir"]
    overall_ok = True

    for scenario in cfg["scenarios"]:
        print(f"\nScenario: {scenario['label']}")
        print(f"  Running {cfg['runner']['iterations']} iterations against {cfg['agent_base_url']} ...")

        try:
            results, out_dir = asyncio.run(_run_scenario(scenario, cfg, out_root, log))
        except Exception as exc:
            log.error("scenario '%s' failed: %s", scenario["label"], exc)
            overall_ok = False
            continue

        n = len(results)
        passed = sum(1 for r in results if r.passed)
        score = passed / n if n else 0.0
        band = "Excellent" if score == 1.0 else ("Good" if score >= 0.8 else ("Fair" if score >= 0.6 else "Poor"))

        print(f"  Runs: {passed}/{n} passed")
        print(f"  Integrity score: {score:.3f} ({band})")

        failed_runs = [r for r in results if not r.passed]
        if failed_runs:
            for r in failed_runs:
                print(f"  Run {r.index + 1} FAILED:")
                for v in r.violations:
                    print(f"    - {v}")
        else:
            sample_msg = next((r.error_message for r in results if r.error_message), None)
            if sample_msg:
                print(f"  Error message (sample): {sample_msg[:120]}")

        print(f"  Artifacts: {out_dir}")
        print(f"    -> {out_dir}/summary.md")

        if passed < n:
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
