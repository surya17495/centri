"""CENTRI FastAPI app — HTTP API and WebSocket event stream.

Design principle: events are the source of truth; memory is a derived,
re-derivable index. Every route reads from / writes through the event spine.
"""

import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from centri.config import get_settings
from centri.runtime import Runtime

logger = logging.getLogger(__name__)

runtime: Runtime = Runtime()


class UtteranceRequest(BaseModel):
    text: str
    user_id: str = "local"
    source: str = "desktop_text"
    thread_id: str | None = None


class CreateThreadRequest(BaseModel):
    title: str | None = None
    goal: str = ""


class IngestOpenCodeRequest(BaseModel):
    # Path to the external opencode.db to tail. Defaults to the configured
    # CENTRI_OPENCODE_INGEST_DB when omitted.
    db_path: str | None = None
    source: str | None = None
    repo_id: str | None = None


class BootstrapRequest(BaseModel):
    # Optional explicit list of {agent, path, source?} to import. When omitted,
    # bootstrap discovers + imports all available default/configured sources.
    sources: list[dict] | None = None


class ToolInvokeRequest(BaseModel):
    name: str
    arguments: dict | None = None
    thread_id: str | None = None


class RecallRequest(BaseModel):
    # Per-turn cued recall (bridge-api §1). The cue is the user message plus any
    # active file paths; budget_tokens overrides the configured brief budget.
    cue: str
    thread_id: str | None = None
    budget_tokens: int | None = None
    format: str = "markdown+items"


class EventImportRequest(BaseModel):
    # Batch of event-contract envelopes from the fork (bridge-api §2). Idempotent
    # on (source, payload.event_uid); redaction runs before persistence.
    events: list[dict]


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

# ----------------------------------------------------------------------
# Auth (Phase 3a). A shared-secret bearer token guards every route except
# /health (load-balancer probes) once CENTRI_AUTH_TOKEN is set. Empty token
# (the default) keeps local development friction-free. Settings are read per
# request so tests can swap them without rebuilding the app.
# ----------------------------------------------------------------------
_PUBLIC_PATHS = {"/health"}


def _token_ok(provided: str | None) -> bool:
    expected = get_settings().auth_token
    if not expected:
        return True
    return provided is not None and hmac.compare_digest(provided, expected)


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    # OPTIONS preflights never carry credentials; CORS middleware answers them.
    if request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if not _token_ok(_bearer(request.headers.get("authorization"))):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


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
    result = await runtime.coordinator.handle_utterance(
        req.text, req.user_id, req.source, thread_id=req.thread_id
    )
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


_KIND_OF_SECTION = {
    "decisions": "decision",
    "rejections": "decision",
    "conventions": "convention",
    "open_loops": "open_loop",
}


def _recall_response(brief: Any, elapsed_ms: int) -> Dict[str, Any]:
    """Shape a :class:`CuratedBrief` into the bridge-api §1 recall envelope.

    Pure mapping (no I/O) so it is unit-testable without the running app: every
    cued line becomes an item with its score breakdown + ``source_event_id``
    receipt and a coarse ``kind``; the ambient standing layer is flattened into
    ``ambient_items``.
    """
    items = [
        {
            "text": ln.text,
            "score": ln.score,
            "score_breakdown": ln.breakdown,
            "source_event_id": ln.source_event_id,
            "kind": _KIND_OF_SECTION.get(ln.section, "fact"),
        }
        for ln in brief.lines
    ]
    amb = brief.ambient
    ambient_items = list(amb.identity) + list(amb.active_projects) + list(amb.open_loops)
    if amb.narrative:
        ambient_items.append(amb.narrative)
    return {
        "markdown": brief.render(),
        "items": items,
        "ambient_items": ambient_items,
        "policy_version": brief.policy_version,
        "graph_hwm": brief.graph_high_water,
        "elapsed_ms": elapsed_ms,
    }


@app.post("/memory/recall")
async def memory_recall(req: RecallRequest) -> Dict[str, Any]:
    """Per-turn cued brief (bridge-api §1) — runs the pure ``curate()``.

    The fork posts the user message (plus any active file paths) as ``cue`` and
    gets back the rendered markdown brief plus structured ``items`` (each with a
    score breakdown + ``source_event_id`` receipt) and the ambient standing layer,
    stamped with ``policy_version`` and the graph high-water. No LLM at read time;
    the same graph + cue + budget renders the byte-identical brief. The fork fails
    OPEN, so an unbooted memory plane returns an empty brief, never an error.
    """
    import time

    from centri.curation import Budget

    started = time.perf_counter()
    if runtime.curator is None or runtime.memory_graph is None:
        return {
            "markdown": "",
            "items": [],
            "ambient_items": [],
            "policy_version": "",
            "graph_hwm": "",
            "elapsed_ms": 0,
        }

    budget = None
    if req.budget_tokens is not None and req.budget_tokens > 0:
        base = Budget.from_settings(get_settings())
        budget = Budget(
            total=req.budget_tokens,
            ambient=min(base.ambient, req.budget_tokens),
            floor_decisions=min(base.floor_decisions, req.budget_tokens),
            floor_rejections=min(base.floor_rejections, req.budget_tokens),
        )

    brief, _candidates, _cue = await runtime.curator.assemble(
        req.cue, thread_id=req.thread_id, budget=budget
    )
    return _recall_response(brief, int((time.perf_counter() - started) * 1000))


@app.get("/memory/since")
async def get_changed_since(since: str = "", repo_id: str | None = None) -> Dict[str, Any]:
    """Temporal narrative (3c.2): "what changed since X".

    ``since`` accepts an ISO date (``2026-06-10``), a full ISO timestamp, the literal
    ``last-session`` (anchor at the most recent idle gap on the spine), or empty
    (origin — everything so far). A derived, receipt-bearing view over the lossless
    spine + bi-temporal graph; pure given the resolved anchor.
    """
    if runtime.temporal_narrator is None:
        return {"available": False, "reason": "memory system not booted"}
    resolved = await runtime.temporal_narrator.resolve_anchor(since)
    nar = await runtime.temporal_narrator.changed_since(
        resolved["anchor"], repo_id=repo_id, anchor_kind=resolved["kind"]
    )
    return {"available": True, **nar.as_dict()}


@app.get("/memory/where-left-off")
async def get_where_left_off(repo_id: str | None = None) -> Dict[str, Any]:
    """Temporal narrative (3c.2): "where did we leave off" — the resume view."""
    if runtime.temporal_narrator is None:
        return {"available": False, "reason": "memory system not booted"}
    nar = await runtime.temporal_narrator.where_left_off(repo_id=repo_id)
    return {"available": True, **nar.as_dict()}


@app.get("/threads")
async def get_threads() -> Dict[str, Any]:
    threads = await runtime.db.list_threads()
    return {"threads": threads}


@app.post("/threads")
async def create_thread(req: CreateThreadRequest) -> Dict[str, Any]:
    """Create an empty chat thread (sidebar 'new'). Memory stays global."""
    import uuid
    from datetime import datetime, timezone

    thread_id = f"th-{uuid.uuid4().hex[:8]}"
    ts = datetime.now(timezone.utc).isoformat()
    await runtime.db.create_thread(
        thread_id=thread_id,
        title=(req.title or "New chat"),
        goal=req.goal,
        created_at=ts,
        updated_at=ts,
    )
    return {"thread": await runtime.db.get_thread(thread_id)}


@app.post("/ingest/opencode")
async def ingest_opencode(req: IngestOpenCodeRequest) -> Dict[str, Any]:
    """One-shot, idempotent tail of an external opencode.db into the spine (3b.3).

    Re-running over the same store produces no duplicate events (deterministic
    event ids + persisted per-source high-water mark). Ingested events are
    digested by consolidation like native events on the next scheduler tick.
    """
    if runtime.opencode_ingestor is None:
        return {"available": False, "reason": "ingestion subsystem not booted"}
    db_path = req.db_path or get_settings().opencode_ingest_db
    if not db_path:
        raise HTTPException(status_code=400, detail="no db_path supplied and CENTRI_OPENCODE_INGEST_DB unset")
    result = await runtime.opencode_ingestor.ingest(
        db_path, source=req.source, repo_id=req.repo_id
    )
    return result


@app.post("/events/import")
async def events_import(req: EventImportRequest) -> Dict[str, Any]:
    """Batch-import fork event envelopes into the spine (bridge-api §2).

    Each envelope follows docs/event-contract.md (``type``, ``ts``, ``source``,
    optional ``thread_id``/``task_id``/``repo_id``, ``payload``). Idempotent:
    dedupe is on ``(source, payload.event_uid)`` via a deterministic event id
    (``import:<source>:<event_uid>``) guarded by ``event_exists`` — re-posting a
    batch imports nothing new. Redaction runs before persistence inside
    ``db.append_event`` (the ledger is append-only, so a leaked secret would
    persist forever). The ``centri_app.*`` family is accepted as-is. Imported
    events are folded by consolidation on the next scheduler tick, exactly like
    native events.

    Returns ``{accepted, duplicates}``. Envelopes missing ``payload.event_uid``
    cannot be deduped and are rejected (counted under ``rejected``) rather than
    silently double-imported.
    """
    if runtime.db is None:
        return {"available": False, "reason": "spine not booted", "accepted": 0, "duplicates": 0}

    from datetime import datetime, timezone

    accepted = 0
    duplicates = 0
    rejected = 0
    now = datetime.now(timezone.utc).isoformat()

    for env in req.events:
        if not isinstance(env, dict):
            rejected += 1
            continue
        payload = env.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        event_uid = payload.get("event_uid")
        source = str(env.get("source") or "centri-app")
        ev_type = str(env.get("type") or "")
        if not event_uid or not ev_type:
            # No stable identity to dedupe on (or no type) — never blind-import.
            rejected += 1
            continue

        event_id = f"import:{source}:{event_uid}"
        if await runtime.db.event_exists(event_id):
            duplicates += 1
            continue

        ts = str(env.get("ts") or now)
        thread_id = env.get("thread_id")
        task_id = env.get("task_id")
        repo_id = env.get("repo_id")
        importance = str(env.get("importance") or "low")
        await runtime.db.append_event(
            event_id=event_id,
            type=ev_type,
            source=source,
            ts=ts,
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
            importance=importance,
            payload=payload,
        )
        accepted += 1
        if runtime.event_bus:
            # Fan out so the live timeline reflects the import. The bus redacts the
            # whole event before client delivery (event-contract Redaction §).
            await runtime.event_bus.publish({
                "id": event_id,
                "type": ev_type,
                "source": source,
                "ts": ts,
                "thread_id": thread_id,
                "task_id": task_id,
                "repo_id": repo_id,
                "payload": payload,
            })

    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


@app.post("/memory/embeddings/backfill")
async def embeddings_backfill() -> Dict[str, Any]:
    """Idempotently compute write-time vectors for existing graph nodes (Unit 2).

    Live decisions/facts with no stored vector are embedded with the configured
    provider and re-written in place; re-running is a no-op once vectors exist.
    Honest-unavailable: with no embedding model configured this reports
    ``embedded: 0`` rather than faking work. Progress streams on the spine as
    ``embedding.backfill.*`` events.
    """
    if runtime.consolidator is None:
        return {"available": False, "reason": "consolidation subsystem not booted"}
    available = bool(getattr(runtime.embedding_provider, "available", False))
    result = await runtime.consolidator.backfill_embeddings()
    result["available"] = available
    result["stamp"] = getattr(runtime.embedding_provider, "stamp", "embedding:unavailable")
    return result


@app.get("/ingest/discover")
async def ingest_discover() -> Dict[str, Any]:
    """Probe well-known coding-agent stores and report what was found (3b.4).

    Read-only: a client can ask "found 1,400 OpenCode messages, 600 Claude Code
    sessions — import?" before committing to a bootstrap. Honest-unavailable —
    sources that are absent or unreadable carry a reason rather than being faked.
    """
    if runtime.ingest_registry is None:
        return {"available": False, "reason": "ingestion subsystem not booted", "sources": []}
    summary = runtime.ingest_registry.discover_summary()
    # First-run onboarding flag derives from the backend (has any source been
    # ingested?), not client localStorage — so a fresh client still knows whether
    # memory has already been seeded.
    try:
        summary["bootstrapped"] = await runtime.db.has_ingest_state()
    except Exception:  # noqa: BLE001
        summary["bootstrapped"] = False
    # Single-LLM-config (3b.5): also surface providers already configured in
    # OpenCode so onboarding can say "your OpenCode providers are reused" without
    # a second config step. Key material is never included — has_key only.
    if runtime.opencode_config is not None:
        try:
            summary["opencode_providers"] = [
                p.as_dict() for p in runtime.opencode_config.discovered_providers()
            ]
        except Exception:  # noqa: BLE001 — provider surfacing never sinks discovery
            summary["opencode_providers"] = []
    return summary


@app.get("/providers/discovered")
async def providers_discovered() -> Dict[str, Any]:
    """Providers already configured in OpenCode, reused by CENTRI (3b.5).

    Decision 5 (single LLM config): OpenCode's provider auth is the source of
    truth, so a user never configures providers twice. This reports *which*
    providers OpenCode has set up (and whether a key is present) — never the key
    material itself, which only ever reaches the in-process model router.
    """
    if runtime.opencode_config is None:
        return {"available": False, "reason": "opencode config reader not booted", "providers": []}
    try:
        providers = [p.as_dict() for p in runtime.opencode_config.discovered_providers()]
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"unreadable: {exc}", "providers": []}
    return {"available": True, "providers": providers, "count": len(providers)}


@app.get("/models/catalog")
async def models_catalog(refresh: bool = False) -> Dict[str, Any]:
    """models.dev model catalog for the shell's provider/model display (3b.5).

    Catalog only — LiteLLM remains the transport for actual calls. On-disk cached
    with a TTL; honest-unavailable offline with no warm cache. Not a hard
    dependency: the rest of CENTRI runs regardless of this endpoint's result.
    """
    if runtime.models_catalog is None:
        return {"available": False, "reason": "models catalog not booted"}
    return runtime.models_catalog.get(force_refresh=refresh)


@app.post("/ingest/bootstrap")
async def ingest_bootstrap(req: BootstrapRequest) -> Dict[str, Any]:
    """One-time full import of discovered coding-agent histories (3b.4).

    A fresh install runs this once so memory is complete from day one. Because
    ingestion is high-water-mark based, bootstrap *is* the first tick — the same
    code path as the ambient tail, just with an empty HWM. Idempotent: re-running
    imports nothing new. Emits ``ingest.bootstrap.*`` progress events on the spine
    so the shell timeline shows the import.
    """
    if runtime.ingest_registry is None:
        return {"available": False, "reason": "ingestion subsystem not booted"}
    sources = None
    if req.sources:
        from centri.ingest.base import DiscoveredSource

        sources = [
            DiscoveredSource(
                agent=str(s.get("agent", "")),
                path=str(s.get("path", "")),
                available=True,
                source=str(s.get("source", "")),
            )
            for s in req.sources
            if s.get("agent") and s.get("path")
        ]
    return await runtime.ingest_registry.bootstrap(sources=sources)


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
        for key in ("id", "type", "source", "ts", "thread_id", "task_id", "repo_id", "tenant_id", "importance")
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
async def get_events(limit: int = 50, thread_id: str | None = None) -> Dict[str, Any]:
    events = await runtime.db.recent_events(limit=limit, thread_id=thread_id)
    return {"events": [_normalize_event_row(dict(e)) for e in events]}


@app.websocket("/events/stream")
async def events_stream(websocket: WebSocket):
    # Browsers cannot set headers on WebSocket connects, so the token may also
    # arrive as a query parameter. Reject before accept => HTTP 403 handshake.
    token = websocket.query_params.get("token") or _bearer(
        websocket.headers.get("authorization")
    )
    if not _token_ok(token):
        await websocket.close(code=4401)
        return
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


@app.get("/tools")
async def get_tools() -> Dict[str, Any]:
    """List every registered tool with provider availability + honest reason (Phase 4).

    A provider with no credentials still lists its tools, flagged
    ``available=false`` with a reason (e.g. ``composio:unavailable:no-api-key``),
    so a caller sees what would be possible once configured — never faked.
    """
    if runtime.tool_registry is None:
        return {"available": False, "reason": "tool subsystem not booted", "tools": []}
    return {"available": True, "tools": runtime.tool_registry.list_tools()}


@app.post("/tools/invoke")
async def invoke_tool(req: ToolInvokeRequest) -> Dict[str, Any]:
    """Invoke a tool through the registry (the only execution path, Decision 11).

    Read-only tools (e.g. TAVILY_SEARCH) run immediately. A side-effectful tool
    surfaces an approval the same way a coding task does — the registry awaits the
    shared approval gate (``Jobs._await_approval``), which creates an
    ``approval.requested`` card and blocks until it is resolved via the existing
    ``/approvals/{id}/approve|reject`` routes (or times out → deny). The full event
    trail (tool.requested → tool.completed/failed/denied) lands on the spine.
    """
    if runtime.tool_registry is None:
        return {"available": False, "reason": "tool subsystem not booted"}

    async def _gate(payload: Dict[str, Any]) -> str:
        # Reuse the same approval machinery hands use; thread_id carried for the card.
        return await runtime.jobs._await_approval(None, req.thread_id, payload)

    result = await runtime.tool_registry.invoke(
        req.name,
        req.arguments or {},
        approval_gate=_gate,
        thread_id=req.thread_id,
    )
    return {"available": True, **result}


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


class SettingsOverridesRequest(BaseModel):
    settings: Dict[str, Any]


@app.get("/settings/overrides")
async def get_settings_overrides() -> Dict[str, Any]:
    overrides = await runtime.db.get_all_setting_overrides()
    return {"overrides": overrides}


@app.post("/settings/overrides")
async def update_settings_overrides(req: SettingsOverridesRequest) -> Dict[str, Any]:
    for key, value in req.settings.items():
        from centri.config import get_settings
        if not hasattr(get_settings(), key):
            raise HTTPException(status_code=400, detail=f"Invalid setting key: {key}")
        await runtime.db.set_setting_override(key, str(value))
    
    # Reload settings overrides in memory
    overrides = await runtime.db.get_all_setting_overrides()
    from centri.config import update_settings
    update_settings(overrides)
    return {"status": "ok", "overrides": overrides}


# ----------------------------------------------------------------------
# Voice — returns in Phase 3 behind a clean interface. Honest-unavailable now.
# ----------------------------------------------------------------------
@app.get("/voice/status")
@app.post("/voice/status")
async def voice_status() -> Dict[str, Any]:
    return {"configured": False, "active": False, "reason": "voice arrives in Phase 3"}
