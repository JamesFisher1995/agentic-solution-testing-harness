"""Shared agent scaffolding."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared.client import SolutionClient as AnalyserClient


class Agent:
    """Base class: gives every agent a client, config slice, and a named logger.

    Kept deliberately thin — the agents are coordinated by the Orchestrator, not
    by a framework. This mirrors the application's own 'no LangGraph, a plain
    sequential state machine is easier to debug' design choice.
    """

    name: str = "agent"

    def __init__(self, client: AnalyserClient) -> None:
        self.client = client
        self.log = logging.getLogger(f"harness.{self.name}")


@dataclass
class SelectedCR:
    """The frozen input fed identically to every run."""

    cr_id: str
    normalised_cr: dict
    existing_tests: list[dict]
    related_user_stories: list[dict]
    source: str  # "inline" | "ado"
