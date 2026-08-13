"""Generic async HTTP client for a solution under test.

Endpoint paths are configurable so the harnesses are not tied to any specific
solution's API shape. Pass an `endpoints` dict in config to override defaults.

Default endpoints match the agentic-ai-stlc impact analyser:
  submit  POST /analyse/cr
  poll    GET  /analyse/cr/{run_id}
  delete  DELETE /analyse/cr/{run_id}

For a different solution, override in config:
  "endpoints": {
    "submit": "/api/analyse",
    "poll": "/api/analyse/{run_id}",
    "delete": "/api/analyse/{run_id}"
  }
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class HarnessError(RuntimeError):
    pass


class SolutionClient:
    DEFAULT_ENDPOINTS = {
        "submit": "/analyse/cr",
        "poll": "/analyse/cr/{run_id}",
        "delete": "/analyse/cr/{run_id}",
        "fetch_cr": "/cr/{tool}/{cr_id}",
        "fetch_tests": "/test-cases/ado",
        "search": "/search/ado",
    }

    def __init__(
        self,
        agent_base_url: str,
        integration_base_url: str = "",
        tenant_id: str = "",
        endpoints: Optional[dict[str, str]] = None,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._agent = agent_base_url.rstrip("/")
        self._integration = integration_base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._endpoints = {**self.DEFAULT_ENDPOINTS, **(endpoints or {})}
        self._http = client or httpx.AsyncClient(timeout=30.0)
        self._owns_http = client is None

    async def __aenter__(self) -> "SolutionClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def health(self) -> dict[str, Any]:
        res = await self._http.get(f"{self._agent}/health")
        res.raise_for_status()
        return res.json()

    async def submit(
        self,
        *,
        cr_id: str,
        normalised_cr: dict[str, Any],
        existing_tests: Optional[list[dict[str, Any]]] = None,
        related_user_stories: Optional[list[dict[str, Any]]] = None,
        tcg_enabled: bool = True,
        extra_body: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "tenant_id": self._tenant_id,
            "cr_id": cr_id,
            "normalised_cr": normalised_cr,
            "analysis_mode": "full",
            "tcg_enabled": tcg_enabled,
        }
        if existing_tests is not None:
            body["existing_tests"] = existing_tests
        if related_user_stories:
            body["related_user_stories"] = related_user_stories
            body["related_user_story_count"] = len(related_user_stories)
        if extra_body:
            body.update(extra_body)

        path = self._endpoints["submit"]
        res = await self._http.post(f"{self._agent}{path}", json=body, timeout=timeout)
        res.raise_for_status()
        return res.json()

    async def poll(self, run_id: str) -> dict[str, Any]:
        path = self._endpoints["poll"].format(run_id=run_id)
        res = await self._http.get(
            f"{self._agent}{path}",
            params={"tenant_id": self._tenant_id},
        )
        if res.status_code == 404:
            raise HarnessError(f"run {run_id} not found (404)")
        res.raise_for_status()
        return res.json()

    async def delete(self, run_id: str) -> bool:
        path = self._endpoints["delete"].format(run_id=run_id)
        res = await self._http.delete(
            f"{self._agent}{path}",
            params={"tenant_id": self._tenant_id},
        )
        return res.status_code in (204, 200)

    def _integration_headers(self, url: str, project: str, token: str) -> dict[str, str]:
        return {
            "X-Integration-Url": url,
            "X-Integration-Project": project,
            "X-Integration-Token": token,
        }

    async def fetch_cr(
        self, *, tool: str, cr_id: str, url: str, project: str, token: str
    ) -> dict[str, Any]:
        path = self._endpoints["fetch_cr"].format(tool=tool, cr_id=cr_id)
        res = await self._http.get(
            f"{self._integration}{path}",
            headers=self._integration_headers(url, project, token),
        )
        res.raise_for_status()
        return res.json()

    async def fetch_test_cases_by_tags(
        self, *, tags: list[str], url: str, project: str, token: str
    ) -> list[dict[str, Any]]:
        path = self._endpoints["fetch_tests"]
        res = await self._http.get(
            f"{self._integration}{path}",
            params={"tags": ",".join(tags)},
            headers=self._integration_headers(url, project, token),
        )
        res.raise_for_status()
        return res.json().get("items", [])
