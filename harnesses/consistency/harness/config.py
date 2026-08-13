"""Consistency harness configuration.

Loaded from a JSON file and/or environment variables.

response_schema maps generic field names to your solution's actual JSON field names.
Override any field that differs from the defaults (which match the agentic-ai-stlc
impact analyser).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any, Optional


@dataclass
class ResponseSchema:
    """Maps generic field names to the solution's actual response field names."""
    tests_field: str = "test_cases"
    test_id_field: str = "test_id"
    action_field: str = "recommended_action"
    score_field: str = "composite_score"
    risk_field: str = "overall_risk"
    gaps_field: str = "coverage_gaps"
    gap_component_field: str = "component"
    summary_field: str = "summary"
    count_fields: dict[str, str] = field(default_factory=lambda: {
        "must_run": "must_run",
        "should_run": "should_run",
        "consider": "consider",
        "exclude": "exclude",
        "coverage_gaps": "coverage_gaps",
        "new_test_cases": "new_test_cases",
    })


@dataclass
class SelectorConfig:
    mode: str = "inline"
    cr_file: Optional[str] = None
    cr_id: Optional[str] = None
    source: str = "ado"
    test_case_tags: list[str] = field(default_factory=list)
    integration_url: Optional[str] = None
    integration_project: Optional[str] = None
    integration_token: Optional[str] = None


@dataclass
class RunnerConfig:
    iterations: int = 5
    tcg_enabled: bool = True
    bypass_cache: bool = True
    max_concurrency: int = 1
    poll_interval_sec: float = 3.0
    poll_timeout_sec: float = 600.0
    submit_timeout_sec: float = 30.0
    delete_runs_after: bool = False


@dataclass
class EvaluatorConfig:
    weights: dict[str, float] = field(default_factory=lambda: {
        "test_set_stability":              0.30,
        "action_agreement":                0.25,
        "score_stability":                 0.15,
        "ranking_stability":               0.10,
        "risk_agreement":                  0.10,
        "summary_stability":               0.05,
        "coverage_gap_stability":          0.03,
        "generated_test_case_consistency": 0.02,
    })
    score_stdev_full_penalty: float = 0.25


@dataclass
class Config:
    agent_base_url: str = "http://localhost:8000"
    integration_base_url: str = "http://localhost:3001"
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    output_dir: str = "runs/consistency"
    endpoints: dict[str, str] = field(default_factory=dict)

    selector: SelectorConfig = field(default_factory=SelectorConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)
    response_schema: ResponseSchema = field(default_factory=ResponseSchema)
    batch: list[SelectorConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        raw: dict[str, Any] = {}
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        cfg = cls.from_dict(raw)
        cfg._apply_env()
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        selector = SelectorConfig(**_subset(raw.get("selector", {}), SelectorConfig))
        runner = RunnerConfig(**_subset(raw.get("runner", {}), RunnerConfig))
        evaluator = EvaluatorConfig(**_subset(raw.get("evaluator", {}), EvaluatorConfig))
        schema = ResponseSchema(**_subset(raw.get("response_schema", {}), ResponseSchema))
        batch: list[SelectorConfig] = []
        for entry in raw.get("batch", []):
            merged = {f.name: getattr(selector, f.name) for f in fields(SelectorConfig)}
            merged.update(_subset(entry, SelectorConfig))
            batch.append(SelectorConfig(**merged))
        top = _subset(raw, cls, exclude={"selector", "runner", "evaluator", "response_schema", "batch"})
        return cls(selector=selector, runner=runner, evaluator=evaluator, response_schema=schema, batch=batch, **top)

    def _apply_env(self) -> None:
        self.agent_base_url = os.getenv("HARNESS_AGENT_URL", self.agent_base_url)
        self.integration_base_url = os.getenv("HARNESS_INTEGRATION_URL", self.integration_base_url)
        self.tenant_id = os.getenv("HARNESS_TENANT_ID", self.tenant_id)
        self.output_dir = os.getenv("HARNESS_OUTPUT_DIR", self.output_dir)
        if os.getenv("HARNESS_ITERATIONS"):
            self.runner.iterations = int(os.environ["HARNESS_ITERATIONS"])
        self.selector.integration_url = os.getenv("ADO_BASE_URL", self.selector.integration_url)
        self.selector.integration_project = os.getenv("ADO_PROJECT_KEY", self.selector.integration_project)
        self.selector.integration_token = os.getenv("ADO_TOKEN", self.selector.integration_token)

    def validate(self) -> None:
        if self.runner.iterations < 1:
            raise ValueError("runner.iterations must be >= 1")
        if self.selector.mode not in ("inline", "ado"):
            raise ValueError("selector.mode must be 'inline' or 'ado'")
        if self.selector.mode == "inline" and not self.selector.cr_file:
            raise ValueError("selector.cr_file is required when selector.mode='inline'")
        if self.selector.mode == "ado":
            if not self.selector.cr_id:
                raise ValueError("selector.cr_id is required when selector.mode='ado'")
            missing = [
                name for name, val in (
                    ("integration_url", self.selector.integration_url),
                    ("integration_project", self.selector.integration_project),
                    ("integration_token", self.selector.integration_token),
                ) if not val
            ]
            if missing:
                raise ValueError(
                    "selector.mode='ado' needs credentials; missing: " + ", ".join(missing)
                )


def _subset(raw: dict[str, Any], cls: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    valid = {f.name for f in fields(cls)} - exclude
    return {k: v for k, v in raw.items() if k in valid}
