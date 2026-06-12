"""Tool contract core tests (Decision 11) — fully in-memory, no network.

Pin the contract: ``ToolRegistry.invoke`` is the only execution path; it emits
``tool.requested`` then ``tool.completed`` / ``tool.failed`` / ``tool.denied``;
side-effectful tools round-trip the approval gate (deny => no execution); the
completion event carries a deterministic fact hint that consolidation folds into
the graph with a receipt; secrets in tool output are redacted before persistence;
and a registry with zero providers is honest-unavailable.
"""

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.db import Database
from centri.memory_graph import MemoryGraph
from centri.tools import ToolProvider, ToolRegistry, ToolResult, ToolSpec, is_read_only_slug


class _FakeProvider(ToolProvider):
    """A configurable in-memory provider for contract tests."""

    name = "fake"

    def __init__(self, available: bool = True, output: Any = "ok", fail: bool = False):
        self._available = available
        self._output = output
        self._fail = fail
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return "" if self._available else "fake:unavailable:no-key"

    def list_tools(self) -> List[ToolSpec]:
        return [
            ToolSpec(name="FAKE_SEARCH", provider=self.name, description="read-only search", side_effectful=False),
            ToolSpec(name="FAKE_WRITE", provider=self.name, description="side-effectful write", side_effectful=True),
        ]

    async def execute(self, name: str, arguments: Dict[str, Any], **kwargs: Any) -> ToolResult:
        self.calls.append({"name": name, "arguments": arguments})
        if self._fail:
            return ToolResult(status="failed", error="boom")
        return ToolResult(status="completed", output=self._output, raw={"echo": arguments})


@pytest.fixture
async def db():
    tmpdir = tempfile.mkdtemp()
    database = Database(Path(tmpdir) / "state.db")
    yield database
    await database.close()


async def _event_types(db: Database) -> List[str]:
    rows = await db.recent_events(limit=100)
    # recent_events is newest-first; return oldest-first for trail-order asserts.
    return [r["type"] for r in reversed(rows)]


class TestReadOnlyClassification:
    def test_search_get_list_are_read_only(self):
        assert is_read_only_slug("TAVILY_SEARCH")
        assert is_read_only_slug("GITHUB_GET_REPO")
        assert is_read_only_slug("X_LIST_ITEMS")
        assert is_read_only_slug("FETCH_URL")

    def test_others_are_side_effectful(self):
        assert not is_read_only_slug("GITHUB_CREATE_ISSUE")
        assert not is_read_only_slug("SLACK_SEND_MESSAGE")
        assert not is_read_only_slug("")


class TestRegistry:
    async def test_zero_providers_is_honest_unavailable(self, db):
        reg = ToolRegistry(db)
        assert reg.list_tools() == []
        res = await reg.invoke("ANYTHING")
        assert res["result"]["status"] == "unavailable"
        assert "unknown tool" in res["result"]["error"]

    async def test_list_tools_flags_unavailable_with_reason(self, db):
        reg = ToolRegistry(db)
        reg.register(_FakeProvider(available=False))
        tools = reg.list_tools()
        assert tools and all(t["available"] is False for t in tools)
        assert all(t["reason"] == "fake:unavailable:no-key" for t in tools)

    async def test_unavailable_provider_never_executes(self, db):
        reg = ToolRegistry(db)
        provider = _FakeProvider(available=False)
        reg.register(provider)
        res = await reg.invoke("FAKE_SEARCH")
        assert res["result"]["status"] == "unavailable"
        assert provider.calls == []  # never faked success
        types = await _event_types(db)
        assert types == ["tool.requested", "tool.failed"]


class TestReadOnlyPath:
    async def test_read_only_skips_gate_and_completes(self, db):
        reg = ToolRegistry(db)
        reg.register(_FakeProvider(output="result text"))

        gate_calls: List[Dict[str, Any]] = []

        async def gate(payload):
            gate_calls.append(payload)
            return "deny"

        res = await reg.invoke("FAKE_SEARCH", {"query": "btc"}, approval_gate=gate)
        assert res["result"]["status"] == "completed"
        assert res["result"]["output"] == "result text"
        assert gate_calls == []  # read-only never touches the gate
        types = await _event_types(db)
        assert types == ["tool.requested", "tool.completed"]


class TestSideEffectGating:
    async def test_denied_does_not_execute(self, db):
        reg = ToolRegistry(db)
        provider = _FakeProvider()
        reg.register(provider)

        async def gate(payload):
            return "deny"

        res = await reg.invoke("FAKE_WRITE", {"x": 1}, approval_gate=gate)
        assert res["result"]["status"] == "failed"
        assert provider.calls == []
        types = await _event_types(db)
        assert types == ["tool.requested", "tool.denied"]

    async def test_no_gate_denies_side_effectful(self, db):
        reg = ToolRegistry(db)
        provider = _FakeProvider()
        reg.register(provider)
        res = await reg.invoke("FAKE_WRITE", {"x": 1})  # no gate supplied
        assert res["result"]["status"] == "failed"
        assert provider.calls == []

    async def test_allowed_executes(self, db):
        reg = ToolRegistry(db)
        provider = _FakeProvider()
        reg.register(provider)

        async def gate(payload):
            assert payload["tool"] == "FAKE_WRITE"
            assert payload["risk"] == "high"
            return "allow"

        res = await reg.invoke("FAKE_WRITE", {"x": 1}, approval_gate=gate)
        assert res["result"]["status"] == "completed"
        assert provider.calls == [{"name": "FAKE_WRITE", "arguments": {"x": 1}}]
        types = await _event_types(db)
        assert types == ["tool.requested", "tool.completed"]


class TestFailurePath:
    async def test_provider_failure_emits_tool_failed(self, db):
        reg = ToolRegistry(db)
        reg.register(_FakeProvider(fail=True))
        res = await reg.invoke("FAKE_SEARCH", {"query": "x"})
        assert res["result"]["status"] == "failed"
        assert res["result"]["error"] == "boom"
        types = await _event_types(db)
        assert types == ["tool.requested", "tool.failed"]


class TestFactHintFolding:
    async def test_completed_fact_hint_folds_into_graph(self, db):
        graph = MemoryGraph(db)
        worker = Consolidator(db, graph)
        reg = ToolRegistry(db)
        reg.register(_FakeProvider(output="Bitcoin is at $70k"))

        res = await reg.invoke("FAKE_SEARCH", {"query": "btc price"})
        assert res["result"]["status"] == "completed"

        # Replay the spine through consolidation (same path production uses).
        n = await worker.rebuild_from_events()
        assert n >= 1
        facts = await graph.current_facts()
        match = [f for f in facts if f.topic == "tool:fake:FAKE_SEARCH"]
        assert len(match) == 1
        assert "Bitcoin is at $70k" in match[0].statement
        assert match[0].source_event_id  # receipt back to the spine
        assert "tool" in match[0].tags and "fake" in match[0].tags

    async def test_failed_invocation_writes_no_fact(self, db):
        graph = MemoryGraph(db)
        worker = Consolidator(db, graph)
        reg = ToolRegistry(db)
        reg.register(_FakeProvider(fail=True))
        await reg.invoke("FAKE_SEARCH", {"query": "x"})
        await worker.rebuild_from_events()
        facts = await graph.current_facts()
        assert not any(f.topic.startswith("tool:") for f in facts)


class TestRedaction:
    async def test_secret_in_output_is_redacted_before_persistence(self, db):
        reg = ToolRegistry(db)
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        reg.register(_FakeProvider(output=f"your key is {secret}"))
        await reg.invoke("FAKE_SEARCH", {"query": "x"})
        rows = await db.recent_events(limit=10)
        blob = "".join(r["payload_json"] for r in rows)
        assert secret not in blob  # redaction ran before the ledger write
