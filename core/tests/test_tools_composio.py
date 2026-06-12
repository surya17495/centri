"""Composio tool provider tests — fully mocked HTTP, no network.

A mocked httpx transport stands in for the Composio backend (mirrors the
fully-mocked style of test_letta_http_store.py): execute success maps to a
completed ToolResult, an API error / successful=false maps to failed with the
error preserved, no-key is honest-unavailable (never executes), the allowlist is
parsed, and SEARCH/GET/LIST/FETCH slugs classify read-only vs side-effectful.
"""

import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.config import Settings
from centri.tools.composio import (
    UNAVAILABLE_NO_KEY,
    ComposioToolProvider,
    parse_tool_allowlist,
)


def _client(handler) -> httpx.AsyncClient:
    """An httpx.AsyncClient wired to a MockTransport — no sockets opened."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestAvailability:
    def test_no_key_is_honest_unavailable(self):
        p = ComposioToolProvider(api_key="")
        assert p.available() is False
        assert p.unavailable_reason() == UNAVAILABLE_NO_KEY

    def test_with_key_is_available(self):
        p = ComposioToolProvider(api_key="k-123")
        assert p.available() is True
        assert p.unavailable_reason() == ""

    async def test_no_key_never_executes(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": {}})

        p = ComposioToolProvider(api_key="", http_client=_client(handler))
        res = await p.execute("TAVILY_SEARCH", {"query": "x"})
        assert res.status == "unavailable"
        assert res.error == UNAVAILABLE_NO_KEY
        assert calls == []  # no network touched


class TestAllowlistAndClassification:
    def test_parse_allowlist(self):
        assert parse_tool_allowlist("tavily_search, GITHUB_CREATE_ISSUE") == [
            "TAVILY_SEARCH",
            "GITHUB_CREATE_ISSUE",
        ]
        assert parse_tool_allowlist("") == []

    def test_search_is_read_only_action_is_side_effectful(self):
        p = ComposioToolProvider(
            api_key="k", tools=["TAVILY_SEARCH", "GITHUB_CREATE_ISSUE"]
        )
        specs = {s.name: s for s in p.list_tools()}
        assert specs["TAVILY_SEARCH"].side_effectful is False
        assert specs["GITHUB_CREATE_ISSUE"].side_effectful is True
        assert all(s.provider == "composio" for s in specs.values())

    def test_default_tool_is_tavily_search(self):
        p = ComposioToolProvider(api_key="k")
        names = [s.name for s in p.list_tools()]
        assert names == ["TAVILY_SEARCH"]


class TestExecute:
    async def test_success_maps_to_completed(self):
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["x_api_key"] = request.headers.get("x-api-key")
            import json

            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"successful": True, "data": {"results": ["btc up"]}, "error": None},
            )

        p = ComposioToolProvider(
            api_key="secret-key", user_id="u-1", http_client=_client(handler)
        )
        res = await p.execute("TAVILY_SEARCH", {"query": "btc"})
        assert res.status == "completed"
        assert res.output == {"results": ["btc up"]}
        # The slug is in the path, key in the header, user_id + arguments in body.
        assert captured["url"].endswith("/tools/execute/TAVILY_SEARCH")
        assert captured["x_api_key"] == "secret-key"
        assert captured["body"] == {"arguments": {"query": "btc"}, "user_id": "u-1"}

    async def test_api_error_status_maps_to_failed(self):
        def handler(request):
            return httpx.Response(401, json={"error": "invalid api key"})

        p = ComposioToolProvider(api_key="bad", http_client=_client(handler))
        res = await p.execute("TAVILY_SEARCH", {"query": "x"})
        assert res.status == "failed"
        assert "invalid api key" in res.error

    async def test_successful_false_maps_to_failed(self):
        def handler(request):
            return httpx.Response(200, json={"successful": False, "error": "rate limited"})

        p = ComposioToolProvider(api_key="k", http_client=_client(handler))
        res = await p.execute("TAVILY_SEARCH", {"query": "x"})
        assert res.status == "failed"
        assert res.error == "rate limited"

    async def test_transport_failure_maps_to_failed(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        p = ComposioToolProvider(api_key="k", http_client=_client(handler))
        res = await p.execute("TAVILY_SEARCH", {"query": "x"})
        assert res.status == "failed"
        assert "failed" in res.error.lower()

    async def test_off_allowlist_tool_is_rejected(self):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("off-allowlist tool should not hit the network")

        p = ComposioToolProvider(
            api_key="k", tools=["TAVILY_SEARCH"], http_client=_client(handler)
        )
        res = await p.execute("GITHUB_CREATE_ISSUE", {})
        assert res.status == "failed"
        assert "allowlist" in res.error

    async def test_schema_tolerant_missing_fields(self):
        # No "successful"/"error"/"data" keys — tolerate and surface the body.
        def handler(request):
            return httpx.Response(200, json={"output": "hello"})

        p = ComposioToolProvider(api_key="k", http_client=_client(handler))
        res = await p.execute("TAVILY_SEARCH", {"query": "x"})
        assert res.status == "completed"
        assert res.output == {"output": "hello"}


class TestFromSettings:
    def test_from_settings_reads_config(self):
        s = Settings(
            composio_api_key="cfg-key",
            composio_user_id="me",
            composio_tools="TAVILY_SEARCH, GITHUB_CREATE_ISSUE",
        )
        p = ComposioToolProvider.from_settings(s)
        assert p.available() is True
        names = [t.name for t in p.list_tools()]
        assert names == ["TAVILY_SEARCH", "GITHUB_CREATE_ISSUE"]
