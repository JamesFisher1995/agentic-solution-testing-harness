"""Unit tests for SelectorAgent — inline file and ADO-fetch paths."""

import asyncio
import json

import httpx

from harness.agents.selector import SelectorAgent
from harness.client import AnalyserClient
from harness.config import SelectorConfig
from tests.fixtures import FakeAnalyser


def _client(fake: FakeAnalyser) -> AnalyserClient:
    return AnalyserClient(
        "http://agent", "http://integration", "tenant-1",
        client=httpx.AsyncClient(transport=fake.transport()),
    )


def test_inline_reads_normalised_cr_and_tests(tmp_path):
    doc = {
        "cr_id": "CR-77",
        "normalised_cr": {"title": "T", "description": "D",
                          "acceptance_criteria": "AC", "labels": ["x"]},
        "existing_tests": [{"test_id": "TC-1"}, {"test_id": "TC-2"}],
    }
    f = tmp_path / "cr.json"
    f.write_text(json.dumps(doc), encoding="utf-8")

    cfg = SelectorConfig(mode="inline", cr_file=str(f))

    async def go():
        return await SelectorAgent(client=None).run(cfg)

    selected = asyncio.run(go())
    assert selected.cr_id == "CR-77"
    assert selected.normalised_cr["title"] == "T"
    assert len(selected.existing_tests) == 2
    assert selected.source == "inline"


def test_inline_flat_shape_without_normalised_cr_wrapper(tmp_path):
    doc = {"cr_id": "CR-9", "title": "Flat", "description": "d",
           "acceptance_criteria": "a"}
    f = tmp_path / "cr.json"
    f.write_text(json.dumps(doc), encoding="utf-8")

    async def go():
        return await SelectorAgent(client=None).run(
            SelectorConfig(mode="inline", cr_file=str(f)))

    selected = asyncio.run(go())
    assert selected.normalised_cr["title"] == "Flat"
    assert selected.normalised_cr["labels"] == []   # defaulted


def test_ado_fetch_pulls_cr_and_tagged_tests():
    fake = FakeAnalyser(integration={
        "cr": {"cr_id": "1234", "source": "ado",
               "normalised_cr": {"title": "From ADO", "description": "d",
                                 "acceptance_criteria": "a", "labels": []}},
        "test_cases": [{"test_id": "TC-1"}, {"test_id": "TC-2"}, {"test_id": "TC-3"}],
    })
    cfg = SelectorConfig(
        mode="ado", cr_id="1234", source="ado", test_case_tags=["benefits"],
        integration_url="https://dev.azure.com/org", integration_project="proj",
        integration_token="pat",
    )

    async def go():
        async with _client(fake) as c:
            return await SelectorAgent(c).run(cfg)

    selected = asyncio.run(go())
    assert selected.cr_id == "1234"
    assert selected.normalised_cr["title"] == "From ADO"
    assert len(selected.existing_tests) == 3
    assert selected.source == "ado"
