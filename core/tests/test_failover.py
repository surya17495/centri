"""Piece A3 — end-to-end failover drill.

The default coding hand is ACP (OpenCode-over-ACP); the OpenCode subprocess hand
is the degraded fallback (Decision 2). This drill proves the whole chain through
the real ``Jobs`` + ``Hands`` + ``Database`` stack:

  ACP hand healthy -> task delegated -> ACP process dies mid-task ->
  router degrades to the OpenCode fallback -> task ends with a real terminal
  status, an honest event trail (``hand.degraded`` names who failed and why),
  and NO orphaned task stuck in ``running``.

The hands here are in-process fakes implementing the real ``Hand`` ABC so the
drill is deterministic and offline; the routing/degradation logic under test is
the real ``Hands.execute`` and the real ``Jobs._run_job`` lifecycle.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database  # noqa: E402
from centri.config import Settings  # noqa: E402
from centri.hands import Hands  # noqa: E402
from centri.hands.base import Hand, HandHealth  # noqa: E402
from centri.jobs import Jobs  # noqa: E402
from centri.permissions import Permissions  # noqa: E402
from centri.schemas import HandCapability, HandoffRequest, HandoffResult  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _FakeAcpHand(Hand):
    """Healthy ACP hand whose process is 'killed' mid-task: it streams one
    progress event, then raises as if the subprocess vanished."""

    def __init__(self) -> None:
        self.executed = False

    async def capabilities(self) -> List[HandCapability]:
        return [HandCapability(name="coding.start_task", risk="medium", configured=True, healthy=True)]

    async def health(self) -> HandHealth:
        return HandHealth(healthy=True, reason="acp ready")

    async def execute(self, request, event_sink=None, approval_gate=None) -> HandoffResult:
        self.executed = True
        if event_sink is not None:
            await event_sink({"type": "hand.progress", "source": "hand", "summary": "ACP session started", "session_uid": "ses-acp-1"})
        # The ACP process is killed mid-turn: the stdio stream closes, the hand
        # raises (mirrors the A2 crash path bubbling out of execute()).
        raise ConnectionResetError("ACP agent process killed mid-task")

    async def cancel(self, task_id: str) -> bool:
        return True


class _FakeOpenCodeHand(Hand):
    """Healthy OpenCode subprocess fallback that completes the task."""

    def __init__(self) -> None:
        self.executed = False

    async def capabilities(self) -> List[HandCapability]:
        return [HandCapability(name="coding.start_task", risk="medium", configured=True, healthy=True)]

    async def health(self) -> HandHealth:
        return HandHealth(healthy=True, reason="opencode ready")

    async def execute(self, request, event_sink=None, approval_gate=None) -> HandoffResult:
        self.executed = True
        if event_sink is not None:
            await event_sink({"type": "hand.progress", "source": "hand", "summary": "OpenCode subprocess started"})
        return HandoffResult(
            status="completed",
            summary="OpenCode fallback finished the task.",
            session_uid="oc-sess-1",
            events_to_record=[{"type": "hand.completed", "source": "hand", "stop_reason": "end_turn"}],
        )

    async def cancel(self, task_id: str) -> bool:
        return False


class _UnavailableOpenCodeHand(_FakeOpenCodeHand):
    """OpenCode fallback that is NOT installed: honest-unavailable."""

    async def health(self) -> HandHealth:
        return HandHealth(healthy=False, reason="opencode CLI not found on PATH")

    async def execute(self, request, event_sink=None, approval_gate=None) -> HandoffResult:
        self.executed = True
        return HandoffResult(status="unavailable", summary="opencode CLI not found on PATH")


@pytest.fixture
async def tmp_db():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield db
    await db.close()


def _hands_with(acp: Hand, opencode: Hand) -> Hands:
    """Build a real Hands router (acp first per default priority) but swap in
    in-process fake hands so the drill is deterministic and offline."""
    hands = Hands(Settings(enabled_hands=["acp", "opencode"], hand_priority=["acp", "opencode"]), db=None)
    hands._hands = {"acp": acp, "opencode": opencode}
    return hands


async def _drive_task(db: Database, hands: Hands) -> tuple[str, List[Dict[str, Any]]]:
    """Run one coding task end to end through Jobs and return (task_id, events)."""
    perms = Permissions(Settings())
    jobs = Jobs(db=db, hands=hands, permissions=perms, event_bus=None, memory=None)
    task_id = "tk-failover"
    await db.create_task(task_id, "th-1", "ship the feature", created_at=_now(), updated_at=_now())
    req = HandoffRequest(id=task_id, to_capability="coding.start_task", user_intent="ship the feature")
    await jobs.start(req, task_id)
    # Wait for the background job to reach a terminal state.
    import asyncio
    for _ in range(200):
        t = await db.get_task(task_id)
        if t and t["status"] in ("completed", "failed", "cancelled", "stale"):
            break
        await asyncio.sleep(0.02)
    rows = await db.recent_events(limit=200)
    events = []
    for r in rows:
        e = dict(r)
        try:
            e["payload"] = json.loads(r.get("payload_json") or "{}")
        except Exception:
            e["payload"] = {}
        events.append(e)
    return task_id, events


async def test_failover_degrades_acp_to_opencode(tmp_db: Database):
    """ACP dies mid-task -> router degrades to the OpenCode fallback -> task
    completes, with an honest degrade event and no orphaned running task."""
    acp = _FakeAcpHand()
    opencode = _FakeOpenCodeHand()
    hands = _hands_with(acp, opencode)

    task_id, events = await _drive_task(tmp_db, hands)

    # Both hands ran: ACP first (and crashed), OpenCode picked up the work.
    assert acp.executed is True
    assert opencode.executed is True

    types = [e["type"] for e in events]
    # Honest event trail: the degrade was recorded, naming the failed hand.
    assert "hand.degraded" in types
    degrade = next(e for e in events if e["type"] == "hand.degraded")
    payload = degrade.get("payload") or {}
    assert payload.get("failed_hand") == "acp"
    assert payload.get("fallback_hand") == "opencode"
    assert payload.get("failed_status") == "error"

    # The task ended COMPLETED (the fallback finished it) — not stuck running.
    final = await tmp_db.get_task(task_id)
    assert final["status"] == "completed"
    assert "OpenCode fallback" in (final.get("result") or "")
    assert "task.completed" in types

    # No orphaned running task anywhere in the ledger's terminal state.
    running = await tmp_db.list_tasks(status="running")
    assert running == []


async def test_failover_fails_honestly_when_no_fallback(tmp_db: Database):
    """ACP dies mid-task and the OpenCode fallback is unavailable: the task
    fails honestly (no fake success), event trail intact, no orphaned task."""
    acp = _FakeAcpHand()
    opencode = _UnavailableOpenCodeHand()
    hands = _hands_with(acp, opencode)

    task_id, events = await _drive_task(tmp_db, hands)

    assert acp.executed is True
    # The unavailable fallback was still tried (honest attempt), then surfaced.
    assert opencode.executed is True

    types = [e["type"] for e in events]
    # A degrade was attempted/recorded, but the chain ended unavailable.
    assert "hand.degraded" in types
    final = await tmp_db.get_task(task_id)
    assert final["status"] == "failed"
    # No fake success.
    assert "task.completed" not in types
    assert "task.failed" in types
    # No orphaned running task.
    assert await tmp_db.list_tasks(status="running") == []
