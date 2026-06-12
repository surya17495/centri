"""CENTRI consolidation worker — the "sleep cycle".

Folds windows of raw ledger events into the typed :mod:`centri.memory_graph`
(decisions, facts, open loops), emits ``memory.synthesized``, and resolves
conflicts by **supersession, never accumulation**. This is the
``MemoryStore.consume_events`` synthesis hook of ``docs/memory-architecture.md``
made real.

Hard rules (from the spec):

  1. **Typed objects with receipts, never freeform prose.** Every synthesized
     object links to the ``source_event_id`` it was derived from.
  2. **Conflicts resolve by supersession.** New truth invalidates old truth via
     :meth:`MemoryGraph.supersede_fact` / ``supersede_decision``.
  3. **Never confabulate.** If an outcome cannot be attributed to an event, store
     :data:`centri.memory_graph.OUTCOME_UNKNOWN` rather than inventing a result.

Extraction strategy. CENTRI's differentiator is *event-level capture at the
moment of experience*: the typed events carry explicit synthesis hints in their
``payload`` (``decision`` / ``fact`` / ``open_loop`` / ``loop_resolution``).
The worker reads those structured hints deterministically — no LLM is required
to re-infer prose, and nothing is invented. An optional LLM extractor can be
slotted in behind :meth:`Consolidator.extract` for unstructured histories, but
the structured path is the production path because capture happens up front.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.memory_graph import (
    LOOP_DONE,
    LOOP_PARKED,
    OUTCOME_UNKNOWN,
    STANCE_ADOPTED,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Event families the worker inspects. Synthesis hints live in ``payload`` under
# these keys; an event may carry several at once (e.g. a task.completed that both
# records a decision and closes an open loop).
_HINT_KEYS = ("decision", "fact", "open_loop", "loop_resolution")


class Consolidator:
    """Batch worker that folds events into the typed memory graph."""

    def __init__(self, db: Any, graph: MemoryGraph, event_bus: Any = None):
        self._db = db
        self._graph = graph
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def consume_events(self, events: List[Dict[str, Any]]) -> int:
        """Fold a batch of in-memory event dicts into the graph.

        ``events`` are expected oldest-first. Returns the number of typed objects
        written (decisions + facts + loop transitions). Emits one
        ``memory.synthesized`` event summarizing the batch when anything changed.
        """
        await self._graph.ensure_tables()
        written = 0
        synthesized: List[Dict[str, str]] = []

        for ev in events:
            eid = ev.get("id") or ev.get("event_id")
            payload = ev.get("payload") or {}
            repo_id = ev.get("repo_id") or payload.get("repo_id")
            if not isinstance(payload, dict):
                continue

            for hint in self._iter_hints(payload):
                kind = hint[0]
                obj = await self._apply_hint(kind, hint[1], eid, repo_id)
                if obj is not None:
                    written += 1
                    synthesized.append(obj)

        if synthesized:
            await self._emit_synthesized(synthesized)
            # 3c.0: the ambient standing layer is refreshed by consolidation —
            # a small slow-changing digest stored back in the graph (with a
            # receipt) and prepended to every curated brief. Recompute it from
            # the live graph whenever anything changed.
            await self._refresh_ambient()
        return written

    async def _refresh_ambient(self) -> None:
        """Recompute the ambient standing-context digest from the live graph.

        Deterministic and re-derivable: stored as a Fact (topic
        ``ambient-standing-context``, tag ``ambient``, JSON statement) so it
        supersedes the prior digest like any other node and rebuilds from the
        ledger. No LLM (Decision 3) — a future digest summarizer is an optional
        seam, not the production path.
        """
        from centri.curation import AMBIENT_TAG, AMBIENT_TOPIC

        try:
            decisions = await self._graph.current_decisions()
            facts = await self._graph.current_facts()
            loops = await self._graph.open_loops()

            conventions = [f"{f.topic}: {f.statement}" for f in facts if "convention" in f.tags][:5]
            adopted = [d for d in decisions if d.stance == STANCE_ADOPTED]
            active_projects = sorted({d.repo_id for d in adopted if d.repo_id})[:5]
            top_loops = [loop.intent[:80] for loop in loops][:5]
            narrative = (
                f"Recent memory: {len(adopted)} decisions, {len(facts)} facts, "
                f"{len(loops)} open loops on record."
            )

            digest = {
                "identity": conventions,
                "active_projects": [str(p) for p in active_projects],
                "open_loops": top_loops,
                "narrative": narrative,
            }
            ambient = Fact(
                id="ambient-standing-context",
                topic=AMBIENT_TOPIC,
                statement=json.dumps(digest, sort_keys=True),
                source_event_id=None,
                repo_id=None,
                tags=[AMBIENT_TAG],
            )
            await self._graph.supersede_fact(ambient)
        except Exception:
            logger.debug("Ambient digest refresh failed", exc_info=True)

    async def rebuild_from_events(self) -> int:
        """Discard the graph and re-derive it from the full ledger.

        Proves re-derivability at scale (ROADMAP Phase 2). Reads every event,
        decodes the JSON payload, and replays oldest-first.
        """
        import json

        await self._graph.clear()
        rows = await self._db.recent_events(limit=1_000_000)
        events: List[Dict[str, Any]] = []
        for row in rows:
            raw = row.get("payload_json")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                payload = {}
            events.append(
                {
                    "id": row.get("id"),
                    "type": row.get("type"),
                    "repo_id": row.get("repo_id"),
                    "payload": payload,
                }
            )
        # recent_events is newest-first; replay oldest-first so supersession order
        # matches lived time.
        return await self.consume_events(list(reversed(events)))

    # ------------------------------------------------------------------
    # Hint extraction
    # ------------------------------------------------------------------
    def _iter_hints(self, payload: Dict[str, Any]):
        """Yield ``(kind, body)`` synthesis hints carried on an event payload.

        A hint value may be a single dict or a list of dicts.
        """
        for key in _HINT_KEYS:
            if key not in payload:
                continue
            val = payload[key]
            items = val if isinstance(val, list) else [val]
            for item in items:
                if isinstance(item, dict):
                    yield (key, item)

    async def _apply_hint(
        self, kind: str, body: Dict[str, Any], eid: Optional[str], repo_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        try:
            if kind == "decision":
                return await self._apply_decision(body, eid, repo_id)
            if kind == "fact":
                return await self._apply_fact(body, eid, repo_id)
            if kind == "open_loop":
                return await self._apply_open_loop(body, eid, repo_id)
            if kind == "loop_resolution":
                return await self._apply_loop_resolution(body, eid, repo_id)
        except Exception:
            logger.debug("Hint application failed for %s", kind, exc_info=True)
        return None

    async def _apply_decision(
        self, body: Dict[str, Any], eid: Optional[str], repo_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        topic = (body.get("topic") or "").strip()
        statement = (body.get("statement") or "").strip()
        if not topic or not statement:
            return None
        stance = body.get("stance") or STANCE_ADOPTED
        if stance not in (STANCE_ADOPTED, STANCE_REJECTED):
            stance = STANCE_ADOPTED
        # Never confabulate: if a rationale isn't supplied, leave it empty rather
        # than inventing one.
        rationale = (body.get("rationale") or "").strip()
        dec = Decision(
            id=body.get("id") or f"dec-{eid}-{abs(hash((topic, stance))) % 10_000}",
            topic=topic,
            statement=statement,
            stance=stance,
            rationale=rationale,
            source_event_id=eid,
            repo_id=repo_id,
            tags=list(body.get("tags") or []),
        )
        await self._graph.supersede_decision(dec)
        return {"kind": "decision", "topic": topic, "stance": stance, "id": dec.id}

    async def _apply_fact(
        self, body: Dict[str, Any], eid: Optional[str], repo_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        topic = (body.get("topic") or "").strip()
        statement = (body.get("statement") or "").strip()
        if not topic or not statement:
            return None
        fact = Fact(
            id=body.get("id") or f"fact-{eid}-{abs(hash(topic)) % 10_000}",
            topic=topic,
            statement=statement,
            source_event_id=eid,
            repo_id=repo_id,
            tags=list(body.get("tags") or []),
        )
        await self._graph.supersede_fact(fact)
        return {"kind": "fact", "topic": topic, "id": fact.id}

    async def _apply_open_loop(
        self, body: Dict[str, Any], eid: Optional[str], repo_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        intent = (body.get("intent") or "").strip()
        if not intent:
            return None
        # De-dupe against an existing loop with the same leading intent so a
        # repeated mention touches the loop instead of forking it.
        existing = await self._graph.find_open_loop_by_intent(intent, repo_id)
        if existing:
            await self._graph.touch_loop(existing.id, when=_now())
            return {"kind": "open_loop", "intent": intent, "id": existing.id}
        loop = OpenLoop(
            id=body.get("id") or f"loop-{eid}-{abs(hash(intent)) % 10_000}",
            intent=intent,
            source_event_id=eid,
            repo_id=repo_id,
            cue=(body.get("cue") or "").strip(),
            tags=list(body.get("tags") or []),
        )
        await self._graph.add_open_loop(loop)
        return {"kind": "open_loop", "intent": intent, "id": loop.id}

    async def _apply_loop_resolution(
        self, body: Dict[str, Any], eid: Optional[str], repo_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        """Close or park an open loop. Matches by explicit id or by intent prefix."""
        loop_id = body.get("loop_id") or body.get("id")
        target = None
        if loop_id:
            target = await self._graph.get_open_loop(loop_id)
        if target is None and body.get("intent"):
            target = await self._graph.find_open_loop_by_intent(body["intent"], repo_id)
        if target is None:
            return None
        resolution = body.get("resolution") or LOOP_DONE
        state = LOOP_DONE if resolution in (LOOP_DONE, "done", "completed") else LOOP_PARKED
        await self._graph.set_loop_state(target.id, state, when=_now())
        return {"kind": "loop_resolution", "id": target.id, "state": state}

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------
    async def _emit_synthesized(self, synthesized: List[Dict[str, str]]) -> None:
        ts = _now()
        summary = ", ".join(
            f"{s['kind']}:{s.get('topic') or s.get('intent') or s.get('id')}"
            for s in synthesized[:8]
        )
        payload = {"synthesized": synthesized, "count": len(synthesized), "summary": summary}
        try:
            await self._db.append_event(
                event_id=f"memory-synth-{ts}-{len(synthesized)}",
                type="memory.synthesized",
                source="memory",
                ts=ts,
                payload=payload,
            )
        except Exception:
            logger.debug("memory.synthesized ledger write failed", exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {
                        "type": "memory.synthesized",
                        "ts": ts,
                        "source": "memory",
                        "payload": payload,
                        "summary": summary,
                    }
                )
            except Exception:
                logger.debug("memory.synthesized publish failed", exc_info=True)
