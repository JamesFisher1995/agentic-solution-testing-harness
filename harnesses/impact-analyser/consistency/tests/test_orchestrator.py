"""End-to-end wiring test — Selector → Runner → Evaluator → artifacts on disk.

Runs the whole harness against a faked analyser, with no app/DB/LLM, and asserts
the artifacts are written and internally consistent.
"""

import asyncio
import json
import os

import httpx

from harness.config import Config
from harness.orchestrator import Orchestrator
from tests.fixtures import FakeAnalyser, make_report, make_scored_test


def _write_cr(tmp_path) -> str:
    doc = {
        "cr_id": "CR-E2E",
        "normalised_cr": {"title": "T", "description": "D",
                          "acceptance_criteria": "AC", "labels": []},
        "existing_tests": [{"test_id": "TC-1"}, {"test_id": "TC-2"}],
    }
    f = tmp_path / "cr.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    return str(f)


def _config(tmp_path, iterations=3) -> Config:
    cfg = Config()
    cfg.selector.mode = "inline"
    cfg.selector.cr_file = _write_cr(tmp_path)
    cfg.runner.iterations = iterations
    cfg.runner.poll_interval_sec = 0.001
    cfg.runner.poll_timeout_sec = 2.0
    cfg.output_dir = str(tmp_path / "runs")
    return cfg


def test_end_to_end_writes_artifacts(tmp_path):
    fake = FakeAnalyser()   # identical report every run
    cfg = _config(tmp_path, iterations=3)

    async def go():
        client = httpx.AsyncClient(transport=fake.transport())
        try:
            return await Orchestrator(cfg, http_client=client).run()
        finally:
            await client.aclose()

    report, out_dir = asyncio.run(go())

    assert report.sufficient
    assert report.overall_score == 1.0
    # artifacts
    assert os.path.exists(os.path.join(out_dir, "selected_cr.json"))
    assert os.path.exists(os.path.join(out_dir, "consistency_report.json"))
    assert os.path.exists(os.path.join(out_dir, "summary.md"))
    assert os.path.exists(os.path.join(out_dir, "raw", "run_01.json"))
    assert os.path.exists(os.path.join(out_dir, "raw", "run_03.json"))

    with open(os.path.join(out_dir, "consistency_report.json"), encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["overall_score"] == 1.0
    assert saved["meta"]["iterations"] == 3
    assert saved["meta"]["bypass_cache"] is True


def test_end_to_end_detects_variance(tmp_path):
    # Make TC-2's score swing wildly per run and flip the recommended action.
    variants = iter([
        [make_scored_test("TC-1", 0.80, "Critical"), make_scored_test("TC-2", 0.10, "Low")],
        [make_scored_test("TC-1", 0.80, "Critical"), make_scored_test("TC-2", 0.90, "Critical")],
        [make_scored_test("TC-1", 0.80, "Critical"), make_scored_test("TC-2", 0.50, "Medium")],
    ])

    def report_for(cr_id):
        return make_report(cr_id, scored=next(variants))

    fake = FakeAnalyser(report_for=report_for)
    cfg = _config(tmp_path, iterations=3)

    async def go():
        client = httpx.AsyncClient(transport=fake.transport())
        try:
            return await Orchestrator(cfg, http_client=client).run()
        finally:
            await client.aclose()

    report, _ = asyncio.run(go())
    assert report.sufficient
    # TC-1 stable, TC-2 churns ⇒ overall below perfect, score_stability dented
    assert report.overall_score < 1.0
    assert report.dimensions["score_stability"] < 1.0
    assert report.details["per_test"]["TC-2"]["score_stdev"] > 0.2
