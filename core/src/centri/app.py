"""CENTRI FastAPI app — HTTP API and WebSocket event stream.

Design principle: events are the source of truth; memory is a derived,
re-derivable index. Every route reads from / writes through the event spine.
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from centri.config import get_settings
from centri.runtime import Runtime

logger = logging.getLogger(__name__)

runtime: Runtime = Runtime()


class UtteranceRequest(BaseModel):
    text: str
    user_id: str = "local"
    source: str = "desktop_text"


class ContextRequest(BaseModel):
    surface: str | None = None
    title: str | None = None
    url: str | None = None
    file_path: str | None = None
    selected_text: str | None = None
    working_directory: str | None = None
    listening: bool = False
    speaking: bool = False
    voice_activity_status: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.boot()
    yield
    await runtime.shutdown()


app = FastAPI(title="CENTRI", version="0.1.0", lifespan=lifespan)

# The shell (Tauri webview or vite dev server) calls the REST API cross-origin;
# without CORS headers every fetch is blocked by the browser even though the
# WebSocket (CORS-exempt) connects. Origins are configurable via CENTRI_CORS_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/status")
async def status() -> Dict[str, Any]:
    pending = await runtime.db.pending_approvals()
    running = await runtime.db.list_tasks(status="running")
    threads = await runtime.db.list_threads()
    try:
        hands_caps = await runtime.hands.list_capabilities()
        hands = [
            dict(name=c.name, risk=c.risk, configured=c.configured, healthy=c.healthy, detail=c.detail)
            for c in hands_caps
        ]
    except Exception:
        logger.warning("Hands status serialization failed", exc_info=True)
        hands = []
    try:
        role_models = runtime.model_router.role_models() if runtime.model_router else {}
    except Exception:
        logger.warning("Model role serialization failed", exc_info=True)
        role_models = {}
    return {
        "status": "ok",
        "version": "0.1.0",
        "pending_approvals": len(pending),
        "running_tasks": len(running),
        "active_threads": len(threads),
        "hands": hands,
        "role_models": role_models,
    }


@app.post("/utterance")
async def utterance(req: UtteranceRequest) -> Dict[str, Any]:
    result = await runtime.coordinator.handle_utterance(req.text, req.user_id, req.source)
    return result.__dict__


@app.post("/context")
async def update_context(req: ContextRequest) -> Dict[str, Any]:
    """Generic surface context (Phase 1 Tauri shell). No-op until a shell connects."""
    if runtime.desktop is None:
        return {"ok": False, "reason": "no surface context sink configured"}
    from centri.schemas import DesktopContext

    ctx = DesktopContext(
        surface=req.surface,
        title=req.title,
        url=req.url,
        file_path=req.file_path,
        selected_text=req.selected_text,
        working_directory=req.working_directory,
        listening=req.listening,
        speaking=req.speaking,
        voice_activity_status=req.voice_activity_status,
    )
    await runtime.desktop.update_context(ctx)
    return {"ok": True}


@app.get("/tasks")
async def get_tasks() -> Dict[str, Any]:
    tasks = await runtime.db.list_tasks()
    return {"tasks": tasks}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    ok = await runtime.jobs.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/approvals")
async def get_approvals() -> Dict[str, Any]:
    approvals = await runtime.db.pending_approvals()
    return {"approvals": approvals}


async def _resolve_approval_event(approval_id: str, task_id: str | None, decision: str) -> None:
    """Persist + broadcast approval resolution.

    Persisting matters: history hydration replays the event ledger, and an
    unpersisted resolution would resurrect resolved cards with live
    Approve/Reject buttons after a reload.
    """
    import uuid
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    payload = {"approval_id": approval_id, "decision": decision}
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    await runtime.db.append_event(
        event_id=event_id,
        type="approval.resolved",
        source="api",
        ts=ts,
        thread_id=None,
        task_id=task_id,
        repo_id=None,
        payload=payload,
    )
    if runtime.event_bus:
        await runtime.event_bus.publish({
            "id": event_id,
            "type": "approval.resolved",
            "ts": ts,
            "approval_id": approval_id,
            "task_id": task_id,
            "action": decision,
            "status": decision,
            "payload": payload,
        })


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str) -> Dict[str, Any]:
    from datetime import datetime, timezone

    approval = await runtime.db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.get("status") not in (None, "", "pending"):
        # Idempotency guard: re-approving a resolved approval must not
        # start the task a second time.
        return {"approval_id": approval_id, "status": approval.get("status"), "task_id": approval.get("task_id")}
    await runtime.db.resolve_approval(approval_id, "approved", "api", datetime.now(timezone.utc).isoformat())
    task_id = approval.get("task_id")
    job_id = None
    if task_id and approval.get("requested_action") == "coding.start_task":
        job_id = await runtime.coordinator.start_approved_task(task_id)
    await _resolve_approval_event(approval_id, task_id, "approved")
    return {"approval_id": approval_id, "status": "approved", "task_id": task_id, "job_id": job_id}


@app.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str) -> Dict[str, Any]:
    from datetime import datetime, timezone

    approval = await runtime.db.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.get("status") not in (None, "", "pending"):
        return {"approval_id": approval_id, "status": approval.get("status"), "task_id": approval.get("task_id")}
    await runtime.db.resolve_approval(approval_id, "rejected", "api", datetime.now(timezone.utc).isoformat())
    task_id = approval.get("task_id")
    if task_id:
        await runtime.jobs.cancel(task_id)
    await _resolve_approval_event(approval_id, task_id, "rejected")
    return {"approval_id": approval_id, "status": "rejected", "task_id": task_id}


@app.get("/briefing")
async def get_briefing() -> Dict[str, Any]:
    """Proactive 'what changed / what's blocked / what's next' brief (Phase 2)."""
    if runtime.proactive_brief is None:
        return {"available": False, "reason": "memory system not booted"}
    brief = await runtime.proactive_brief.build()
    return {
        "available": True,
        "changed": brief.changed,
        "blocked": brief.blocked,
        "next_steps": brief.next_steps,
        "dormancy_questions": brief.dormancy_questions,
        "text": brief.render(),
    }


@app.get("/memory/graph")
async def get_memory_graph() -> Dict[str, Any]:
    """Current typed-memory view: live decisions, conventions, open loops."""
    if runtime.memory_graph is None:
        return {"available": False, "reason": "memory system not booted"}
    g = runtime.memory_graph
    decisions = await g.current_decisions()
    facts = await g.current_facts()
    loops = await g.open_loops()
    return {
        "available": True,
        "counts": await g.counts(),
        "decisions": [d.__dict__ for d in decisions],
        "facts": [f.__dict__ for f in facts],
        "open_loops": [loop.__dict__ for loop in loops],
    }


@app.get("/threads")
async def get_threads() -> Dict[str, Any]:
    threads = await runtime.db.list_threads()
    return {"threads": threads}


def _normalize_event_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a DB event row like the live WebSocket envelope.

    DB rows store the payload as a JSON string (payload_json) and carry no
    top-level convenience mirrors; the shell consumes the live envelope shape,
    so history hydration must match it or replayed events are dropped.
    """
    payload: Dict[str, Any] = {}
    raw = row.get("payload_json")
    if raw:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
    event: Dict[str, Any] = {
        key: row.get(key)
        for key in ("id", "type", "source", "ts", "thread_id", "task_id", "repo_id", "importance")
    }
    event["payload"] = payload
    for key in (
        "status", "summary", "session_uid", "text", "user_id", "response_type",
        "message", "approval_id", "label", "risk", "description", "percent", "title", "action",
    ):
        if key in payload:
            event[key] = payload[key]
    return event


@app.get("/events")
async def get_events(limit: int = 50) -> Dict[str, Any]:
    events = await runtime.db.recent_events(limit=limit)
    return {"events": [_normalize_event_row(dict(e)) for e in events]}


@app.websocket("/events/stream")
async def events_stream(websocket: WebSocket):
    await websocket.accept()
    q = await runtime.event_bus.subscribe()
    try:
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.event_bus.unsubscribe(q)


@app.get("/hands")
async def get_hands() -> Dict[str, Any]:
    caps = await runtime.hands.list_capabilities()
    return {"capabilities": [c.__dict__ for c in caps]}


@app.get("/accounts")
async def get_accounts() -> Dict[str, Any]:
    accounts = await runtime.accounts.list_accounts()
    return {"accounts": accounts}


@app.post("/accounts/{provider}/connect")
async def connect_account(provider: str) -> Dict[str, Any]:
    result = await runtime.accounts.connect(provider)
    return result


@app.get("/artifacts/{task_id}")
async def get_artifacts(task_id: str) -> Dict[str, Any]:
    arts = await runtime.coordinator._artifacts.collect_for_task(task_id)
    return {"artifacts": arts}


# ----------------------------------------------------------------------
# Voice — returns in Phase 3 behind a clean interface. Honest-unavailable now.
# ----------------------------------------------------------------------
@app.get("/voice/status")
@app.post("/voice/status")
async def voice_status() -> Dict[str, Any]:
    return {"configured": False, "active": False, "reason": "voice arrives in Phase 3"}
