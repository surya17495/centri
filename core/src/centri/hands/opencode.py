"""CENTRI OpenCode hand — runs the opencode CLI directly, no sidecar.

Implements the :class:`~centri.hands.base.Hand` contract by driving the
``opencode`` CLI as a subprocess. Artifacts collected:
  - stdout/stderr excerpts
  - JSON-structured output when available
  - Session UID tracking (persisted to DB, not just RAM)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from centri.config import get_settings
from centri.hands.base import ApprovalGate, EventSink, Hand, HandHealth
from centri.schemas import HandCapability, HandoffRequest, HandoffResult

logger = logging.getLogger(__name__)

# Legacy in-memory map kept for quick lookup; canonical store is DB.
_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}


class _SessionTracker:
    """Mirrors session state to DB whenever possible."""

    def __init__(self):
        self._db: Optional[Any] = None
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def register_db(self, db: Any) -> None:
        self._db = db

    def upsert(self, session_uid: str, payload: Dict[str, Any]) -> None:
        self._sessions[session_uid] = payload
        if self._db is not None:
            asyncio.create_task(self._persist(session_uid, payload))

    async def _persist(self, session_uid: str, payload: Dict[str, Any]) -> None:
        try:
            await self._db.upsert_session(
                session_id=session_uid,
                session_uid=session_uid,
                hand="opencode",
                status=payload.get("status", "unknown"),
                repo_id=payload.get("repo_id"),
                summary=payload.get("description", ""),
                last_seen=payload.get("last_seen", ""),
            )
        except Exception:
            logger.debug("Session persistence failed for uid=%s", session_uid, exc_info=True)


class OpenCodeHand(Hand):
    """Coding executor via the OpenCode CLI. No sidecar, no password needed."""

    def __init__(self, db: Optional[Any] = None):
        self._cli = get_settings().opencode_cli
        self._tracker = _SessionTracker()
        self._tracker.register_db(db)

    # ------------------------------------------------------------------
    # Subprocess runner
    # ------------------------------------------------------------------
    async def _run(
        self, args: List[str], cwd: Optional[str] = None, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        """Run opencode CLI and return (exit_code, stdout, stderr)."""
        cli = shutil.which(self._cli) or self._cli
        if os.name == "nt" and cli.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c", cli] + args
        else:
            cmd = [cli] + args
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or str(Path.cwd()),
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode or 0,
                stdout_b.decode("utf8", errors="replace"),
                stderr_b.decode("utf8", errors="replace"),
            )
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.warning("OpenCode CLI timed out after %ss: %s", timeout, shlex.join(cmd))
            return -1, "", "timeout"
        except Exception as exc:
            logger.warning("OpenCode CLI failed: %s", exc)
            return -1, "", str(exc)

    async def _detect(self) -> Dict[str, Any]:
        code, out, _ = await self._run(["--version"], timeout=5.0)
        return {"cli_available": code == 0, "version": out.strip() if code == 0 else ""}

    # ------------------------------------------------------------------
    # Session queries
    # ------------------------------------------------------------------
    async def list_sessions(self) -> List[Dict[str, Any]]:
        code, out, _ = await self._run(["session", "list"], timeout=10.0)
        if code != 0:
            return []
        sessions: List[Dict[str, Any]] = []
        lines = out.strip().split("\n")
        for line in lines[2:]:  # skip header lines
            parts = line.split(None, 2)
            if len(parts) >= 2:
                sessions.append(
                    {
                        "id": parts[0],
                        "title": parts[1] if len(parts) > 1 else "",
                        "updated": parts[2] if len(parts) > 2 else "",
                    }
                )
        return sessions

    # ------------------------------------------------------------------
    # Hand contract
    # ------------------------------------------------------------------
    async def capabilities(self) -> List[HandCapability]:
        healthy = False
        version = ""
        try:
            d = await self._detect()
            healthy = d.get("cli_available", False)
            version = d.get("version", "")
        except Exception:
            pass
        detail = f"OpenCode coding hand ({version})" if version else "OpenCode coding hand"
        return [
            HandCapability(name="coding.start_task", risk="medium", configured=True, healthy=healthy, detail=detail),
            HandCapability(name="coding.status", risk="low", configured=True, healthy=healthy, detail="OpenCode session status"),
            HandCapability(name="coding.steer_session", risk="low", configured=True, healthy=healthy, detail="OpenCode steering"),
        ]

    async def health(self) -> HandHealth:
        try:
            d = await self._detect()
            if d.get("cli_available"):
                return HandHealth(healthy=True, reason=d.get("version", ""))
            return HandHealth(healthy=False, reason=f"opencode CLI '{self._cli}' not found on PATH")
        except Exception as exc:
            return HandHealth(healthy=False, reason=str(exc))

    async def execute(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> HandoffResult:
        # The CLI runs to completion per invocation, so there is no live
        # streaming channel; we emit a single "started" progress event through
        # the sink for honest UI feedback, then run and return the result.
        if event_sink is not None:
            try:
                await event_sink({"type": "hand.progress", "summary": "OpenCode subprocess started", "percent": 1})
            except Exception:
                pass
        if request.to_capability == "coding.steer_session":
            return await self._steer(request)
        if request.to_capability == "coding.status":
            return await self._status(request)
        return await self._start_task(request)

    async def cancel(self, task_id: str) -> bool:
        # The CLI runs to completion per invocation; there is no out-of-band
        # cancel channel. Report honestly that cancel is unsupported.
        return False

    # ------------------------------------------------------------------
    # Capability implementations
    # ------------------------------------------------------------------
    async def _status(self, request: HandoffRequest) -> HandoffResult:
        sessions = await self.list_sessions()
        if not sessions:
            return HandoffResult(status="no_session", summary="No active OpenCode sessions.")
        latest = sessions[0]
        uid = latest.get("id")
        _ACTIVE_SESSIONS["latest"] = latest
        if uid:
            self._tracker.upsert(uid, {
                "cwd": request.context.repo_state.root if request.context and request.context.repo_state else None,
                "description": f"status check at {latest.get('title', '')}",
                "status": "idle",
            })
        return HandoffResult(
            status="ok",
            summary=f"Latest session: {latest.get('title', 'untitled')} ({uid}). {len(sessions)} total.",
            session_uid=uid,
            events_to_record=[{"type": "opencode.session_status", "sessions": len(sessions), "latest_uid": uid}],
        )

    async def _start_task(self, request: HandoffRequest) -> HandoffResult:
        description = request.user_intent or ""
        cwd = self._working_directory(request)
        code, out, err = await self._run(
            ["run", description, "--format", "json"], cwd=cwd, timeout=300.0
        )
        return self._build_result(code=code, out=out, err=err, description=description, cwd=cwd, request_id=request.id)

    async def _steer(self, request: HandoffRequest) -> HandoffResult:
        message = request.user_intent or ""
        cwd = self._working_directory(request)
        code, out, err = await self._run(
            ["run", "-c", message, "--format", "json"], cwd=cwd, timeout=300.0
        )
        return self._build_result(code=code, out=out, err=err, description=message, cwd=cwd, request_id=request.id)

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------
    def _build_result(
        self, code: int, out: str, err: str, description: str, cwd: Optional[str], request_id: str
    ) -> HandoffResult:
        artifacts: List[Dict[str, Any]] = []
        summary = ""
        parsed_data: Optional[Dict[str, Any]] = None

        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        for line in reversed(lines):
            try:
                parsed_data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

        session_uid: Optional[str] = None
        if parsed_data is not None:
            session_uid = parsed_data.get("session_uid")
            artifacts.append({"type": "json_output", "title": f"OpenCode result: {request_id[:8]}", "data": parsed_data})
            summary = str(parsed_data.get("summary", parsed_data.get("message", "")))
            if session_uid:
                self._tracker.upsert(session_uid, {"cwd": cwd, "description": description, "status": "running"})
                _ACTIVE_SESSIONS[session_uid] = {"cwd": cwd, "description": description, "status": "running"}

        stdout_excerpt = out[:3000]
        stderr_excerpt = err[:2000]
        if stdout_excerpt:
            artifacts.append({"type": "stdout", "title": "stdout", "text": stdout_excerpt})
        if stderr_excerpt:
            artifacts.append({"type": "stderr", "title": "stderr", "text": stderr_excerpt})

        if not summary:
            if code == 0:
                summary = f"Task completed: {description[:100]}."
            else:
                summary = f"Task failed (exit={code}): {err[:200] or out[:200]}"

        return HandoffResult(
            status="completed" if code == 0 else "failed",
            summary=summary,
            session_uid=session_uid,
            artifacts=artifacts,
            events_to_record=[
                {
                    "type": "opencode.run",
                    "description": description,
                    "cwd": cwd,
                    "exit_code": code,
                    "request_id": request_id,
                    "session_uid": session_uid,
                }
            ],
        )

    def _working_directory(self, request: HandoffRequest) -> Optional[str]:
        if request.context and request.context.desktop_context:
            return request.context.desktop_context.working_directory
        if request.context and request.context.repo_state and request.context.repo_state.root:
            return request.context.repo_state.root
        return None
