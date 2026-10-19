"""RCA Harness Report Generator

Reads the latest run from each of the four RCA harness output directories
and produces a single combined Word document.

Usage
-----
    python3 harnesses/rca/report.py
    python3 harnesses/rca/report.py --out reports/RCA_Harness_Report.docx
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_RUNS_ROOT = _REPO_ROOT / "runs" / "rca"

# ── Colour palette ───────────────────────────────────────────────────────────
_NAVY   = RGBColor(0x1F, 0x39, 0x64)
_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
_GREEN  = RGBColor(0x00, 0x70, 0x50)
_AMBER  = RGBColor(0xBF, 0x7F, 0x00)
_RED    = RGBColor(0xC0, 0x00, 0x00)
_LIGHT  = RGBColor(0xD6, 0xE4, 0xF7)
_STRIPE = RGBColor(0xF2, 0xF7, 0xFF)
_GREY   = RGBColor(0x44, 0x44, 0x44)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _band(score: float) -> str:
    if score >= 1.0:  return "Excellent"
    if score >= 0.8:  return "Good"
    if score >= 0.6:  return "Fair"
    return "Poor"


def _band_colour(score: float) -> RGBColor:
    if score >= 0.8:  return _GREEN
    if score >= 0.6:  return _AMBER
    return _RED


def _load_json(p: Path) -> Any:
    with open(p) as f:
        return json.load(f)


def _set_cell_bg(cell, rgb: RGBColor) -> None:
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    hex_val = f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_val)
    tcPr.append(shd)


def _cell(cell, text: str, bold: bool = False, colour: Optional[RGBColor] = None,
          align: str = "left", size_pt: int = 9) -> None:
    p = cell.paragraphs[0]
    p.clear()
    run = p.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size_pt)
    if colour:
        run.font.color.rgb = colour
    p.alignment = {
        "left":   WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right":  WD_ALIGN_PARAGRAPH.RIGHT,
    }.get(align, WD_ALIGN_PARAGRAPH.LEFT)


def _heading(doc: Document, text: str, level: int) -> None:
    p = doc.add_heading(text, level=level)
    for run in p.runs:
        run.font.color.rgb = _NAVY


def _table_header(table, headers: list[str], widths_in: list[float]) -> None:
    hdr = table.rows[0]
    for cell, txt, w in zip(hdr.cells, headers, widths_in):
        cell.width = Inches(w)
        _set_cell_bg(cell, _LIGHT)
        _cell(cell, txt, bold=True, colour=_NAVY, align="center")


def _score_str(score: float) -> str:
    return f"{score:.3f} — {_band(score)}"


def _stripe_row(row, idx: int) -> None:
    if idx % 2 == 1:
        for c in row.cells:
            _set_cell_bg(c, _STRIPE)


# ── Consistency ──────────────────────────────────────────────────────────────

_CONF_MAP = {"low": 0.25, "medium": 0.5, "high": 0.75, "very high": 0.9}


def _conf_to_float(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return _CONF_MAP.get(str(v).lower().strip(), 0.5)


def _jaccard(a: str, b: str) -> float:
    ta = set(re.sub(r"[^\w\s]", "", a.lower()).split())
    tb = set(re.sub(r"[^\w\s]", "", b.lower()).split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def _compute_consistency_scores(results: list[dict]) -> dict[str, float]:
    cats      = [r["report"]["rca"].get("cause_category", "") for r in results]
    sevs      = [r["report"]["summary"].get("severity", "") for r in results]
    causes    = [r["report"]["rca"].get("primary_cause", "") for r in results]
    confs     = [_conf_to_float(r["report"]["rca"].get("confidence_overall", 0.5)) for r in results]

    n = len(results)
    mode_cat  = max(set(cats), key=cats.count)
    mode_sev  = max(set(sevs), key=sevs.count)

    cat_agree  = sum(1 for c in cats if c == mode_cat) / n
    sev_agree  = sum(1 for s in sevs if s == mode_sev) / n

    # pairwise Jaccard similarity
    pairs = [(causes[i], causes[j]) for i in range(n) for j in range(i + 1, n)]
    sim = sum(_jaccard(a, b) for a, b in pairs) / len(pairs) if pairs else 1.0

    # coefficient of variation → stability = 1 - CV (clamped to [0,1])
    mean_c = sum(confs) / n
    if mean_c == 0:
        stability = 1.0
    else:
        std_c = (sum((c - mean_c) ** 2 for c in confs) / n) ** 0.5
        stability = max(0.0, 1.0 - std_c / mean_c)

    weights = {"cause_category_agreement": 0.35, "severity_agreement": 0.25,
               "primary_cause_similarity": 0.25, "confidence_stability": 0.15}
    raw = {"cause_category_agreement": cat_agree, "severity_agreement": sev_agree,
           "primary_cause_similarity": sim,        "confidence_stability": stability}
    overall = sum(raw[k] * weights[k] for k in weights)
    return {**raw, "overall": overall}


def _write_consistency(doc: Document, run_dir: Path) -> dict[str, float]:
    results = _load_json(run_dir / "results.json")
    complete = [r for r in results if r.get("status") == "complete"]

    _heading(doc, "1. Consistency Harness", level=2)

    if not complete:
        doc.add_paragraph("No completed runs found.")
        return {"overall": 0.0}

    scores = _compute_consistency_scores(complete)
    n = len(results)
    ok = len(complete)
    fixture_label = run_dir.name.split("_", 1)[1].replace("_", " ").title() if "_" in run_dir.name else run_dir.name

    doc.add_paragraph(
        f"Fixture: {fixture_label}   |   Runs: {ok}/{n} completed   |   "
        f"Overall: {_score_str(scores['overall'])}"
    )

    # metrics table
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    _table_header(t, ["Metric", "Score"], [4.0, 2.0])
    metric_labels = [
        ("cause_category_agreement", "Cause Category Agreement"),
        ("severity_agreement",       "Severity Agreement"),
        ("primary_cause_similarity", "Primary Cause Similarity"),
        ("confidence_stability",     "Confidence Stability"),
        ("overall",                  "Overall"),
    ]
    for key, label in metric_labels:
        val = scores[key]
        row = t.add_row()
        bold = key == "overall"
        _cell(row.cells[0], label, bold=bold)
        _cell(row.cells[1], _score_str(val), bold=bold,
              colour=_band_colour(val), align="center")

    doc.add_paragraph()

    # per-run table
    t2 = doc.add_table(rows=1, cols=5)
    t2.style = "Table Grid"
    _table_header(t2, ["Run", "Status", "cause_category", "severity", "confidence"],
                  [0.5, 1.0, 1.6, 1.1, 1.0])
    for r in results:
        row = t2.add_row()
        _stripe_row(row, r["index"])
        rca = (r.get("report") or {}).get("rca") or {}
        summ = (r.get("report") or {}).get("summary") or {}
        _cell(row.cells[0], str(r["index"] + 1), align="center")
        _cell(row.cells[1], r.get("status", "—"))
        _cell(row.cells[2], rca.get("cause_category") or "—")
        _cell(row.cells[3], summ.get("severity") or "—")
        _cell(row.cells[4], str(rca.get("confidence_overall") or "—"))

    doc.add_paragraph()

    if scores["severity_agreement"] < 0.8:
        p = doc.add_paragraph(
            "Note: severity_agreement is the primary consistency gap. The LLM oscillates "
            "between severity levels for ambiguous failures."
        )
        p.runs[0].font.color.rgb = _AMBER
        p.runs[0].font.italic = True

    return scores


# ── Integrity ────────────────────────────────────────────────────────────────

def _write_integrity(doc: Document, run_dirs: list[Path]) -> float:
    _heading(doc, "2. Integrity Harness", level=2)
    doc.add_paragraph(
        "Verifies the agent refuses to hallucinate a report when there is no task "
        "or test log data (pipeline failed before any tasks ran)."
    )
    doc.add_paragraph()

    all_p, all_n = 0, 0
    for run_dir in run_dirs:
        results = _load_json(run_dir / "results.json")
        n = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        all_p += passed
        all_n += n
        score = passed / n if n else 0.0
        # derive scenario label from dir name
        slug = run_dir.name.split("_", 1)[1].replace("_", " ").title() if "_" in run_dir.name else run_dir.name

        doc.add_paragraph(
            f"Scenario: {slug}   |   Runs: {passed}/{n} passed   |   "
            f"Score: {_score_str(score)}"
        )

        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        _table_header(t, ["Run", "API status", "Error message (truncated)", "Pass"],
                      [0.5, 1.0, 4.2, 0.5])
        for r in results:
            row = t.add_row()
            _stripe_row(row, r["index"])
            tick = "✓" if r.get("passed") else "✗"
            colour = _GREEN if r.get("passed") else _RED
            _cell(row.cells[0], str(r["index"] + 1), align="center")
            _cell(row.cells[1], r.get("status", "—"))
            _cell(row.cells[2], (r.get("error_message") or "—")[:90])
            _cell(row.cells[3], tick, colour=colour, bold=True, align="center")

        violations = [r for r in results if not r.get("passed")]
        if violations:
            for v in violations:
                for msg in (v.get("violations") or []):
                    p = doc.add_paragraph(f"✗ Run {v['index']+1}: {msg}")
                    p.runs[0].font.color.rgb = _RED

        doc.add_paragraph()

    return all_p / all_n if all_n else 0.0


# ── Reliability ──────────────────────────────────────────────────────────────

def _write_reliability(doc: Document, run_dirs: list[Path]) -> float:
    _heading(doc, "3. Reliability Harness", level=2)
    doc.add_paragraph(
        "Asserts that the agent consistently identifies the correct cause_category "
        "and failure_type for known failure scenarios."
    )
    doc.add_paragraph()

    all_p, all_n = 0, 0
    for run_dir in run_dirs:
        results = _load_json(run_dir / "results.json")
        n = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        all_p += passed
        all_n += n
        score = passed / n if n else 0.0

        first_pass = next((r for r in results if r.get("passed")), results[0] if results else {})
        exp_cat  = first_pass.get("cause_category", "—")
        exp_type = first_pass.get("failure_type", "—")
        slug = run_dir.name.split("_", 1)[1].replace("_", " ").title() if "_" in run_dir.name else run_dir.name

        doc.add_paragraph(
            f"Scenario: {slug}   |   "
            f"Expected: cause_category={exp_cat!r}, failure_type={exp_type!r}   |   "
            f"Runs: {passed}/{n}   |   Score: {_score_str(score)}"
        )

        t = doc.add_table(rows=1, cols=5)
        t.style = "Table Grid"
        _table_header(t,
                      ["Run", "cause_category", "failure_type", "primary_cause (snippet)", "Pass"],
                      [0.5, 1.3, 1.2, 3.0, 0.5])
        for r in results:
            row = t.add_row()
            _stripe_row(row, r["index"])
            tick = "✓" if r.get("passed") else "✗"
            colour = _GREEN if r.get("passed") else _RED
            snippet = (r.get("primary_cause_snippet") or "—")[:80]
            _cell(row.cells[0], str(r["index"] + 1), align="center")
            _cell(row.cells[1], r.get("cause_category") or "—")
            _cell(row.cells[2], r.get("failure_type") or "—")
            _cell(row.cells[3], snippet)
            _cell(row.cells[4], tick, colour=colour, bold=True, align="center")

        failed_runs = [r for r in results if not r.get("passed")]
        if failed_runs:
            for r in failed_runs:
                for msg in (r.get("violations") or []):
                    p = doc.add_paragraph(f"✗ Run {r['index']+1}: {msg}")
                    p.runs[0].font.color.rgb = _RED

        doc.add_paragraph()

    overall = all_p / all_n if all_n else 0.0
    p = doc.add_paragraph(f"Overall reliability: {all_p}/{all_n}  →  {_score_str(overall)}")
    p.runs[0].bold = True
    p.runs[0].font.color.rgb = _band_colour(overall)
    return overall


# ── Completeness ─────────────────────────────────────────────────────────────

def _write_completeness(doc: Document, run_dirs: list[Path]) -> float:
    _heading(doc, "4. Completeness Harness", level=2)
    doc.add_paragraph(
        "Measures how much of the gold-standard information the agent recalls: "
        "keyword recall (0.40), file recall (0.35), recommendation recall (0.25)."
    )
    doc.add_paragraph()

    totals: list[float] = []
    for run_dir in run_dirs:
        results = _load_json(run_dir / "results.json")
        n = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        slug = run_dir.name.split("_", 1)[1].replace("_", " ").title() if "_" in run_dir.name else run_dir.name

        # scores nested inside r["scores"]
        dims = ["keyword_recall", "file_recall", "recommendation_recall", "overall"]
        avgs: dict[str, float] = {}
        for d in dims:
            vals = [r["scores"][d] for r in results if (r.get("scores") or {}).get(d) is not None]
            avgs[d] = sum(vals) / len(vals) if vals else 0.0

        totals.append(avgs["overall"])

        doc.add_paragraph(
            f"Fixture: {slug}   |   Runs: {passed}/{n} passed   |   "
            f"Overall: {_score_str(avgs['overall'])}"
        )

        # averages table
        t = doc.add_table(rows=1, cols=2)
        t.style = "Table Grid"
        _table_header(t, ["Dimension", "Avg score"], [3.5, 2.0])
        dim_labels = [
            ("keyword_recall",        "Keyword Recall (weight 0.40)"),
            ("file_recall",           "File Recall (weight 0.35)"),
            ("recommendation_recall", "Recommendation Recall (weight 0.25)"),
            ("overall",               "Overall"),
        ]
        for key, label in dim_labels:
            val = avgs[key]
            row = t.add_row()
            bold = key == "overall"
            _cell(row.cells[0], label, bold=bold)
            _cell(row.cells[1], _score_str(val), bold=bold,
                  colour=_band_colour(val), align="center")

        doc.add_paragraph()

        # per-run table
        t2 = doc.add_table(rows=1, cols=7)
        t2.style = "Table Grid"
        _table_header(t2,
                      ["Run", "Status", "keyword", "file", "rec", "overall", "Pass"],
                      [0.5, 1.0, 0.8, 0.7, 0.7, 0.9, 0.5])
        for r in results:
            row = t2.add_row()
            _stripe_row(row, r["index"])
            sc = r.get("scores") or {}
            ov = sc.get("overall", 0.0)
            tick = "✓" if r.get("passed") else "✗"
            colour = _GREEN if r.get("passed") else _RED
            _cell(row.cells[0], str(r["index"] + 1), align="center")
            _cell(row.cells[1], r.get("status", "—"))
            _cell(row.cells[2], f"{sc.get('keyword_recall', 0):.2f}", align="center")
            _cell(row.cells[3], f"{sc.get('file_recall', 0):.2f}", align="center")
            _cell(row.cells[4], f"{sc.get('recommendation_recall', 0):.2f}", align="center")
            _cell(row.cells[5], f"{ov:.2f}", colour=_band_colour(ov), align="center")
            _cell(row.cells[6], tick, colour=colour, bold=True, align="center")

        doc.add_paragraph()

        # recall gaps
        kw_misses: dict[str, int] = {}
        rec_misses: dict[str, int] = {}
        for r in results:
            sc = r.get("scores") or {}
            for kw in sc.get("keywords_missed", []):
                kw_misses[kw] = kw_misses.get(kw, 0) + 1
            for rec in sc.get("recs_missed", []):
                rec_misses[rec] = rec_misses.get(rec, 0) + 1

        if kw_misses or rec_misses:
            _heading(doc, "Recall Gaps", level=4)
            if kw_misses:
                p = doc.add_paragraph("Keywords missed:")
                p.runs[0].bold = True
                for kw, cnt in sorted(kw_misses.items(), key=lambda x: -x[1]):
                    q = doc.add_paragraph(f"  • {kw!r} — missed {cnt}/{n} runs")
                    q.runs[0].font.color.rgb = _AMBER
            if rec_misses:
                p = doc.add_paragraph("Recommendations missed:")
                p.runs[0].bold = True
                for rec, cnt in sorted(rec_misses.items(), key=lambda x: -x[1]):
                    q = doc.add_paragraph(f"  • {rec!r} — missed {cnt}/{n} runs")
                    q.runs[0].font.color.rgb = _AMBER

        doc.add_paragraph()

    return sum(totals) / len(totals) if totals else 0.0


# ── Executive summary ────────────────────────────────────────────────────────

def _write_exec_summary(doc: Document, scores: dict[str, float]) -> None:
    _heading(doc, "Executive Summary", level=2)

    t = doc.add_table(rows=1, cols=3)
    t.style = "Table Grid"
    _table_header(t, ["Harness", "Score", "Rating"], [2.5, 1.5, 1.5])

    for label, score in scores.items():
        row = t.add_row()
        _cell(row.cells[0], label, bold=True)
        _cell(row.cells[1], f"{score:.3f}", colour=_band_colour(score), align="center")
        _cell(row.cells[2], _band(score), colour=_band_colour(score), bold=True, align="center")

    doc.add_paragraph()

    if scores:
        combined = sum(scores.values()) / len(scores)
        p = doc.add_paragraph(f"Combined average: {combined:.3f} — {_band(combined)}")
        p.runs[0].bold = True
        p.runs[0].font.size = Pt(11)
        p.runs[0].font.color.rgb = _band_colour(combined)

    doc.add_paragraph()


# ── Main ─────────────────────────────────────────────────────────────────────

def _latest_run(subdir: str) -> Optional[Path]:
    d = _RUNS_ROOT / subdir
    if not d.exists():
        return None
    candidates = [p for p in d.iterdir() if p.is_dir()]
    return sorted(candidates)[-1] if candidates else None


def _slug_from_dir(p: Path) -> str:
    """Strip leading timestamp (e.g. 20261019T184010Z_) from a dir name."""
    name = p.name
    if len(name) >= 16 and name[8] == "T" and "_" in name[16:]:
        return name.split("_", 1)[1]
    return name


def _latest_runs_by_label(subdir: str, label_key: str) -> dict[str, Path]:
    d = _RUNS_ROOT / subdir
    if not d.exists():
        return {}
    latest: dict[str, Path] = {}
    for p in sorted(d.iterdir()):
        if not p.is_dir():
            continue
        rf = p / "results.json"
        if not rf.exists():
            continue
        try:
            rs = _load_json(rf)
            label = (rs[0].get(label_key) if rs else None) or _slug_from_dir(p)
        except Exception:
            label = _slug_from_dir(p)
        latest[label] = p  # later dirs overwrite earlier → keep latest per label
    return latest


def build_report(out_path: Path) -> None:
    doc = Document()

    # title
    title = doc.add_heading("RCA Agent — Test Harness Report", level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.color.rgb = _NAVY

    ts = datetime.now(timezone.utc).strftime("%d %B %Y")
    sub = doc.add_paragraph(
        f"Generated: {ts}   |   "
        "Harnesses: Consistency · Integrity · Reliability · Completeness"
    )
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in sub.runs:
        run.font.color.rgb = _GREY
    doc.add_paragraph()

    # collect latest run dirs
    consistency_dir  = _latest_run("consistency")
    integrity_dirs   = list(_latest_runs_by_label("integrity",   "scenario_label").values())
    reliability_dirs = list(_latest_runs_by_label("reliability", "scenario_label").values())
    completeness_dirs = list(_latest_runs_by_label("completeness", "fixture_label").values())

    # compute executive summary scores
    exec_scores: dict[str, float] = {}

    if consistency_dir and (consistency_dir / "results.json").exists():
        rs = _load_json(consistency_dir / "results.json")
        complete = [r for r in rs if r.get("status") == "complete"]
        if complete:
            sc = _compute_consistency_scores(complete)
            exec_scores["Consistency"] = sc["overall"]

    if integrity_dirs:
        totals = []
        for d in integrity_dirs:
            rs = _load_json(d / "results.json")
            n = len(rs); p = sum(1 for r in rs if r.get("passed"))
            totals.append(p / n if n else 0.0)
        exec_scores["Integrity"] = sum(totals) / len(totals)

    if reliability_dirs:
        totals = []
        for d in reliability_dirs:
            rs = _load_json(d / "results.json")
            n = len(rs); p = sum(1 for r in rs if r.get("passed"))
            totals.append(p / n if n else 0.0)
        exec_scores["Reliability"] = sum(totals) / len(totals)

    if completeness_dirs:
        totals = []
        for d in completeness_dirs:
            rs = _load_json(d / "results.json")
            vals = [r["scores"]["overall"] for r in rs if (r.get("scores") or {}).get("overall") is not None]
            totals.append(sum(vals) / len(vals) if vals else 0.0)
        exec_scores["Completeness"] = sum(totals) / len(totals)

    _write_exec_summary(doc, exec_scores)
    doc.add_page_break()

    _heading(doc, "Detailed Results", level=1)

    consistency_score: Optional[dict] = None
    if consistency_dir and (consistency_dir / "results.json").exists():
        consistency_score = _write_consistency(doc, consistency_dir)
        doc.add_page_break()

    if integrity_dirs:
        _write_integrity(doc, integrity_dirs)
        doc.add_page_break()

    if reliability_dirs:
        _write_reliability(doc, reliability_dirs)
        doc.add_page_break()

    if completeness_dirs:
        _write_completeness(doc, completeness_dirs)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    print(f"Report written to: {out_path}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rca-report", description=__doc__)
    p.add_argument("--out", default="reports/RCA_Harness_Report.docx",
                   help="output path (default: reports/RCA_Harness_Report.docx)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path
    build_report(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
