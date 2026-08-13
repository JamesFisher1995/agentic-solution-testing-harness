"""SelectorAgent — obtain the change request under test.

Produces a single frozen SelectedCR that the RunnerAgent will feed, byte-for-byte
identical, into every iteration. Freezing here is what makes the experiment fair:
the only thing allowed to vary between runs is the analyser's own behaviour.
"""

from __future__ import annotations

import json

from ..config import SelectorConfig
from .base import Agent, SelectedCR


class SelectorAgent(Agent):
    name = "selector"

    async def run(self, cfg: SelectorConfig) -> SelectedCR:
        if cfg.mode == "inline":
            return self._from_file(cfg)
        if cfg.mode == "ado":
            return await self._from_ado(cfg)
        raise ValueError(f"unknown selector mode: {cfg.mode!r}")

    # ------------------------------------------------------------------
    def _from_file(self, cfg: SelectorConfig) -> SelectedCR:
        with open(cfg.cr_file, "r", encoding="utf-8") as fh:
            doc = json.load(fh)

        normalised = doc.get("normalised_cr") or {
            k: doc.get(k, "")
            for k in ("title", "description", "acceptance_criteria")
        }
        # labels must be a list — coerce a missing/blank value rather than letting
        # an empty string ("") slip through from the flat-shape comprehension.
        labels = normalised.get("labels") or doc.get("labels") or []
        normalised["labels"] = labels if isinstance(labels, list) else []
        cr_id = str(doc.get("cr_id") or cfg.cr_id or "CR-INLINE")
        existing = doc.get("existing_tests") or []
        stories = doc.get("related_user_stories") or []

        self.log.info(
            "inline CR %s — %d existing tests, %d related stories",
            cr_id, len(existing), len(stories),
        )
        return SelectedCR(
            cr_id=cr_id,
            normalised_cr=normalised,
            existing_tests=existing,
            related_user_stories=stories,
            source="inline",
        )

    # ------------------------------------------------------------------
    async def _from_ado(self, cfg: SelectorConfig) -> SelectedCR:
        creds = dict(
            url=cfg.integration_url,
            project=cfg.integration_project,
            token=cfg.integration_token,
        )
        fetched = await self.client.fetch_cr(tool=cfg.source, cr_id=cfg.cr_id, **creds)
        normalised = fetched["normalised_cr"]

        existing: list[dict] = []
        if cfg.test_case_tags:
            existing = await self.client.fetch_test_cases_by_tags(
                tags=cfg.test_case_tags, **creds
            )
            self.log.info(
                "fetched %d test cases for tags %s", len(existing), cfg.test_case_tags
            )
        else:
            self.log.warning(
                "no test_case_tags configured — analyser will score 0 existing "
                "tests, so the consistency report will only cover classification "
                "and summary fields"
            )

        return SelectedCR(
            cr_id=str(cfg.cr_id),
            normalised_cr=normalised,
            existing_tests=existing,
            related_user_stories=[],
            source="ado",
        )
