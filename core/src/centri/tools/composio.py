"""Composio tool provider (Phase 4 / Decision 11).

Composio (composio.dev) exposes a large catalog of third-party tools behind one
HTTP API. CENTRI's first tool provider wraps a configurable allowlist of those
tool slugs — Tavily web search is the demo tool — behind the ``ToolProvider``
contract so they flow through ``ToolRegistry.invoke`` with the same event trail,
approval gating, and consolidation fact hint as any other tool.

Honest-unavailable: with no API key the provider reports ``available() == False``
with reason ``composio:unavailable:no-api-key`` and NEVER executes — the registry
lists it as unavailable rather than faking success. The API key reaches only the
``x-api-key`` request header; it is never logged or placed in an event payload.

HTTP uses ``httpx`` (already a core dependency). The transport is injectable so
tests run fully mocked with no network (mirrors ``test_letta_http_store.py``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from centri.tools.base import ToolProvider, ToolResult, ToolSpec, is_read_only_slug

logger = logging.getLogger(__name__)

UNAVAILABLE_NO_KEY = "composio:unavailable:no-api-key"
_DEFAULT_TIMEOUT = 30.0


def parse_tool_allowlist(raw: str) -> List[str]:
    """Parse the comma-separated CENTRI_COMPOSIO_TOOLS allowlist into slugs."""
    return [s.strip().upper() for s in (raw or "").split(",") if s.strip()]


class ComposioToolProvider(ToolProvider):
    """Wraps Composio's tool-execute API behind the CENTRI tool contract."""

    name = "composio"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://backend.composio.dev/api/v3",
        user_id: str = "default",
        tools: Optional[List[str]] = None,
        *,
        http_client: Any = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")
        self._user_id = user_id or "default"
        self._tools = tools if tools is not None else ["TAVILY_SEARCH"]
        # Injectable for tests; a real httpx.AsyncClient is created lazily per
        # call otherwise (so the provider has no live connection until invoked).
        self._http_client = http_client
        self._timeout = timeout

    # ------------------------------------------------------------------
    # ToolProvider contract
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Any, http_client: Any = None) -> "ComposioToolProvider":
        return cls(
            api_key=getattr(settings, "composio_api_key", ""),
            base_url=getattr(settings, "composio_base_url", "https://backend.composio.dev/api/v3"),
            user_id=getattr(settings, "composio_user_id", "default"),
            tools=parse_tool_allowlist(getattr(settings, "composio_tools", "TAVILY_SEARCH")),
            http_client=http_client,
        )

    def available(self) -> bool:
        return bool(self._api_key)

    def unavailable_reason(self) -> str:
        return "" if self._api_key else UNAVAILABLE_NO_KEY

    def list_tools(self) -> List[ToolSpec]:
        """Specs built from the allowlist (no network — slugs are authoritative).

        Read-only classification is conservative: SEARCH/GET/LIST/FETCH slugs are
        side_effectful=False (skip the approval gate); everything else is gated.
        """
        specs: List[ToolSpec] = []
        for slug in self._tools:
            specs.append(
                ToolSpec(
                    name=slug,
                    provider=self.name,
                    description=f"Composio tool {slug}",
                    side_effectful=not is_read_only_slug(slug),
                    input_schema={},
                )
            )
        return specs

    async def execute(self, name: str, arguments: Dict[str, Any], **kwargs: Any) -> ToolResult:
        """Execute a tool via ``POST {base}/tools/execute/{slug}``.

        Honest-failure: an HTTP error, a non-2xx status, or a Composio
        ``successful=false`` body all map to ``ToolResult(status="failed")`` with
        the error preserved — never a faked success. Schema-tolerant parsing
        (``data`` / ``successful`` / ``error``) like the ingest adapters.
        """
        if not self.available():
            return ToolResult(status="unavailable", error=UNAVAILABLE_NO_KEY)
        if name.upper() not in self._tools:
            return ToolResult(status="failed", error=f"tool '{name}' not in allowlist")

        url = f"{self._base_url}/tools/execute/{name}"
        headers = {"x-api-key": self._api_key, "content-type": "application/json"}
        body = {"arguments": arguments or {}, "user_id": self._user_id}

        try:
            status_code, data = await self._post(url, headers, body)
        except Exception as exc:  # network / transport failure
            logger.debug("Composio execute transport error for %s: %s", name, exc)
            return ToolResult(status="failed", error=f"composio request failed: {exc}")

        return self._parse_response(status_code, data)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    async def _post(self, url: str, headers: Dict[str, str], body: Dict[str, Any]):
        """POST and return (status_code, parsed_json). Uses the injected client
        when present, else a short-lived httpx.AsyncClient."""
        import httpx

        if self._http_client is not None:
            resp = await self._http_client.post(url, headers=headers, json=body)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}
        return resp.status_code, data

    @staticmethod
    def _parse_response(status_code: int, data: Dict[str, Any]) -> ToolResult:
        if not isinstance(data, dict):
            data = {"data": data}
        # Composio signals tool-level success via ``successful``; an HTTP error or
        # an explicit error field is an honest failure.
        successful = data.get("successful")
        error = data.get("error") or data.get("message")
        if status_code >= 400:
            return ToolResult(
                status="failed",
                error=str(error or f"HTTP {status_code}"),
                raw=data,
            )
        if successful is False:
            return ToolResult(status="failed", error=str(error or "composio reported failure"), raw=data)
        output = data.get("data", data)
        return ToolResult(status="completed", output=output, raw=data)
