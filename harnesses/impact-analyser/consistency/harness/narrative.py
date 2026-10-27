"""Generate a plain-English interpretive narrative for a consistency report.

Calls the Kilo AI API (OpenAI-compatible) to produce a short, human-readable
commentary on the scores — the same kind of summary a QA lead would write after
reviewing the numbers.

Uses httpx (already a harness dependency) — no extra packages needed.

Completely optional: if KILO_API_KEY is unset the function returns None and the
report is written without a narrative section. Falls back to KILO_API_URL and
KILO_MODEL from the environment (mirrors the app's own llm.py conventions).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from .agents.base import SelectedCR
    from .agents.evaluator import ConsistencyReport
    from .agents.runner import RunResult

logger = logging.getLogger("harness.narrative")

_DEFAULT_API_URL = "https://api.kilo.ai"
_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
_MAX_TOKENS = 1500
_TIMEOUT = 60.0

_SYSTEM = (
    "You are a QA lead. Write a short paragraph (5–6 sentences, no lists, no "
    "headers) interpreting a consistency report for an AI test impact analyser. "
    "Mention: the headline rating, what is most stable, what is most variable, one "
    "notable test, and one practical recommendation. Plain English only."
)

_SYSTEM_IMPROVEMENTS = (
    "You are a QA lead advising a team on how to improve the consistency of an AI test impact analyser. "
    "You will be given dimension scores for a single change request. "
    "Focus only on dimensions that scored below 0.75. "
    "For each weak dimension, write one short bullet point explaining in plain English what the low score "
    "means in practice and one concrete action the team could take to improve it — for example, refining "
    "the prompt, improving the CR description, adjusting acceptance criteria, or adding test tags. "
    "If all dimensions score 0.75 or above, write a single sentence saying the analyser is performing well "
    "and no immediate changes are needed. "
    "No preamble, no headers — just the bullet points."
)


def generate_narrative(
    report: "ConsistencyReport",
    selected: "SelectedCR",
    results: "list[RunResult]",
) -> Optional[str]:
    """Return a markdown narrative section, or None if generation is unavailable."""
    # Default to the anonymous free-tier key — no sign-up required.
    # Set KILO_API_KEY=off (or any non-URL value that isn't a real key) to skip.
    api_key = os.getenv("KILO_API_KEY", "anonymous")
    if api_key.lower() in ("", "off", "false", "disabled", "none", "0"):
        logger.debug("Narrative generation disabled via KILO_API_KEY=%s", api_key)
        return None

    api_url = os.getenv("KILO_API_URL", _DEFAULT_API_URL).rstrip("/")
    model = os.getenv("KILO_MODEL", _DEFAULT_MODEL)

    try:
        prompt = _build_prompt(report, selected, results)
        response = httpx.post(
            f"{api_url}/api/openrouter/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": _MAX_TOKENS,
                "stream": False,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        # The free Kilo tier can return truncated JSON at high token counts;
        # fall back to partial text extraction rather than failing silently.
        try:
            text = response.json()["choices"][0]["message"]["content"].strip()
        except Exception:  # noqa: BLE001
            raw = response.text
            logger.debug("Kilo response body (raw): %s", raw[:500])
            # Try to extract whatever content arrived before truncation
            marker = '"content":'
            idx = raw.find(marker)
            if idx == -1:
                raise
            snippet = raw[idx + len(marker):].lstrip().lstrip('"')
            text = snippet.split('"')[0].replace("\\n", "\n").strip()
            if not text:
                raise
            logger.debug("Extracted partial narrative (%d chars)", len(text))
        text = _extract_final_paragraph(text)
        return f"## Analysis\n\n{text}\n"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Narrative generation failed: %s", exc)
        return None


def generate_tc_topic_similarity(
    report: "ConsistencyReport",
) -> Optional[str]:
    """Ask the LLM whether generated test cases across runs cover the same topics.

    Returns a one-sentence plain-English verdict, or None if unavailable.
    """
    api_key = os.getenv("KILO_API_KEY", "anonymous")
    if api_key.lower() in ("", "off", "false", "disabled", "none", "0"):
        return None

    tc_lists = report.details.get("generated_tc_titles") or []
    if not tc_lists or all(len(tc) == 0 for tc in tc_lists):
        return "No test cases were generated in any run — topic similarity cannot be assessed."

    api_url = os.getenv("KILO_API_URL", _DEFAULT_API_URL).rstrip("/")
    model = os.getenv("KILO_MODEL", _DEFAULT_MODEL)

    prompt_lines = [
        "Below are the titles of test cases generated by an AI test impact analyser across multiple runs of the same change request.",
        "Each run is listed separately. The wording may differ but the topics may be the same.",
        "In one sentence, state whether the generated test cases cover the same topics across runs, or whether the topics vary significantly.",
        "",
    ]
    for i, titles in enumerate(tc_lists, 1):
        if titles:
            prompt_lines.append(f"Run {i}: " + " | ".join(titles))
        else:
            prompt_lines.append(f"Run {i}: (no test cases generated)")

    try:
        response = httpx.post(
            f"{api_url}/api/openrouter/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 200,
                "stream": False,
                "messages": [
                    {"role": "system", "content": "You are a QA lead. Answer in one plain English sentence only. No preamble."},
                    {"role": "user", "content": "\n".join(prompt_lines)},
                ],
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("TC topic similarity generation failed: %s", exc)
        return None


def generate_improvements(
    report: "ConsistencyReport",
    selected: "SelectedCR",
) -> Optional[str]:
    """Return a markdown recommended improvements section, or None if unavailable."""
    api_key = os.getenv("KILO_API_KEY", "anonymous")
    if api_key.lower() in ("", "off", "false", "disabled", "none", "0"):
        return None

    api_url = os.getenv("KILO_API_URL", _DEFAULT_API_URL).rstrip("/")
    model = os.getenv("KILO_MODEL", _DEFAULT_MODEL)

    weak = {k: v for k, v in (report.dimensions or {}).items() if v is not None and v < 0.75}
    if not weak:
        return "## Recommended Improvements\n\nThe analyser is performing well across all dimensions — no immediate changes are needed.\n"

    prompt_lines = [
        f"Change request: {(selected.normalised_cr or {}).get('title', '')} (CR {selected.cr_id})",
        "",
        "Dimension scores (only those below 0.75 are listed — these are the weak areas):",
    ]
    dim_descriptions = {
        "test_set_stability":     "whether the same tests are selected every run",
        "action_agreement":       "whether each test gets the same Must/Should/Consider/Exclude recommendation every run",
        "score_stability":        "whether the LLM's relevance score for each test is consistent across runs",
        "ranking_stability":      "whether the order tests are prioritised is consistent across runs",
        "risk_agreement":         "whether the overall risk rating is the same every run",
        "summary_stability":      "whether the headline counts (must-run, should-run, exclude) are consistent across runs",
        "coverage_gap_stability": "whether the same coverage gaps are identified every run",
    }
    for dim, score in weak.items():
        desc = dim_descriptions.get(dim, dim)
        prompt_lines.append(f"  {dim} ({desc}): {score:.3f}")

    try:
        response = httpx.post(
            f"{api_url}/api/openrouter/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": _MAX_TOKENS,
                "stream": False,
                "messages": [
                    {"role": "system", "content": _SYSTEM_IMPROVEMENTS},
                    {"role": "user", "content": "\n".join(prompt_lines)},
                ],
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        try:
            text = response.json()["choices"][0]["message"]["content"].strip()
        except Exception:  # noqa: BLE001
            raw = response.text
            marker = '"content":'
            idx = raw.find(marker)
            if idx == -1:
                raise
            snippet = raw[idx + len(marker):].lstrip().lstrip('"')
            text = snippet.split('"')[0].replace("\\n", "\n").strip()
            if not text:
                raise
        return f"## Recommended Improvements\n\n{text}\n"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Improvements generation failed: %s", exc)
        return None


def _extract_final_paragraph(text: str) -> str:
    """Strip chain-of-thought reasoning and return only the final prose paragraph.

    Reasoning models often prepend a scratchpad before writing the actual answer.
    We detect the boundary by finding the last paragraph that looks like finished
    prose: multiple sentences, no reasoning markers like 'Let's', 'Next:', etc.
    Falls back to the full text if no clean paragraph is found.
    """
    reasoning_markers = {
        "let's", "next sentence", "next:", "craft:", "we need",
        "must mention", "so headline", "most stable:", "most variable:",
        "notable test:", "practical rec", "write ", "produce ",
    }
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    # Walk from the end, return the first paragraph that doesn't smell like reasoning
    for para in reversed(paragraphs):
        lower = para.lower()
        if any(m in lower for m in reasoning_markers):
            continue
        # Must look like prose: at least two sentences ending with a period
        if para.count(".") >= 2:
            return para
    return text  # fallback: return everything


def _build_prompt(
    report: "ConsistencyReport",
    selected: "SelectedCR",
    results: "list[RunResult]",
) -> str:
    d = report.details
    per_test = d.get("per_test") or {}

    per_test_lines = []
    for tid, info in per_test.items():
        per_test_lines.append(
            f"  Test {tid}: mean score {info['score_mean']}, "
            f"stdev {info['score_stdev']} (min {info['score_min']} max {info['score_max']}), "
            f"action agreement {info['action_agreement']}"
        )

    run_summary = ", ".join(
        f"run {r.index + 1} {r.status}" + (f" ({r.error})" if r.error else "")
        for r in results
    )

    lines = [
        f"Change request: {(selected.normalised_cr or {}).get('title', '')} (CR {selected.cr_id})",
        f"Existing tests scored: {len(selected.existing_tests)}",
        f"Valid runs: {report.runs_evaluated} of "
        f"{report.runs_evaluated + report.runs_failed} "
        f"(outcomes: {run_summary})",
        "",
        f"Overall consistency score: {report.overall_score} ({report.band})",
        "",
        "Dimension scores:",
    ]
    for dim, score in (report.dimensions or {}).items():
        weight = (report.weights_used or {}).get(dim)
        score_s = "excluded (insufficient data)" if score is None else f"{score:.3f}"
        weight_s = "" if weight is None else f" (weight {weight:.2f})"
        lines.append(f"  {dim}: {score_s}{weight_s}")

    lines += [
        "",
        f"Risk classification per run: {d.get('overall_risk_values')}",
        f"Blast radius per run: {d.get('blast_radius_values')} "
        f"(agreement {d.get('blast_radius_agreement')})",
        f"Change-type stability: {d.get('change_types_stability')}",
        "",
        "Per-test stability (tests appearing in every run):",
    ]
    if per_test_lines:
        lines += per_test_lines
    else:
        lines.append("  No tests were common to all runs.")

    if d.get("summary_cv"):
        lines += [
            "",
            "Headline count variability (coefficient of variation per bucket):",
            "  " + json.dumps(d["summary_cv"]),
        ]

    return "\n".join(lines)
