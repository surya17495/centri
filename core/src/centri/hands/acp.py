"""CENTRI ACP hand — a real Agent Client Protocol client.

ACP (agentclientprotocol.com) speaks JSON-RPC 2.0 over stdio: CENTRI launches the
agent as a subprocess and exchanges newline-delimited JSON messages on its
stdin/stdout. This hand drives that protocol:

  - ``initialize``           negotiate protocol version + capabilities
  - ``session/new``          open a session bound to the working directory
  - ``session/prompt``       submit the delegation brief as a prompt turn
  - ``session/update`` (in)  streamed agent_message_chunk / tool_call /
                             tool_call_update / plan -> CENTRI task.progress
  - ``session/request_permission`` (in) destructive action -> CENTRI approval gate
  - ``session/cancel``       cancellation

Agent->client requests we must answer: ``session/request_permission`` (routed to
the approval gate) and ``fs/read_text_file`` / ``fs/write_text_file`` (served
against the real filesystem so the agent can operate). Everything else is replied
to with a method-not-found error so a misbehaving agent cannot hang us.

Honest-failure principle: ``health()`` reports the hand healthy only if a launch
command is configured and the agent binary is resolvable; otherwise it is
unavailable with a reason and never claims success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from centri.hands.base import ApprovalGate, EventSink, Hand, HandHealth
from centri.schemas import HandCapability, HandoffRequest, HandoffResult

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_DEFAULT_TIMEOUT = 600.0


class AcpError(Exception):
    """An ACP JSON-RPC error response."""


class AcpConnection:
    """One JSON-RPC-over-stdio conversation with a spawned ACP agent.

    Outgoing requests get a numeric id and a future resolved when the matching
    response arrives. Incoming notifications and agent->client requests are
    dispatched to handlers registered by the hand.
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._notify_handler = None
        self._request_handler = None
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    def on_notification(self, handler) -> None:
        self._notify_handler = handler

    def on_request(self, handler) -> None:
        self._request_handler = handler

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("ACP: non-JSON line ignored: %r", line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ACP read loop error: %s", exc)
        finally:
            self._fail_pending(AcpError("ACP agent stream closed"))

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        # Response to one of our requests.
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(str(msg["error"])))
                else:
                    fut.set_result(msg["result"])
            return
        # Agent->client request (expects a response).
        if "id" in msg and "method" in msg:
            await self._handle_incoming_request(msg)
            return
        # Notification (no id).
        if "method" in msg:
            if self._notify_handler is not None:
                try:
                    await self._notify_handler(msg["method"], msg.get("params") or {})
                except Exception:
                    logger.debug("ACP notification handler error", exc_info=True)

    async def _handle_incoming_request(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        req_id = msg["id"]
        if self._request_handler is None:
            await self._send_error(req_id, -32601, f"No handler for {method}")
            return
        try:
            result = await self._request_handler(method, params)
            if result is _METHOD_NOT_FOUND:
                await self._send_error(req_id, -32601, f"Method not found: {method}")
            else:
                await self._send_raw({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            await self._send_error(req_id, -32603, str(exc))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _send_raw(self, msg: Dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        data = (json.dumps(msg) + "\n").encode("utf8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _send_error(self, req_id: Any, code: int, message: str) -> None:
        await self._send_raw({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    async def request(self, method: str, params: Dict[str, Any], timeout: float = _DEFAULT_TIMEOUT) -> Any:
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._send_raw({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass


_METHOD_NOT_FOUND = object()


class AcpHand(Hand):
    """Real ACP client hand. Spawns an ACP agent and drives a prompt turn."""

    def __init__(self, command: Optional[str] = None):
        # Shell-style command to launch the agent, e.g. "opencode acp".
        self._command = command
        # task_id -> live connection, so cancel() can reach an in-flight turn.
        self._connections: Dict[str, AcpConnection] = {}

    # ------------------------------------------------------------------
    # Hand contract
    # ------------------------------------------------------------------
    async def capabilities(self) -> List[HandCapability]:
        configured = bool(self._command)
        healthy = (await self.health()).healthy
        detail = f"ACP agent: {self._command}" if configured else "no ACP command configured"
        return [
            HandCapability(name="coding.start_task", risk="medium", configured=configured, healthy=healthy, detail=detail),
            HandCapability(name="coding.steer_session", risk="low", configured=configured, healthy=healthy, detail=detail),
        ]

    async def health(self) -> HandHealth:
        if not self._command:
            return HandHealth(healthy=False, reason="no ACP command configured")
        binary = shlex.split(self._command)[0]
        if shutil.which(binary) is None and not Path(binary).exists():
            return HandHealth(healthy=False, reason=f"ACP agent binary '{binary}' not found on PATH")
        return HandHealth(healthy=True, reason=f"ACP agent '{self._command}' resolvable")

    async def execute(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> HandoffResult:
        if not self._command:
            return HandoffResult(status="unavailable", summary="no ACP command configured")
        try:
            return await self._run_turn(request, event_sink, approval_gate)
        except asyncio.TimeoutError:
            return HandoffResult(status="failed", summary="ACP agent timed out")
        except AcpError as exc:
            return HandoffResult(status="failed", summary=f"ACP error: {exc}")
        except Exception as exc:
            logger.error("ACP execute failed: %s", exc, exc_info=True)
            return HandoffResult(status="error", summary=str(exc))

    async def cancel(self, task_id: str) -> bool:
        conn = self._connections.get(task_id)
        if conn is None:
            return False
        try:
            await conn.notify("session/cancel", {})
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Prompt turn
    # ------------------------------------------------------------------
    async def _run_turn(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink],
        approval_gate: Optional[ApprovalGate],
    ) -> HandoffResult:
        cwd = self._working_directory(request) or str(Path.cwd())
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(self._command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        conn = AcpConnection(proc)
        collected: List[str] = []
        artifacts: List[Dict[str, Any]] = []
        # Full-fidelity turn record (Phase 3b.1): agent text is kept untruncated
        # and tool activity is traced so the spine retains the whole session,
        # not the 240-char UI summaries.
        tool_trace: List[Dict[str, Any]] = []

        async def _emit(event: Dict[str, Any]) -> None:
            if event_sink is not None:
                try:
                    await event_sink(event)
                except Exception:
                    logger.debug("ACP event_sink error", exc_info=True)

        async def _on_notification(method: str, params: Dict[str, Any]) -> None:
            if method != "session/update":
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                text = (update.get("content") or {}).get("text", "")
                if text:
                    collected.append(text)
                    # UI gets a short live summary; the full text is preserved in
                    # the hand.transcript event recorded at turn end.
                    await _emit({"type": "task.progress", "source": "hand", "summary": text[:240]})
            elif kind == "tool_call":
                title = update.get("title", update.get("kind", "tool"))
                tool_trace.append({
                    "tool_call_id": update.get("toolCallId"),
                    "title": title,
                    "status": update.get("status", "pending"),
                })
                await _emit({
                    "type": "task.progress",
                    "source": "hand",
                    "summary": f"tool: {title}",
                    "tool_call_id": update.get("toolCallId"),
                    "status": update.get("status", "pending"),
                })
            elif kind == "tool_call_update":
                status = update.get("status", "")
                tool_trace.append({
                    "tool_call_id": update.get("toolCallId"),
                    "status": status,
                })
                await _emit({
                    "type": "hand.progress",
                    "source": "hand",
                    "summary": f"tool {update.get('toolCallId', '')}: {status}",
                    "status": status,
                })
                if status == "completed":
                    artifacts.append({
                        "type": "tool_result",
                        "title": str(update.get("toolCallId", "tool")),
                        "data": update.get("content", []),
                    })
            elif kind == "plan":
                await _emit({"type": "task.progress", "source": "hand", "summary": "plan updated", "plan": update.get("entries", [])})

        async def _on_request(method: str, params: Dict[str, Any]) -> Any:
            if method == "session/request_permission":
                return await self._handle_permission(params, approval_gate, _emit)
            if method == "fs/read_text_file":
                return self._fs_read(params)
            if method == "fs/write_text_file":
                return self._fs_write(params)
            return _METHOD_NOT_FOUND

        conn.on_notification(_on_notification)
        conn.on_request(_on_request)
        conn.start()

        if request.id:
            self._connections[request.id] = conn

        try:
            await conn.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": "centri", "version": "0.1.0"},
            }, timeout=30.0)

            session = await conn.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=30.0)
            session_id = session.get("sessionId", "")

            await _emit({"type": "hand.progress", "source": "hand", "summary": "ACP session started", "session_uid": session_id})

            prompt_result = await conn.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": request.user_intent or ""}],
            })
            stop_reason = prompt_result.get("stopReason", "end_turn")

            full_text = "".join(collected).strip()
            summary = full_text or f"ACP turn finished ({stop_reason})."
            status = "completed" if stop_reason in ("end_turn", "max_tokens") else (
                "cancelled" if stop_reason == "cancelled" else "failed"
            )
            # Phase 3b.1: persist the verbatim turn to the spine. The fact hint
            # is a deterministic excerpt with a receipt (source_event_id), so
            # consolidation gains awareness of delegated work without an LLM and
            # without confabulating.
            transcript_event: Dict[str, Any] = {
                "type": "hand.transcript",
                "source": "hand",
                "session_uid": session_id,
                "intent": request.user_intent or "",
                "stop_reason": stop_reason,
                "text": full_text,
                "tool_trace": tool_trace,
            }
            if full_text:
                transcript_event["fact"] = {
                    "topic": f"delegated-session:{session_id or request.id}",
                    "statement": (
                        f"Delegated session for '{(request.user_intent or '')[:120]}' "
                        f"({status}/{stop_reason}): {full_text[:400]}"
                    ),
                    "tags": ["hand", "transcript", "acp"],
                }
            return HandoffResult(
                status=status,
                summary=summary[:2000],
                session_uid=session_id or None,
                artifacts=artifacts,
                events_to_record=[transcript_event, {
                    "type": "hand.completed",
                    "source": "hand",
                    "stop_reason": stop_reason,
                    "session_uid": session_id,
                }],
            )
        finally:
            if request.id:
                self._connections.pop(request.id, None)
            await conn.close()

    async def _handle_permission(
        self, params: Dict[str, Any], approval_gate: Optional[ApprovalGate], emit
    ) -> Dict[str, Any]:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        payload = {
            "tool": tool_call.get("title") or tool_call.get("kind") or "action",
            "title": tool_call.get("title") or "Permission request",
            "action": tool_call.get("rawInput") or tool_call.get("kind") or "",
            "preview": tool_call.get("content") or "",
            "risk": "high",
            "options": options,
        }
        await emit({"type": "approval.requested", "source": "hand", "summary": payload["title"], **payload})
        outcome = "deny"
        if approval_gate is not None:
            try:
                outcome = await approval_gate(payload)
            except Exception:
                outcome = "deny"
        option_id = self._pick_option(options, outcome)
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    @staticmethod
    def _pick_option(options: List[Dict[str, Any]], outcome: str) -> Optional[str]:
        """Map an allow/deny outcome to one of the agent's offered optionIds."""
        if not options:
            return None
        allow_kinds = {"allow_once", "allow_always", "allow"}
        deny_kinds = {"reject_once", "reject_always", "deny", "reject"}
        want = allow_kinds if outcome == "allow" else deny_kinds
        for opt in options:
            kind = str(opt.get("kind", "")).lower()
            oid = str(opt.get("optionId", "")).lower()
            if kind in want or oid in want or (outcome == "allow" and "allow" in oid) or (outcome != "allow" and ("deny" in oid or "reject" in oid)):
                return opt.get("optionId")
        # Fall back to first option for allow, last for deny.
        return options[0].get("optionId") if outcome == "allow" else options[-1].get("optionId")

    # ------------------------------------------------------------------
    # Filesystem requests (agent -> client)
    # ------------------------------------------------------------------
    @staticmethod
    def _fs_read(params: Dict[str, Any]) -> Dict[str, Any]:
        path = params.get("path", "")
        try:
            text = Path(path).read_text(encoding="utf8")
        except Exception as exc:
            raise AcpError(f"fs/read_text_file failed: {exc}")
        return {"content": text}

    @staticmethod
    def _fs_write(params: Dict[str, Any]) -> Dict[str, Any]:
        path = params.get("path", "")
        content = params.get("content", "")
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf8")
        except Exception as exc:
            raise AcpError(f"fs/write_text_file failed: {exc}")
        return {}

    def _working_directory(self, request: HandoffRequest) -> Optional[str]:
        ctx = request.context
        if ctx and getattr(ctx, "desktop_context", None) and ctx.desktop_context.working_directory:
            return ctx.desktop_context.working_directory
        if ctx and getattr(ctx, "repo_state", None) and ctx.repo_state and ctx.repo_state.root:
            return ctx.repo_state.root
        return None
