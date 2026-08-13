"""The three cooperating agents of the consistency harness.

SelectorAgent → RunnerAgent → EvaluatorAgent, wired by the Orchestrator.
Each is a small, single-responsibility unit with an async `run(...)` entry point
and structured dataclass output, so the orchestrator passes typed artifacts
between them rather than loose dicts.
"""
