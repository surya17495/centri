"""Hot context cache — fast (<50ms) access to recent operational state.

Keeps a warm snapshot rebuilt from event_bus events so the coordinator
never blocks on DB calls for hot context.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class HotContextSnapshot:
    """The fast-rebuild context slice."""

    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    active_thread_id: Optional[str] = None
    active_task_id: Optional[str] = None
    repo_id: Optional[str] = None
    repo_name: Optional[str] = None
    repo_branch: Optional[str] = None
    repo_dirty: bool = False
    session_uid: Optional[str] = None
    session_status: str = "unknown"
    letta_identity: Optional[str] = None
    relevant_recall: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    last_updated: float = 0.0


class HotContextCache:
    """Rebuilds hot context from event-bus events."""

    def __init__(self, max_events: int = 20):
        self._snapshot = HotContextSnapshot()
        self._lock = asyncio.Lock()
        self._max_events = max_events

    # ------------------------------------------------------------------
    # Public API (fast reads, no DB)
    # ------------------------------------------------------------------
    async def get(self) -> HotContextSnapshot:
        async with self._lock:
            return HotContextSnapshot(
                recent_events=list(self._snapshot.recent_events),
                active_thread_id=self._snapshot.active_thread_id,
                active_task_id=self._snapshot.active_task_id,
                repo_id=self._snapshot.repo_id,
                repo_name=self._snapshot.repo_name,
                repo_branch=self._snapshot.repo_branch,
                repo_dirty=self._snapshot.repo_dirty,
                session_uid=self._snapshot.session_uid,
                session_status=self._snapshot.session_status,
                letta_identity=self._snapshot.letta_identity,
                relevant_recall=list(self._snapshot.relevant_recall),
                constraints=list(self._snapshot.constraints),
                last_updated=self._snapshot.last_updated,
            )

    async def apply_event(self, event: Dict[str, Any]) -> None:
        """Ingest a single event and bump the hot snapshot.

        Handles both flat event shapes and payload-wrapped shapes for
        backward compatibility.
        """
        async with self._lock:
            ev_type = event.get("type", "")
            payload = event.get("payload", {})

            # Recent events ring buffer (every event goes in)
            self._snapshot.recent_events.insert(0, dict(event))
            if len(self._snapshot.recent_events) > self._max_events:
                self._snapshot.recent_events.pop()

            # Helper: try top-level then payload
            def _get(key: str) -> Any:
                return event.get(key, payload.get(key))

            if ev_type == "user.utterance":
                tid = _get("thread_id")
                if tid:
                    self._snapshot.active_thread_id = tid

            if ev_type == "task.started":
                tid = _get("task_id")
                if tid:
                    self._snapshot.active_task_id = tid

            if ev_type == "task.updated":
                status = _get("status")
                if status in ("completed", "failed", "cancelled"):
                    self._snapshot.active_task_id = None
                else:
                    tid = _get("task_id")
                    if tid:
                        self._snapshot.active_task_id = tid
                suid = _get("session_uid")
                if suid:
                    self._snapshot.session_uid = suid

            if ev_type == "task.failed":
                self._snapshot.active_task_id = None

            if ev_type == "repo.changed":
                self._snapshot.repo_id = _get("repo_id") or self._snapshot.repo_id
                self._snapshot.repo_name = _get("name")
                self._snapshot.repo_branch = _get("branch")
                self._snapshot.repo_dirty = _get("dirty") or False

            if ev_type == "context.updated":
                self._snapshot.session_uid = _get("session_uid")
                self._snapshot.session_status = _get("status") or "unknown"
                rid = _get("repo_id")
                if rid:
                    self._snapshot.repo_id = rid
                rname = _get("repo_name")
                if rname:
                    self._snapshot.repo_name = rname
                rbranch = _get("repo_branch")
                if rbranch:
                    self._snapshot.repo_branch = rbranch
                rd = _get("repo_dirty")
                if rd is not None:
                    self._snapshot.repo_dirty = bool(rd)

            if ev_type == "memory.recall":
                recalled = _get("recall") or []
                if recalled:
                    self._snapshot.relevant_recall = recalled[:3]

            if ev_type == "identity.updated":
                identity = _get("identity")
                if identity:
                    self._snapshot.letta_identity = str(identity)[:500]

            if ev_type == "constraints.updated":
                csts = _get("constraints") or []
                self._snapshot.constraints = list(csts)

            self._snapshot.last_updated = event.get("ts", 0.0)

    # ------------------------------------------------------------------
    # Direct setters (used by background enrichment)
    # ------------------------------------------------------------------
    async def set_identity(self, identity: Optional[str]) -> None:
        async with self._lock:
            self._snapshot.letta_identity = identity

    async def set_constraints(self, constraints: List[str]) -> None:
        async with self._lock:
            self._snapshot.constraints = list(constraints)

    async def clear_task(self) -> None:
        async with self._lock:
            self._snapshot.active_task_id = None

    async def clear_thread(self) -> None:
        async with self._lock:
            self._snapshot.active_thread_id = None

    async def update_from_packet(self, packet: Any) -> None:
        """Merge a ContextPacket (or any object with repo_state/session_state
        attributes) into the hot snapshot.  Used by background enrichment.
        """
        async with self._lock:
            if hasattr(packet, "repo_state") and packet.repo_state:
                rs = packet.repo_state
                self._snapshot.repo_id = getattr(rs, "id", self._snapshot.repo_id)
                self._snapshot.repo_name = getattr(rs, "name", self._snapshot.repo_name)
                self._snapshot.repo_branch = getattr(rs, "branch", self._snapshot.repo_branch)
                self._snapshot.repo_dirty = getattr(rs, "dirty", self._snapshot.repo_dirty)

            if hasattr(packet, "session_state") and packet.session_state:
                ss = packet.session_state
                self._snapshot.session_uid = getattr(
                    ss, "session_uid", self._snapshot.session_uid
                )
                self._snapshot.session_status = getattr(
                    ss, "status", self._snapshot.session_status
                )

            if hasattr(packet, "letta_identity"):
                if packet.letta_identity is not None:
                    self._snapshot.letta_identity = str(packet.letta_identity)[:500]

            if hasattr(packet, "relevant_recall"):
                if packet.relevant_recall:
                    self._snapshot.relevant_recall = list(packet.relevant_recall)[:3]

            if hasattr(packet, "constraints"):
                if packet.constraints:
                    self._snapshot.constraints = list(packet.constraints)

            from datetime import datetime, timezone
            self._snapshot.last_updated = datetime.now(timezone.utc).timestamp()
