"""Tests for the real ACP client hand against a scripted fake ACP agent.

The fake agent (``acp_fake_agent.py``) is launched as a real subprocess and speaks
JSON-RPC over stdio, so these tests exercise the actual wire protocol: lifecycle
(initialize / session/new / session/prompt), streaming session updates mapped to
events, the permission round-trip, and cancellation.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.hands.acp import AcpHand  # noqa: E402
from centri.schemas import ContextPacket, HandoffRequest, RepoState  # noqa: E402

_FAKE = Path(__file__).parent / "acp_fake_agent.py"


def _command(mode: str) -> str:
    # Set the mode via env on the spawned process by wrapping in python -c is
    # awkward; instead the hand inherits our env, so we set it per-test.
    os.environ["ACP_FAKE_MODE"] = mode
    return f"{sys.executable} {_FAKE}"


def _request(tmp_path: Path) -> HandoffRequest:
    packet = ContextPacket(repo_state=RepoState(id="r1", name="proj", root=str(tmp_path)))
    return HandoffRequest(id="hof-test-1", to_capability="coding.start_task", user_intent="do the thing", context=packet)


async def test_health_unavailable_without_command():
    hand = AcpHand(command=None)
    health = await hand.health()
    assert health.healthy is False
    assert "no ACP command" in health.reason


async def test_health_healthy_with_resolvable_command():
    hand = AcpHand(command=_command("stream"))
    health = await hand.health()
    assert health.healthy is True


async def test_lifecycle_and_streaming(tmp_path):
    hand = AcpHand(command=_command("stream"))
    events = []

    async def sink(ev):
        events.append(ev)

    result = await hand.execute(_request(tmp_path), event_sink=sink)

    assert result.status == "completed"
    assert "Working on it." in result.summary
    assert "Done." in result.summary
    assert result.session_uid == "sess-fake-1"

    types = [e["type"] for e in events]
    # Streamed progress arrived live (not just at completion).
    assert "task.progress" in types
    assert "hand.progress" in types
    # The tool_call_update completed produced an artifact.
    assert any(a["type"] == "tool_result" for a in result.artifacts)
    # A session-started progress event carried the session uid.
    assert any(e.get("session_uid") == "sess-fake-1" for e in events)


async def test_permission_round_trip_allow(tmp_path):
    hand = AcpHand(command=_command("permission"))
    events = []
    gate_calls = []

    async def sink(ev):
        events.append(ev)

    async def gate(payload):
        gate_calls.append(payload)
        return "allow"

    result = await hand.execute(_request(tmp_path), event_sink=sink, approval_gate=gate)

    assert result.status == "completed"
    # The gate was consulted with the destructive tool's details.
    assert gate_calls and gate_calls[0]["tool"] == "rm -rf build"
    # An approval.requested event was streamed for the UI.
    assert any(e["type"] == "approval.requested" for e in events)
    # The agent acknowledged the allow outcome in its final message.
    assert "selected" in result.summary


async def test_permission_round_trip_deny(tmp_path):
    hand = AcpHand(command=_command("permission"))

    async def gate(payload):
        return "deny"

    result = await hand.execute(_request(tmp_path), approval_gate=gate)
    assert result.status == "completed"
    assert "selected" in result.summary  # deny still maps to a selected optionId


async def test_cancel(tmp_path):
    hand = AcpHand(command=_command("cancel"))
    req = _request(tmp_path)

    task = asyncio.create_task(hand.execute(req))
    # Give the turn time to start and register its connection.
    await asyncio.sleep(0.5)
    cancelled = await hand.cancel(req.id)
    assert cancelled is True

    result = await asyncio.wait_for(task, timeout=10.0)
    assert result.status == "cancelled"


async def test_transcript_event_keeps_full_text(tmp_path, monkeypatch):
    """Phase 3b.1: the spine receives the verbatim turn, not the 240-char UI cut."""
    monkeypatch.setenv("ACP_FAKE_LONG", "1")
    hand = AcpHand(command=_command("stream"))
    events = []

    async def sink(ev):
        events.append(ev)

    result = await hand.execute(_request(tmp_path), event_sink=sink)
    assert result.status == "completed"

    transcripts = [e for e in result.events_to_record if e["type"] == "hand.transcript"]
    assert len(transcripts) == 1
    t = transcripts[0]
    # Full text is untruncated (>240 chars) and contains both ends of the turn.
    assert len(t["text"]) > 240
    assert "Working on it." in t["text"]
    assert "Detailed transcript sentence" in t["text"]
    assert t["session_uid"] == "sess-fake-1"
    assert t["stop_reason"] == "end_turn"
    # Tool activity was traced (call + completion update).
    ids = [c.get("tool_call_id") for c in t["tool_trace"]]
    assert "call_1" in ids
    statuses = [c.get("status") for c in t["tool_trace"]]
    assert "completed" in statuses
    # Deterministic fact hint so consolidation learns about delegated work.
    fact = t["fact"]
    assert fact["topic"] == "delegated-session:sess-fake-1"
    assert "do the thing" in fact["statement"]
    assert fact["tags"] == ["hand", "transcript", "acp"]
    # Live UI summaries stay short; the transcript precedes hand.completed.
    for ev in events:
        if ev["type"] == "task.progress" and "summary" in ev:
            assert len(ev["summary"]) <= 240
    types = [e["type"] for e in result.events_to_record]
    assert types.index("hand.transcript") < types.index("hand.completed")
