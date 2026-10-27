"""Thin async HTTP client for the analyser + integration services.

Every request shape here mirrors the real endpoints verified in the codebase:
  * agent-bus-api      POST /analyse/cr, GET /analyse/cr/{id}, DELETE /analyse/cr/{id}
  * integration-service GET /cr/{tool}/{id}, GET /test-cases/ado, GET /search/ado

The client is constructed with an httpx.AsyncClient so tests can inject an
httpx.MockTransport and exercise the full agent logic with no app running.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class AnalyserError(RuntimeError):
    """Raised when the analyser API returns an unexpected response."""


class AnalyserClient:
    def __init__(
        self,
        agent_base_url: str,
        integration_base_url: str,
        tenant_id: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._agent = agent_base_url.rstrip("/")
        self._integration = integration_base_url.rstrip("/")
        self._tenant_id = tenant_id
        # Allow injection (MockTransport in tests); otherwise own a real client.
        self._http = client or httpx.AsyncClient(timeout=30.0)
        self._owns_http = client is None

    async def __aenter__(self) -> "AnalyserClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---------------- agent-bus-api ----------------

    async def health(self) -> dict[str, Any]:
        res = await self._http.get(f"{self._agent}/health")
        res.raise_for_status()
        return res.json()

    async def submit_cr(
        self,
        *,
        cr_id: str,
        normalised_cr: dict[str, Any],
        existing_tests: Optional[list[dict[str, Any]]] = None,
        related_user_stories: Optional[list[dict[str, Any]]] = None,
        tcg_enabled: bool = True,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST /analyse/cr. Returns the raw 202 body.

        Either {run_id, cr_id, status:"running"} for a fresh run, or a full
        report dict carrying {"cache_hit": true} when the analyser short-circuited.
        """
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

        res = await self._http.post(f"{self._agent}/analyse/cr", json=body, timeout=timeout)
        res.raise_for_status()
        return res.json()

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """GET /analyse/cr/{run_id}. Returns the analyser_runs row."""
        res = await self._http.get(
            f"{self._agent}/analyse/cr/{run_id}",
            params={"tenant_id": self._tenant_id},
        )
        if res.status_code == 404:
            raise AnalyserError(f"run {run_id} not found (404) — tenant mismatch or deleted")
        res.raise_for_status()
        return res.json()

    async def delete_run(self, run_id: str) -> bool:
        res = await self._http.delete(
            f"{self._agent}/analyse/cr/{run_id}",
            params={"tenant_id": self._tenant_id},
        )
        return res.status_code in (204, 200)

    # ---------------- integration-service ----------------

    def _integration_headers(
        self, url: str, project: str, token: str
    ) -> dict[str, str]:
        return {
            "X-Integration-Url": url,
            "X-Integration-Project": project,
            "X-Integration-Token": token,
        }

    async def fetch_cr(
        self, *, tool: str, cr_id: str, url: str, project: str, token: str
    ) -> dict[str, Any]:
        """GET /cr/{tool}/{cr_id} → {cr_id, source, normalised_cr}."""
        res = await self._http.get(
            f"{self._integration}/cr/{tool}/{cr_id}",
            headers=self._integration_headers(url, project, token),
        )
        res.raise_for_status()
        return res.json()

    async def fetch_test_cases_by_tags(
        self, *, tags: list[str], url: str, project: str, token: str
    ) -> list[dict[str, Any]]:
        """GET /test-cases/ado?tags=… → items[{test_id, test_name, suite, tags}]."""
        res = await self._http.get(
            f"{self._integration}/test-cases/ado",
            params={"tags": ",".join(tags)},
            headers=self._integration_headers(url, project, token),
        )
        res.raise_for_status()
        return res.json().get("items", [])
