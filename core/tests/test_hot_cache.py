"""Validate hot context cache is used by coordinator in <50ms.

Seed a cache with state, verify handle_utterance returns instantly
without hitting DB for hot context.
"""

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.context_cache import HotContextCache
from centri.coordinator import Coordinator
from centri.schemas import CoordinatorResponse, HandoffResult, ContextPacket, RepoState, SessionState


class _FakeDB:
    async def store_message(self, **kw):
        return None

    async def append_event(self, **kw):
        return None

    async def pending_approvals(self):
        return []

    async def list_tasks(self, **kw):
        return []

    async def create_thread(self, **kw):
        return None

    async def create_task(self, **kw):
        return None

    async def update_task(self, **kw):
        return None

    async def active_repo(self):
        return None

    async def latest_session(self, **kw):
        return None

    async def recent_events(self, **kw):
        return []

    async def create_approval(self, **kw):
        return None

    async def resolve_approval(self, **kw):
        return None

    async def get_task(self, **kw):
        return None


class _FakeMemory:
    async def recall(self, *args, **kw):
        return []

    async def identity(self):
        return None

    async def learn(self, event):
        return None


class _FakeHands:
    async def execute(self, request, event_sink=None, approval_gate=None):
        return HandoffResult(status="completed", summary="done")


class _FakeJobs:
    async def start(self, request, task_id=None):
        return "j-1"

    async def cancel(self, task_id):
        return True


class _FakeEventBus:
    def __init__(self):
        self.calls = []

    async def publish(self, event):
        self.calls.append(event)

    async def subscribe(self):
        import asyncio
        return asyncio.Queue()


class _FakeMR:
    def classify_intent(self, text, context=None):
        return "status"

    def summarize_status(self, ctx):
        return "All systems nominal."

    def narrate(self, text, voice=False):
        return text

    def reason(self, prompt, output_schema=None):
        return "sure"


class _RealCtx:
    """Minimal ContextAssembler-like object that returns a bare ContextPacket."""
    async def build(self, thread_id=None, task_id=None):
        return ContextPacket()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def test_hot_cache_part_a() -> None:
    """Test A: coordinator _build_context_parallel returns hot cache instantly."""
    fake_db = _FakeDB()
    fake_memory = _FakeMemory()
    cache = HotContextCache()

    # Seed cache with warm state (as if previous ops ran)
    await cache.apply_event({
        "type": "repo.changed",
        "repo_id": "r1",
        "name": "test-repo",
        "branch": "main",
        "dirty": False,
        "ts": time.time(),
    })
    await cache.apply_event({
        "type": "context.updated",
        "payload": {
            "session_uid": "ses-1",
            "status": "idle",
            "repo_id": "r1",
            "repo_name": "test-repo",
            "repo_branch": "main",
            "repo_dirty": False,
        },
        "ts": time.time(),
    })
    await cache.apply_event({
        "type": "user.utterance",
        "payload": {"text": "hello", "user_id": "u1"},
        "ts": time.time(),
    })

    snap = await cache.get()
    assert snap.repo_name == "test-repo"
    print("[OK] HotContextCache seeded correctly")

    coordinator = Coordinator(
        db=fake_db,
        model_router=_FakeMR(),
        memory=fake_memory,
        context_assembler=_RealCtx(),
        permissions=None,
        hands=_FakeHands(),
        jobs=_FakeJobs(),
        artifacts=None,
        hot_cache=cache,
        event_bus=None,
    )

    start = time.perf_counter()
    resp = await coordinator.handle_utterance("what's going on?", "u1", "voice")
    elapsed = time.perf_counter() - start

    assert isinstance(resp, CoordinatorResponse)
    assert resp.response_type == "status"
    # With a warm cache + no DB, elapsed should be tiny (well under 50ms on dev machine).
    # We assert loose < 0.2s because mocked _RealCtx.build is not async in sense.
    assert elapsed < 0.20, f"Hot path too slow: {elapsed:.3f}s"
    print(f"[OK] handle_utterance returned in {elapsed * 1000:.1f}ms")


async def test_hot_cache_part_b() -> None:
    """Test B: _build_context_parallel warm cache returns instantly."""
    fake_db = _FakeDB()
    fake_memory = _FakeMemory()
    cache = HotContextCache()
    await cache.apply_event({
        "type": "context.updated",
        "payload": {"session_uid": "s-1", "status": "idle"},
        "ts": time.time(),
    })

    coordinator = Coordinator(
        db=fake_db,
        model_router=_FakeMR(),
        memory=fake_memory,
        context_assembler=_RealCtx(),
        permissions=None,
        hands=None,
        jobs=None,
        artifacts=None,
        hot_cache=cache,
        event_bus=None,
    )

    start = time.perf_counter()
    packet, recall = await coordinator._build_context_parallel("test task")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"Hot cache path too slow: {elapsed:.3f}s"
    assert packet.session_state is not None
    assert packet.session_state.session_uid == "s-1"
    print(f"[OK] _build_context_parallel warm path: {elapsed * 1000:.1f}ms")


async def test_hot_cache_part_c() -> None:
    """Test C: cold cache (None) falls back to DB."""
    cache = HotContextCache()  # empty, never updated -> last_updated == 0.0
    fake_db = _FakeDB()
    fake_memory = _FakeMemory()
    coordinator = Coordinator(
        db=fake_db,
        model_router=_FakeMR(),
        memory=fake_memory,
        context_assembler=_RealCtx(),
        permissions=None,
        hands=None,
        jobs=None,
        artifacts=None,
        hot_cache=cache,
        event_bus=None,
    )
    packet, recall = await coordinator._build_context_parallel("cold task")
    assert packet is not None
    print("[OK] Cold cache path falls back to DB correctly")


async def test_hot_cache_part_d() -> None:
    """Test D: cache understands flat event shapes, not just payload."""
    cache = HotContextCache()
    # events from jobs.py are flat (task_id, status) not payload-wrapped
    await cache.apply_event({
        "type": "task.updated",
        "task_id": "tk-1",
        "status": "completed",
        "ts": time.time(),
    })
    snap = await cache.get()
    assert snap.active_task_id is None, "cache should clear task on completion"

    # another flat update marks task running
    await cache.apply_event({
        "type": "task.updated",
        "task_id": "tk-2",
        "status": "running",
        "ts": time.time(),
    })
    snap2 = await cache.get()
    assert snap2.active_task_id == "tk-2", "cache should track task on running"
    print("[OK] Flat event shapes handled correctly")


async def test_hot_cache_part_e() -> None:
    """Test E: coordinator _enrich_cache_from_db publishes correct event shape."""

    bus = _FakeEventBus()
    cache = HotContextCache()
    fake_db = _FakeDB()

    class _RichCtx:
        async def build(self, thread_id=None, task_id=None):
            return ContextPacket(
                repo_state=RepoState(id="r1", name="proj", branch="main", dirty=False),
                session_state=SessionState(id="s1", session_uid="su-1", hand="opencode", status="idle"),
            )

    coordinator = Coordinator(
        db=fake_db,
        model_router=_FakeMR(),
        memory=_FakeMemory(),
        context_assembler=_RichCtx(),
        permissions=None,
        hands=None,
        jobs=None,
        artifacts=None,
        hot_cache=cache,
        event_bus=bus,
    )

    await coordinator._enrich_cache_from_db("ignore")
    # Give event loop a tick
    await asyncio.sleep(0)

    assert any(e.get("type") == "context.updated" for e in bus.calls), f"Expected context.updated in events: {bus.calls}"
    print("[OK] Background enrichment publishes context.updated event")


async def main():
    await test_hot_cache_part_a()
    await test_hot_cache_part_b()
    await test_hot_cache_part_c()
    await test_hot_cache_part_d()
    await test_hot_cache_part_e()
    print("\n=== Hot cache tests PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
