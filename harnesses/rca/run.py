"""RCA Consistency Harness — entry point.

Submits the same pipeline failure fixture N times against the live RCA endpoint,
then scores the consistency of the responses across runs.

Usage
-----
    # from the repo root
    python harnesses/rca/run.py

    # override iterations
    python harnesses/rca/run.py --iterations 10

    # point at a different host
    python harnesses/rca/run.py --agent-url http://10.0.0.5:8000

    # health-check only
    python harnesses/rca/run.py --check-health
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

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from any cwd
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = _HERE / "config.json"


@dataclass
class RunnerCfg:
    iterations: int = 5
    poll_interval_sec: float = 3.0
    poll_timeout_sec: float = 300.0


@dataclass
class EvaluatorCfg:
    weights: dict[str, float] = field(default_factory=lambda: {
        "cause_category_agreement": 0.35,
        "severity_agreement":       0.25,
        "primary_cause_similarity": 0.25,
        "confidence_stability":     0.15,
    })


@dataclass
class FixtureCfg:
    label: str
    file: str


@dataclass
class Config:
    agent_base_url: str = "http://localhost:8000"
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    output_dir: str = "runs/rca/consistency"
    runner: RunnerCfg = field(default_factory=RunnerCfg)
    evaluator: EvaluatorCfg = field(default_factory=EvaluatorCfg)
    fixtures: list[FixtureCfg] = field(default_factory=list)

    @classmethod
    def load(cls, path: Optional[str]) -> "Config":
        cfg_path = Path(path) if path else _DEFAULT_CONFIG
        if not cfg_path.is_absolute():
            cfg_path = _REPO_ROOT / cfg_path
        with open(cfg_path) as f:
            raw = json.load(f)

        c = cls()
        c.agent_base_url = raw.get("agent_base_url", c.agent_base_url).rstrip("/")
        c.tenant_id = raw.get("tenant_id", c.tenant_id)
        c.output_dir = raw.get("output_dir", c.output_dir)

        r = raw.get("runner", {})
        c.runner = RunnerCfg(
            iterations=r.get("iterations", c.runner.iterations),
            poll_interval_sec=r.get("poll_interval_sec", c.runner.poll_interval_sec),
            poll_timeout_sec=r.get("poll_timeout_sec", c.runner.poll_timeout_sec),
        )

        e = raw.get("evaluator", {})
        c.evaluator = EvaluatorCfg(
            weights=e.get("weights", c.evaluator.weights),
        )

        c.fixtures = [
            FixtureCfg(label=fx["label"], file=fx["file"])
            for fx in raw.get("fixtures", [])
        ]
        return c


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
    """POST /analyse/pipeline-failure and return the JSON response."""
    failure_data = {
        k: v for k, v in fixture.items()
        if k not in {"label", "gold_standard", "pipeline_id", "pipeline_name",
                     "ado_run_id"}
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
    cfg: RunnerCfg,
) -> dict[str, Any]:
    """Poll until complete/failed/timeout. Returns the final row."""
    deadline = time.monotonic() + cfg.poll_timeout_sec
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
        await asyncio.sleep(cfg.poll_interval_sec)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _cause_category_agreement(reports: list[dict]) -> float:
    """Fraction of runs that agree on cause_category (majority wins)."""
    cats = [r.get("rca", {}).get("cause_category", "") for r in reports]
    if not cats:
        return 0.0
    from collections import Counter
    top_count = Counter(cats).most_common(1)[0][1]
    return top_count / len(cats)


def _severity_agreement(reports: list[dict]) -> float:
    """Fraction of runs that agree on top-level severity."""
    sevs = [r.get("summary", {}).get("severity", "") for r in reports]
    if not sevs:
        return 0.0
    from collections import Counter
    top_count = Counter(sevs).most_common(1)[0][1]
    return top_count / len(sevs)


def _primary_cause_similarity(reports: list[dict]) -> float:
    """Average Jaccard similarity of primary_cause word sets across all pairs."""
    texts = [
        set(r.get("rca", {}).get("primary_cause", "").lower().split())
        for r in reports
    ]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        return 1.0
    scores = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            inter = texts[i] & texts[j]
            union = texts[i] | texts[j]
            scores.append(len(inter) / len(union) if union else 1.0)
    return sum(scores) / len(scores)


_CONF_MAP = {"low": 0.25, "medium": 0.5, "high": 0.75, "very high": 0.9}


def _conf_to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return _CONF_MAP.get(str(v).lower().strip(), 0.5)


def _confidence_stability(reports: list[dict]) -> float:
    """1 - coefficient_of_variation of confidence_overall across runs."""
    confs = [_conf_to_float(r.get("rca", {}).get("confidence_overall")) for r in reports]
    if len(confs) < 2:
        return 1.0
    mean = sum(confs) / len(confs)
    if mean == 0:
        return 1.0
    variance = sum((c - mean) ** 2 for c in confs) / len(confs)
    cv = (variance ** 0.5) / mean
    return max(0.0, 1.0 - cv)


def _score(reports: list[dict], weights: dict[str, float]) -> dict[str, float]:
    metrics = {
        "cause_category_agreement": _cause_category_agreement(reports),
        "severity_agreement":       _severity_agreement(reports),
        "primary_cause_similarity": _primary_cause_similarity(reports),
        "confidence_stability":     _confidence_stability(reports),
    }
    total_w = sum(weights.values())
    overall = sum(weights.get(k, 0) * v for k, v in metrics.items()) / total_w
    return {**metrics, "overall": overall}


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


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _write_report(
    out_dir: Path,
    fixture_label: str,
    run_results: list[dict],
    scores: dict[str, float],
    elapsed_total_sec: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Raw results JSON
    with open(out_dir / "results.json", "w") as f:
        json.dump(run_results, f, indent=2)

    # Markdown summary
    n = len(run_results)
    ok = sum(1 for r in run_results if r.get("ok"))
    band = _band(scores["overall"])

    lines = [
        f"# RCA Consistency Report",
        f"",
        f"**Fixture:** {fixture_label}  ",
        f"**Runs:** {ok}/{n} succeeded  ",
        f"**Overall consistency:** {scores['overall']:.3f} ({band})  ",
        f"**Elapsed:** {elapsed_total_sec:.1f}s  ",
        f"",
        f"## Metrics",
        f"",
        f"| Metric | Score |",
        f"|---|---|",
    ]
    for k, v in scores.items():
        if k != "overall":
            lines.append(f"| {k.replace('_', ' ').title()} | {v:.3f} |")
    lines += [
        f"",
        f"## Per-run results",
        f"",
        f"| Run | Status | cause_category | severity | confidence |",
        f"|---|---|---|---|---|",
    ]
    for r in run_results:
        rpt = r.get("report", {}) or {}
        rca = rpt.get("rca", {}) or {}
        summ = rpt.get("summary", {}) or {}
        lines.append(
            f"| {r['index'] + 1} | {r['status']} "
            f"| {rca.get('cause_category', '—')} "
            f"| {summ.get('severity', '—')} "
            f"| {rca.get('confidence_overall', '—')} |"
        )

    with open(out_dir / "summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    index: int
    run_id: Optional[str] = None
    status: str = "pending"
    report: Optional[dict] = None
    error: Optional[str] = None
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in _DONE and self.report is not None

    def to_dict(self) -> dict:
        return {
            "index":       self.index,
            "run_id":      self.run_id,
            "status":      self.status,
            "report":      self.report,
            "error":       self.error,
            "elapsed_sec": self.elapsed_sec,
            "ok":          self.ok,
        }


async def _run_once(
    index: int,
    client: httpx.AsyncClient,
    cfg: Config,
    fixture: dict[str, Any],
    log: logging.Logger,
) -> RunResult:
    res = RunResult(index=index)
    t0 = time.monotonic()
    # Append nonce to ado_run_id to bypass RCA cache, same technique as CR harness.
    nonce_ado_run_id = f"{fixture['ado_run_id']}#{uuid.uuid4().hex[:8]}"
    try:
        submitted = await _submit(client, cfg.agent_base_url, cfg.tenant_id, fixture, nonce_ado_run_id)

        if submitted.get("cache_hit"):
            res.status = "cache_hit"
            res.run_id = submitted.get("run_id") or submitted.get("id")
            res.report = {k: v for k, v in submitted.items() if k not in {"cache_hit", "run_id"}}
            log.warning("run %d: unexpected cache hit for nonce ado_run_id=%s", index, nonce_ado_run_id)
            return res

        run_id = submitted.get("run_id")
        if not run_id:
            raise RuntimeError(f"submit returned no run_id: {submitted!r}")
        res.run_id = run_id
        log.info("run %d submitted — run_id=%s ado_run_id=%s", index, run_id, nonce_ado_run_id)

        row = await _poll(client, cfg.agent_base_url, cfg.tenant_id, run_id, cfg.runner)
        res.status = row.get("status", "unknown")

        if res.status in _DONE:
            res.report = row.get("report")
            if res.report is None:
                raise RuntimeError(f"run {run_id} complete but report is null")
        else:
            res.error = row.get("error_message") or res.status

    except Exception as exc:
        res.status = "error"
        res.error = f"{type(exc).__name__}: {exc}"
        log.error("run %d failed: %s", index, res.error)
    finally:
        res.elapsed_sec = round(time.monotonic() - t0, 2)
    return res


async def _run_fixture(
    fixture_cfg: FixtureCfg,
    cfg: Config,
    out_root: Path,
    log: logging.Logger,
) -> tuple[dict[str, float], list[RunResult], Path]:
    fixture_path = _REPO_ROOT / fixture_cfg.file
    with open(fixture_path) as f:
        fixture = json.load(f)

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [
            _run_once(i, client, cfg, fixture, log)
            for i in range(cfg.runner.iterations)
        ]
        results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - t0
    results = list(results)
    ok_reports = [r.report for r in results if r.ok and r.report]

    if len(ok_reports) < 2:
        scores = {"overall": 0.0, "cause_category_agreement": 0.0,
                  "severity_agreement": 0.0, "primary_cause_similarity": 0.0,
                  "confidence_stability": 0.0}
        log.warning("Insufficient successful runs (%d/%d) — cannot score consistency",
                    len(ok_reports), cfg.runner.iterations)
    else:
        scores = _score(ok_reports, cfg.evaluator.weights)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fixture_slug = fixture_cfg.label.lower().replace(" ", "_").replace("—", "").replace(",", "")[:40]
    out_dir = out_root / f"{ts}_{fixture_slug}"
    _write_report(out_dir, fixture_cfg.label, [r.to_dict() for r in results], scores, elapsed)

    return scores, results, out_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rca-consistency-harness", description=__doc__)
    p.add_argument("--config", help="path to config.json (default: harnesses/rca/config.json)")
    p.add_argument("--iterations", type=int, help="override runner.iterations")
    p.add_argument("--agent-url", help="override agent base URL")
    p.add_argument("--tenant-id", help="override tenant_id")
    p.add_argument("--output-dir", help="override output directory")
    p.add_argument("--check-health", action="store_true", help="probe /health and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _check_health(cfg: Config) -> int:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(f"{cfg.agent_base_url}/health")
            print(f"OK  {cfg.agent_base_url}/health -> {res.status_code}")
            return 0
        except Exception as exc:
            print(f"FAIL {cfg.agent_base_url}/health -> {exc}")
            return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("rca-consistency")

    cfg = Config.load(args.config)
    if args.iterations is not None:
        cfg.runner.iterations = args.iterations
    if args.agent_url:
        cfg.agent_base_url = args.agent_url.rstrip("/")
    if args.tenant_id:
        cfg.tenant_id = args.tenant_id
    if args.output_dir:
        cfg.output_dir = args.output_dir

    if args.check_health:
        return asyncio.run(_check_health(cfg))

    if not cfg.fixtures:
        print("ERROR: no fixtures defined in config.json")
        return 1

    out_root = _REPO_ROOT / cfg.output_dir

    overall_ok = True
    for fx_cfg in cfg.fixtures:
        print(f"\nFixture: {fx_cfg.label}")
        print(f"  Running {cfg.runner.iterations} iterations against {cfg.agent_base_url} ...")
        try:
            scores, results, out_dir = asyncio.run(_run_fixture(fx_cfg, cfg, out_root, log))
        except Exception as exc:
            log.error("fixture '%s' failed: %s", fx_cfg.label, exc)
            overall_ok = False
            continue

        ok = sum(1 for r in results if r.ok)
        band = _band(scores["overall"])
        print(f"  Runs: {ok}/{len(results)} succeeded")
        print(f"  Overall consistency: {scores['overall']:.3f} ({band})")
        print(f"  cause_category_agreement : {scores['cause_category_agreement']:.3f}")
        print(f"  severity_agreement       : {scores['severity_agreement']:.3f}")
        print(f"  primary_cause_similarity : {scores['primary_cause_similarity']:.3f}")
        print(f"  confidence_stability     : {scores['confidence_stability']:.3f}")
        print(f"  Artifacts: {out_dir}")
        print(f"    -> {out_dir}/summary.md")

        if ok < 1:
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
