"""3c.2 — temporal narrative: "what changed since X" and "where did we leave off".

This sits *beside* :mod:`centri.curation` (like :mod:`centri.curation_replay`) so the
golden-pinned read surface stays untouched. It is the temporal-recall layer over the
photographic spine + the bi-temporal typed graph (Decision 13): a person compresses
"what happened since Tuesday" into a few sentences and can zoom into the underlying
verified events. Here that compression is a DERIVED VIEW — nothing is written, nothing
is deleted.

Two queries:

  - :meth:`TemporalNarrator.changed_since` — diff the live graph against an ISO anchor.
    A node *created* after the anchor is an addition; a node *invalidated* after the
    anchor is a supersession (old value → new value); an open loop opened / closed /
    re-touched after the anchor is a status change. Every narrative line carries a
    ``source_event_id`` receipt back to verbatim ground truth.
  - :meth:`TemporalNarrator.where_left_off` — the "resume" view: anchor on the last
    real activity, then surface what is still in flight (open loops), the most recent
    decisions, and the last thing that happened — so the partner can pick up cold.

**Purity.** Everything here is pure given its inputs (the graph snapshot + an anchor).
ISO-8601 timestamps sort lexically, so the diff is string comparison — no calendar
library, no ``now()`` baked into the output, no randomness, no LLM. The same
``(graph, anchor)`` renders a byte-identical narrative. Anchor *resolution* may consult
the spine (last-session gap detection), but the narrative itself is a pure function of
the resolved anchor + the graph.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from centri.curation import _digest
from centri.memory_graph import (
    LOOP_DONE,
    LOOP_DORMANT,
    LOOP_OPEN,
    LOOP_PARKED,
    MemoryGraph,
)

# Default idle gap that separates one working "session" from the next. Used only by
# anchor resolution (``last-session``); the narrative itself never reads wall-clock.
DEFAULT_SESSION_GAP_SECONDS = 6 * 3600


def _iso_after(ts: Optional[str], anchor: str) -> bool:
    """True when ISO timestamp ``ts`` is strictly after ``anchor``.

    ISO-8601 with a fixed offset sorts lexically, so this is a pure string compare —
    deterministic and locale-free, consistent with the curation purity invariant.
    """
    return bool(ts) and (ts or "") > anchor


@dataclass
class NarrativeLine:
    """One line of temporal narrative with a receipt back to the spine."""

    kind: str            # added | superseded | reopened | closed | parked | in_flight | last
    category: str        # decision | rejection | fact | open_loop | event
    text: str            # the gist line (length-bounded, derived — never invented)
    receipt: Optional[str]  # source_event_id into the lossless ledger
    at: str = ""         # the node/event timestamp this line is anchored on

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "category": self.category,
            "text": self.text,
            "receipt": self.receipt,
            "at": self.at,
        }


@dataclass
class TemporalNarrative:
    """A "what changed since X" / "where did we leave off" narrative view.

    ``lines`` is the ordered gist (newest change first). ``anchor`` is the resolved
    ISO timestamp the diff was taken against (empty for ``where_left_off`` cold start).
    Pure derived view: re-derivable from the graph + anchor.
    """

    anchor: str
    anchor_kind: str     # iso | last-session | origin
    lines: List[NarrativeLine] = field(default_factory=list)
    query: str = "changed_since"  # changed_since | where_left_off

    def is_empty(self) -> bool:
        return not self.lines

    def render(self) -> str:
        if self.query == "where_left_off":
            head = "Where we left off:"
        elif self.anchor_kind == "origin":
            head = "Everything so far:"
        else:
            head = f"What changed since {self.anchor}:"
        if not self.lines:
            return head + "\n  (nothing recorded)"
        body = "\n".join(f"  - {ln.text}" for ln in self.lines)
        return f"{head}\n{body}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "anchor": self.anchor,
            "anchor_kind": self.anchor_kind,
            "lines": [ln.as_dict() for ln in self.lines],
            "text": self.render(),
        }


class TemporalNarrator:
    """Builds temporal narratives from the typed graph (a derived view, with receipts).

    Read-only: it never writes to the graph or the spine. The graph's *full* history
    (including superseded rows) is needed to narrate supersessions, so this reads the
    raw tables rather than only the live views.
    """

    def __init__(self, graph: MemoryGraph, db: Any = None):
        self._graph = graph
        self._db = db

    # ------------------------------------------------------------------
    # "What changed since X"
    # ------------------------------------------------------------------
    async def changed_since(
        self,
        anchor: str,
        *,
        repo_id: Optional[str] = None,
        anchor_kind: str = "iso",
        limit: int = 40,
    ) -> TemporalNarrative:
        """Narrate every typed-graph change strictly after the ISO ``anchor``.

        Additions (nodes created after the anchor), supersessions (nodes invalidated
        after the anchor, rendered old→new), and open-loop status changes (reopened /
        closed / parked after the anchor). Newest change first; every line keeps a
        receipt. Pure given (graph, anchor).
        """
        await self._graph.ensure_tables()
        nar = TemporalNarrative(anchor=anchor, anchor_kind=anchor_kind, query="changed_since")

        decisions = await self._all_decisions(repo_id)
        facts = await self._all_facts(repo_id)
        loops = await self._all_loops(repo_id)

        # Map id -> node so a supersession line can name the new value it points to.
        dec_by_id = {d["id"]: d for d in decisions}
        fact_by_id = {f["id"]: f for f in facts}
        # Ids that are the NEW value of an in-window supersession: their standalone
        # "added" line is subsumed by the "was X, now Y" line, so suppress it (a
        # person narrates the change once, not as "added" + "changed").
        superseded_targets = {
            d["superseded_by"] for d in decisions
            if d.get("superseded_by") and _iso_after(d.get("invalidated_at"), anchor)
        } | {
            f["superseded_by"] for f in facts
            if f.get("superseded_by") and _iso_after(f.get("invalidated_at"), anchor)
        }

        lines: List[NarrativeLine] = []

        for d in decisions:
            category = "rejection" if d.get("stance") == "rejected" else "decision"
            verb = "rejected" if category == "rejection" else "decided"
            if d.get("superseded_by") and _iso_after(d.get("invalidated_at"), anchor):
                newer = dec_by_id.get(d["superseded_by"])
                new_txt = _digest(newer["statement"]) if newer else "a newer decision"
                lines.append(NarrativeLine(
                    kind="superseded", category=category,
                    text=f"{d['topic']}: superseded — was \"{_digest(d['statement'])}\", now \"{new_txt}\"",
                    receipt=(newer or d).get("source_event_id"),
                    at=d.get("invalidated_at") or "",
                ))
            elif (
                not d.get("superseded_by")
                and d["id"] not in superseded_targets
                and _iso_after(d.get("created_at"), anchor)
            ):
                lines.append(NarrativeLine(
                    kind="added", category=category,
                    text=f"{d['topic']}: {verb} \"{_digest(d['statement'])}\"",
                    receipt=d.get("source_event_id"), at=d.get("created_at") or "",
                ))

        for f in facts:
            if f.get("topic") in _reserved_topics():
                continue
            if f.get("superseded_by") and _iso_after(f.get("invalidated_at"), anchor):
                newer = fact_by_id.get(f["superseded_by"])
                new_txt = _digest(newer["statement"]) if newer else "a newer value"
                lines.append(NarrativeLine(
                    kind="superseded", category="fact",
                    text=f"{f['topic']}: changed — was \"{_digest(f['statement'])}\", now \"{new_txt}\"",
                    receipt=(newer or f).get("source_event_id"),
                    at=f.get("invalidated_at") or "",
                ))
            elif (
                not f.get("superseded_by")
                and f["id"] not in superseded_targets
                and _iso_after(f.get("created_at"), anchor)
            ):
                lines.append(NarrativeLine(
                    kind="added", category="fact",
                    text=f"{f['topic']}: {_digest(f['statement'])}",
                    receipt=f.get("source_event_id"), at=f.get("created_at") or "",
                ))

        for loop in loops:
            line = self._loop_change_line(loop, anchor)
            if line:
                lines.append(line)

        # Newest change first; (at desc, then text asc) makes ties total + byte-stable.
        lines.sort(key=lambda ln: (ln.at, ln.text), reverse=True)
        nar.lines = lines[:limit]
        return nar

    # ------------------------------------------------------------------
    # "Where did we leave off"
    # ------------------------------------------------------------------
    async def where_left_off(
        self,
        *,
        repo_id: Optional[str] = None,
        max_loops: int = 5,
        max_recent_decisions: int = 3,
    ) -> TemporalNarrative:
        """The resume view: what's still in flight + the last few decisions + the last event.

        Anchored on the most recent real activity. Open loops are "still in flight";
        the latest live decisions give the standing context; the last spine event
        (if a db is wired) is the literal "last thing that happened". Receipts on
        every line. Pure given the graph; the anchor reads the spine's last event.
        """
        await self._graph.ensure_tables()
        last_event = await self._last_activity_event()
        anchor = (last_event or {}).get("ts", "") or ""
        nar = TemporalNarrative(
            anchor=anchor,
            anchor_kind="iso" if anchor else "origin",
            query="where_left_off",
        )
        lines: List[NarrativeLine] = []

        open_loops = await self._graph.open_loops(
            repo_id=repo_id, states=[LOOP_OPEN, LOOP_DORMANT]
        )
        for loop in open_loops[:max_loops]:
            suffix = " (dormant)" if loop.state == LOOP_DORMANT else ""
            lines.append(NarrativeLine(
                kind="in_flight", category="open_loop",
                text=f"still open: {_digest(loop.intent)}{suffix}",
                receipt=loop.source_event_id, at=loop.last_touched_at or loop.created_at,
            ))

        decisions = await self._graph.current_decisions(repo_id=repo_id)
        for d in decisions[:max_recent_decisions]:
            verb = "rejected" if d.stance == "rejected" else "decided"
            lines.append(NarrativeLine(
                kind="last",
                category=("rejection" if d.stance == "rejected" else "decision"),
                text=f"{d.topic}: {verb} \"{_digest(d.statement)}\"",
                receipt=d.source_event_id, at=d.created_at,
            ))

        if last_event is not None:
            lines.append(NarrativeLine(
                kind="last", category="event",
                text=f"last activity: {self._event_gist(last_event)}",
                receipt=last_event.get("id"), at=anchor,
            ))

        nar.lines = lines
        return nar

    # ------------------------------------------------------------------
    # Anchor resolution
    # ------------------------------------------------------------------
    async def resolve_anchor(
        self, since: Optional[str], *, session_gap_seconds: int = DEFAULT_SESSION_GAP_SECONDS
    ) -> Dict[str, str]:
        """Resolve a ``since`` argument into a concrete ISO anchor + a kind label.

        Accepts an ISO date/datetime (``2026-06-10`` or a full timestamp), the literal
        ``"last-session"`` (the spine is scanned for the most recent idle gap larger
        than ``session_gap_seconds``; the anchor is the event just before that gap), or
        ``None`` / empty (origin — narrate everything). Returns
        ``{"anchor": iso, "kind": "iso"|"last-session"|"origin"}``.
        """
        token = (since or "").strip()
        if not token:
            return {"anchor": "", "kind": "origin"}
        if token == "last-session":
            anchor = await self._last_session_anchor(session_gap_seconds)
            return {"anchor": anchor, "kind": "last-session" if anchor else "origin"}
        # A bare date (YYYY-MM-DD) anchors at the start of that day; ISO datetimes
        # are used verbatim. Lexical comparison handles both.
        if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
            return {"anchor": token + "T00:00:00+00:00", "kind": "iso"}
        return {"anchor": token, "kind": "iso"}

    async def _last_session_anchor(self, gap_seconds: int) -> str:
        """The ISO timestamp of the last event before the most recent idle gap.

        Scans events newest-first; the first pair whose spacing exceeds ``gap_seconds``
        marks the session boundary, and the older of the pair is the anchor (so the
        diff covers the current session). Empty when there is no such gap.
        """
        if self._db is None:
            return ""
        rows = await self._db.recent_events(limit=2000)
        timestamps = [r.get("ts", "") for r in rows if r.get("ts")]
        for newer, older in zip(timestamps, timestamps[1:]):
            if _gap_seconds(older, newer) > gap_seconds:
                return older
        return ""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _loop_change_line(self, loop: Any, anchor: str) -> Optional[NarrativeLine]:
        state = loop.state
        if state in (LOOP_DONE, LOOP_PARKED) and _iso_after(loop.updated_at, anchor):
            verb = "completed" if state == LOOP_DONE else "parked"
            return NarrativeLine(
                kind=("closed" if state == LOOP_DONE else "parked"), category="open_loop",
                text=f"{verb}: {_digest(loop.intent)}", receipt=loop.source_event_id,
                at=loop.updated_at or "",
            )
        if state in (LOOP_OPEN, LOOP_DORMANT):
            if _iso_after(loop.created_at, anchor):
                return NarrativeLine(
                    kind="added", category="open_loop",
                    text=f"new open loop: {_digest(loop.intent)}",
                    receipt=loop.source_event_id, at=loop.created_at or "",
                )
            if _iso_after(loop.last_touched_at, anchor):
                return NarrativeLine(
                    kind="reopened", category="open_loop",
                    text=f"revisited: {_digest(loop.intent)}",
                    receipt=loop.source_event_id, at=loop.last_touched_at or "",
                )
        return None

    async def _last_activity_event(self) -> Optional[Dict[str, Any]]:
        if self._db is None:
            return None
        rows = await self._db.recent_events(limit=50)
        for r in rows:
            # Skip the derived bookkeeping events so "last activity" names real work.
            if str(r.get("type", "")).startswith(("curation.", "memory.synthesized")):
                continue
            return r
        return rows[0] if rows else None

    @staticmethod
    def _event_gist(ev: Dict[str, Any]) -> str:
        etype = ev.get("type", "event")
        payload = ev.get("payload_json") or ev.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        text = payload.get("text") or payload.get("summary") or payload.get("description") or ""
        gist = _digest(str(text)) if text else ""
        return f"{etype} — {gist}" if gist else str(etype)

    async def _all_decisions(self, repo_id: Optional[str]) -> List[Dict[str, Any]]:
        return await self._fetch_all("mem_decisions", repo_id)

    async def _all_facts(self, repo_id: Optional[str]) -> List[Dict[str, Any]]:
        return await self._fetch_all("mem_facts", repo_id)

    async def _all_loops(self, repo_id: Optional[str]) -> List[Any]:
        # Loops have no superseded history to narrate, so the live-view query (which
        # also handles the repo scoping) plus the terminal states covers every change.
        return await self._graph.open_loops(
            repo_id=repo_id, states=[LOOP_OPEN, LOOP_DORMANT, LOOP_DONE, LOOP_PARKED]
        )

    async def _fetch_all(self, table: str, repo_id: Optional[str]) -> List[Dict[str, Any]]:
        """Raw rows (incl. superseded) so supersessions can be narrated old→new."""
        sql = f"SELECT * FROM {table}"
        params: List[Any] = []
        if repo_id:
            sql += " WHERE (repo_id = ? OR repo_id IS NULL)"
            params.append(repo_id)
        cur = await self._graph._db._execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _reserved_topics() -> tuple:
    from centri.memory_graph import RESERVED_FACT_TOPICS
    return RESERVED_FACT_TOPICS


def _gap_seconds(older_iso: str, newer_iso: str) -> float:
    """Seconds between two ISO timestamps; 0 on any parse failure (never raises)."""
    from datetime import datetime

    try:
        a = datetime.fromisoformat(older_iso)
        b = datetime.fromisoformat(newer_iso)
        return abs((b - a).total_seconds())
    except (TypeError, ValueError):
        return 0.0
