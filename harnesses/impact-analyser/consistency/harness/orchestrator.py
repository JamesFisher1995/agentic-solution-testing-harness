"""Orchestrator — wires Selector → Runner → Evaluator and writes artifacts.

Deliberately a plain sequential coordinator (no agent framework), echoing the
application's own 'a sequential state machine is easier to debug than a graph'
choice. Each stage produces a typed artifact consumed by the next.

Single-CR artifacts written to <output_dir>/<timestamp>/:
    selected_cr.json        the frozen input fed to every run
    raw/run_01.json …       each run's full report (or error)
    consistency_report.json the machine-readable metrics
    summary.md              a human-readable digest

Batch artifacts written to <output_dir>/<timestamp>/:
    cr_<id>/                per-CR sub-directory (same structure as above)
    batch_summary.md        combined table across all CRs
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .agents.base import SelectedCR
from .agents.evaluator import ConsistencyReport, EvaluatorAgent
from .agents.runner import RunnerAgent, RunResult
from .agents.selector import SelectorAgent
from .client import AnalyserClient
from .config import Config, SelectorConfig
from .report import render_summary_md, render_docx

logger = logging.getLogger("harness.orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config, *, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = cfg
        self._http_client = http_client

    async def run(self) -> tuple[ConsistencyReport, str]:
        """Single-CR run (original interface). Returns (report, out_dir)."""
        cfg = self.cfg
        cfg.validate()
        out_dir = os.path.join(
            cfg.output_dir, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        os.makedirs(os.path.join(out_dir, "raw"), exist_ok=True)
        async with self._make_client() as client:
            report, selected, results = await self._run_one(
                client, cfg.selector, out_dir
            )
        return report, out_dir

    async def run_batch(self) -> tuple[list[tuple[ConsistencyReport, str]], str]:
        """Batch run — one CR at a time, shared runner/evaluator config.

        Returns ([(report, cr_out_dir), ...], batch_out_dir).
        """
        cfg = self.cfg
        selectors = cfg.batch or [cfg.selector]

        batch_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        batch_dir = os.path.join(cfg.output_dir, batch_timestamp)
        os.makedirs(batch_dir, exist_ok=True)

        outcomes: list[tuple[ConsistencyReport, str]] = []
        total = len(selectors)

        async with self._make_client() as client:
            for i, sel in enumerate(selectors, 1):
                cr_label = sel.cr_id or f"cr{i}"
                cr_dir = os.path.join(batch_dir, f"cr_{cr_label}")
                os.makedirs(os.path.join(cr_dir, "raw"), exist_ok=True)
                logger.info("Batch [%d/%d] — CR %s", i, total, cr_label)
                try:
                    report, selected, results = await self._run_one(client, sel, cr_dir, run_label=batch_timestamp)
                    outcomes.append((report, cr_dir))
                except Exception as exc:  # noqa: BLE001
                    logger.error("Batch [%d/%d] CR %s failed: %s", i, total, cr_label, exc)

        _write_batch_summary(batch_dir, outcomes, cfg)
        logger.info("Batch complete — %d/%d CRs succeeded. Artifacts: %s",
                    len(outcomes), total, batch_dir)
        return outcomes, batch_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_client(self) -> AnalyserClient:
        cfg = self.cfg
        return AnalyserClient(
            cfg.agent_base_url, cfg.integration_base_url, cfg.tenant_id,
            client=self._http_client,
        )

    async def _run_one(
        self,
        client: AnalyserClient,
        sel_cfg: SelectorConfig,
        out_dir: str,
        run_label: Optional[str] = None,
    ) -> tuple[ConsistencyReport, SelectedCR, list[RunResult]]:
        cfg = self.cfg
        selector = SelectorAgent(client)
        runner = RunnerAgent(client)
        evaluator = EvaluatorAgent(client)

        logger.info("[1/3] Selecting CR (mode=%s)...", sel_cfg.mode)
        selected = await selector.run(sel_cfg)
        _write_json(os.path.join(out_dir, "selected_cr.json"), dataclasses.asdict(selected))

        logger.info(
            "[2/3] Running impact analysis %dx on CR %s...",
            cfg.runner.iterations, selected.cr_id,
        )
        results = await runner.run(selected, cfg.runner)
        for r in results:
            _write_json(
                os.path.join(out_dir, "raw", f"run_{r.index + 1:02d}.json"),
                dataclasses.asdict(r),
            )

        logger.info("[3/3] Evaluating consistency across %d runs...", len(results))
        report = evaluator.run(results, cfg.evaluator)

        report_dict = report.to_dict()
        report_dict["meta"] = {
            "cr_id": selected.cr_id,
            "source": selected.source,
            "existing_test_count": len(selected.existing_tests),
            "iterations": cfg.runner.iterations,
            "bypass_cache": cfg.runner.bypass_cache,
            "tcg_enabled": cfg.runner.tcg_enabled,
            "agent_base_url": cfg.agent_base_url,
            "tenant_id": cfg.tenant_id,
            "run_statuses": [r.status for r in results],
            "elapsed_sec": [r.elapsed_sec for r in results],
        }
        _write_json(os.path.join(out_dir, "consistency_report.json"), report_dict)

        run_id = run_label or os.path.basename(out_dir)
        with open(os.path.join(out_dir, f"summary_{run_id}.md"), "w", encoding="utf-8") as fh:
            fh.write(render_summary_md(report, selected, results, cfg))

        docx_path = os.path.join(out_dir, "consistency_report.docx")
        render_docx(report, selected, results, cfg, docx_path)

        return report, selected, results


# ---------------------------------------------------------------------------
# Batch combined summary
# ---------------------------------------------------------------------------

def _write_batch_summary(
    batch_dir: str,
    outcomes: list[tuple[ConsistencyReport, str]],
    cfg: Config,
) -> None:
    selectors = cfg.batch or [cfg.selector]
    lines: list[str] = []
    lines.append("# Batch consistency summary\n")
    lines.append(
        f"**{len(outcomes)} of {len(selectors)} CRs completed** · "
        f"{cfg.runner.iterations} iterations each · "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
    )

    # Overall score table
    lines.append("## Results\n")
    lines.append("| CR | Title | Score | Band | Tests scored | Runs ok/total |")
    lines.append("|---|---|---|---|---|---|")
    for (report, cr_dir), sel in zip(outcomes, selectors):
        selected_path = os.path.join(cr_dir, "selected_cr.json")
        cr_title = ""
        try:
            with open(selected_path, encoding="utf-8") as fh:
                data = json.load(fh)
                cr_title = (data.get("normalised_cr") or {}).get("title", "")[:60]
        except Exception:  # noqa: BLE001
            pass
        cr_id = sel.cr_id or "?"
        score_s = f"{report.overall_score:.3f}" if report.sufficient else "—"
        band = report.band if report.sufficient else "insufficient"
        test_count = len(json.load(open(selected_path, encoding="utf-8")).get("existing_tests", [])) if os.path.exists(selected_path) else "?"
        lines.append(
            f"| {cr_id} | {cr_title} | {score_s} | {band} | "
            f"{test_count} | {report.runs_evaluated}/{report.runs_total} |"
        )
    lines.append("")

    # Per-dimension breakdown table
    lines.append("## Per-dimension breakdown\n")
    dim_keys = [
        "test_set_stability", "action_agreement", "score_stability",
        "ranking_stability", "risk_agreement", "summary_stability", "coverage_gap_stability",
    ]
    header = "| CR | " + " | ".join(k.replace("_", " ") for k in dim_keys) + " |"
    sep = "|---|" + "---|" * len(dim_keys)
    lines.append(header)
    lines.append(sep)
    for (report, _), sel in zip(outcomes, selectors):
        cr_id = sel.cr_id or "?"
        cells = []
        for k in dim_keys:
            v = report.dimensions.get(k)
            cells.append("—" if v is None else f"{v:.3f}")
        lines.append(f"| {cr_id} | " + " | ".join(cells) + " |")
    lines.append("")

    summary_path = os.path.join(batch_dir, "batch_summary.md")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info("Batch summary written to %s", summary_path)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
