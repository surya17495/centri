"""Cue-driven memory injection — assemble, don't wait-to-be-queried.

``docs/memory-architecture.md`` is explicit that memory is **assembled and
pushed**, not queried. At a cue (delegation, session start, repo open) CENTRI
assembles the relevant decisions, rejected approaches, conventions, and open
alternatives into the brief handed to whatever :class:`~centri.hands.base.Hand`
executes the work — the same brief shape for an OpenCode subprocess or an ACP
peer.

This module is the production injection path. ``centri-bench``'s *brief
completeness* metric is measured against exactly the section this assembles, not
a special benchmark path (centri-bench.md: "the same cue-driven injection used in
production").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from centri.memory_graph import (
    LOOP_DORMANT,
    LOOP_OPEN,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "or", "in", "on", "with", "we",
    "i", "it", "is", "be", "do", "does", "this", "that", "improve", "fix", "add",
    "make", "update", "change", "please", "lets", "let", "should", "can", "now",
}


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOPWORDS and len(w) > 2]


def _relevance(cue_tokens: List[str], *fields: str) -> int:
    """Cheap lexical overlap score between the cue and an object's text.

    Embeddings (sqlite-vec) are the cold-tier upgrade noted in the architecture
    doc; for the warm-tier builder graph (~1e5 nodes) token overlap is enough and
    keeps injection on the hot path with zero model calls.
    """
    hay = " ".join(fields).lower()
    hay_tokens = set(re.findall(r"[a-z0-9]+", hay))
    return sum(1 for t in cue_tokens if t in hay_tokens)


@dataclass
class MemoryBriefSection:
    """The assembled memory context for one delegation cue."""

    decisions: List[Decision] = field(default_factory=list)
    rejections: List[Decision] = field(default_factory=list)
    conventions: List[Fact] = field(default_factory=list)
    open_loops: List[OpenLoop] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.decisions or self.rejections or self.conventions or self.open_loops)

    def render(self) -> str:
        """Render to the prose block injected into the hand brief."""
        lines: List[str] = []
        if self.decisions:
            lines.append("Decisions already made (do not relitigate):")
            for d in self.decisions:
                tail = f" — {d.rationale}" if d.rationale else ""
                lines.append(f"  - {d.statement}{tail} [{d.source_event_id or 'no-receipt'}]")
        if self.rejections:
            lines.append("Approaches already REJECTED (do not re-propose without stating what changed):")
            for d in self.rejections:
                why = f" — rejected because {d.rationale}" if d.rationale else ""
                lines.append(f"  - {d.statement}{why} [{d.source_event_id or 'no-receipt'}]")
        if self.conventions:
            lines.append("Project conventions / current facts:")
            for f in self.conventions:
                lines.append(f"  - {f.topic}: {f.statement} [{f.source_event_id or 'no-receipt'}]")
        if self.open_loops:
            lines.append("Open loops / alternatives still on the table:")
            for loop in self.open_loops:
                lines.append(f"  - {loop.intent}" + (f" (cue: {loop.cue})" if loop.cue else ""))
        return "Memory (assembled from the event ledger):\n" + "\n".join(lines)


class MemoryBriefAssembler:
    """Assembles a :class:`MemoryBriefSection` for a cue from the typed graph."""

    def __init__(self, graph: MemoryGraph):
        self._graph = graph

    async def assemble(
        self,
        cue: str,
        repo_id: Optional[str] = None,
        max_per_section: int = 6,
        min_score: int = 1,
    ) -> MemoryBriefSection:
        """Assemble relevant memory for ``cue``.

        Relevant decisions/rejections/conventions are ranked by lexical overlap
        with the cue and capped per section. Open loops are always surfaced
        (prospective memory is pushed unprompted regardless of cue match), but
        re-ranked so cue-relevant ones lead.
        """
        cue_tokens = _tokens(cue)
        await self._graph.ensure_tables()

        all_decisions = await self._graph.current_decisions(repo_id=repo_id)
        adopted = [d for d in all_decisions if d.stance != STANCE_REJECTED]
        rejected = [d for d in all_decisions if d.stance == STANCE_REJECTED]
        facts = await self._graph.current_facts(repo_id=repo_id)
        loops = await self._graph.open_loops(repo_id=repo_id, states=[LOOP_OPEN])

        def rank_decisions(items: List[Decision]) -> List[Decision]:
            scored = [(_relevance(cue_tokens, d.topic, d.statement, d.rationale, *d.tags), d) for d in items]
            # Keep cue-relevant ones; if the cue matched nothing, fall back to the
            # most recent so the brief is never silently empty.
            hits = [d for s, d in sorted(scored, key=lambda x: -x[0]) if s >= min_score]
            chosen = hits if hits else items
            return chosen[:max_per_section]

        def rank_facts(items: List[Fact]) -> List[Fact]:
            scored = [(_relevance(cue_tokens, f.topic, f.statement, *f.tags), f) for f in items]
            hits = [f for s, f in sorted(scored, key=lambda x: -x[0]) if s >= min_score]
            chosen = hits if hits else items
            return chosen[:max_per_section]

        def rank_loops(items: List[OpenLoop]) -> List[OpenLoop]:
            scored = [(_relevance(cue_tokens, loop.intent, loop.cue, *loop.tags), loop) for loop in items]
            return [loop for _, loop in sorted(scored, key=lambda x: -x[0])][:max_per_section]

        return MemoryBriefSection(
            decisions=rank_decisions(adopted),
            rejections=rank_decisions(rejected),
            conventions=rank_facts(facts),
            open_loops=rank_loops(loops),
        )


@dataclass
class ProactiveBrief:
    """The unprompted 'what changed / what's blocked / what's next' summary."""

    changed: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    dormancy_questions: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.changed or self.blocked or self.next_steps or self.dormancy_questions)

    def render(self) -> str:
        lines: List[str] = []
        if self.changed:
            lines.append("What changed:")
            lines += [f"  - {c}" for c in self.changed]
        if self.blocked:
            lines.append("What's blocked:")
            lines += [f"  - {b}" for b in self.blocked]
        if self.next_steps:
            lines.append("What's next:")
            lines += [f"  - {n}" for n in self.next_steps]
        if self.dormancy_questions:
            # The one allowed piece of spoonfeeding: a yes/no per dormant loop.
            lines += self.dormancy_questions
        return "\n".join(lines)


class ProactiveBriefBuilder:
    """Assembles the proactive briefing from the ledger and the typed graph.

    Read-only and re-derivable: 'what changed' comes from recent
    ``memory.synthesized`` events, 'what's blocked' from failed/blocked tasks,
    'what's next' from open loops (prospective memory surfaced unprompted), and
    the dormancy questions from loops already marked dormant by the scheduler.
    """

    def __init__(self, db: Any, graph: MemoryGraph):
        self._db = db
        self._graph = graph

    async def build(self, repo_id: Optional[str] = None, window: int = 50) -> ProactiveBrief:
        await self._graph.ensure_tables()
        brief = ProactiveBrief()

        try:
            events = await self._db.recent_events(limit=window)
        except Exception:
            events = []
        for ev in events:
            etype = ev.get("type", "")
            payload = ev.get("payload_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            payload = payload or {}
            if etype == "memory.synthesized":
                summary = payload.get("summary") or ""
                if summary:
                    brief.changed.append(summary)
            elif etype in ("task.failed", "hand.failed", "hand.blocked"):
                desc = payload.get("error") or payload.get("summary") or payload.get("description") or etype
                brief.blocked.append(str(desc)[:160])
        brief.changed = brief.changed[:5]
        brief.blocked = brief.blocked[:5]

        open_loops = await self._graph.open_loops(repo_id=repo_id, states=[LOOP_OPEN])
        for loop in open_loops[:5]:
            brief.next_steps.append(loop.intent)

        dormant = await self._graph.open_loops(repo_id=repo_id, states=[LOOP_DORMANT])
        for loop in dormant[:3]:
            brief.dormancy_questions.append(f'Still pursuing "{loop.intent}", or park it?')

        return brief
