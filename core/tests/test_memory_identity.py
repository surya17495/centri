"""Verify identity cache round-trips and task synthesis writes to DB.

pytest: python -m pytest tests/test_memory_identity.py -v
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
from centri.memory import Memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _FakeLettaResponse:
    def __init__(self, data):
        self.status_code = 200
        self._data = data

    def json(self):
        return self._data


class _FakeLettaClient:
    def __init__(self):
        self.posts = []

    async def post(self, path: str, json: dict):
        self.posts.append({"path": path, "json": json})
        if json["messages"][0]["text"].startswith("recall:"):
            return _FakeLettaResponse({"messages": [{"text": "[letta] prior project decision"}]})
        return _FakeLettaResponse({"ok": True})


@pytest.fixture
async def tmp_db():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_identity_cache_roundtrip(tmp_db: Database):
    db = tmp_db
    mem = Memory(db)
    identity = {
        "blocks": [{"label": "persona", "value": "fast operator"}],
        "persona": "fast operator",
        "human": "user",
        "agent_id": mem._agent_id,
    }
    await mem._save_local_identity(identity)
    cached = await mem._local_identity()
    assert cached["agent_id"] == mem._agent_id
    assert cached["persona"] == "fast operator"
    assert len(cached["blocks"]) == 1
    assert cached["degraded"] is True  # always flagged degraded when from local


@pytest.mark.asyncio
async def test_task_synthesis_writes_event(tmp_db: Database):
    db = tmp_db
    mem = Memory(db)
    event = {
        "type": "task.completed",
        "task_id": "tk-1",
        "description": "refactor auth middleware",
        "summary": "Finished auth refactor.",
    }
    await mem.learn(event)
    # Wait for async
    await asyncio.sleep(0.1)
    events = await db.recent_events(limit=10)
    synth = [e for e in events if json.loads(e["payload_json"]).get("synthesized_summary")]
    proc = [e for e in events if e["type"] == "procedural.memory"]
    assert len(synth) >= 1, f"Expected synthesized events, got {[e['type'] for e in events]}"
    data = json.loads(synth[0]["payload_json"])
    assert data["synthesized_summary"] == "refactor auth middleware (completed)"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_procedural_recall_returns_synthesized(tmp_db: Database):
    db = tmp_db
    mem = Memory(db)
    # Seed a synthesized event
    await db.append_event(
        event_id="synth-1",
        type="procedural.memory",
        source="memory.synthesis",
        ts=_now(),
        task_id="tk-1",
        payload={
            "action": "refactor auth middleware",
            "status": "completed",
            "outcome": "Clean build.",
            "synthesized_summary": "refactor auth middleware (completed)",
        },
    )
    results = await mem._procedural_recall("refactor auth", limit=5)
    assert any("refactor auth middleware (completed)" in r for r in results)


@pytest.mark.asyncio
async def test_letta_recall_wins_over_local_fallback(tmp_db: Database):
    db = tmp_db
    await db.append_event(
        event_id="synth-1",
        type="procedural.memory",
        source="memory.synthesis",
        ts=_now(),
        payload={"synthesized_summary": "local fallback should not win"},
    )
    mem = Memory(db, letta_client=_FakeLettaClient())
    results = await mem.recall("project decision", limit=5)
    assert results == ["[letta] prior project decision"]


@pytest.mark.asyncio
async def test_task_memory_promotes_to_letta_when_available(tmp_db: Database):
    db = tmp_db
    fake_letta = _FakeLettaClient()
    mem = Memory(db, letta_client=fake_letta)
    await mem.learn(
        {
            "type": "task.completed",
            "task_id": "tk-1",
            "thread_id": "th-1",
            "description": "fix realtime voice path",
            "summary": "LiveKit owns voice.",
        }
    )
    events = await db.recent_events(limit=20)
    event_types = [event["type"] for event in events]
    assert "memory.promoted" in event_types
    assert "memory.fallback" not in event_types
    assert any("fix realtime voice path" in post["json"]["messages"][0]["text"] for post in fake_letta.posts)


if __name__ == "__main__":
    import asyncio
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    asyncio.run(test_identity_cache_roundtrip(db))
    asyncio.run(test_task_synthesis_writes_event(db))
    asyncio.run(test_procedural_recall_returns_synthesized(db))
    print("\n=== Memory identity tests PASSED ===")
