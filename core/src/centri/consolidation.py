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

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from centri.consolidation_prompt import (
    OP_ADD_DECISION,
    OP_ADD_FACT,
    OP_CLOSE_LOOP,
    OP_FINISH,
    OP_OPEN_LOOP,
    OP_SCHEMA,
    OP_SUPERSEDE,
    LiveDigest,
    build_messages,
    parse_ops,
)
from centri.memory_graph import (
    KIND_DECISION,
    KIND_FACT,
    LOOP_DONE,
    LOOP_PARKED,
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

# Event types the LLM tier never reasons over — its own provenance receipts and
# the deterministic worker's emissions would otherwise feed back into the batch.
_LLM_TIER_EXCLUDED_TYPES = (
    "memory.synthesized",
    "consolidation.proposal.applied",
    "consolidation.proposal.rejected",
    "consolidation.batch",
    "embedding.backfill.started",
    "embedding.backfill.progress",
    "embedding.backfill.completed",
    "brief.session_start",
)

# Relative-time words a proposed statement may not contain (absolute-date
# hygiene — the Letta sleeptime lesson). A statement using these is rejected.
_RELATIVE_TIME_RE = re.compile(
    r"\b(today|yesterday|tomorrow|now|currently|recently|just now|"
    r"last week|next week|this week|this morning|tonight|"
    r"a moment ago|moments ago|earlier today|right now|lately|nowadays)\b",
    re.IGNORECASE,
)


class Consolidator:
    """Batch worker that folds events into the typed memory graph."""

    def __init__(
        self,
        db: Any,
        graph: MemoryGraph,
        event_bus: Any = None,
        embedding_provider: Any = None,
    ):
        self._db = db
        self._graph = graph
        self._event_bus = event_bus
        # Write-time embeddings (Unit 2): when a real provider is configured, a
        # decision/fact's vector is computed HERE (at write) and stored on the
        # node. Read time stays pure cosine (no model call). Honest-unavailable
        # by default (NullEmbeddingProvider yields None → no vector written).
        if embedding_provider is None:
            from centri.curation import NullEmbeddingProvider

            embedding_provider = NullEmbeddingProvider()
        self._embeddings = embedding_provider

    def _embed(self, topic: str, statement: str) -> Optional[List[float]]:
        """Write-time vector for a node, or None when honest-unavailable.

        The embedded text is ``topic + statement`` so a node's vector reflects
        both its subject and its content. Never raises into the fold loop.
        """
        if not getattr(self._embeddings, "available", False):
            return None
        text = f"{topic}: {statement}".strip()
        try:
            return self._embeddings.embed(text)
        except Exception:  # noqa: BLE001 — embedding must never break a write
            logger.debug("write-time embed failed", exc_info=True)
            return None

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

    async def backfill_embeddings(self, *, batch_size: int = 200) -> Dict[str, int]:
        """Idempotently compute write-time vectors for existing nodes that lack one.

        For each live decision/fact with no stored ``vector``, compute it with the
        configured provider and re-write the node in place (``add_*`` is
        INSERT OR REPLACE, keyed by id, so history/supersession pointers are
        untouched). Re-running is a no-op once vectors exist — the read of
        ``vector is None`` is the idempotency guard, mirroring the ingest HWM
        pattern. Emits ``embedding.backfill.{started,progress,completed}`` on the
        spine so the shell timeline shows it. Honest-unavailable: with the null
        provider nothing is embedded and the result reports ``embedded=0``.
        """
        await self._graph.ensure_tables()
        available = bool(getattr(self._embeddings, "available", False))
        stamp = getattr(self._embeddings, "stamp", "embedding:unavailable")

        decisions = await self._graph.current_decisions()
        facts = await self._graph.current_facts()
        pending = [("decision", d) for d in decisions if d.vector is None]
        pending += [("fact", f) for f in facts if f.vector is None]
        total = len(pending)

        await self._emit_backfill("started", {"total": total, "available": available, "stamp": stamp})

        embedded = 0
        skipped = 0
        if not available:
            # Nothing to do, but report honestly: these nodes stay un-embedded.
            await self._emit_backfill(
                "completed",
                {"total": total, "embedded": 0, "skipped": total, "stamp": stamp},
            )
            return {"total": total, "embedded": 0, "skipped": total}

        done = 0
        for kind, node in pending:
            vec = self._embed(node.topic, node.statement)
            if vec is None:
                skipped += 1
            else:
                node.vector = vec
                if kind == "decision":
                    await self._graph.add_decision(node)
                else:
                    await self._graph.add_fact(node)
                embedded += 1
            done += 1
            if done % batch_size == 0:
                await self._emit_backfill(
                    "progress", {"done": done, "total": total, "embedded": embedded}
                )

        await self._emit_backfill(
            "completed",
            {"total": total, "embedded": embedded, "skipped": skipped, "stamp": stamp},
        )
        return {"total": total, "embedded": embedded, "skipped": skipped}

    async def _emit_backfill(self, phase: str, body: Dict[str, Any]) -> None:
        ts = _now()
        payload = {"phase": phase, **body}
        try:
            await self._db.append_event(
                event_id=f"embed-backfill-{phase}-{ts}",
                type=f"embedding.backfill.{phase}",
                source="memory",
                ts=ts,
                payload=payload,
            )
        except Exception:
            logger.debug("embedding.backfill ledger write failed", exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {
                        "type": f"embedding.backfill.{phase}",
                        "ts": ts,
                        "source": "memory",
                        "payload": payload,
                        "summary": f"embedding backfill {phase}: {body}",
                    }
                )
            except Exception:
                logger.debug("embedding.backfill publish failed", exc_info=True)

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
            vector=self._embed(topic, statement),
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
            vector=self._embed(topic, statement),
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


def event_has_hints(payload: Any) -> bool:
    """True when an event payload carries any deterministic synthesis hint.

    The LLM tier (Increment 3) only ever processes events for which this is
    False, so the deterministic hint path stays the single authoritative writer
    for hinted events and the two tiers never double-write.
    """
    if not isinstance(payload, dict):
        return False
    for key in _HINT_KEYS:
        val = payload.get(key)
        if isinstance(val, dict) and val:
            return True
        if isinstance(val, list) and any(isinstance(i, dict) and i for i in val):
            return True
    return False


def _tokenset(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Coarse Jaccard token-set similarity used for dedupe in the gatekeeper."""
    ta, tb = _tokenset(a), _tokenset(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    return union > 0 and (inter / union) >= threshold


class ConsolidationLLMTier:
    """LLM tier (Increment 3): proposes typed ops over *unhinted* events.

    The model never writes the graph. It emits a JSON array of operations
    (:data:`centri.consolidation_prompt.OP_SCHEMA`); this class is the
    deterministic **gatekeeper** that validates and applies or rejects each op,
    and every applied/rejected op becomes a spine event
    (``consolidation.proposal.applied`` / ``.rejected``) carrying the op, the
    source event ids, the model id, and the rejection reason. Re-derivable,
    auditable, revertible.

    Honest-unavailable: with no configured client the tier does nothing and the
    deterministic tier is unaffected.
    """

    def __init__(
        self,
        db: Any,
        graph: MemoryGraph,
        client: Any = None,
        *,
        event_bus: Any = None,
        embedding_provider: Any = None,
        batch_threshold: int = 8,
        digest_limit: int = 40,
        per_event_chars: int = 600,
    ):
        self._db = db
        self._graph = graph
        self._client = client
        self._event_bus = event_bus
        if embedding_provider is None:
            from centri.curation import NullEmbeddingProvider

            embedding_provider = NullEmbeddingProvider()
        self._embeddings = embedding_provider
        self._batch_threshold = max(1, int(batch_threshold))
        self._digest_limit = max(1, int(digest_limit))
        self._per_event_chars = max(80, int(per_event_chars))

    @property
    def available(self) -> bool:
        """True only when an LLM client is configured (honest-unavailable)."""
        return self._client is not None

    def _embed(self, topic: str, statement: str) -> Optional[List[float]]:
        if not getattr(self._embeddings, "available", False):
            return None
        try:
            return self._embeddings.embed(f"{topic}: {statement}".strip())
        except Exception:  # noqa: BLE001 — embedding must never break a write
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def consume_unhinted(
        self, events: List[Dict[str, Any]], *, force: bool = False
    ) -> Dict[str, Any]:
        """Run one LLM consolidation batch over the unhinted events in ``events``.

        ``force`` runs even below the batch threshold (used by the live check and
        staleness-triggered passes). Returns a structured result:
        ``{available, ran, batch_size, proposed, applied, rejected, reasons,
        usage, model}``. Never raises into the scheduler tick.
        """
        unhinted = [e for e in events if self._is_candidate(e)]
        result: Dict[str, Any] = {
            "available": self.available,
            "ran": False,
            "batch_size": len(unhinted),
            "proposed": 0,
            "applied": 0,
            "rejected": 0,
            "reasons": [],
            "usage": {},
            "model": getattr(self._client, "model", "") if self._client else "",
        }
        if not self.available:
            return result
        if not unhinted:
            return result
        if not force and len(unhinted) < self._batch_threshold:
            return result

        await self._graph.ensure_tables()
        digest = await self._build_digest(unhinted)
        messages = build_messages(unhinted, digest, per_event_chars=self._per_event_chars)

        try:
            chat = await asyncio.to_thread(self._client.complete, messages)
        except Exception as exc:  # noqa: BLE001 — a model/transport failure is honest-unavailable
            logger.warning("Consolidation LLM call failed: %s", exc)
            result["reasons"].append(f"llm-call-failed: {exc}")
            return result

        result["ran"] = True
        result["usage"] = dict(chat.usage)
        result["model"] = chat.model
        ops, parse_err = parse_ops(chat.content)
        source_ids = [str(e.get("id") or e.get("event_id")) for e in unhinted if (e.get("id") or e.get("event_id"))]

        if parse_err is not None:
            await self._reject({"op": "<malformed>"}, source_ids, chat.model, parse_err)
            result["rejected"] += 1
            result["reasons"].append(parse_err)
            await self._emit_batch(result, source_ids)
            return result

        result["proposed"] = sum(1 for op in ops if (op.get("op") or "") != OP_FINISH)
        for op in ops:
            kind = (op.get("op") or "").strip()
            if kind == OP_FINISH:
                continue
            applied, reason = await self._apply_op(op, source_ids, chat.model)
            if applied:
                result["applied"] += 1
            else:
                result["rejected"] += 1
                result["reasons"].append(reason or "rejected")

        if result["applied"]:
            await self._refresh_ambient_via_worker()
        await self._emit_batch(result, source_ids)
        return result

    def _is_candidate(self, ev: Dict[str, Any]) -> bool:
        etype = ev.get("type") or ""
        if etype in _LLM_TIER_EXCLUDED_TYPES:
            return False
        return not event_has_hints(ev.get("payload") or {})

    async def _build_digest(self, batch: List[Dict[str, Any]]) -> LiveDigest:
        repo_id = None
        for e in batch:
            repo_id = e.get("repo_id") or (e.get("payload") or {}).get("repo_id")
            if repo_id:
                break
        decisions = await self._graph.current_decisions(repo_id=repo_id)
        facts = await self._graph.current_facts(repo_id=repo_id)
        loops = await self._graph.open_loops(repo_id=repo_id)
        n = self._digest_limit
        return LiveDigest(
            decisions=[
                {"id": d.id, "topic": d.topic, "statement": d.statement, "stance": d.stance}
                for d in decisions[:n]
            ],
            facts=[{"id": f.id, "topic": f.topic, "statement": f.statement} for f in facts[:n]],
            open_loops=[{"id": loop.id, "intent": loop.intent} for loop in loops[:n]],
        )

    # ------------------------------------------------------------------
    # Gatekeeper — validate then apply, or reject. Each path emits a receipt.
    # ------------------------------------------------------------------
    async def _apply_op(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        kind = (op.get("op") or "").strip()
        if kind not in OP_SCHEMA:
            return await self._reject(op, source_ids, model, f"unknown op: {kind or '<missing>'}")

        # Schema: required keys present and non-empty.
        required, _optional = OP_SCHEMA[kind]
        for key in required:
            val = op.get(key)
            if val is None or (isinstance(val, str) and not val.strip()):
                return await self._reject(op, source_ids, model, f"missing required field: {key}")

        try:
            if kind == OP_ADD_FACT:
                return await self._apply_add_fact(op, source_ids, model)
            if kind == OP_ADD_DECISION:
                return await self._apply_add_decision(op, source_ids, model)
            if kind == OP_OPEN_LOOP:
                return await self._apply_open_loop(op, source_ids, model)
            if kind == OP_CLOSE_LOOP:
                return await self._apply_close_loop(op, source_ids, model)
            if kind == OP_SUPERSEDE:
                return await self._apply_supersede(op, source_ids, model)
        except Exception as exc:  # noqa: BLE001 — a single bad op must not abort the batch
            logger.debug("Op application failed", exc_info=True)
            return await self._reject(op, source_ids, model, f"apply error: {exc}")
        return await self._reject(op, source_ids, model, "unhandled op")

    def _date_ok(self, statement: str) -> Optional[str]:
        """Return a rejection reason if the statement uses relative time, else None."""
        m = _RELATIVE_TIME_RE.search(statement or "")
        if m:
            return f"relative-time statement (use ISO dates): '{m.group(0)}'"
        return None

    async def _apply_add_fact(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        topic = str(op["topic"]).strip()
        statement = str(op["statement"]).strip()
        bad = self._date_ok(statement)
        if bad:
            return await self._reject(op, source_ids, model, bad)
        # Dedupe: a live fact on the same topic whose statement is ~equal already exists.
        for f in await self._graph.current_facts():
            if f.topic.strip().lower() == topic.lower() and _similar(f.statement, statement):
                return await self._reject(op, source_ids, model, f"duplicate of fact {f.id}")
        eid = source_ids[0] if source_ids else None
        fact = Fact(
            id=f"llm-fact-{abs(hash((topic, statement))) % 10_000_000}",
            topic=topic,
            statement=statement,
            source_event_id=eid,
            tags=_clean_tags(op.get("tags")),
            vector=self._embed(topic, statement),
        )
        await self._graph.supersede_fact(fact)
        return await self._accept(op, source_ids, model, {"kind": "fact", "id": fact.id})

    async def _apply_add_decision(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        topic = str(op["topic"]).strip()
        statement = str(op["statement"]).strip()
        bad = self._date_ok(statement)
        if bad:
            return await self._reject(op, source_ids, model, bad)
        stance = op.get("stance") or STANCE_ADOPTED
        if stance not in (STANCE_ADOPTED, STANCE_REJECTED):
            stance = STANCE_ADOPTED
        for d in await self._graph.current_decisions(stance=stance):
            if d.topic.strip().lower() == topic.lower() and _similar(d.statement, statement):
                return await self._reject(op, source_ids, model, f"duplicate of decision {d.id}")
        eid = source_ids[0] if source_ids else None
        dec = Decision(
            id=f"llm-dec-{abs(hash((topic, statement, stance))) % 10_000_000}",
            topic=topic,
            statement=statement,
            stance=stance,
            rationale=str(op.get("rationale") or "").strip(),
            source_event_id=eid,
            tags=_clean_tags(op.get("tags")),
            vector=self._embed(topic, statement),
        )
        await self._graph.supersede_decision(dec)
        return await self._accept(op, source_ids, model, {"kind": "decision", "id": dec.id})

    async def _apply_open_loop(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        intent = str(op["intent"]).strip()
        existing = await self._graph.find_open_loop_by_intent(intent, None)
        if existing:
            await self._graph.touch_loop(existing.id, when=_now())
            return await self._accept(op, source_ids, model, {"kind": "open_loop", "id": existing.id})
        eid = source_ids[0] if source_ids else None
        loop = OpenLoop(
            id=f"llm-loop-{abs(hash(intent)) % 10_000_000}",
            intent=intent,
            source_event_id=eid,
            cue=str(op.get("cue") or "").strip(),
            tags=_clean_tags(op.get("tags")),
        )
        await self._graph.add_open_loop(loop)
        return await self._accept(op, source_ids, model, {"kind": "open_loop", "id": loop.id})

    async def _apply_close_loop(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        loop_id = op.get("loop_id")
        intent_match = op.get("intent_match")
        if not loop_id and not intent_match:
            return await self._reject(op, source_ids, model, "close_loop needs loop_id or intent_match")
        target = None
        if loop_id:
            target = await self._graph.get_open_loop(str(loop_id))
        if target is None and intent_match:
            target = await self._graph.find_open_loop_by_intent(str(intent_match), None)
        if target is None:
            return await self._reject(op, source_ids, model, "close_loop target not found")
        resolution = op.get("resolution") or LOOP_DONE
        state = LOOP_DONE if resolution in (LOOP_DONE, "done", "completed") else LOOP_PARKED
        await self._graph.set_loop_state(target.id, state, when=_now())
        return await self._accept(op, source_ids, model, {"kind": "close_loop", "id": target.id, "state": state})

    async def _apply_supersede(
        self, op: Dict[str, Any], source_ids: List[str], model: str
    ) -> Tuple[bool, Optional[str]]:
        node_id = str(op["node_id"]).strip()
        node_kind = str(op["kind"]).strip()
        new_statement = str(op["new_statement"]).strip()
        bad = self._date_ok(new_statement)
        if bad:
            return await self._reject(op, source_ids, model, bad)
        eid = source_ids[0] if source_ids else None
        if node_kind == KIND_FACT:
            target = next((f for f in await self._graph.current_facts() if f.id == node_id), None)
            if target is None:
                return await self._reject(op, source_ids, model, f"supersede target not live: {node_id}")
            new = Fact(
                id=f"llm-fact-{abs(hash((target.topic, new_statement))) % 10_000_000}",
                topic=target.topic,
                statement=new_statement,
                source_event_id=eid,
                repo_id=target.repo_id,
                tags=list(target.tags),
                vector=self._embed(target.topic, new_statement),
            )
            await self._graph.supersede_fact(new)
            return await self._accept(
                op, source_ids, model, {"kind": "fact", "id": new.id, "superseded": node_id}
            )
        if node_kind == KIND_DECISION:
            target = next((d for d in await self._graph.current_decisions() if d.id == node_id), None)
            if target is None:
                return await self._reject(op, source_ids, model, f"supersede target not live: {node_id}")
            new = Decision(
                id=f"llm-dec-{abs(hash((target.topic, new_statement))) % 10_000_000}",
                topic=target.topic,
                statement=new_statement,
                stance=target.stance,
                rationale=target.rationale,
                source_event_id=eid,
                repo_id=target.repo_id,
                tags=list(target.tags),
                vector=self._embed(target.topic, new_statement),
            )
            await self._graph.supersede_decision(new)
            return await self._accept(
                op, source_ids, model, {"kind": "decision", "id": new.id, "superseded": node_id}
            )
        return await self._reject(op, source_ids, model, f"supersede kind must be fact|decision, got {node_kind}")

    async def _refresh_ambient_via_worker(self) -> None:
        """Refresh the ambient digest after LLM writes, reusing the worker's logic."""
        try:
            worker = Consolidator(self._db, self._graph, event_bus=self._event_bus, embedding_provider=self._embeddings)
            await worker._refresh_ambient()
        except Exception:
            logger.debug("Ambient refresh after LLM tier failed", exc_info=True)

    # ------------------------------------------------------------------
    # Provenance receipts
    # ------------------------------------------------------------------
    async def _accept(
        self, op: Dict[str, Any], source_ids: List[str], model: str, applied: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        await self._emit_proposal("applied", op, source_ids, model, applied=applied)
        return True, None

    async def _reject(
        self, op: Dict[str, Any], source_ids: List[str], model: str, reason: str
    ) -> Tuple[bool, Optional[str]]:
        await self._emit_proposal("rejected", op, source_ids, model, reason=reason)
        return False, reason

    async def _emit_proposal(
        self,
        phase: str,
        op: Dict[str, Any],
        source_ids: List[str],
        model: str,
        *,
        applied: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        ts = _now()
        payload: Dict[str, Any] = {
            "op": op,
            "source_event_ids": source_ids,
            "model": model,
        }
        if applied is not None:
            payload["applied"] = applied
            summary = f"consolidation op applied: {op.get('op')} -> {applied.get('id')}"
        else:
            payload["reason"] = reason
            summary = f"consolidation op rejected ({reason}): {op.get('op')}"
        etype = f"consolidation.proposal.{phase}"
        try:
            await self._db.append_event(
                event_id=f"consol-{phase}-{ts}-{abs(hash(json.dumps(op, sort_keys=True))) % 100000}",
                type=etype,
                source="memory",
                ts=ts,
                payload=payload,
            )
        except Exception:
            logger.debug("%s ledger write failed", etype, exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {"type": etype, "ts": ts, "source": "memory", "payload": payload, "summary": summary}
                )
            except Exception:
                logger.debug("%s publish failed", etype, exc_info=True)

    async def _emit_batch(self, result: Dict[str, Any], source_ids: List[str]) -> None:
        ts = _now()
        payload = {
            "batch_size": result["batch_size"],
            "proposed": result["proposed"],
            "applied": result["applied"],
            "rejected": result["rejected"],
            "usage": result["usage"],
            "model": result["model"],
            "source_event_ids": source_ids,
        }
        summary = (
            f"consolidation batch: {result['applied']} applied, "
            f"{result['rejected']} rejected over {result['batch_size']} events"
        )
        try:
            await self._db.append_event(
                event_id=f"consol-batch-{ts}",
                type="consolidation.batch",
                source="memory",
                ts=ts,
                payload=payload,
            )
        except Exception:
            logger.debug("consolidation.batch ledger write failed", exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {"type": "consolidation.batch", "ts": ts, "source": "memory", "payload": payload, "summary": summary}
                )
            except Exception:
                logger.debug("consolidation.batch publish failed", exc_info=True)


def _clean_tags(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()]
