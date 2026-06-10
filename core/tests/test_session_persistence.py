"""Verify OpenCode session UID and artifacts survive restart.

pytest: python -m pytest tests/test_session_persistence.py -v
"""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.schemas import HandoffRequest, ContextPacket, HandoffResult
from centri.jobs import Jobs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _FakeHand:
    """Fake hand that returns a session_uid + artifacts — no real CLI."""

    async def start_task(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(
            status="completed",
            summary="Patched auth middleware.",
            session_uid="sess-abc123",
            artifacts=[
                {"type": "file_diff", "title": "auth.patch", "lines": 12},
                {"type": "stdout", "title": "stdout", "text": "OK done"},
            ],
            events_to_record=[
                {"type": "opencode.run", "session_uid": "sess-abc123", "exit_code": 0},
            ],
        )

    async def execute(self, request: HandoffRequest) -> HandoffResult:
        return await self.start_task(request)


class FakeHands:
    """Minimal container matching what Jobs expects."""

    def __init__(self):
        self._hand = _FakeHand()

    async def execute(self, request: HandoffRequest) -> HandoffResult:
        return await self._hand.execute(request)


class FakeEventBus:
    def __init__(self):
        self.events = []

    async def publish(self, event: dict):
        self.events.append(event)


class FakePermissions:
    def assert_allowed(self, action, ctx):
        return "ok"


@pytest.fixture
async def tmp_db():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_session_uid_persisted(tmp_db: Database):
    db = tmp_db
    await db.create_thread("th-1", "patch auth", "patch auth middleware", created_at=_now(), updated_at=_now())
    await db.create_task("tk-1", "th-1", "patch auth middleware", created_at=_now(), updated_at=_now())

    hands = FakeHands()
    event_bus = FakeEventBus()
    jobs = Jobs(db, hands, FakePermissions(), event_bus=event_bus)

    handoff = HandoffRequest(
        id="hof-1",
        to_capability="coding.start_task",
        user_intent="patch auth",
        context=ContextPacket(),
    )
    await jobs.start(handoff, task_id="tk-1")

    # Poll until completion
    for _ in range(60):
        task = await db.get_task("tk-1")
        if task["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)

    assert task["status"] == "completed"
    assert task["session_uid"] == "sess-abc123"


@pytest.mark.asyncio
async def test_artifact_events_persisted(tmp_db: Database):
    db = tmp_db
    await db.create_thread("th-1", "patch auth", "patch auth middleware", created_at=_now(), updated_at=_now())
    await db.create_task("tk-1", "th-1", "patch auth middleware", created_at=_now(), updated_at=_now())

    hands = FakeHands()
    event_bus = FakeEventBus()
    jobs = Jobs(db, hands, FakePermissions(), event_bus=event_bus)

    handoff = HandoffRequest(
        id="hof-1",
        to_capability="coding.start_task",
        user_intent="patch auth",
        context=ContextPacket(),
    )
    await jobs.start(handoff, task_id="tk-1")

    for _ in range(60):
        task = await db.get_task("tk-1")
        if task["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)

    events = await db.recent_events(limit=20, thread_id=None)
    payloads = [json.loads(e["payload_json"]) for e in events]
    artifact_payloads = [p for p in payloads if p.get("title") == "auth.patch" or p.get("type") == "file_diff"]
    assert len(artifact_payloads) >= 1


@pytest.mark.asyncio
async def test_recover_on_boot_marks_stale(tmp_db: Database):
    db = tmp_db
    # Inject a running task without a supervisor
    await db.create_thread("th-2", "old task", "old", created_at=_now(), updated_at=_now())
    await db.create_task("tk-2", "th-2", "old task", created_at=_now(), updated_at=_now())
    await db.update_task("tk-2", status="running", updated_at=_now())

    event_bus = FakeEventBus()
    hands = FakeHands()
    jobs = Jobs(db, hands, FakePermissions(), event_bus=event_bus)

    await jobs.recover_on_boot()
    task = await db.get_task("tk-2")
    assert task["status"] == "stale"
    assert "Recovered on boot" in (task.get("error") or "")
    assert any(e["type"] == "task.recovered" for e in event_bus.events)


if __name__ == "__main__":
    import asyncio
    # Run inline for quick standalone testing
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    asyncio.run(test_session_uid_persisted(db))
    asyncio.run(test_artifact_events_persisted(db))
    asyncio.run(test_recover_on_boot_marks_stale(db))
    print("\n=== Session persistence tests PASSED ===")
