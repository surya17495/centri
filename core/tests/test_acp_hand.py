"""Tests for the real ACP client hand against a scripted fake ACP agent.

The fake agent (``acp_fake_agent.py``) is launched as a real subprocess and speaks
JSON-RPC over stdio, so these tests exercise the actual wire protocol: lifecycle
(initialize / session/new / session/prompt), streaming session updates mapped to
events, the permission round-trip, and cancellation.
"""

import asyncio
import os
import shutil
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


async def test_realish_update_kinds_are_handled(tmp_path, monkeypatch):
    """The real opencode binary emits update kinds the original fake never did:
    ``agent_thought_chunk``, ``available_commands_update``, ``usage_update``.

    Verified against the real binary (see ``test_real_opencode_acp_lifecycle``);
    this deterministic test pins the behavior so CI catches a regression even
    when the binary is absent. Reasoning is captured into the transcript's
    ``reasoning`` field but never leaks into the user-facing summary or fact.
    """
    monkeypatch.setenv("ACP_FAKE_REALISH", "1")
    hand = AcpHand(command=_command("stream"))
    events = []

    async def sink(ev):
        events.append(ev)

    result = await hand.execute(_request(tmp_path), event_sink=sink)
    assert result.status == "completed"
    # The unknown command/usage updates did not crash the turn.
    assert "Working on it." in result.summary
    assert "Done." in result.summary

    transcripts = [e for e in result.events_to_record if e["type"] == "hand.transcript"]
    assert len(transcripts) == 1
    t = transcripts[0]
    # Reasoning is captured for fidelity...
    assert "reasoning" in t
    assert "The user wants me to work on it." in t["reasoning"]
    # ...but never in the user-facing message text or fact statement.
    assert "The user wants me to work on it." not in t["text"]
    assert "The user wants me to work on it." not in t["fact"]["statement"]


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode binary not on PATH")
async def test_real_opencode_acp_lifecycle(tmp_path):
    """Integration test against the REAL ``opencode acp`` binary.

    Skipped when opencode is not installed. Drives the full lifecycle
    (initialize -> session/new -> session/prompt) through AcpHand and asserts a
    protocol-compatible, honest outcome. Either:
      - the turn completes (a model was resolvable in this environment), or
      - it fails/unavailable honestly (no model key) — never a hang or crash.
    The hand must not raise and must record an honest event trail.
    """
    hand = AcpHand(command="opencode acp")
    health = await hand.health()
    assert health.healthy is True

    events = []

    async def sink(ev):
        events.append(ev)

    req = HandoffRequest(
        id="hof-real-acp",
        to_capability="coding.start_task",
        user_intent="Reply with exactly the word PONG and do nothing else.",
        context=ContextPacket(repo_state=RepoState(id="r1", name="proj", root=str(tmp_path))),
    )
    result = await asyncio.wait_for(hand.execute(req, event_sink=sink), timeout=120.0)

    # Honest outcome: a real protocol status, never an unhandled crash.
    assert result.status in ("completed", "failed", "error", "unavailable")
    # A real session was negotiated and its uid carried on the spine.
    assert any(e.get("type") == "hand.progress" and e.get("session_uid") for e in events)
    # The turn was recorded honestly (transcript + completion when it ran).
    recorded = [e["type"] for e in result.events_to_record]
    if result.status == "completed":
        assert result.session_uid and result.session_uid.startswith("ses")
        assert "hand.transcript" in recorded
        assert "hand.completed" in recorded
