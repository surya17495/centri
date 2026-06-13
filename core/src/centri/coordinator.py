"""CENTRI coordinator -- the brain.

Understand -> decide -> act -> narrate -> remember.

Hot path (<50ms):
  - HotContextCache gives recent events/thread/task/repo/session instantly.
  - DB + memory are fire-and-forget for background enrichment.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.schemas import (
    ContextPacket,
    CoordinatorResponse,
    HandoffRequest,
    RepoState,
    SessionState,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 3c.2 temporal-intent detection. Kept module-level + deterministic (pure string
# matching) so it is trivially testable and adds no LLM cost on the hot path.
_RESUME_PHRASES = (
    "where did we leave off",
    "where were we",
    "where we left off",
    "pick up where",
    "catch me up",
    "what were we doing",
)
_SINCE_PHRASES = (
    "what changed since",
    "what's changed since",
    "what has changed since",
    "what's new since",
    "whats new since",
    "what happened since",
    "anything change since",
    "changes since",
)


def _is_resume_query(lowered: str) -> bool:
    return any(p in lowered for p in _RESUME_PHRASES)


def _is_temporal_query(lowered: str) -> bool:
    return _is_resume_query(lowered) or any(p in lowered for p in _SINCE_PHRASES)


def _extract_since(lowered: str) -> str:
    """Pull a temporal anchor out of a 'what changed since X' utterance.

    Recognizes an explicit ISO date (``2026-06-10``), the phrase 'last session'
    (→ the ``last-session`` idle-gap anchor), and otherwise returns empty (origin —
    narrate everything). Pure string matching; the narrator does the resolution.
    """
    import re as _re

    m = _re.search(r"\d{4}-\d{2}-\d{2}", lowered)
    if m:
        return m.group(0)
    if "last session" in lowered or "previous session" in lowered:
        return "last-session"
    return ""


class Coordinator:
    """Main brain."""

    def __init__(
        self,
        db: Any,
        model_router: Any,
        memory: Any,
        context_assembler: Any,
        permissions: Any,
        hands: Any,
        jobs: Any,
        artifacts: Any,
        desktop: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        hot_cache: Optional[Any] = None,
        briefing_builder: Optional[Any] = None,
        memory_brief: Optional[Any] = None,
        curator: Optional[Any] = None,
        temporal_narrator: Optional[Any] = None,
        proactive_brief: Optional[Any] = None,
        session_brief_enabled: bool = True,
    ):
        self._db = db
        self._mr = model_router
        self._memory = memory
        self._ctx = context_assembler
        self._perm = permissions
        self._hands = hands
        self._jobs = jobs
        self._artifacts = artifacts
        self._desktop = desktop
        self._event_bus = event_bus
        self._hot_cache = hot_cache
        self._briefing = briefing_builder
        # Phase 2 cue-driven memory injection (MemoryBriefAssembler).
        self._memory_brief = memory_brief
        # 3c.0 deterministic context curation. When present this is the live
        # brief path (pure curate(); ambient + cued layers, receipts, miss/waste
        # instrumentation); MemoryBriefAssembler stays the fallback for callers
        # without a curator (e.g. the bench harness).
        self._curator = curator
        # 3c.2 temporal narrative — "what changed since X" / "where did we leave off".
        # A pure derived view over the spine + bi-temporal graph; honest-unavailable
        # (None) for callers without it wired.
        self._temporal = temporal_narrator
        self._last_curated_brief = None
        # Increment 2 — session-start push briefing. The ProactiveBriefBuilder
        # ("what changed / what's blocked / what's next / dormancy") is built once
        # at session start and surfaced unprompted: emitted as a brief.session_start
        # spine event AND stashed here so the FIRST turn's curated context carries
        # it (a one-shot, cleared after the first utterance consumes it).
        self._proactive_brief = proactive_brief
        self._session_brief_enabled = session_brief_enabled
        self._pending_session_brief: Optional[str] = None

    async def _publish(self, event: Dict[str, Any]) -> None:
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(event)
            except Exception:
                pass

    async def _record_event(
        self,
        event_type: str,
        *,
        source: str = "coordinator",
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = _now()
        body = dict(payload or {})
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        event: Dict[str, Any] = {
            "id": event_id,
            "type": event_type,
            "ts": ts,
            "source": source,
            "thread_id": thread_id,
            "task_id": task_id,
            "repo_id": repo_id,
            "payload": body,
        }
        for key in ("status", "summary", "session_uid", "text", "user_id", "response_type", "message", "approval_id", "label", "risk", "description"):
            if key in body:
                event[key] = body[key]
        await self._db.append_event(
            event_id=event_id,
            type=event_type,
            source=source,
            ts=ts,
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
            payload=body,
        )
        await self._publish(event)

    async def _narrate(self, message: str) -> None:
        """Fire a narrate event for the voice TTS to pick up."""
        await self._record_event("narrate", source="coordinator", payload={"text": message})

    # ------------------------------------------------------------------
    # Session-start push briefing (Increment 2)
    # ------------------------------------------------------------------
    async def emit_session_brief(self, repo_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Build the proactive brief at session start and surface it unprompted.

        Deterministic + LLM-free (just a read over the spine + typed graph), so
        it is cheap enough to run on every session start. Two surfaces, per the
        injection contract: (1) a ``brief.session_start`` spine event — persisted
        and published so connected shells render it — carrying receipt counts of
        each section; (2) the rendered brief stashed in ``_pending_session_brief``
        so the FIRST turn's curated context prepends it like any other ambient
        standing block. Honest no-op when disabled or no builder is wired, and an
        empty brief still emits the event (with zero counts) so "nothing changed"
        is itself an auditable, re-derivable fact rather than silence.
        """
        if not self._session_brief_enabled or self._proactive_brief is None:
            return None
        try:
            brief = await self._proactive_brief.build(repo_id=repo_id)
        except Exception:
            logger.debug("Session-start brief build failed", exc_info=True)
            return None

        receipts = {
            "changed_count": len(brief.changed),
            "blocked_count": len(brief.blocked),
            "next_count": len(brief.next_steps),
            "dormancy_count": len(brief.dormancy_questions),
        }
        rendered = brief.render()
        payload = {
            "summary": rendered,
            "is_empty": brief.is_empty(),
            **receipts,
        }
        await self._record_event(
            "brief.session_start",
            source="memory",
            repo_id=repo_id,
            payload=payload,
        )
        # Stash for the first turn's curated context (one-shot). An empty render
        # is not injected — there is nothing to prepend — but the event above
        # still recorded that the session started with nothing pending.
        self._pending_session_brief = rendered or None
        return payload

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    async def _resolve_thread(self, thread_id: Optional[str]) -> str:
        """Resolve the chat thread an utterance belongs to, creating on first use.

        Threads scope the *chat* timeline only; memory stays global (the whole
        point of 3b.2). A caller may pass an explicit id (sidebar switch) or
        none, in which case utterances land in a single default thread so every
        chat event still has a home and `/events?thread_id=` can filter them.
        """
        if thread_id:
            existing = await self._db.get_thread(thread_id)
            if not existing:
                await self._db.create_thread(
                    thread_id=thread_id,
                    title="New chat",
                    goal="",
                    created_at=_now(),
                    updated_at=_now(),
                )
            else:
                await self._db.update_thread(thread_id=thread_id, updated_at=_now())
            return thread_id
        return await self._default_thread()

    async def _default_thread(self) -> str:
        """The catch-all chat thread used when no thread_id is supplied."""
        existing = await self._db.get_thread("th-default")
        if not existing:
            await self._db.create_thread(
                thread_id="th-default",
                title="General",
                goal="",
                created_at=_now(),
                updated_at=_now(),
            )
        return "th-default"

    async def handle_utterance(
        self,
        text: str,
        user_id: str,
        source: str = "voice",
        thread_id: Optional[str] = None,
    ) -> CoordinatorResponse:
        # Voice greeting is a special path
        if text == "__voice_greeting__":
            greeting = await asyncio.to_thread(self._mr.narrate, "Greet the user and offer your assistance.", True)
            await self._narrate(greeting)
            return CoordinatorResponse(response_type="greeting", message=greeting)

        chat_thread = await self._resolve_thread(thread_id)

        await self._db.store_message(
            channel=source,
            user_id=user_id,
            direction="in",
            content=text,
            ts=_now(),
            is_voice=(source == "voice"),
        )
        event_id = f"evt-{_now()}"
        await self._db.append_event(
            event_id=event_id,
            type="user.utterance",
            source=source,
            ts=_now(),
            thread_id=chat_thread,
            payload={"text": text, "user_id": user_id, "thread_id": chat_thread},
        )
        await self._publish({"type": "user.utterance", "text": text, "user_id": user_id, "thread_id": chat_thread, "ts": _now()})

        # Build context in parallel with memory recall
        packet, _recall = await self._build_context_parallel(text)

        # First turn after session start: prepend the proactive session brief into
        # the curated context (one-shot), the same way ambient standing context is
        # surfaced. Consumed once so later turns are not re-briefed.
        if self._pending_session_brief:
            packet.relevant_recall = [self._pending_session_brief] + list(
                packet.relevant_recall or []
            )
            self._pending_session_brief = None

        intent = await asyncio.to_thread(self._classify_intent, text, packet)

        # 3c.0.2 — universal per-turn curation. EVERY chat turn flows through the
        # same pure curate() Curator path as coding delegation (Decision 13:
        # memory quality must be identical in chat and coding). Status, steering,
        # and general chat get the curated ambient + cued brief with receipts and
        # curation.brief/miss-waste instrumentation, so the 3c.1 replay harness
        # covers chat too. Coding tasks (and approval responses, which lead into a
        # coding task) curate inside build_delegation_brief against the hand brief,
        # so we skip the chat-side curation for them to avoid a duplicate
        # curation.brief event for the same turn.
        if intent not in ("coding_task", "approval_response", "stop"):
            await self._curate_chat_context(packet, text, chat_thread)

        if intent == "temporal":
            resp = await self._handle_temporal(text, event_id)
        elif intent == "status":
            resp = await self._handle_status(packet, event_id)
        elif intent == "steering":
            resp = await self._handle_steering(text, packet, event_id)
        elif intent == "coding_task":
            resp = await self._handle_coding_task(text, packet, event_id)
        elif intent == "approval_response":
            resp = await self._handle_approval_response(text, event_id)
        elif intent == "stop":
            resp = CoordinatorResponse(response_type="info", message="Stopping.")
        else:
            resp = await self._handle_general(text, packet, event_id)

        # Narrate if this came from voice and isn't already an error/approval flow
        if source == "desktop_voice" and resp.response_type not in ("approval_requested", "error"):
            await self._narrate(resp.message)

        await self._record_event(
            "coordinator.response",
            source="coordinator",
            thread_id=chat_thread,
            task_id=resp.data.get("task_id") if isinstance(resp.data, dict) else None,
            payload={
                "response_type": resp.response_type,
                "message": resp.message,
                "data": resp.data,
            },
        )
        return resp

    # ------------------------------------------------------------------
    # Context assembly (parallel enrichment)
    # ------------------------------------------------------------------
    async def _build_context_parallel(self, text: str) -> tuple[ContextPacket, List[str]]:
        """Assemble hot context from cache; kick off DB+memory in background.

        Hot path: reads from hot_cache instantly (<50ms). No DB or memory wait.
        Background: rebuild full context and backfill cache + recall.
        """
        if self._hot_cache is not None:
            hot_snap = await self._hot_cache.get()
        else:
            hot_snap = None

        if hot_snap and hot_snap.last_updated:
            # Warm cache — return immediately with whatever recall is cached
            packet = self._snapshot_to_packet(hot_snap, list(hot_snap.relevant_recall))
            # Background: fetch DB + fresh memory and backfill cache
            asyncio.create_task(self._enrich_cache_from_db(text))
            return packet, list(hot_snap.relevant_recall)

        # Cold cache / no cache — fallback to DB + wait for recall
        logger.debug("Hot cache cold; falling back to DB build")
        packet = await self._ctx.build()
        recall = []
        try:
            recall = await asyncio.wait_for(self._memory.recall(text, limit=3), timeout=3.0)
        except asyncio.TimeoutError:
            logger.debug("Memory recall timed out; using empty recall")
        packet.relevant_recall = recall
        return packet, recall

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def _snapshot_to_packet(self, snap: Any, recall: List[str]) -> ContextPacket:
        """Turn a HotContextSnapshot into a ContextPacket for downstream."""
        repo_state = None
        if getattr(snap, "repo_id", None) or getattr(snap, "repo_name", None):
            repo_state = RepoState(
                id=getattr(snap, "repo_id", None),
                name=getattr(snap, "repo_name", ""),
                root="",
                branch=getattr(snap, "repo_branch", None),
                dirty=getattr(snap, "repo_dirty", False),
                ahead=0,
                behind=0,
                last_seen=None,
            )
        session_state = None
        if getattr(snap, "session_uid", None):
            session_state = SessionState(
                id=snap.session_uid,
                session_uid=snap.session_uid,
                hand="opencode",
                status=getattr(snap, "session_status", "unknown"),
                repo_id=getattr(snap, "repo_id", None),
                summary="",
                last_seen=None,
            )
        current_task = None
        if getattr(snap, "active_task_id", None):
            current_task = {"id": snap.active_task_id, "task_id": snap.active_task_id}
        active_thread = None
        if getattr(snap, "active_thread_id", None):
            active_thread = {"id": snap.active_thread_id}
        return ContextPacket(
            active_thread=active_thread,
            current_task=current_task,
            repo_state=repo_state,
            session_state=session_state,
            recent_events=list(getattr(snap, "recent_events", [])),
            letta_identity=getattr(snap, "letta_identity", None),
            relevant_recall=recall,
            constraints=list(getattr(snap, "constraints", [])),
        )

    async def _enrich_cache_from_db(self, text: str) -> None:
        """Build full context from DB and backfill hot_cache."""
        try:
            packet = await self._ctx.build()
            # Also fetch fresh memory recall
            try:
                recall = await asyncio.wait_for(self._memory.recall(text, limit=3), timeout=3.0)
                packet.relevant_recall = recall
            except asyncio.TimeoutError:
                logger.debug("Memory recall timed out during background enrichment")
            if self._hot_cache is not None:
                await self._hot_cache.update_from_packet(packet)
            # Publish enriched context so any downstream listeners are consistent
            if self._event_bus is not None:
                await self._event_bus.publish({
                    "type": "context.updated",
                    "payload": {
                        "session_uid": packet.session_state.session_uid if packet.session_state else None,
                        "status": packet.session_state.status if packet.session_state else "unknown",
                        "repo_id": packet.repo_state.id if packet.repo_state else None,
                        "repo_name": packet.repo_state.name if packet.repo_state else None,
                        "repo_branch": packet.repo_state.branch if packet.repo_state else None,
                        "repo_dirty": packet.repo_state.dirty if packet.repo_state else False,
                    },
                    "ts": _now(),
                })
        except Exception:
            logger.debug("Background enrichment failed", exc_info=True)

    # ------------------------------------------------------------------
    # Identity background hook
    # ------------------------------------------------------------------
    async def _background_identity(self) -> None:
        try:
            identity = await self._memory.identity()
            if identity and self._event_bus:
                await self._event_bus.publish({
                    "type": "identity.updated",
                    "payload": {
                        "identity": json.dumps(identity)[:500],
                    },
                    "ts": _now(),
                })
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Intent classification
    # ------------------------------------------------------------------
    def _classify_intent(self, text: str, packet: ContextPacket) -> str:
        lowered = text.strip().lower()
        # 3c.2 temporal narrative — checked before the coding/status heuristics so
        # "what changed since X" / "where did we leave off" / "catch me up" are not
        # mis-read as a coding task ("changed", "add" overlap the coding keywords).
        if self._temporal is not None and _is_temporal_query(lowered):
            return "temporal"
        if any(k in lowered for k in ["status", "what's going on", "what is happening", "what's happening"]):
            return "status"
        if any(k in lowered for k in ["tell it to", "steer", "send message to", "give it"]):
            return "steering"
        if any(k in lowered for k in ["fix", "implement", "create", "add", "write", "build", "test", "run tests", "refactor", "solve"]):
            return "coding_task"
        if any(k in lowered for k in ["approve", "reject", "cancel that", "yes do it", "no don't"]):
            return "approval_response"
        if any(k in lowered for k in ["stop", "halt", "pause"]):
            return "stop"
        try:
            llm_intent = self._mr.classify_intent(text, context=str(packet.recent_events[:3]))
            if llm_intent in ("status", "steering", "coding_task", "approval_response", "stop"):
                return llm_intent
        except Exception:
            pass
        return "general"

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def _handle_status(self, packet: ContextPacket, event_id: str) -> CoordinatorResponse:
        repo = packet.repo_state
        session = packet.session_state
        pending = await self._db.pending_approvals()
        running = await self._db.list_tasks(status="running")
        blockers: list[str] = packet.constraints if packet.constraints else []
        ctx_str = self._packet_summary(packet)
        message = await asyncio.to_thread(self._mr.summarize_status, ctx_str)
        await self._db.append_event(
            event_id=f"{event_id}-status",
            type="coordinator.status",
            source="coordinator",
            ts=_now(),
            payload={"pending_approvals": len(pending), "running_tasks": len(running)},
        )
        return CoordinatorResponse(
            response_type="status",
            message=message,
            data={
                "repo": repo.name if repo else None,
                "session": session.session_uid if session else None,
                "running_tasks": len(running),
                "pending_approvals": len(pending),
                "blockers": blockers,
            },
        )

    # ------------------------------------------------------------------
    # Temporal narrative (3c.2) — "what changed since X" / "where left off"
    # ------------------------------------------------------------------
    async def _handle_temporal(self, text: str, event_id: str) -> CoordinatorResponse:
        """Answer a temporal-recall question from the derived narrative view.

        Deterministic and receipt-bearing: it reads the typed graph through the
        TemporalNarrator (no LLM at read time, no confabulation). "where did we
        leave off" → resume view; otherwise "what changed since <anchor>" with the
        anchor extracted from the utterance (bare date / 'last session' / origin).
        """
        lowered = text.strip().lower()
        if _is_resume_query(lowered):
            nar = await self._temporal.where_left_off()
        else:
            since = _extract_since(lowered)
            resolved = await self._temporal.resolve_anchor(since)
            nar = await self._temporal.changed_since(
                resolved["anchor"], anchor_kind=resolved["kind"]
            )
        await self._db.append_event(
            event_id=f"{event_id}-temporal",
            type="coordinator.temporal",
            source="coordinator",
            ts=_now(),
            payload={
                "query": nar.query,
                "anchor": nar.anchor,
                "anchor_kind": nar.anchor_kind,
                "line_count": len(nar.lines),
            },
        )
        return CoordinatorResponse(
            response_type="temporal",
            message=nar.render(),
            data={
                "query": nar.query,
                "anchor": nar.anchor,
                "anchor_kind": nar.anchor_kind,
                "lines": [ln.as_dict() for ln in nar.lines],
            },
        )

    # ------------------------------------------------------------------
    # Steering
    # ------------------------------------------------------------------
    async def _handle_steering(
        self, text: str, packet: ContextPacket, event_id: str
    ) -> CoordinatorResponse:
        action = "coding.steer_session"
        allowed = self._perm.assert_allowed(action, {})
        if allowed == "blocked":
            return CoordinatorResponse(response_type="error", message="Steering is currently blocked.")
        if allowed == "approval_required":
            await self._create_approval(event_id, None, None, "Steer active session", text, "low", action)
            return CoordinatorResponse(response_type="approval_requested", message="Steering requires approval first.")

        # Build hands briefing from packet
        briefing = self._briefing.build(packet, text) if self._briefing else text

        handoff = HandoffRequest(
            id=f"hof-{event_id}",
            to_capability="coding.steer_session",
            user_intent=briefing,
            context=packet,
        )
        result = await self._hands.execute(handoff)
        narration = await asyncio.to_thread(self._mr.narrate, result.summary)
        return CoordinatorResponse(response_type="steering", message=narration, data={"summary": result.summary})

    # ------------------------------------------------------------------
    # Coding task
    # ------------------------------------------------------------------
    async def _handle_coding_task(
        self, text: str, packet: ContextPacket, event_id: str
    ) -> CoordinatorResponse:
        action = "coding.start_task"
        allowed = self._perm.assert_allowed(action, {})
        if allowed == "blocked":
            return CoordinatorResponse(response_type="error", message="Coding tasks are blocked right now.")

        thread_id = f"th-{uuid.uuid4().hex[:8]}"
        task_id = f"tk-{uuid.uuid4().hex[:8]}"
        repo_id = packet.repo_state.id if packet.repo_state else None
        await self._db.create_thread(
            thread_id=thread_id,
            title=text[:80],
            goal=text,
            repo_id=repo_id,
            created_at=_now(),
            updated_at=_now(),
        )
        await self._db.create_task(
            task_id=task_id,
            thread_id=thread_id,
            description=text,
            created_at=_now(),
            updated_at=_now(),
        )

        if allowed == "approval_required":
            approval = await self._create_approval(event_id, task_id, thread_id, text[:80], text, "medium", action)
            approval_id = approval["approval_id"] if approval else None
            await self._publish({"type": "approval.requested", "approval_id": approval_id, "task_id": task_id, "label": text[:80], "ts": _now()})
            return CoordinatorResponse(
                response_type="approval_requested",
                message="I'll start that once you approve.",
                data={"task_id": task_id, "thread_id": thread_id, "approval_id": approval_id},
            )

        # Build hands briefing from packet (delegation-brief seam)
        briefing = await self.build_delegation_brief(packet, text)

        handoff = HandoffRequest(
            id=f"hof-{event_id}",
            to_capability="coding.start_task",
            user_intent=briefing,
            context=packet,
            risk="medium",
            approval_required=False,
        )
        job_id = await self._jobs.start(handoff, task_id=task_id)
        await self._db.update_task(
            task_id=task_id,
            status="running",
            hand="opencode",
            capability="coding.start_task",
            updated_at=_now(),
        )

        # Fire-and-forget Letta learn
        asyncio.create_task(
            self._background_learn(
                {
                    "type": "task.started",
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "repo_id": repo_id,
                    "description": text,
                }
            )
        )

        await self._record_event(
            "task.started",
            source="coordinator",
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
            payload={"description": text, "status": "running"},
        )
        narration = await asyncio.to_thread(self._mr.narrate, f"Started task: {text}.")
        return CoordinatorResponse(
            response_type="task_created",
            message=narration,
            data={"task_id": task_id, "thread_id": thread_id, "job_id": job_id},
        )

    async def _background_learn(self, event: Dict[str, Any]) -> None:
        try:
            await self._memory.learn(event)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Delegation brief (Phase 1 seam; full memory injection is Phase 2)
    # ------------------------------------------------------------------
    async def build_delegation_brief(self, packet: ContextPacket, text: str) -> str:
        """Assemble the brief handed to a coding hand.

        Phase 2: cue-driven memory injection. The terse ``text`` is the cue; the
        :class:`~centri.memory_brief.MemoryBriefAssembler` pulls the relevant
        decisions, rejected approaches, conventions, and open loops out of the
        typed graph and pushes them into the brief — the same path
        ``centri-bench`` measures for *brief completeness*. The Phase 1 recent-task
        scan and core-block enrichment remain as a fallback layer beneath it.
        """
        # Phase 2 cue-driven injection from the typed memory graph.
        repo_id = packet.repo_state.id if packet.repo_state else None

        # 3c.0: deterministic context curation is the live brief path when a
        # curator is wired. It assembles the ambient standing layer + cued
        # ranked retrieval via the pure curate(), stamps the result with
        # policy_version + graph high-water, logs cue-expansion provenance, and
        # emits curation.miss/curation.waste instrumentation. MemoryBriefAssembler
        # remains the fallback for callers without a curator (e.g. the bench).
        if self._curator is not None:
            injected = await self._curate_into_packet(packet, text, repo_id)
            if injected:
                return await self._finish_brief(packet, text)
        elif self._memory_brief is not None:
            try:
                section = await self._memory_brief.assemble(text, repo_id=repo_id)
                # Stash the structured section so callers (and the bench) can
                # inspect exactly what was injected, not just the rendered prose.
                self._last_memory_section = section
                if not section.is_empty():
                    existing = list(packet.relevant_recall or [])
                    packet.relevant_recall = existing + [section.render()]
            except Exception:
                logger.debug("Cue-driven memory injection failed", exc_info=True)

        return await self._finish_brief(packet, text)

    async def _curate_chat_context(
        self, packet: ContextPacket, text: str, chat_thread: Optional[str]
    ) -> bool:
        """3c.0.2: curate a plain chat turn through the same live curate() path.

        Chat turns previously got only ``memory.recall(text, limit=3)`` (often
        served stale from the hot cache); this routes them through the Curator so
        the curated ambient + cued brief — with receipts and
        ``curation.brief``/miss-waste instrumentation — is the chat context too,
        identical to coding delegation (Decision 13). The cued layer is computed
        live per turn (a small latency cost over the cache fast path, accepted so
        chat recall is never stale); the hot cache's ambient slice still seeds the
        packet instantly when warm. No-op (returns False) when no curator is
        wired or the graph yields nothing — the existing recall stays untouched.
        """
        if self._curator is None:
            return False
        repo_id = packet.repo_state.id if packet.repo_state else None
        return await self._curate_into_packet(
            packet, text, repo_id, thread_id=chat_thread, turn_kind="chat"
        )

    async def _curate_into_packet(
        self,
        packet: ContextPacket,
        text: str,
        repo_id: Optional[str],
        *,
        thread_id: Optional[str] = None,
        turn_kind: str = "delegation",
    ) -> bool:
        """Run the deterministic curator and inject its brief into ``packet``.

        Returns True if anything was injected. Stamps the delegation with the
        ``policy_version`` + ``graph_high_water`` receipt, logs cue-expansion
        provenance, and emits ``curation.miss``/``curation.waste`` instrumentation
        (deterministically, against the assembled brief). Never raises into the
        hand path — a curation failure degrades to the fallback layers.

        ``thread_id`` overrides the packet's active thread (so a chat turn curates
        against its chat thread, making thread-affinity work for chat). ``turn_kind``
        ("chat" | "delegation") rides on the curation events so the replay harness
        can partition chat vs. coding turns.
        """
        from centri import curation as _cur

        try:
            if thread_id is None:
                thread_id = (packet.active_thread or {}).get("id") if packet.active_thread else None
            recent_turns = self._recent_user_turns(packet)
            active_files = list(getattr(packet.repo_state, "changed_files", None) or []) if packet.repo_state else []
            active_task = (packet.current_task or {}).get("id") if packet.current_task else None

            brief, candidates, cue = await self._curator.assemble(
                text,
                repo_id=repo_id,
                thread_id=thread_id,
                recent_turns=recent_turns,
                active_files=active_files,
                active_task=active_task,
            )
            self._last_curated_brief = brief

            # Cue-expansion provenance on the spine (honest-unavailable seam).
            await self._record_event(
                "curation.cue",
                source="curation",
                thread_id=thread_id,
                repo_id=repo_id,
                payload={
                    "policy_version": brief.policy_version,
                    "graph_high_water": brief.graph_high_water,
                    "turn_kind": turn_kind,
                    **self._curator.expander.expansion_log(cue),
                },
            )

            # Miss/waste instrumentation (E). The "turn text" available at brief
            # time is the cue itself; the 3c.1 replay harness re-scores against the
            # full resulting transcript. Emitting now establishes the event shape.
            misses, wastes = _cur.compute_miss_waste(brief, candidates, text)
            await self._record_event(
                "curation.brief",
                source="curation",
                thread_id=thread_id,
                repo_id=repo_id,
                payload={
                    **_cur.curation_breakdown_payload(brief),
                    "turn_kind": turn_kind,
                    "miss_count": len(misses),
                    "waste_count": len(wastes),
                    "misses": misses,
                    "wastes": wastes,
                },
            )

            rendered = brief.render()
            if rendered:
                existing = list(packet.relevant_recall or [])
                packet.relevant_recall = existing + [rendered]
                return True
            return False
        except Exception:
            logger.debug("Deterministic curation failed", exc_info=True)
            return False

    def _recent_user_turns(self, packet: ContextPacket) -> List[str]:
        """Last few user utterances from recent events (for anaphora resolution)."""
        turns: List[str] = []
        for ev in packet.recent_events or []:
            if (ev.get("type") or "") == "user.utterance":
                txt = ev.get("text") or (ev.get("payload") or {}).get("text") or ""
                if txt:
                    turns.append(txt)
        return turns[-3:]

    async def _finish_brief(self, packet: ContextPacket, text: str) -> str:
        """Phase 1 enrichment layers + final render, shared by both brief paths."""
        try:
            recent = await self._db.list_tasks()
            summaries: List[str] = []
            for t in recent[:5]:
                desc = (t.get("description") or "")[:80]
                status = t.get("status", "")
                result = (t.get("result") or "")[:120]
                if desc:
                    summaries.append(f"- [{status}] {desc}" + (f" -> {result}" if result else ""))
            if summaries:
                existing = list(packet.relevant_recall or [])
                packet.relevant_recall = existing + ["Recent tasks:"] + summaries
        except Exception:
            logger.debug("Recent task summary enrichment failed", exc_info=True)

        try:
            blocks = await self._memory.core_blocks() if hasattr(self._memory, "core_blocks") else None
            if blocks:
                packet.constraints = list(packet.constraints or []) + [
                    f"{k}: {v}" for k, v in blocks.items() if v
                ]
        except Exception:
            logger.debug("Core block enrichment failed", exc_info=True)

        if self._briefing:
            return self._briefing.build(packet, text)
        return text

    # ------------------------------------------------------------------
    # Approval response
    # ------------------------------------------------------------------
    async def _handle_approval_response(self, text: str, event_id: str) -> CoordinatorResponse:
        lowered = text.strip().lower()
        action = "approved" if any(k in lowered for k in ["approve", "yes", "do it"]) else (
            "rejected" if any(k in lowered for k in ["reject", "no", "cancel"]) else None
        )
        if action is None:
            return CoordinatorResponse(response_type="error", message="Did you want to approve or reject?")
        pending = await self._db.pending_approvals()
        if not pending:
            return CoordinatorResponse(response_type="error", message="No pending approvals.")
        target = pending[0]
        await self._db.resolve_approval(
            approval_id=target["id"],
            status=action,
            responded_by="user",
            responded_at=_now(),
        )
        task_id = target.get("task_id")
        if action == "approved" and task_id:
            await self.start_approved_task(task_id)
        elif action == "rejected" and task_id:
            await self._jobs.cancel(task_id)
        await self._publish({"type": "approval.resolved", "approval_id": target["id"], "action": action, "ts": _now()})
        return CoordinatorResponse(
            response_type="approval_resolved",
            message=f"Approval {action}.",
            data={"approval_id": target["id"], "task_id": task_id, "action": action},
        )

    async def start_approved_task(self, task_id: str) -> Optional[str]:
        task = await self._db.get_task(task_id)
        if not task:
            return None
        packet = await self._ctx.build(thread_id=task.get("thread_id"), task_id=task_id)
        text = task.get("description", "")
        briefing = await self.build_delegation_brief(packet, text)
        handoff = HandoffRequest(
            id=f"hof-approved-{task_id}-{uuid.uuid4().hex[:8]}",
            to_capability="coding.start_task",
            user_intent=briefing,
            context=packet,
            risk="medium",
            approval_required=False,
        )
        job_id = await self._jobs.start(handoff, task_id=task_id)
        await self._db.update_task(
            task_id=task_id,
            status="running",
            hand="opencode",
            capability="coding.start_task",
            updated_at=_now(),
        )
        await self._publish({
            "type": "task.started",
            "task_id": task_id,
            "description": task.get("description", ""),
            "ts": _now(),
        })
        return job_id

    # ------------------------------------------------------------------
    # General / fallback
    # ------------------------------------------------------------------
    async def _handle_general(
        self, text: str, packet: ContextPacket, event_id: str
    ) -> CoordinatorResponse:
        # 3c.0.2: the curated brief (ambient + cued, injected into relevant_recall
        # by _curate_chat_context) is part of the reasoning context for chat — the
        # same memory a coding hand would receive, not a separate 3-item recall.
        context = self._packet_summary(packet)
        recall = [r for r in (packet.relevant_recall or []) if r]
        if recall:
            context += "\nMemory:\n" + "\n".join(recall)
        reply = await asyncio.to_thread(
            self._mr.reason, f"User said: {text}\nContext: {context}\nReply concisely.", None
        )
        return CoordinatorResponse(response_type="info", message=reply or "I'm here.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _create_approval(
        self,
        event_id: str,
        task_id: Optional[str],
        thread_id: Optional[str],
        label: str,
        detail: str,
        risk: str,
        action: str,
    ) -> Dict[str, Any]:
        approval_id = f"apv-{uuid.uuid4().hex[:8]}"
        await self._db.create_approval(
            approval_id=approval_id,
            task_id=task_id,
            thread_id=thread_id,
            label=label,
            detail=detail,
            risk=risk,
            requested_action=action,
            requested_at=_now(),
        )
        await self._db.append_event(
            event_id=f"{event_id}-approval",
            type="approval.requested",
            source="coordinator",
            ts=_now(),
            task_id=task_id,
            payload={"approval_id": approval_id, "label": label, "risk": risk},
        )
        return {"approval_id": approval_id}

    def _packet_summary(self, packet: ContextPacket) -> str:
        parts = []
        if packet.repo_state:
            parts.append(f"Repo: {packet.repo_state.name} ({packet.repo_state.branch})")
        if packet.session_state:
            parts.append(f"Session: {packet.session_state.session_uid} {packet.session_state.status}")
        if packet.current_task:
            parts.append(f"Task: {packet.current_task.get('description', '')}")
        if packet.recent_events:
            parts.append(f"Recent events: {len(packet.recent_events)}")
        return "\n".join(parts)
