"""RCA Completeness Harness — scores how completely the RCA report covers the
gold-standard evidence and recommendations.

Design intent
-------------
For each fixture we submit N times and score three recall dimensions against
the gold_standard defined in the fixture:

  keyword_recall     — fraction of primary_cause_keywords found in primary_cause
  file_recall        — fraction of failed_files mentioned anywhere in the report
  recommendation_recall — fraction of must_recommend items addressed in fixes[]

Overall completeness = weighted average across dimensions.

A run passes if overall completeness >= thresholds.pass_min (default 0.80).

Usage
-----
    python3 harnesses/rca/completeness_run.py
    python3 harnesses/rca/completeness_run.py --iterations 10
    python3 harnesses/rca/completeness_run.py --check-health
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
from collections import defaultdict
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

_DEFAULT_CONFIG = _HERE / "completeness_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "agent_base_url": "http://localhost:8000",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "output_dir": "runs/rca/completeness",
    "runner": {
        "iterations": 5,
        "poll_interval_sec": 3.0,
        "poll_timeout_sec": 300.0,
    },
    "thresholds": {
        "pass_min": 0.75,
    },
    "evaluator": {
        "weights": {
            "keyword_recall":         0.40,
            "file_recall":            0.35,
            "recommendation_recall":  0.25,
        }
    },
    "fixtures": [
        {
            "label": "TypeScript compilation error",
            "file": "harnesses/rca/fixtures/build_334_typescript_error.json",
        },
        {
            "label": "Jest unit test failures",
            "file": "harnesses/rca/fixtures/build_failure_test_failures.json",
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
# Scoring
# ---------------------------------------------------------------------------

def _report_text(report: dict) -> str:
    """Flatten the entire report to a single lowercase string for substring search."""
    return json.dumps(report).lower()


def _keyword_recall(report: dict, gold: dict) -> tuple[float, list[str], list[str]]:
    keywords = gold.get("must_identify", {}).get("primary_cause_keywords", [])
    if not keywords:
        return 1.0, [], []
    primary_cause = (report.get("rca", {}).get("primary_cause") or "").lower()
    found = [kw for kw in keywords if kw.lower() in primary_cause]
    missed = [kw for kw in keywords if kw.lower() not in primary_cause]
    return len(found) / len(keywords), found, missed


def _file_recall(report: dict, gold: dict) -> tuple[float, list[str], list[str]]:
    files = gold.get("must_identify", {}).get("failed_files", [])
    if not files:
        return 1.0, [], []
    text = _report_text(report)
    found = [f for f in files if f.lower() in text]
    missed = [f for f in files if f.lower() not in text]
    return len(found) / len(files), found, missed


def _recommendation_recall(report: dict, gold: dict) -> tuple[float, list[str], list[str]]:
    recs = gold.get("must_recommend", [])
    if not recs:
        return 1.0, [], []
    # Collect all fix text from fixes[] array
    fixes = report.get("fixes") or []
    fixes_text = " ".join(
        " ".join([
            str(f.get("description", "")),
            str(f.get("fix_description", "")),
            str(f.get("title", "")),
            str(f.get("action", "")),
        ])
        for f in fixes
    ).lower()
    # Also check rca.primary_cause and rca.evidence_chain
    rca = report.get("rca") or {}
    rca_text = " ".join([
        str(rca.get("primary_cause", "")),
        str(rca.get("evidence_chain", "")),
        json.dumps(rca.get("five_whys", [])),
    ]).lower()
    full_text = fixes_text + " " + rca_text

    found = []
    missed = []
    for rec in recs:
        # Split rec into keywords and check if most of them appear
        words = [w for w in rec.lower().split() if len(w) > 3]
        if not words:
            found.append(rec)
            continue
        hits = sum(1 for w in words if w in full_text)
        if hits / len(words) >= 0.5:
            found.append(rec)
        else:
            missed.append(rec)
    return len(found) / len(recs), found, missed


def _score_run(report: dict, gold: dict, weights: dict) -> dict[str, Any]:
    kw_score, kw_found, kw_missed = _keyword_recall(report, gold)
    file_score, file_found, file_missed = _file_recall(report, gold)
    rec_score, rec_found, rec_missed = _recommendation_recall(report, gold)

    total_w = sum(weights.values())
    overall = (
        weights.get("keyword_recall", 0.4) * kw_score +
        weights.get("file_recall", 0.35) * file_score +
        weights.get("recommendation_recall", 0.25) * rec_score
    ) / total_w

    return {
        "keyword_recall":        kw_score,
        "keywords_found":        kw_found,
        "keywords_missed":       kw_missed,
        "file_recall":           file_score,
        "files_found":           file_found,
        "files_missed":          file_missed,
        "recommendation_recall": rec_score,
        "recs_found":            rec_found,
        "recs_missed":           rec_missed,
        "overall":               overall,
    }


# ---------------------------------------------------------------------------
# Run one iteration
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    index: int
    run_id: Optional[str] = None
    status: str = "pending"
    scores: dict = field(default_factory=dict)
    passed: bool = False
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "index":       self.index,
            "run_id":      self.run_id,
            "status":      self.status,
            "scores":      self.scores,
            "passed":      self.passed,
            "elapsed_sec": self.elapsed_sec,
            "error":       self.error,
        }


async def _run_once(
    index: int,
    client: httpx.AsyncClient,
    cfg: dict,
    fixture: dict[str, Any],
    gold: dict,
    log: logging.Logger,
) -> RunResult:
    res = RunResult(index=index)
    t0 = time.monotonic()
    runner = cfg["runner"]
    weights = cfg["evaluator"]["weights"]
    pass_min = cfg["thresholds"]["pass_min"]
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

        if res.status not in _DONE:
            res.error = row.get("error_message") or res.status
            res.passed = False
            return res

        report = row.get("report") or {}
        res.scores = _score_run(report, gold, weights)
        res.passed = res.scores["overall"] >= pass_min

    except Exception as exc:
        res.status = "error"
        res.error = f"{type(exc).__name__}: {exc}"
        res.passed = False
        log.error("run %d error: %s", index + 1, exc)
    finally:
        res.elapsed_sec = round(time.monotonic() - t0, 2)

    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _band(score: float) -> str:
    if score >= 0.90:
        return "Excellent"
    if score >= 0.80:
        return "Good"
    if score >= 0.70:
        return "Acceptable"
    if score >= 0.60:
        return "Poor"
    return "Failing"


def _write_report(
    out_dir: Path,
    label: str,
    results: list[RunResult],
    gold: dict,
    cfg: dict,
    elapsed_total: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "results.json", "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)

    n = len(results)
    pass_min = cfg["thresholds"]["pass_min"]
    passed = sum(1 for r in results if r.passed)
    ok_scores = [r.scores for r in results if r.scores]

    # Average scores across completed runs
    avg: dict[str, float] = {}
    for key in ["keyword_recall", "file_recall", "recommendation_recall", "overall"]:
        vals = [s[key] for s in ok_scores if key in s]
        avg[key] = sum(vals) / len(vals) if vals else 0.0

    # Which items were missed on each run
    missed_keywords: dict[str, int] = defaultdict(int)
    missed_files: dict[str, int] = defaultdict(int)
    missed_recs: dict[str, int] = defaultdict(int)
    for s in ok_scores:
        for kw in s.get("keywords_missed", []):
            missed_keywords[kw] += 1
        for f in s.get("files_missed", []):
            missed_files[f] += 1
        for r in s.get("recs_missed", []):
            missed_recs[r] += 1

    lines = [
        "# RCA Completeness Report",
        "",
        f"**Fixture:** {label}  ",
        f"**Runs:** {passed}/{n} passed (>= {pass_min:.0%})  ",
        f"**Overall completeness:** {avg.get('overall', 0):.3f} ({_band(avg.get('overall', 0))})  ",
        f"**Elapsed:** {elapsed_total:.1f}s  ",
        "",
        "## Average scores across runs",
        "",
        "| Dimension | Score |",
        "|---|---|",
        f"| keyword_recall | {avg.get('keyword_recall', 0):.3f} |",
        f"| file_recall | {avg.get('file_recall', 0):.3f} |",
        f"| recommendation_recall | {avg.get('recommendation_recall', 0):.3f} |",
        f"| **overall** | **{avg.get('overall', 0):.3f}** |",
        "",
        "## Per-run results",
        "",
        "| Run | Status | keyword | file | rec | overall | Pass |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r.scores
        tick = "✓" if r.passed else "✗"
        if s:
            lines.append(
                f"| {r.index + 1} | {r.status} "
                f"| {s.get('keyword_recall', 0):.2f} "
                f"| {s.get('file_recall', 0):.2f} "
                f"| {s.get('recommendation_recall', 0):.2f} "
                f"| {s.get('overall', 0):.3f} "
                f"| {tick} |"
            )
        else:
            lines.append(f"| {r.index + 1} | {r.status} | — | — | — | — | {tick} |")

    if missed_keywords or missed_files or missed_recs:
        lines += ["", "## Recall gaps (items missed across runs)", ""]
        if missed_keywords:
            lines.append("**Keywords missed:**")
            for kw, count in sorted(missed_keywords.items(), key=lambda x: -x[1]):
                lines.append(f"- `{kw}` — missed {count}/{len(ok_scores)} runs")
        if missed_files:
            lines.append("")
            lines.append("**Files missed:**")
            for f, count in sorted(missed_files.items(), key=lambda x: -x[1]):
                lines.append(f"- `{f}` — missed {count}/{len(ok_scores)} runs")
        if missed_recs:
            lines.append("")
            lines.append("**Recommendations missed:**")
            for r, count in sorted(missed_recs.items(), key=lambda x: -x[1]):
                lines.append(f"- `{r}` — missed {count}/{len(ok_scores)} runs")

    with open(out_dir / "summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    return avg


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


async def _run_fixture(
    fixture_cfg: dict,
    cfg: dict,
    out_root: Path,
    log: logging.Logger,
) -> tuple[list[RunResult], dict[str, float], Path]:
    fixture_path = _REPO_ROOT / fixture_cfg["file"]
    with open(fixture_path) as f:
        fixture = json.load(f)
    gold = fixture.get("gold_standard", {})

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [
            _run_once(i, client, cfg, fixture, gold, log)
            for i in range(cfg["runner"]["iterations"])
        ]
        results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0

    results = list(results)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = fixture_cfg["label"].lower().replace(" ", "_").replace("—", "").replace(",", "")[:40]
    out_dir = out_root / f"{ts}_{slug}"
    avg = _write_report(out_dir, fixture_cfg["label"], results, gold, cfg, elapsed)

    return results, avg or {}, out_dir


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rca-completeness-harness", description=__doc__)
    p.add_argument("--config", help="path to completeness_config.json")
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
    log = logging.getLogger("rca-completeness")

    cfg = load_config(args.config, args.iterations)
    if args.agent_url:
        cfg["agent_base_url"] = args.agent_url.rstrip("/")
    if args.tenant_id:
        cfg["tenant_id"] = args.tenant_id

    if args.check_health:
        return asyncio.run(_check_health(cfg))

    out_root = _REPO_ROOT / cfg["output_dir"]
    pass_min = cfg["thresholds"]["pass_min"]
    all_passed = 0
    all_total = 0
    overall_ok = True

    for fx_cfg in cfg["fixtures"]:
        print(f"\nFixture: {fx_cfg['label']}")
        print(f"  Running {cfg['runner']['iterations']} iterations ...")

        try:
            results, avg, out_dir = asyncio.run(_run_fixture(fx_cfg, cfg, out_root, log))
        except Exception as exc:
            log.error("fixture '%s' failed: %s", fx_cfg["label"], exc)
            overall_ok = False
            continue

        n = len(results)
        passed = sum(1 for r in results if r.passed)
        all_passed += passed
        all_total += n

        band = _band(avg.get("overall", 0))
        print(f"  Runs: {passed}/{n} passed (>= {pass_min:.0%})")
        print(f"  Overall completeness: {avg.get('overall', 0):.3f} ({band})")
        print(f"  keyword_recall        : {avg.get('keyword_recall', 0):.3f}")
        print(f"  file_recall           : {avg.get('file_recall', 0):.3f}")
        print(f"  recommendation_recall : {avg.get('recommendation_recall', 0):.3f}")
        print(f"  Artifacts: {out_dir}/summary.md")

        if passed < n:
            overall_ok = False

    if all_total:
        overall = all_passed / all_total
        print(f"\nOverall: {all_passed}/{all_total} runs passed  {overall:.3f} ({_band(overall)})")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
