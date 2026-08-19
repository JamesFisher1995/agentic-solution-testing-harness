#!/usr/bin/env python3
"""
Completeness Harness
====================
Answers: "When there IS a real impact, does the AI find ALL of it?"

Three checks per run:
  1. Test recall    — did the AI flag all gold-standard Must Run / Should Run tests?
  2. False exclusions — were any critical tests marked Exclude?
  3. Gap recall     — did the AI identify all known coverage gaps, and generate a
                      test case for each one?

Usage:
  python3 harnesses/completeness/run.py
  python3 harnesses/completeness/run.py --config harnesses/completeness/config.json
  python3 harnesses/completeness/run.py --iterations 3
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so `from shared.X import Y` works when this
# script is run as  python3 harnesses/completeness/run.py  from any cwd.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.client import SolutionClient  # noqa: E402
from shared.dotenv import load_dotenv     # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("completeness")

# ---------------------------------------------------------------------------
# Colour helpers for Word report
# ---------------------------------------------------------------------------

GREEN  = "375623"
RED    = "C00000"
AMBER  = "C55A11"
BLUE   = "1F3864"
LGREY  = "F2F2F2"
DGREY  = "595959"


def rgb(hex6: str):
    from docx.shared import RGBColor  # type: ignore
    r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    return RGBColor(r, g, b)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str, iterations_override: int | None = None) -> dict:
    load_dotenv()
    with open(path) as fh:
        cfg = json.load(fh)
    if iterations_override is not None:
        cfg["runner"]["iterations"] = iterations_override
    sel = cfg.setdefault("selector", {})
    sel["integration_url"] = os.getenv("ADO_BASE_URL", sel.get("integration_url", ""))
    sel["integration_project"] = os.getenv("ADO_PROJECT_KEY", sel.get("integration_project", ""))
    sel["integration_token"] = os.getenv("ADO_TOKEN", sel.get("integration_token", ""))
    return cfg


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def _poll(client: SolutionClient, run_id: str, runner_cfg: dict) -> dict:
    deadline = time.monotonic() + runner_cfg.get("poll_timeout_sec", 1200.0)
    interval = runner_cfg.get("poll_interval_sec", 3.0)
    done = {"complete", "cache_hit"}
    while True:
        row = await client.poll(run_id)
        status = row.get("status", "unknown")
        if status in done:
            report = row.get("report")
            if report is None:
                raise RuntimeError(f"run {run_id} {status} but report is null")
            return report
        if status == "failed":
            raise RuntimeError(f"run {run_id} failed: {row.get('error_message')}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"run {run_id} still {status!r} after timeout")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Coverage gap recall check (mechanical)
# ---------------------------------------------------------------------------

def check_gap_recall(coverage_gaps: list[dict], must_find_gaps: list[str]) -> tuple[list[str], list[str]]:
    """Return (found, missing) — gaps whose component matches a must_find_gaps entry."""
    found, missing = [], []
    for expected in must_find_gaps:
        match = next(
            (g.get("component", "") for g in coverage_gaps
             if expected.lower() in (g.get("component") or "").lower()
             or expected.lower() in (g.get("note") or "").lower()),
            None,
        )
        if match:
            found.append(match)
        else:
            missing.append(expected)
    return found, missing


def check_gap_coverage(coverage_gaps: list[dict], generated_tcs: list[dict]) -> tuple[int, int]:
    """Return (gaps_with_tc, total_gaps) — how many gaps have a matching generated TC."""
    gaps_with_tc = 0
    for gap in coverage_gaps:
        component = (gap.get("component") or "").lower()
        covered = any(
            component in (tc.get("title") or "").lower()
            or component in " ".join(
                s.get("action", "") for s in tc.get("steps", [])
            ).lower()
            for tc in generated_tcs
        )
        if covered:
            gaps_with_tc += 1
    return gaps_with_tc, len(coverage_gaps)


# ---------------------------------------------------------------------------
# Recall + false-exclusion checks (mechanical)
# ---------------------------------------------------------------------------

def _matches(test_name: str, expected: str) -> bool:
    return expected.lower() in test_name.lower() or test_name.lower() in expected.lower()


def check_recall(
    scored_tests: list[dict], must_flag: list[str], schema: dict
) -> tuple[list[str], list[str]]:
    """Return (found, missing) against the must_flag gold standard."""
    action_field = schema.get("action_field", "recommended_action")
    name_field = schema.get("test_name_field", "test_name")

    flagged = [
        (t.get(name_field) or "").strip()
        for t in scored_tests
        if (t.get(action_field) or "").lower() in {"must run", "should run"}
    ]
    found, missing = [], []
    for expected in must_flag:
        match = next((f for f in flagged if _matches(f, expected)), None)
        if match:
            found.append(match)
        else:
            missing.append(expected)
    return found, missing


def check_false_exclusions(
    scored_tests: list[dict], must_not_exclude: list[str], schema: dict
) -> list[str]:
    """Return list of must_not_exclude tests that were actually marked Exclude."""
    action_field = schema.get("action_field", "recommended_action")
    name_field = schema.get("test_name_field", "test_name")

    excluded = [
        (t.get(name_field) or "").strip()
        for t in scored_tests
        if (t.get(action_field) or "").lower() == "exclude"
    ]
    violations = []
    for critical in must_not_exclude:
        match = next((e for e in excluded if _matches(e, critical)), None)
        if match:
            violations.append(match)
    return violations


# ---------------------------------------------------------------------------
# Run outcome
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RunOutcome:
    index: int
    run_id: str | None
    cr_id_used: str
    recall_score: float          # fraction of must_flag tests found
    found_tests: list[str]
    missing_tests: list[str]
    false_exclusions: list[str]
    gap_recall_score: float      # fraction of must_find_gaps identified
    gaps_found: list[str]
    gaps_missing: list[str]
    gaps_with_tc: int            # gaps that have a generated TC covering them
    total_gaps: int              # total gaps found by solution
    tc_count: int
    overall_risk: str | None
    elapsed_sec: float
    passed: bool
    error: str | None


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

async def run_once(
    client: SolutionClient,
    index: int,
    normalised_cr: dict,
    existing_tests: list,
    cr_cfg: dict,
    cfg: dict,
) -> RunOutcome:
    runner_cfg = cfg["runner"]
    schema = cfg.get("response_schema", {})

    summary_key = schema.get("summary_key", "summary")
    risk_field = schema.get("risk_field", "overall_risk")
    scored_key = schema.get("scored_tests_key", "scored_tests")
    coverage_gaps_key = schema.get("coverage_gaps_key", "coverage_gaps")
    gen_tcs_key = schema.get("generated_tcs_key", "generated_test_cases")

    cr_id = cr_cfg["cr_id"]
    gold = cr_cfg["gold_standard"]
    thresholds = cfg.get("thresholds", {})
    recall_min = thresholds.get("recall_min", 0.90)
    gap_recall_min = thresholds.get("gap_recall_min", 0.80)

    bypass = runner_cfg.get("bypass_cache", True)
    cr_id_used = f"{cr_id}#{uuid.uuid4().hex[:8]}" if bypass else cr_id
    started = time.monotonic()

    try:
        submit = await client.submit(
            cr_id=cr_id_used,
            normalised_cr=normalised_cr,
            existing_tests=existing_tests,
            tcg_enabled=runner_cfg.get("tcg_enabled", True),
        )

        if submit.get("cache_hit"):
            report = {k: v for k, v in submit.items() if k != "cache_hit"}
            run_id = submit.get("run_id")
        else:
            run_id = submit.get("run_id")
            if not run_id:
                raise RuntimeError(f"no run_id in submit response: {submit!r}")
            logger.info("run %d submitted — run_id=%s", index + 1, run_id)
            report = await _poll(client, run_id, runner_cfg)

        scored = report.get(scored_key) or []
        coverage_gaps = report.get(coverage_gaps_key) or []
        generated_tcs = report.get(gen_tcs_key) or []
        summary = report.get(summary_key) or {}
        overall_risk = summary.get(risk_field)

        # Test recall
        found, missing = check_recall(scored, gold["must_flag"], schema)
        false_excl = check_false_exclusions(scored, gold["must_not_exclude"], schema)
        recall_score = len(found) / len(gold["must_flag"]) if gold["must_flag"] else 1.0

        # Coverage gap recall
        must_find_gaps = gold.get("must_find_gaps", [])
        gaps_found, gaps_missing = check_gap_recall(coverage_gaps, must_find_gaps)
        gap_recall_score = len(gaps_found) / len(must_find_gaps) if must_find_gaps else 1.0
        gaps_with_tc, total_gaps = check_gap_coverage(coverage_gaps, generated_tcs)

        passed = (
            recall_score >= recall_min
            and len(false_excl) == 0
            and gap_recall_score >= gap_recall_min
        )

        elapsed = time.monotonic() - started

        status = "PASS" if passed else "FAIL"
        logger.info(
            "Run %d: %s  recall=%.2f (%d/%d)  gap_recall=%.2f (%d/%d)  gap_tc=%d/%d  risk=%s  (%.1fs)",
            index + 1, status,
            recall_score, len(found), len(gold["must_flag"]),
            gap_recall_score, len(gaps_found), len(must_find_gaps),
            gaps_with_tc, total_gaps, overall_risk, elapsed,
        )
        for m in missing:
            logger.warning("  ! Missing from Must/Should Run: %s", m)
        for v in false_excl:
            logger.warning("  ! Critical test Excluded: %s", v)
        for g in gaps_missing:
            logger.warning("  ! Coverage gap not identified: %s", g)

        return RunOutcome(
            index=index,
            run_id=run_id,
            cr_id_used=cr_id_used,
            recall_score=recall_score,
            found_tests=found,
            missing_tests=missing,
            false_exclusions=false_excl,
            gap_recall_score=gap_recall_score,
            gaps_found=gaps_found,
            gaps_missing=gaps_missing,
            gaps_with_tc=gaps_with_tc,
            total_gaps=total_gaps,
            tc_count=len(generated_tcs),
            overall_risk=overall_risk,
            elapsed_sec=elapsed,
            passed=passed,
            error=None,
        )

    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.error("Run %d ERROR: %s", index + 1, exc)
        must_find_gaps = gold.get("must_find_gaps", [])
        return RunOutcome(
            index=index, run_id=None, cr_id_used=cr_id_used,
            recall_score=0.0, found_tests=[], missing_tests=gold["must_flag"],
            false_exclusions=[],
            gap_recall_score=0.0, gaps_found=[], gaps_missing=must_find_gaps,
            gaps_with_tc=0, total_gaps=0, tc_count=0,
            overall_risk=None, elapsed_sec=elapsed, passed=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------

def _cr_score(outcomes: list[RunOutcome]) -> float:
    if not outcomes:
        return 0.0
    return sum(o.recall_score for o in outcomes) / len(outcomes)


def _band(score: float) -> str:
    if score >= 0.95:
        return "Excellent"
    if score >= 0.90:
        return "Good"
    if score >= 0.80:
        return "Needs improvement"
    return "Poor"


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def render_report(cr_results: list[dict]) -> str:
    lines = ["=" * 64, "  COMPLETENESS HARNESS REPORT", "=" * 64]
    total_runs = sum(len(r["outcomes"]) for r in cr_results)
    total_passed = sum(sum(1 for o in r["outcomes"] if o.passed) for r in cr_results)
    cr_scores = [_cr_score(r["outcomes"]) for r in cr_results]
    overall = sum(cr_scores) / len(cr_scores) if cr_scores else 0.0
    lines.append(f"  CRs tested:      {len(cr_results)}")
    lines.append(f"  Total runs:      {total_runs}")
    lines.append(f"  Overall passed:  {total_passed}")
    lines.append(f"  Overall score:   {overall:.3f}  [{_band(overall)}]")
    lines.append("")

    for r in cr_results:
        outcomes: list[RunOutcome] = r["outcomes"]
        cr_cfg = r["cr_cfg"]
        gold = cr_cfg["gold_standard"]
        score = _cr_score(outcomes)
        passed_count = sum(1 for o in outcomes if o.passed)

        lines.append(f"  {cr_cfg['label']} — {r['cr_title']}")
        lines.append(f"  CR: {cr_cfg['cr_id']}   Must-flag tests: {len(gold['must_flag'])}")
        lines.append(f"  Gold standard rationale: {gold['rationale'][:100]}...")
        lines.append("  " + "─" * 60)
        lines.append(f"  Completeness score: {score:.3f}  [{_band(score)}]   ({passed_count}/{len(outcomes)} passed)")
        lines.append("")

        header = f"  {'Run':<5} {'Status':<8} {'Recall':<8} {'GapRecall':<10} {'Gap TC':<7} {'Excl.Viol':<10} {'TCs':<4} {'Time'}"
        lines.append(header)
        lines.append("  " + "-" * 70)
        for o in outcomes:
            status = "PASS" if o.passed else "FAIL"
            lines.append(
                f"  {o.index+1:<5} {status:<8} {o.recall_score:.2f}    "
                f"{o.gap_recall_score:.2f}      "
                f"{o.gaps_with_tc}/{o.total_gaps}     "
                f"{len(o.false_exclusions):<10} {o.tc_count:<4} {o.elapsed_sec:.1f}s"
            )
        lines.append("")

    lines.append("=" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------

def render_json(cr_results: list[dict], cfg: dict) -> dict:
    cr_scores = [_cr_score(r["outcomes"]) for r in cr_results]
    overall = sum(cr_scores) / len(cr_scores) if cr_scores else 0.0
    return {
        "overall_completeness_score": round(overall, 4),
        "band": _band(overall),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "crs": [
            {
                "cr_id": r["cr_cfg"]["cr_id"],
                "cr_label": r["cr_cfg"]["label"],
                "cr_title": r["cr_title"],
                "completeness_score": round(_cr_score(r["outcomes"]), 4),
                "must_flag_count": len(r["cr_cfg"]["gold_standard"]["must_flag"]),
                "runs": [
                    {
                        "index": o.index,
                        "run_id": o.run_id,
                        "cr_id_used": o.cr_id_used,
                        "passed": o.passed,
                        "recall_score": round(o.recall_score, 4),
                        "found_tests": o.found_tests,
                        "missing_tests": o.missing_tests,
                        "false_exclusions": o.false_exclusions,
                        "gap_recall_score": round(o.gap_recall_score, 4),
                        "gaps_found": o.gaps_found,
                        "gaps_missing": o.gaps_missing,
                        "gaps_with_tc": o.gaps_with_tc,
                        "total_gaps": o.total_gaps,
                        "tc_count": o.tc_count,
                        "overall_risk": o.overall_risk,
                        "elapsed_sec": round(o.elapsed_sec, 1),
                        "error": o.error,
                    }
                    for o in r["outcomes"]
                ],
            }
            for r in cr_results
        ],
    }


# ---------------------------------------------------------------------------
# Word report
# ---------------------------------------------------------------------------

def _set_col_widths(table: Any, widths_cm: list[float]) -> None:
    from docx.oxml.ns import qn  # type: ignore
    from docx.shared import Cm  # type: ignore
    from docx.oxml import OxmlElement  # type: ignore
    for row in table.rows:
        for cell, w in zip(row.cells, widths_cm):
            tc = cell._tc
            tcPr = tc.find(qn("w:tcPr"))
            if tcPr is None:
                tcPr = OxmlElement("w:tcPr")
                tc.insert(0, tcPr)
            tcW = tcPr.find(qn("w:tcW"))
            if tcW is None:
                tcW = OxmlElement("w:tcW")
                tcPr.append(tcW)
            tcW.set(qn("w:w"), str(int(Cm(w).twips)))
            tcW.set(qn("w:type"), "dxa")


def _shade_cell(cell: Any, hex6: str) -> None:
    from docx.oxml.ns import qn  # type: ignore
    from docx.oxml import OxmlElement  # type: ignore
    tc = cell._tc
    tcPr = tc.find(qn("w:tcPr"))
    if tcPr is None:
        tcPr = OxmlElement("w:tcPr")
        tc.insert(0, tcPr)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex6)
    tcPr.append(shd)


def _add_table_borders(table: Any) -> None:
    from docx.oxml.ns import qn  # type: ignore
    from docx.oxml import OxmlElement  # type: ignore
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "CCCCCC")
        borders.append(el)
    tblPr.append(borders)


def _cell_text(cell: Any, text: str, bold: bool = False, colour: str | None = None, size_pt: int = 10) -> None:
    from docx.shared import Pt  # type: ignore
    para = cell.paragraphs[0]
    run = para.add_run(text)
    run.bold = bold
    run.font.size = Pt(size_pt)
    if colour:
        run.font.color.rgb = rgb(colour)


def render_docx(cr_results: list[dict], cfg: dict, out_path: str) -> None:
    try:
        from docx import Document  # type: ignore
        from docx.shared import Pt, Cm  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
    except ImportError:
        logger.warning("python-docx not installed — skipping Word report. Run: pip3 install python-docx")
        return

    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    cr_scores = [_cr_score(r["outcomes"]) for r in cr_results]
    overall = sum(cr_scores) / len(cr_scores) if cr_scores else 0.0
    total_runs = sum(len(r["outcomes"]) for r in cr_results)
    total_passed = sum(sum(1 for o in r["outcomes"] if o.passed) for r in cr_results)

    # Title
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title_p.add_run("AI SOLUTION TESTING HARNESS")
    run.bold = True
    run.font.size = Pt(18)
    run.font.color.rgb = rgb(BLUE)

    sub_p = doc.add_paragraph()
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub_p.add_run("Completeness Harness Report")
    run.font.size = Pt(13)
    run.font.color.rgb = rgb(DGREY)

    doc.add_paragraph()

    # Meta table
    meta = doc.add_table(rows=4, cols=2)
    _add_table_borders(meta)
    _set_col_widths(meta, [4.5, 11.5])
    for row, (k, v) in zip(meta.rows, [
        ("Generated", datetime.datetime.utcnow().strftime("%d %B %Y %H:%M UTC")),
        ("CRs tested", str(len(cr_results))),
        ("Total runs", f"{total_passed}/{total_runs} passed"),
        ("Overall score", f"{overall:.3f}  [{_band(overall)}]"),
    ]):
        _shade_cell(row.cells[0], LGREY)
        _cell_text(row.cells[0], k, bold=True)
        colour = GREEN if overall >= 0.90 else AMBER if overall >= 0.80 else RED
        _cell_text(row.cells[1], v, colour=colour if k == "Overall score" else None)

    doc.add_paragraph()

    # Per-CR sections
    for r in cr_results:
        outcomes: list[RunOutcome] = r["outcomes"]
        cr_cfg = r["cr_cfg"]
        gold = cr_cfg["gold_standard"]
        score = _cr_score(outcomes)
        passed_count = sum(1 for o in outcomes if o.passed)
        s_colour = GREEN if score >= 0.90 else AMBER if score >= 0.80 else RED

        # Section heading
        h = doc.add_paragraph()
        run = h.add_run(f"{cr_cfg['label']} — {r['cr_title']}")
        run.bold = True
        run.font.size = Pt(13)
        run.font.color.rgb = rgb(BLUE)

        # Score banner
        banner = doc.add_paragraph()
        brun = banner.add_run(
            f"Completeness score: {score:.3f}  [{_band(score)}]   "
            f"({passed_count}/{len(outcomes)} runs passed)"
        )
        brun.bold = True
        brun.font.color.rgb = rgb(s_colour)

        # Gold standard info
        doc.add_paragraph(f"Gold standard: {gold['rationale']}")
        gs_p = doc.add_paragraph()
        gs_p.add_run("Must-flag tests: ").bold = True
        gs_p.add_run(", ".join(gold["must_flag"]))

        doc.add_paragraph()

        # Gold standard gaps info
        must_find_gaps = gold.get("must_find_gaps", [])
        if must_find_gaps:
            gs_gaps = doc.add_paragraph()
            gs_gaps.add_run("Must-find gaps: ").bold = True
            gs_gaps.add_run(", ".join(must_find_gaps))

        doc.add_paragraph()

        # Run results table
        headers = ["Run", "Status", "Test Recall", "Gap Recall", "Gap->TC", "Excl. Violations", "TCs gen.", "Time"]
        widths = [1.0, 1.3, 2.0, 2.0, 1.5, 2.5, 1.5, 1.5]
        tbl = doc.add_table(rows=1 + len(outcomes), cols=len(headers))
        _add_table_borders(tbl)
        _set_col_widths(tbl, widths)

        for cell, hdr in zip(tbl.rows[0].cells, headers):
            _shade_cell(cell, BLUE)
            _cell_text(cell, hdr, bold=True, colour="FFFFFF")

        for o in outcomes:
            row_cells = tbl.rows[o.index + 1].cells
            status = "PASS" if o.passed else "FAIL"
            s_col = GREEN if o.passed else RED
            vals = [
                str(o.index + 1),
                status,
                f"{o.recall_score:.2f} ({len(o.found_tests)}/{len(gold['must_flag'])})",
                f"{o.gap_recall_score:.2f} ({len(o.gaps_found)}/{len(must_find_gaps)})",
                f"{o.gaps_with_tc}/{o.total_gaps}",
                str(len(o.false_exclusions)) if not o.false_exclusions else ", ".join(o.false_exclusions[:2]),
                str(o.tc_count),
                f"{o.elapsed_sec:.1f}s",
            ]
            colours = [
                None, s_col,
                GREEN if o.recall_score >= 0.90 else RED,
                GREEN if o.gap_recall_score >= 0.80 else RED,
                GREEN if o.gaps_with_tc == o.total_gaps else AMBER,
                RED if o.false_exclusions else GREEN,
                None, None,
            ]
            for cell, val, col in zip(row_cells, vals, colours):
                _cell_text(cell, val, colour=col)

        doc.add_paragraph()

        # Missing tests detail
        from collections import Counter
        all_missing_tests = [m for o in outcomes for m in o.missing_tests]
        all_missing_gaps = [g for o in outcomes for g in o.gaps_missing]
        if all_missing_tests:
            mc = Counter(all_missing_tests)
            note = doc.add_paragraph()
            note.add_run("Tests consistently missed: ").bold = True
            note.add_run(", ".join(f"{t} ({c}x)" for t, c in mc.most_common()))
        else:
            note = doc.add_paragraph()
            note.add_run("All gold-standard tests flagged on every run.").font.color.rgb = rgb(GREEN)
        if all_missing_gaps:
            mc = Counter(all_missing_gaps)
            note = doc.add_paragraph()
            note.add_run("Coverage gaps consistently missed: ").bold = True
            note.add_run(", ".join(f"{g} ({c}x)" for g, c in mc.most_common()))
        else:
            note = doc.add_paragraph()
            note.add_run("All gold-standard coverage gaps identified on every run.").font.color.rgb = rgb(GREEN)

        doc.add_paragraph()

    # Overall verdict
    verdict_p = doc.add_paragraph()
    verdict_colour = GREEN if overall >= 0.90 else AMBER if overall >= 0.80 else RED
    verdict_text = (
        "All CRs passed. The solution correctly flagged all critical tests and generated valid test cases."
        if total_passed == total_runs
        else "One or more CRs missed critical tests or flagged false exclusions. Review violations above."
    )
    verdict_p.add_run(verdict_text).font.color.rgb = rgb(verdict_colour)

    doc.save(out_path)
    logger.info("Word report written to %s", out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(cfg: dict) -> int:
    endpoints = cfg.get("endpoints", {})
    client = SolutionClient(
        cfg["agent_base_url"],
        cfg.get("integration_base_url", ""),
        cfg["tenant_id"],
        endpoints=endpoints,
    )

    async with client:
        try:
            await client.health()
            logger.info("Agent service healthy")
        except Exception as exc:
            logger.error("Agent service health check failed: %s", exc)
            logger.error("Is the stack running?")
            return 1

        sel = cfg.get("selector", {})
        runner_cfg = cfg["runner"]
        crs = cfg.get("crs", [])
        if not crs:
            logger.error("No CRs defined in config. Add a 'crs' array to config.json.")
            return 1

        # Fetch existing tests once
        logger.info("Fetching existing test cases (tags: %s)...", sel.get("test_case_tags"))
        existing_tests = await client.fetch_test_cases_by_tags(
            tags=sel.get("test_case_tags") or [],
            url=sel.get("integration_url", ""),
            project=sel.get("integration_project", ""),
            token=sel.get("integration_token", ""),
        )
        logger.info("Loaded %d existing tests", len(existing_tests))

        cr_results = []

        for cr_cfg in crs:
            cr_id = cr_cfg["cr_id"]
            label = cr_cfg.get("label", cr_id)
            outcomes: list[RunOutcome] = []

            logger.info("=" * 50)
            logger.info("CR: %s (%s)", label, cr_id)

            # Fetch CR from integration
            logger.info("Fetching CR %s ...", cr_id)
            cr_data = await client.fetch_cr(
                tool=sel.get("source", "ado"),
                cr_id=cr_id,
                url=sel.get("integration_url", ""),
                project=sel.get("integration_project", ""),
                token=sel.get("integration_token", ""),
            )
            normalised_cr = cr_data.get("normalised_cr") or {}
            cr_title = normalised_cr.get("title", "(no title)")
            logger.info("Title: %s", cr_title)
            logger.info("Gold standard must-flag: %s", cr_cfg["gold_standard"]["must_flag"])

            logger.info("Running %d iterations...", runner_cfg["iterations"])
            for i in range(runner_cfg["iterations"]):
                logger.info("--- Run %d/%d ---", i + 1, runner_cfg["iterations"])
                outcome = await run_once(client, i, normalised_cr, existing_tests, cr_cfg, cfg)
                outcomes.append(outcome)

            cr_results.append({"cr_cfg": cr_cfg, "cr_title": cr_title, "outcomes": outcomes})

        # Write outputs
        ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_dir = os.path.join(cfg["output_dir"], ts)
        os.makedirs(out_dir, exist_ok=True)

        json_path = os.path.join(out_dir, "completeness_report.json")
        with open(json_path, "w") as fh:
            json.dump(render_json(cr_results, cfg), fh, indent=2)

        txt = render_report(cr_results)
        txt_path = os.path.join(out_dir, "summary.txt")
        with open(txt_path, "w") as fh:
            fh.write(txt)

        docx_path = os.path.join(out_dir, "completeness_report.docx")
        render_docx(cr_results, cfg, docx_path)

        print("\n" + txt)
        print(f"\nArtifacts written to: {out_dir}")
        print(f"  {json_path}")
        print(f"  {txt_path}")
        print(f"  {docx_path}")

        cr_scores = [_cr_score(r["outcomes"]) for r in cr_results]
        overall = sum(cr_scores) / len(cr_scores) if cr_scores else 0.0
        return 0 if overall >= cfg.get("thresholds", {}).get("recall_min", 0.90) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Completeness harness for AI solution testing")
    parser.add_argument("--config", default="harnesses/completeness/config.json")
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args()
    cfg = load_config(args.config, iterations_override=args.iterations)
    raise SystemExit(asyncio.run(main_async(cfg)))


if __name__ == "__main__":
    main()
