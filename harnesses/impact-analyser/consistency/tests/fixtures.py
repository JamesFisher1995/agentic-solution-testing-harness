"""Builders for synthetic analyser reports + a stateful fake HTTP transport.

These let the whole agent stack run under pytest with no application, DB, or LLM
— the same 'fakes, no live DB/LLM' approach the application's own smoke tests use.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx

_ACTIONS = {
    "Critical": "Must run",
    "High": "Must run",
    "Medium": "Should run",
    "Low": "Consider",
}


def make_scored_test(test_id: str, score: float, risk: str = "High",
                     action: Optional[str] = None) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "test_name": f"name-{test_id}",
        "composite_score": score,
        "risk_level": risk,
        "recommended_action": action or _ACTIONS.get(risk, "Consider"),
    }


def make_report(
    cr_id: str = "CR-1",
    scored: Optional[list[dict[str, Any]]] = None,
    *,
    overall_risk: str = "High",
    blast_radius: str = "Module-wide",
    change_types: Optional[list[str]] = None,
    gaps: Optional[list[str]] = None,
    generated: int = 0,
) -> dict[str, Any]:
    scored = scored if scored is not None else [
        make_scored_test("TC-1", 0.82, "Critical"),
        make_scored_test("TC-2", 0.61, "High"),
        make_scored_test("TC-3", 0.35, "Low"),
    ]
    counts = {"must_run": 0, "should_run": 0, "consider": 0, "exclude": 0}
    for t in scored:
        a = t["recommended_action"]
        key = {"Must run": "must_run", "Should run": "should_run",
               "Consider": "consider", "Exclude": "exclude"}.get(a)
        if key:
            counts[key] += 1
    return {
        "cr_id": cr_id,
        "parsed_cr": {"cr_id": cr_id, "title": "t"},
        "classification": {
            "change_types": change_types or ["functional"],
            "blast_radius": blast_radius,
            "risk_modifier": 0.75,
            "components_affected": ["claim-form", "validation"],
        },
        "dependency_map": {"direct": [], "transitive": []},
        "scored_tests": scored,
        "coverage_gaps": [{"component": c, "note": "", "test_type": "functional",
                           "suggested_new_test": ""} for c in (gaps or [])],
        "generated_test_cases": [{"test_case_id": f"TC-{cr_id}-{i}"} for i in range(generated)],
        "impacted_user_stories": [],
        "summary": {
            "total_existing_tests": len(scored),
            "scored_tests": len(scored),
            "coverage_gaps": len(gaps or []),
            "new_test_cases": generated,
            "overall_risk": overall_risk,
            **counts,
        },
    }


class FakeAnalyser:
    """A stateful httpx.MockTransport handler emulating agent-bus-api.

    report_for(cr_id) decides what report each submitted CR resolves to, letting a
    test script per-run variance. By default every run resolves to the same report
    (perfect consistency).
    """

    def __init__(
        self,
        report_for: Optional[Callable[[str], dict[str, Any]]] = None,
        *,
        running_polls: int = 0,        # GETs that return 'running' before completing
        cache_hit_for: Optional[Callable[[str], bool]] = None,
        integration: Optional[dict[str, Any]] = None,
    ) -> None:
        self._report_for = report_for or (lambda cr_id: make_report(cr_id))
        self._running_polls = running_polls
        self._cache_hit_for = cache_hit_for or (lambda cr_id: False)
        self._integration = integration or {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._poll_counts: dict[str, int] = {}
        self._seq = 0
        self.submitted_cr_ids: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/health":
            return _json(200, {"status": "ok"})

        if method == "POST" and path == "/analyse/cr":
            return self._submit(request)

        if method == "GET" and path.startswith("/analyse/cr/"):
            run_id = path.rsplit("/", 1)[-1]
            return self._get_run(run_id)

        if method == "DELETE" and path.startswith("/analyse/cr/"):
            run_id = path.rsplit("/", 1)[-1]
            self._runs.pop(run_id, None)
            return httpx.Response(204)

        if method == "GET" and path.startswith("/cr/"):
            return _json(200, self._integration.get("cr", {}))

        if method == "GET" and path == "/test-cases/ado":
            return _json(200, {"items": self._integration.get("test_cases", []),
                               "count": len(self._integration.get("test_cases", []))})

        return _json(404, {"detail": f"unhandled {method} {path}"})

    def _submit(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cr_id = body["cr_id"]
        self.submitted_cr_ids.append(cr_id)
        report = self._report_for(cr_id)

        if self._cache_hit_for(cr_id):
            return _json(202, {**report, "cache_hit": True, "run_id": f"cached-{cr_id}"})

        self._seq += 1
        run_id = f"run-{self._seq}"
        self._runs[run_id] = report
        self._poll_counts[run_id] = 0
        return _json(202, {"run_id": run_id, "cr_id": cr_id, "status": "running"})

    def _get_run(self, run_id: str) -> httpx.Response:
        if run_id not in self._runs:
            return _json(404, {"detail": "run not found"})
        self._poll_counts[run_id] += 1
        if self._poll_counts[run_id] <= self._running_polls:
            return _json(200, {"id": run_id, "status": "running", "report": None})
        return _json(200, {"id": run_id, "status": "complete",
                           "report": self._runs[run_id]})


def _json(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)
