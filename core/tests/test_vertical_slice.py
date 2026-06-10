"""Milestone A vertical slice through the real FastAPI /utterance endpoint."""

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import centri.app as app_module
from centri.briefing import BriefingBuilder
from centri.context_cache import HotContextCache
from centri.coordinator import Coordinator
from centri.db import Database
from centri.event_bus import EventBus
from centri.jobs import Jobs
from centri.memory import Memory
from centri.runtime import Runtime
from centri.schemas import HandoffResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _FakeModelRouter:
    def narrate(self, text: str, voice: bool = True) -> str:
        return f"Started: {text}" if "Started task:" in text else text

    def summarize_status(self, context: str) -> str:
        return context or "idle"

    def reason(self, prompt: str, output_schema=None):
        return "OK."

    def classify_intent(self, text: str, context: str | None = None) -> str:
        return "coding_task"


class _AllowAllPermissions:
    def assert_allowed(self, action: str, ctx: dict) -> str:
        return "ok"


class _ExplodingContextAssembler:
    async def build(self, *args, **kwargs):
        raise AssertionError("hot path should not await DB context build")


class _FakeHands:
    def __init__(self):
        self.requests = []

    async def execute(self, request, event_sink=None, approval_gate=None):
        self.requests.append(request)
        if event_sink is not None:
            await event_sink({"type": "hand.progress", "summary": "fake stream start", "percent": 1})
        await asyncio.sleep(0.01)
        return HandoffResult(
            status="completed",
            summary="Applied the fake vertical-slice patch.",
            session_uid="sess-vertical-123",
            artifacts=[
                {"type": "file_diff", "title": "vertical_slice.patch", "summary": "One fake patch applied."},
                {"type": "stdout", "title": "run.log", "summary": "fake open code output"},
            ],
            events_to_record=[
                {"type": "task.progress", "summary": "Applying patch", "percent": 50},
                {"type": "hand.progress", "summary": "Fake OpenCode worker is active", "percent": 50},
            ],
        )


class _FakeNotifier:
    def __init__(self):
        self.completed = []
        self.failed = []
        self.approvals = []

    async def notify_task_completed(self, task_id: str, summary: str) -> None:
        self.completed.append({"task_id": task_id, "summary": summary})

    async def notify_task_failed(self, task_id: str, error: str) -> None:
        self.failed.append({"task_id": task_id, "error": error})

    async def notify_approval_request(self, approval_id: str, label: str, risk: str) -> None:
        self.approvals.append({"approval_id": approval_id, "label": label, "risk": risk})


def _fetch_one(db_path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _fetch_all(db_path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _build_runtime(db_path: Path) -> tuple[Runtime, _FakeHands, _FakeNotifier]:
    runtime = Runtime()
    runtime.event_bus = EventBus()
    runtime.db = Database(db_path)
    runtime.hot_cache = HotContextCache()
    runtime.memory = Memory(runtime.db, event_bus=runtime.event_bus)
    runtime.permissions = _AllowAllPermissions()
    runtime.hands = _FakeHands()
    runtime.jobs = Jobs(runtime.db, runtime.hands, runtime.permissions, runtime.event_bus, memory=runtime.memory)
    runtime.notifier = _FakeNotifier()
    runtime.coordinator = Coordinator(
        db=runtime.db,
        model_router=_FakeModelRouter(),
        memory=runtime.memory,
        context_assembler=_ExplodingContextAssembler(),
        permissions=runtime.permissions,
        hands=runtime.hands,
        jobs=runtime.jobs,
        artifacts=None,
        desktop=None,
        event_bus=runtime.event_bus,
        hot_cache=runtime.hot_cache,
        briefing_builder=BriefingBuilder(),
    )
    runtime._background_tasks = []

    async def boot() -> None:
        runtime._background_tasks.append(asyncio.create_task(runtime._event_cache_loop()))
        runtime._background_tasks.append(asyncio.create_task(runtime._notification_event_loop()))
        await asyncio.sleep(0)
        await runtime.event_bus.publish(
            {
                "type": "repo.changed",
                "ts": _now(),
                "repo_id": "repo-vertical",
                "name": "project-J",
                "branch": "main",
                "dirty": True,
                "payload": {
                    "repo_id": "repo-vertical",
                    "name": "project-J",
                    "branch": "main",
                    "dirty": True,
                },
            }
        )
        await asyncio.sleep(0.05)

    async def shutdown() -> None:
        for task in runtime._background_tasks:
            task.cancel()
        if runtime._background_tasks:
            await asyncio.gather(*runtime._background_tasks, return_exceptions=True)
        runtime._background_tasks.clear()
        await runtime.db.close()

    runtime.boot = boot  # type: ignore[method-assign]
    runtime.shutdown = shutdown  # type: ignore[method-assign]
    return runtime, runtime.hands, runtime.notifier


@pytest.mark.parametrize("user_text", ["Implement the hard vertical slice thoroughly"])
def test_vertical_slice_via_utterance_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_text: str):
    db_path = tmp_path / "vertical-slice.db"
    runtime, hands, notifier = _build_runtime(db_path)
    monkeypatch.setattr(app_module, "runtime", runtime)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/utterance",
            json={"text": user_text, "user_id": "tester", "source": "desktop_text"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["response_type"] == "task_created"

        task_id = body["data"]["task_id"]
        thread_id = body["data"]["thread_id"]

        assert _wait_until(
            lambda: (_fetch_one(db_path, "SELECT status FROM tasks WHERE id = ?", (task_id,)) or {}).get("status") == "completed"
        ), "task never completed"
        assert _wait_until(
            lambda: any(item["task_id"] == task_id for item in notifier.completed)
        ), "completion notification never fired"
        assert _wait_until(
            lambda: any(item["type"] == "memory.synthesized" for item in _fetch_all(db_path, "SELECT type FROM events WHERE task_id = ?", (task_id,)))
        ), "memory synthesis event never persisted"

        task_row = _fetch_one(
            db_path,
            "SELECT id, thread_id, status, description, hand, capability, session_uid, result FROM tasks WHERE id = ?",
            (task_id,),
        )
        thread_row = _fetch_one(db_path, "SELECT id, title, goal FROM threads WHERE id = ?", (thread_id,))
        session_row = _fetch_one(
            db_path,
            "SELECT session_uid, hand, status, summary FROM sessions WHERE session_uid = ?",
            ("sess-vertical-123",),
        )
        message_row = _fetch_one(
            db_path,
            "SELECT channel, user_id, direction, content FROM messages WHERE user_id = ? ORDER BY ts DESC LIMIT 1",
            ("tester",),
        )
        event_rows = _fetch_all(
            db_path,
            "SELECT type, task_id, payload_json FROM events WHERE task_id = ? ORDER BY ts ASC",
            (task_id,),
        )

        assert task_row is not None
        assert thread_row is not None
        assert session_row is not None
        assert message_row is not None
        assert task_row["thread_id"] == thread_id
        assert task_row["status"] == "completed"
        assert task_row["session_uid"] == "sess-vertical-123"
        assert task_row["hand"] == "opencode"
        assert task_row["capability"] == "coding.start_task"
        assert thread_row["goal"] == user_text
        assert session_row["status"] == "completed"
        assert session_row["hand"] == "opencode"
        assert message_row["content"] == user_text

        assert hands.requests, "fake hand was never invoked"
        handoff = hands.requests[0]
        assert handoff.to_capability == "coding.start_task"
        assert handoff.context is not None
        assert handoff.context.repo_state is not None
        assert handoff.context.repo_state.name == "project-J"
        assert handoff.context.repo_state.branch == "main"

        event_types = [row["type"] for row in event_rows]
        assert "task.started" in event_types
        assert "task.progress" in event_types
        assert "artifact.created" in event_types
        assert "task.completed" in event_types
        assert "task.updated" in event_types
        assert "memory.synthesized" in event_types
        assert "notification.sent" in event_types

        progress_payloads = [json.loads(row["payload_json"]) for row in event_rows if row["type"] == "task.progress"]
        artifact_payloads = [json.loads(row["payload_json"]) for row in event_rows if row["type"] == "artifact.created"]
        notification_payloads = [json.loads(row["payload_json"]) for row in event_rows if row["type"] == "notification.sent"]

        assert any(payload.get("percent") == 50 for payload in progress_payloads)
        assert any(payload.get("title") == "vertical_slice.patch" for payload in artifact_payloads)
        assert any(payload.get("kind") == "task.completed" for payload in notification_payloads)
        assert notifier.completed == [{"task_id": task_id, "summary": "Applied the fake vertical-slice patch."}]
