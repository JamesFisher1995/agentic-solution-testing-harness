"""Unit tests for RunnerAgent — submit/poll loop and cache-bypass, all faked."""

import asyncio

import httpx

from harness.agents.base import SelectedCR
from harness.agents.runner import RunnerAgent
from harness.client import AnalyserClient
from harness.config import RunnerConfig
from tests.fixtures import FakeAnalyser, make_report


def _client(fake: FakeAnalyser) -> AnalyserClient:
    return AnalyserClient(
        "http://agent", "http://integration", "tenant-1",
        client=httpx.AsyncClient(transport=fake.transport()),
    )


def _selected() -> SelectedCR:
    return SelectedCR(
        cr_id="CR-1",
        normalised_cr={"title": "t", "description": "d", "acceptance_criteria": "a", "labels": []},
        existing_tests=[{"test_id": "TC-1"}],
        related_user_stories=[],
        source="inline",
    )


def _fast(**kw) -> RunnerConfig:
    return RunnerConfig(poll_interval_sec=0.001, poll_timeout_sec=2.0, **kw)


def test_runs_n_times_with_cache_bypass_nonces():
    fake = FakeAnalyser()
    cfg = _fast(iterations=3, bypass_cache=True)

    async def go():
        async with _client(fake) as c:
            return await RunnerAgent(c).run(_selected(), cfg)

    results = asyncio.run(go())
    assert len(results) == 3
    assert all(r.ok for r in results)
    # each iteration submitted a distinct, nonce'd cr_id ⇒ genuine cache misses
    assert fake.submitted_cr_ids == ["CR-1#c1", "CR-1#c2", "CR-1#c3"]
    assert all(r.report is not None for r in results)


def test_cache_bypass_disabled_reuses_cr_id():
    fake = FakeAnalyser()
    cfg = _fast(iterations=3, bypass_cache=False)

    async def go():
        async with _client(fake) as c:
            return await RunnerAgent(c).run(_selected(), cfg)

    asyncio.run(go())
    assert fake.submitted_cr_ids == ["CR-1", "CR-1", "CR-1"]


def test_polls_until_complete():
    fake = FakeAnalyser(running_polls=2)   # 'running' twice, then 'complete'
    cfg = _fast(iterations=1)

    async def go():
        async with _client(fake) as c:
            return await RunnerAgent(c).run(_selected(), cfg)

    results = asyncio.run(go())
    assert results[0].status == "complete"
    assert results[0].report is not None


def test_cache_hit_is_captured_and_flagged():
    fake = FakeAnalyser(cache_hit_for=lambda cr_id: True)
    cfg = _fast(iterations=2, bypass_cache=False)

    async def go():
        async with _client(fake) as c:
            return await RunnerAgent(c).run(_selected(), cfg)

    results = asyncio.run(go())
    assert all(r.cache_hit for r in results)
    assert all(r.report is not None for r in results)
    assert all("cache_hit" not in r.report for r in results)  # stripped from report


def test_timeout_is_recorded_not_raised():
    # never completes (running_polls huge) with a tiny timeout
    fake = FakeAnalyser(running_polls=10_000)
    cfg = RunnerConfig(iterations=1, poll_interval_sec=0.001, poll_timeout_sec=0.02)

    async def go():
        async with _client(fake) as c:
            return await RunnerAgent(c).run(_selected(), cfg)

    results = asyncio.run(go())
    assert results[0].status == "timeout"
    assert results[0].ok is False
