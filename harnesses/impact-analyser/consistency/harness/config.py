"""Harness configuration.

Loaded from a JSON file (see config.example.json) and/or environment variables.
Uses only the standard library + dataclasses so the harness has zero install
cost beyond httpx — which keeps the local (no-app) unit tests trivially runnable.

Precedence (highest first): explicit CLI overrides → env vars → JSON file →
dataclass defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any, Optional

# Dev workspace UUID seeded by packages/db/src/seed.ts. The analyser derives the
# tenant from the web session in normal use; driving the API directly we must
# pass it ourselves.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class SelectorConfig:
    """How the SelectorAgent obtains the change request under test.

    mode="inline"  — read normalised_cr + existing_tests from `cr_file` (a JSON
                     document). Most reproducible: the exact same bytes feed every
                     run, so any variance is purely the analyser's.
    mode="ado"     — fetch the CR and its tagged test cases live from ADO via
                     integration-service. Closer to the real workflow but the CR
                     content must not change between the fetch and the runs.
    """

    mode: str = "inline"          # "inline" | "ado"
    cr_file: Optional[str] = None  # required when mode="inline"

    # --- mode="ado" only ---
    cr_id: Optional[str] = None            # e.g. "1234"
    source: str = "ado"                    # integration-service tool key
    test_case_tags: list[str] = field(default_factory=list)  # tags → existing tests
    integration_url: Optional[str] = None  # ADO org/collection URL
    integration_project: Optional[str] = None
    integration_token: Optional[str] = None


@dataclass
class RunnerConfig:
    """How the RunnerAgent submits and polls runs."""

    iterations: int = 5             # how many times to analyse the same CR
    tcg_enabled: bool = True        # generate test cases for coverage gaps
    bypass_cache: bool = True       # nonce the cr_id so each run truly re-executes
    max_concurrency: int = 1        # 1 = strictly sequential (kindest to LLM rate limits)
    poll_interval_sec: float = 3.0  # matches the UI's /analyse poll cadence
    poll_timeout_sec: float = 600.0  # give up on a single run after this long
    submit_timeout_sec: float = 30.0
    delete_runs_after: bool = False  # hard-delete each run once captured (keeps DB tidy)


@dataclass
class EvaluatorConfig:
    """How the EvaluatorAgent scores consistency.

    The weights blend the per-dimension consistency scores (each already in
    [0,1], 1 = identical across runs) into a single headline number. They are
    surfaced in the report so the score is never a black box.
    """

    weights: dict[str, float] = field(default_factory=lambda: {
        "test_set_stability":              0.30,  # do the same tests get selected
        "action_agreement":                0.25,  # do they get the same Must/Should/Consider/Exclude
        "score_stability":                 0.15,  # are composite scores stable
        "ranking_stability":               0.10,  # is the ordering stable
        "risk_agreement":                  0.10,  # is overall_risk stable
        "summary_stability":               0.05,  # are the headline counts stable
        "coverage_gap_stability":          0.03,  # are the same gaps detected
        "generated_test_case_consistency": 0.02,  # are generated test cases consistent
    })
    # composite_score stdev at/above this counts as fully inconsistent (score 0).
    score_stdev_full_penalty: float = 0.25


@dataclass
class Config:
    agent_base_url: str = "http://localhost:8000"
    integration_base_url: str = "http://localhost:3001"
    tenant_id: str = DEFAULT_TENANT_ID
    output_dir: str = "testing/consistency-harness/runs"

    selector: SelectorConfig = field(default_factory=SelectorConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)

    # Batch mode: list of per-CR selector overrides. When present, `selector`
    # acts as the default — each entry in `batch` only needs to specify what
    # differs (e.g. cr_id and test_case_tags). runner/evaluator are shared.
    batch: list[SelectorConfig] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """Build a Config from an optional JSON file, then env overrides."""
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
        # Each batch entry inherits from the top-level selector then overrides.
        batch: list[SelectorConfig] = []
        for entry in raw.get("batch", []):
            merged = {f.name: getattr(selector, f.name) for f in fields(SelectorConfig)}
            merged.update(_subset(entry, SelectorConfig))
            batch.append(SelectorConfig(**merged))
        top = _subset(raw, cls, exclude={"selector", "runner", "evaluator", "batch"})
        return cls(selector=selector, runner=runner, evaluator=evaluator, batch=batch, **top)

    def _apply_env(self) -> None:
        # Top-level connection settings most likely to differ per machine.
        self.agent_base_url = os.getenv("HARNESS_AGENT_URL", self.agent_base_url)
        self.integration_base_url = os.getenv("HARNESS_INTEGRATION_URL", self.integration_base_url)
        self.tenant_id = os.getenv("HARNESS_TENANT_ID", self.tenant_id)
        self.output_dir = os.getenv("HARNESS_OUTPUT_DIR", self.output_dir)
        if os.getenv("HARNESS_ITERATIONS"):
            self.runner.iterations = int(os.environ["HARNESS_ITERATIONS"])
        # ADO credentials commonly come from the environment, not a committed file.
        self.selector.integration_url = os.getenv(
            "ADO_BASE_URL", self.selector.integration_url
        )
        self.selector.integration_project = os.getenv(
            "ADO_PROJECT_KEY", self.selector.integration_project
        )
        self.selector.integration_token = os.getenv(
            "ADO_TOKEN", self.selector.integration_token
        )

    def validate(self) -> None:
        """Fail fast with a clear message rather than deep in an HTTP call."""
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
                    "selector.mode='ado' needs ADO credentials; missing: "
                    + ", ".join(missing)
                    + " (set in config or via ADO_BASE_URL / ADO_PROJECT_KEY / ADO_TOKEN)"
                )


def _subset(raw: dict[str, Any], cls: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    """Keep only keys that are real fields of `cls` — tolerate extra keys in the
    JSON file rather than blowing up on a typo or a forward-compatible field."""
    exclude = exclude or set()
    valid = {f.name for f in fields(cls)} - exclude
    return {k: v for k, v in raw.items() if k in valid}
