"""RunnerAgent — submit the same CR N times and collect the reports.

Cache bypass
------------
The analyser keys its Layer-7 cache on (cr_id, title, description,
acceptance_criteria). Re-submitting the same CR would therefore return the first
run's stored report verbatim (`cache_hit: true`) and measure nothing.

We verified in the codebase that cr_id never reaches any LLM prompt — it is only
an identifier (and the stem of deterministic TC-{cr_id}-{index} test-case IDs).
So appending a per-iteration nonce to cr_id forces a genuine cache miss while the
LLM sees byte-identical input. That is exactly the variable we want to isolate.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import RunnerConfig
from .base import Agent, SelectedCR

# Terminal statuses returned by GET /analyse/cr/{id}.
_DONE = {"complete", "cache_hit"}
_FAILED = {"failed"}


@dataclass
class RunResult:
    index: int
    cr_id_used: str            # the (possibly nonce'd) id actually submitted
    run_id: Optional[str] = None
    status: str = "pending"
    report: Optional[dict] = None
    error: Optional[str] = None
    cache_hit: bool = False
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in _DONE and self.report is not None


class RunnerAgent(Agent):
    name = "runner"

    async def run(self, selected: SelectedCR, cfg: RunnerConfig) -> list[RunResult]:
        sem = asyncio.Semaphore(max(1, cfg.max_concurrency))

        async def _one(i: int) -> RunResult:
            async with sem:
                return await self._run_once(i, selected, cfg)

        # Even at concurrency 1 this preserves submission order in the results.
        results = await asyncio.gather(*[_one(i) for i in range(cfg.iterations)])
        return list(results)

    # ------------------------------------------------------------------
    async def _run_once(
        self, index: int, selected: SelectedCR, cfg: RunnerConfig
    ) -> RunResult:
        cr_id = self._cr_id_for(selected.cr_id, index, cfg.bypass_cache)
        res = RunResult(index=index, cr_id_used=cr_id)
        started = time.monotonic()

        try:
            submit = await self.client.submit(
                cr_id=cr_id,
                normalised_cr=selected.normalised_cr,
                existing_tests=selected.existing_tests,
                related_user_stories=selected.related_user_stories,
                tcg_enabled=cfg.tcg_enabled,
                timeout=cfg.submit_timeout_sec,
            )

            # Cache hit: the body IS the report (carries cache_hit + run_id).
            if submit.get("cache_hit"):
                res.cache_hit = True
                res.run_id = submit.get("run_id")
                res.report = {k: v for k, v in submit.items() if k != "cache_hit"}
                res.status = "cache_hit"
                self.log.warning(
                    "run %d hit the cache (cr_id=%s) — variance not measured for "
                    "this run; is bypass_cache disabled or a stale run present?",
                    index, cr_id,
                )
                return res

            run_id = submit.get("run_id")
            if not run_id:
                raise RuntimeError(f"submit returned no run_id: {submit!r}")
            res.run_id = run_id
            self.log.info("run %d submitted — run_id=%s cr_id=%s", index, run_id, cr_id)

            await self._poll(res, cfg)

            if res.status in _DONE and res.report is not None and cfg.delete_runs_after:
                await self.client.delete(run_id)
        except Exception as exc:  # noqa: BLE001 — record, never crash the batch
            res.status = "error"
            res.error = f"{type(exc).__name__}: {exc}"
            self.log.error("run %d failed: %s", index, res.error)
        finally:
            res.elapsed_sec = round(time.monotonic() - started, 2)
        return res

    async def _poll(self, res: RunResult, cfg: RunnerConfig) -> None:
        deadline = time.monotonic() + cfg.poll_timeout_sec
        while True:
            row = await self.client.poll(res.run_id)
            status = row.get("status", "unknown")
            if status in _DONE:
                res.status = status
                res.report = row.get("report")
                if res.report is None:
                    raise RuntimeError(f"run {res.run_id} {status} but report is null")
                return
            if status in _FAILED:
                res.status = "failed"
                res.error = row.get("error_message") or "pipeline failed"
                return
            if time.monotonic() >= deadline:
                res.status = "timeout"
                res.error = f"still {status!r} after {cfg.poll_timeout_sec}s"
                return
            await asyncio.sleep(cfg.poll_interval_sec)

    @staticmethod
    def _cr_id_for(base: str, index: int, bypass: bool) -> str:
        import uuid as _uuid
        # Random suffix guarantees a cache miss even if a previous session's run
        # with the same sequential index is still present in the database.
        return f"{base}#{_uuid.uuid4().hex[:8]}" if bypass else base
