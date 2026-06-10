"""CENTRI jobs — long-running work lifecycle."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from centri.schemas import HandoffRequest

logger = logging.getLogger(__name__)


class Jobs:
    """Track long-running handoffs: start, cancel, resume, poll, recover."""

    def __init__(self, db: Any, hands: Any, permissions: Any, event_bus: Any = None, memory: Any = None):
        self._db = db
        self._hands = hands
        self._permissions = permissions
        self._event_bus = event_bus
        self._memory = memory
        self._running: Dict[str, asyncio.Task] = {}

    async def start(self, request: HandoffRequest, task_id: str) -> str:
        task = asyncio.create_task(self._run_job(task_id, request))
        self._running[task_id] = task
        task.add_done_callback(lambda _t: self._running.pop(task_id, None))
        return task_id

    async def _record_event(
        self,
        event_type: str,
        *,
        source: str = "jobs",
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = _now()
        body = dict(payload or {})
        event: Dict[str, Any] = {
            "type": event_type,
            "ts": ts,
            "source": source,
            "thread_id": thread_id,
            "task_id": task_id,
            "repo_id": repo_id,
            "payload": body,
        }
        for key in ("status", "summary", "session_uid", "error", "description", "percent", "title"):
            if key in body:
                event[key] = body[key]
        await self._db.append_event(
            event_id=f"evt-{task_id or 'global'}-{ts}",
            type=event_type,
            source=source,
            ts=ts,
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
            payload=body,
        )
        if self._event_bus:
            await self._event_bus.publish(event)

    async def _run_job(self, task_id: str, request: HandoffRequest) -> None:
        task_row = await self._db.get_task(task_id)
        thread_id = task_row.get("thread_id") if task_row else None
        description = task_row.get("description") if task_row else request.user_intent
        repo_id = None
        if request.context and getattr(request.context, "repo_state", None):
            repo_id = getattr(request.context.repo_state, "id", None)
        try:
            await self._db.update_task(task_id, status="running", updated_at=_now())
            result = await self._hands.execute(request)
            status = "completed" if result.status in ("completed", "ok", "steered") else "failed"
            for event in result.events_to_record:
                await self._record_event(
                    event.get("type", "job.result"),
                    source=str(event.get("source", "jobs")),
                    thread_id=thread_id,
                    task_id=task_id,
                    repo_id=repo_id,
                    payload=event,
                )
            # Persist session UID and artifacts if returned
            session_uid = result.session_uid
            await self._db.update_task(
                task_id=task_id,
                status=status,
                result=result.summary,
                error=result.summary if status == "failed" else None,
                session_uid=session_uid,
                updated_at=_now(),
                completed_at=_now(),
            )
            if session_uid:
                await self._db.upsert_session(
                    session_id=f"sess-{session_uid}",
                    session_uid=session_uid,
                    hand="opencode",
                    status=status,
                    repo_id=repo_id,
                    summary=result.summary,
                    last_seen=_now(),
                    payload={"task_id": task_id, "thread_id": thread_id},
                )
            # Persist artifact events (structured proof of work)
            if result.artifacts:
                for art in result.artifacts:
                    await self._record_event(
                        "artifact.created",
                        source="jobs",
                        thread_id=thread_id,
                        task_id=task_id,
                        repo_id=repo_id,
                        payload=art,
                    )
            terminal_payload = {
                "status": status,
                "summary": result.summary,
                "session_uid": session_uid,
                "description": description,
            }
            if status == "completed":
                await self._record_event(
                    "task.completed",
                    source="jobs",
                    thread_id=thread_id,
                    task_id=task_id,
                    repo_id=repo_id,
                    payload=terminal_payload,
                )
            else:
                terminal_payload["error"] = result.summary
                await self._record_event(
                    "task.failed",
                    source="jobs",
                    thread_id=thread_id,
                    task_id=task_id,
                    repo_id=repo_id,
                    payload=terminal_payload,
                )
            await self._record_event(
                "task.updated",
                source="jobs",
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
                payload=terminal_payload,
            )
            # Synthesize task outcome into procedural memory
            if self._memory:
                asyncio.create_task(
                    self._memory.learn(
                        {
                            "type": "task.completed" if status == "completed" else "task.failed",
                            "task_id": task_id,
                            "thread_id": thread_id,
                            "repo_id": repo_id,
                            "description": description,
                            "summary": result.summary,
                            "error": result.summary if status == "failed" else None,
                            "session_uid": session_uid,
                        }
                    )
                )
        except Exception as exc:
            logger.error("Job %s failed: %s", task_id, exc)
            await self._db.update_task(
                task_id=task_id,
                status="failed",
                error=str(exc),
                updated_at=_now(),
                completed_at=_now(),
            )
            payload = {"status": "failed", "error": str(exc), "description": description}
            await self._record_event(
                "task.failed",
                source="jobs",
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
                payload=payload,
            )
            await self._record_event(
                "task.updated",
                source="jobs",
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
                payload=payload,
            )

    async def cancel(self, task_id: str) -> bool:
        task = await self._db.get_task(task_id)
        if not task:
            return False
        await self._db.update_task(task_id=task_id, status="cancelled", updated_at=_now(), completed_at=_now())
        if task_id in self._running:
            self._running[task_id].cancel()
            del self._running[task_id]
        if self._event_bus:
            await self._event_bus.publish({
                "type": "task.updated",
                "task_id": task_id,
                "status": "cancelled",
                "ts": _now(),
            })
        return True

    async def resume(self, task_id: str) -> bool:
        task = await self._db.get_task(task_id)
        if not task or task.get("status") != "cancelled":
            return False
        await self._db.update_task(task_id=task_id, status="running", updated_at=_now(), completed_at=None)
        return True

    async def poll_once(self) -> None:
        running = await self._db.list_tasks(status="running")
        for t in running:
            task_id = t["id"]
            session_uid = t.get("session_uid")
            if session_uid:
                pass

    async def recover_on_boot(self) -> None:
        """On boot, any DB task marked running has lost its supervisor.

        Mark as stale so the user (or a health poller) can decide whether to
        resume or discard.  Publish recovery events so the hot cache and UIs
        stay consistent.
        """
        running = await self._db.list_tasks(status="running")
        for t in running:
            task_id = t["id"]
            logger.warning("Task %s was running at shutdown; marking stale", task_id)
            await self._db.update_task(
                task_id=task_id,
                status="stale",
                error="Recovered on boot: supervisor lost the task process",
                updated_at=_now(),
            )
            if self._event_bus:
                await self._event_bus.publish({
                    "type": "task.recovered",
                    "task_id": task_id,
                    "status": "stale",
                    "reason": "supervisor_restart",
                    "ts": _now(),
                })

    async def status(self, task_id: str) -> Optional[Dict[str, Any]]:
        return await self._db.get_task(task_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
