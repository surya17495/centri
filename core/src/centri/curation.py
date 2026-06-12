"""Deterministic context curation — "context as cache" (3c.0).

The context window is a **cache**, not storage: state lives in the ledger/graph
and per-turn context is assembled fresh by a *pure, versioned* function

    brief = curate(graph_snapshot, cue, budget, policy_version)

(ROADMAP Decisions 6-8). No wall-clock, no randomness, no LLM at read time —
the same ``(snapshot, cue, budget, policy_version)`` always renders the
byte-identical brief, which is what the golden snapshot tests pin. Every line
carries a per-feature score breakdown *and* a ``source_event_id`` receipt, but
receipts are invisible in the human-facing render by default ("no visible
remembering"); they are available on demand via the structured
:class:`CuratedBrief`.

Layers (Decision 8):
  - **ambient** — a small, slow-changing standing block present in *every* brief
    (identity/conventions, active projects, top open loops, short recent-past
    narrative), refreshed by consolidation and stored in the graph with receipts;
    prepended within its own small budget.
  - **cued** — per-turn ranked retrieval over the typed graph.

Pipeline: :class:`CueBuilder` (A) → :class:`Ranker` (B) → :class:`Budgeter` (C)
→ render, plus the ambient layer (D) and miss/waste instrumentation (E). The
optional cue-expansion LLM seam may only *expand the cue* (add query terms),
never select facts; it is honest-unavailable (logged terms, deterministic
fallback) and takes no model call in 3c.0.

Write-time embeddings are a 3c.1 follow-on: :class:`Candidate` already exposes a
``vector`` slot and the ranker reads features off the candidate, so stored-vector
similarity slots in later as one more pure-arithmetic feature with no new
dependency now.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from centri.memory_graph import (
    LOOP_OPEN,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)

# Bump when the curation policy changes in a way that alters briefs. Golden
# snapshots are keyed by this, so a deliberate change is a new snapshot, never a
# silent drift.
POLICY_VERSION = "3c.0"

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "or", "in", "on", "with", "we",
    "i", "it", "is", "be", "do", "does", "this", "that", "improve", "fix", "add",
    "make", "update", "change", "please", "lets", "let", "should", "can", "now",
    "the", "thing", "stuff", "about",
}

# Type prior: a decision outranks a convention outranks a plain fact outranks an
# observation, all else equal (spec B). Open loops rank with conventions — they
# are prospective state the turn often needs.
_TYPE_PRIOR = {
    "decision": 1.0,
    "rejection": 0.95,
    "convention": 0.8,
    "open_loop": 0.7,
    "fact": 0.5,
    "observation": 0.25,
}


@dataclass(frozen=True)
class RankWeights:
    """Linear-ranker feature weights (spec B). Config-overridable; the defaults
    are the ratified policy for ``POLICY_VERSION``."""

    overlap: float = 1.0          # entity/cue lexical overlap (BM25-ish)
    type_prior: float = 0.6       # decision > convention > fact > observation
    open_loop_boost: float = 0.5  # open loop whose cue the turn touches
    thread_affinity: float = 0.4  # thread-local above global background
    recency: float = 0.05         # TIEBREAK ONLY — deliberately tiny

    @classmethod
    def from_settings(cls, settings: Any) -> "RankWeights":
        def _f(name: str, default: float) -> float:
            val = getattr(settings, name, None)
            try:
                return float(val) if val not in (None, "") else default
            except (TypeError, ValueError):
                return default

        return cls(
            overlap=_f("curation_w_overlap", cls.overlap),
            type_prior=_f("curation_w_type_prior", cls.type_prior),
            open_loop_boost=_f("curation_w_open_loop", cls.open_loop_boost),
            thread_affinity=_f("curation_w_thread_affinity", cls.thread_affinity),
            recency=_f("curation_w_recency", cls.recency),
        )


@dataclass(frozen=True)
class Budget:
    """Token budget for a brief (spec C). Tokens approximated as words/~0.75."""

    total: int = 900
    ambient: int = 280          # the ambient layer's own small slice
    floor_decisions: int = 120  # decisions always get a minimum
    floor_rejections: int = 60

    @classmethod
    def from_settings(cls, settings: Any) -> "Budget":
        def _i(name: str, default: int) -> int:
            val = getattr(settings, name, None)
            try:
                return int(val) if val not in (None, "") else default
            except (TypeError, ValueError):
                return default

        return cls(
            total=_i("curation_budget_total", cls.total),
            ambient=_i("curation_budget_ambient", cls.ambient),
            floor_decisions=_i("curation_floor_decisions", cls.floor_decisions),
            floor_rejections=_i("curation_floor_rejections", cls.floor_rejections),
        )


def _tokens(text: str) -> List[str]:
    return [
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if w not in _STOPWORDS and len(w) > 2
    ]


def _est_tokens(text: str) -> int:
    """Deterministic token estimate — word count, no tokenizer dependency."""
    return len(re.findall(r"\S+", text or ""))


# ---------------------------------------------------------------------------
# A. Cue builder
# ---------------------------------------------------------------------------
@dataclass
class Cue:
    """A structured query built deterministically from turn-time signals.

    ``terms`` is the union of utterance tokens, alias expansions, anaphora-
    resolved tokens from recent thread turns, and one-hop graph neighbor topics.
    ``expansion_terms`` records *why* a term is present (provenance, logged on
    the spine). The ranker reads ``terms``; everything else is explainability.
    """

    raw: str
    terms: List[str] = field(default_factory=list)
    alias_hits: List[str] = field(default_factory=list)
    anaphora_terms: List[str] = field(default_factory=list)
    hop_terms: List[str] = field(default_factory=list)
    expansion_terms: List[str] = field(default_factory=list)
    thread_id: Optional[str] = None
    repo_id: Optional[str] = None
    active_files: List[str] = field(default_factory=list)
    active_task: Optional[str] = None

    def term_set(self) -> set:
        return set(self.terms)


class CueBuilder:
    """Builds a :class:`Cue` from utterance + active state + the graph (spec A).

    Deterministic: every input is a value passed in (no wall-clock, no I/O beyond
    the graph snapshot). Aliases are *facts in the graph* tagged ``alias`` whose
    topic is the alias phrase and statement the canonical term — learnable, not
    hard-coded. Anaphora resolution is verbatim token lift from the last few
    thread turns supplied by the caller. The one graph hop pulls topics of
    decisions/facts that share a matched term (neighbors of matched entities).
    """

    def __init__(self, graph: MemoryGraph):
        self._graph = graph

    async def build(
        self,
        utterance: str,
        *,
        thread_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        recent_turns: Optional[Sequence[str]] = None,
        active_files: Optional[Sequence[str]] = None,
        active_task: Optional[str] = None,
    ) -> Cue:
        await self._graph.ensure_tables()
        base = _tokens(utterance)
        terms: List[str] = list(base)
        expansion: List[str] = []

        # Alias expansion — aliases are facts tagged "alias".
        alias_hits: List[str] = []
        aliases = await self._alias_table(repo_id)
        utter_l = (utterance or "").lower()
        for phrase, canonical in aliases:
            if phrase and phrase in utter_l:
                ctoks = _tokens(canonical)
                alias_hits.append(canonical)
                for t in ctoks:
                    if t not in terms:
                        terms.append(t)
                        expansion.append(f"alias:{phrase}->{t}")

        # Anaphora — verbatim tokens from the last few turns of THIS thread. We
        # only lift content tokens (the stopword/length filter), so "it"/"that"
        # drop out and the nouns they referred to remain.
        anaphora_terms: List[str] = []
        if _has_anaphora(utterance):
            for turn in list(recent_turns or [])[-3:]:
                for t in _tokens(turn):
                    if t not in terms:
                        terms.append(t)
                        anaphora_terms.append(t)
                        expansion.append(f"anaphora:{t}")

        # Active-state signals — touched files/repo become soft terms.
        for path in active_files or []:
            for t in _tokens(path):
                if t not in terms:
                    terms.append(t)
                    expansion.append(f"active-file:{t}")

        # One deterministic graph hop — neighbors of entities matched by the
        # base terms. A neighbor shares a term with a matched node; we add its
        # topic tokens so a sibling decision on the same entity can surface.
        hop_terms: List[str] = []
        matched_topics = await self._matched_topics(set(base) | set(alias_hits and _tokens(" ".join(alias_hits))), repo_id)
        for topic in matched_topics:
            for t in _tokens(topic):
                if t not in terms:
                    terms.append(t)
                    hop_terms.append(t)
                    expansion.append(f"graph-hop:{t}")

        return Cue(
            raw=utterance,
            terms=terms,
            alias_hits=alias_hits,
            anaphora_terms=anaphora_terms,
            hop_terms=hop_terms,
            expansion_terms=expansion,
            thread_id=thread_id,
            repo_id=repo_id,
            active_files=list(active_files or []),
            active_task=active_task,
        )

    async def _alias_table(self, repo_id: Optional[str]) -> List[Tuple[str, str]]:
        facts = await self._graph.current_facts(repo_id=repo_id)
        out: List[Tuple[str, str]] = []
        for f in facts:
            if "alias" in f.tags:
                out.append((f.topic.strip().lower(), f.statement))
        # Deterministic order: longest phrase first so "the auth thing" wins over
        # "auth" when both are aliases.
        out.sort(key=lambda p: (-len(p[0]), p[0]))
        return out

    async def _matched_topics(self, base_terms: set, repo_id: Optional[str]) -> List[str]:
        if not base_terms:
            return []
        topics: List[str] = []
        seen: set = set()
        for d in await self._graph.current_decisions(repo_id=repo_id):
            if base_terms & set(_tokens(d.topic) + _tokens(d.statement)):
                if d.topic not in seen:
                    topics.append(d.topic)
                    seen.add(d.topic)
        for f in await self._graph.current_facts(repo_id=repo_id):
            if base_terms & set(_tokens(f.topic) + _tokens(f.statement)):
                if f.topic not in seen:
                    topics.append(f.topic)
                    seen.add(f.topic)
        topics.sort()  # deterministic
        return topics


_ANAPHORA = {"it", "that", "this", "them", "those", "these", "there", "again", "same"}


def _has_anaphora(utterance: str) -> bool:
    toks = re.findall(r"[a-z0-9]+", (utterance or "").lower())
    return any(t in _ANAPHORA for t in toks)


# ---------------------------------------------------------------------------
# B. Ranker
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """A scorable memory item with a receipt and an optional stored vector.

    ``vector`` is the 3c.1 write-time-embedding slot: unused in 3c.0 (kept
    ``None``), it lets stored-vector similarity become one more pure-arithmetic
    feature later without changing this interface.
    """

    key: str
    item_type: str       # decision | rejection | convention | fact | open_loop
    topic: str
    text: str
    source_event_id: Optional[str]
    created_at: str
    repo_id: Optional[str] = None
    thread_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    touches_cue: bool = False  # open loop whose cue the turn touches
    vector: Optional[List[float]] = None
    obj: Any = None  # the original graph object, for rendering


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: float
    breakdown: Dict[str, float]


class Ranker:
    """Explicit-feature linear ranker (spec B). Pure and explainable.

    Hard filters run first (superseded/invalidated never reach here — the graph's
    live views already exclude them; redaction is applied at write time). The
    score is a weighted sum of features each computable from the spine/graph, and
    every candidate keeps its per-feature breakdown for the brief.
    """

    def __init__(self, weights: Optional[RankWeights] = None):
        self._w = weights or RankWeights()

    def rank(self, cue: Cue, candidates: Sequence[Candidate]) -> List[ScoredCandidate]:
        cue_terms = cue.term_set()
        scored: List[ScoredCandidate] = []
        for c in candidates:
            bd = self._features(cue, cue_terms, c)
            total = round(sum(self._w_for(name) * v for name, v in bd.items()), 6)
            scored.append(ScoredCandidate(candidate=c, score=total, breakdown=bd))
        # Deterministic order: score desc, then created_at desc (recency is only a
        # tiebreak), then key asc to make ties total.
        scored.sort(key=lambda s: (-s.score, -_ts_ordinal(s.candidate.created_at), s.candidate.key))
        return scored

    def _w_for(self, name: str) -> float:
        return {
            "overlap": self._w.overlap,
            "type_prior": self._w.type_prior,
            "open_loop_boost": self._w.open_loop_boost,
            "thread_affinity": self._w.thread_affinity,
            "recency": self._w.recency,
        }[name]

    def _features(self, cue: Cue, cue_terms: set, c: Candidate) -> Dict[str, float]:
        hay = set(_tokens(c.topic) + _tokens(c.text) + [t.lower() for t in c.tags])
        overlap_n = len(cue_terms & hay)
        # BM25-ish saturation so a flood of repeats doesn't dominate.
        overlap = round(overlap_n / (overlap_n + 1.0), 6) if overlap_n else 0.0
        type_prior = _TYPE_PRIOR.get(c.item_type, 0.25)
        open_loop_boost = 1.0 if (c.item_type == "open_loop" and c.touches_cue) else 0.0
        # Thread affinity: thread-local items get the boost; cross-thread
        # decisions still surface (they just don't get this particular bump).
        if c.thread_id and cue.thread_id and c.thread_id == cue.thread_id:
            thread_affinity = 1.0
        else:
            thread_affinity = 0.0
        recency = _recency_score(c.created_at)
        return {
            "overlap": overlap,
            "type_prior": type_prior,
            "open_loop_boost": open_loop_boost,
            "thread_affinity": thread_affinity,
            "recency": recency,
        }


def _ts_ordinal(created_at: str) -> int:
    """Deterministic integer ordinal of an ISO timestamp (digits only).

    Used as the recency tiebreak in the ranker sort: larger == newer. No
    wall-clock — purely a function of the stored string, so the ordering is
    reproducible at any read time.
    """
    digits = re.sub(r"\D", "", created_at or "")[:14]
    return int(digits) if digits else 0


def _recency_score(created_at: str) -> float:
    """Tiebreak-only recency: a tiny deterministic function of the ISO string.

    Crucially NO wall-clock — comparing to ``now()`` would make briefs vary by
    read time and break the pure-function contract. Instead we map the ISO
    timestamp's lexical position to a tiny [0,1) bump so newer (lexically larger)
    timestamps edge out older ones at equal score, deterministically.
    """
    if not created_at:
        return 0.0
    # Use year+month+day+hour digits as a monotonic-ish small fraction.
    digits = re.sub(r"\D", "", created_at)[:12] or "0"
    # Normalize into [0,1) by a fixed divisor wide enough for 12 digits.
    try:
        return round(int(digits) / 1_000_000_000_000.0, 6)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# C. Budgeter (knapsack with digest fallback)
# ---------------------------------------------------------------------------
@dataclass
class BriefLine:
    """One rendered line with its receipt + score breakdown (explainability)."""

    section: str          # decisions | rejections | conventions | open_loops
    text: str             # the rendered, human-facing line (no receipt inline)
    detail: str           # "full" | "digest"
    score: float
    breakdown: Dict[str, float]
    source_event_id: Optional[str]
    key: str


def _digest(text: str, limit_words: int = 14) -> str:
    words = re.findall(r"\S+", text or "")
    if len(words) <= limit_words:
        return text
    return " ".join(words[:limit_words]) + "…"


class Budgeter:
    """Greedy knapsack by score with per-section floors + digest fallback (C).

    Each kept item is rendered full, as a one-line digest, or dropped — chosen
    deterministically by descending score under the token budget. Section floors
    reserve a minimum so decisions never get starved by a flood of facts.
    """

    def __init__(self, budget: Optional[Budget] = None):
        self._b = budget or Budget()

    def select(self, ranked: Sequence[ScoredCandidate]) -> List[BriefLine]:
        remaining = self._b.total
        # Reserve section floors up front.
        reserved = {
            "decisions": self._b.floor_decisions,
            "rejections": self._b.floor_rejections,
        }
        lines: List[BriefLine] = []
        for sc in ranked:
            section = _section_of(sc.candidate.item_type)
            full = _render_item(sc.candidate, "full")
            full_cost = _est_tokens(full)
            digest = _render_item(sc.candidate, "digest")
            digest_cost = _est_tokens(digest)

            floor = reserved.get(section, 0)
            avail = remaining + floor  # this section may dip into its reservation
            if full_cost <= avail:
                chosen, cost, detail = full, full_cost, "full"
            elif digest_cost <= avail:
                chosen, cost, detail = digest, digest_cost, "digest"
            else:
                continue  # dropped — cannot afford even a digest

            # Spend the floor first, then the shared pool.
            spend_from_floor = min(floor, cost)
            if section in reserved:
                reserved[section] = floor - spend_from_floor
            remaining -= max(0, cost - spend_from_floor)

            lines.append(
                BriefLine(
                    section=section,
                    text=chosen,
                    detail=detail,
                    score=sc.score,
                    breakdown=sc.breakdown,
                    source_event_id=sc.candidate.source_event_id,
                    key=sc.candidate.key,
                )
            )
        return lines


_SECTION_ORDER = ["decisions", "rejections", "conventions", "open_loops"]
_SECTION_TITLES = {
    "decisions": "Decisions already made (do not relitigate):",
    "rejections": "Approaches already REJECTED (do not re-propose without stating what changed):",
    "conventions": "Project conventions / current facts:",
    "open_loops": "Open loops / alternatives still on the table:",
}


def _section_of(item_type: str) -> str:
    if item_type == "decision":
        return "decisions"
    if item_type == "rejection":
        return "rejections"
    if item_type == "open_loop":
        return "open_loops"
    return "conventions"  # convention + fact both render under conventions


def _render_item(c: Candidate, detail: str) -> str:
    body = c.text if detail == "full" else _digest(c.text)
    if c.item_type in ("convention", "fact"):
        return f"{c.topic}: {body}"
    if c.item_type == "open_loop":
        return body
    return body


# ---------------------------------------------------------------------------
# Candidate extraction from the graph snapshot
# ---------------------------------------------------------------------------
async def gather_candidates(
    graph: MemoryGraph, cue: Cue, repo_id: Optional[str]
) -> List[Candidate]:
    """Pull the live (non-superseded) graph items as scorable candidates.

    Hard filter: only live views are read, so superseded/invalidated nodes never
    enter. Aliases are infrastructure for the cue, not brief content, so they are
    excluded here.
    """
    await graph.ensure_tables()
    cands: List[Candidate] = []

    for d in await graph.current_decisions(repo_id=repo_id):
        item_type = "rejection" if d.stance == STANCE_REJECTED else "decision"
        text = d.statement + (f" — {d.rationale}" if d.rationale else "")
        cands.append(
            Candidate(
                key=f"decision:{d.id}",
                item_type=item_type,
                topic=d.topic,
                text=text,
                source_event_id=d.source_event_id,
                created_at=d.created_at,
                repo_id=d.repo_id,
                tags=list(d.tags),
                obj=d,
            )
        )

    for f in await graph.current_facts(repo_id=repo_id):
        # Aliases are cue infrastructure; the ambient digest is its own layer.
        # Neither is a cued candidate.
        if "alias" in f.tags or AMBIENT_TAG in f.tags:
            continue
        item_type = "convention" if "convention" in f.tags else "fact"
        cands.append(
            Candidate(
                key=f"fact:{f.id}",
                item_type=item_type,
                topic=f.topic,
                text=f.statement,
                source_event_id=f.source_event_id,
                created_at=f.created_at,
                repo_id=f.repo_id,
                tags=list(f.tags),
                obj=f,
            )
        )

    cue_terms = cue.term_set()
    for loop in await graph.open_loops(repo_id=repo_id, states=[LOOP_OPEN]):
        hay = set(_tokens(loop.intent) + _tokens(loop.cue) + [t.lower() for t in loop.tags])
        cands.append(
            Candidate(
                key=f"loop:{loop.id}",
                item_type="open_loop",
                topic=loop.intent[:40],
                text=loop.intent + (f" (cue: {loop.cue})" if loop.cue else ""),
                source_event_id=loop.source_event_id,
                created_at=loop.created_at,
                repo_id=loop.repo_id,
                tags=list(loop.tags),
                touches_cue=bool(cue_terms & hay),
                obj=loop,
            )
        )

    return cands


# ---------------------------------------------------------------------------
# D. Ambient layer
# ---------------------------------------------------------------------------
AMBIENT_TOPIC = "ambient-standing-context"

# A fact tagged with this is the consolidation-maintained ambient digest. Stored
# in the graph (with a receipt) so it is re-derivable and supersedable like any
# other node.
AMBIENT_TAG = "ambient"


@dataclass
class Ambient:
    identity: List[str] = field(default_factory=list)
    active_projects: List[str] = field(default_factory=list)
    open_loops: List[str] = field(default_factory=list)
    narrative: str = ""
    source_event_id: Optional[str] = None

    def is_empty(self) -> bool:
        return not (self.identity or self.active_projects or self.open_loops or self.narrative)

    def render(self, budget: int) -> str:
        lines: List[str] = []
        if self.identity:
            lines.append("Who/conventions: " + "; ".join(self.identity))
        if self.active_projects:
            lines.append("Active: " + "; ".join(self.active_projects))
        if self.open_loops:
            lines.append("Top open loops: " + "; ".join(self.open_loops))
        if self.narrative:
            lines.append(self.narrative)
        block = "\n".join(lines)
        # Trim to budget deterministically (whole words).
        if _est_tokens(block) > budget:
            words = re.findall(r"\S+", block)[:budget]
            block = " ".join(words) + "…"
        return block


async def load_ambient(graph: MemoryGraph, repo_id: Optional[str] = None) -> Ambient:
    """Read the consolidation-maintained ambient digest from the graph."""
    await graph.ensure_tables()
    for f in await graph.current_facts(repo_id=repo_id, include_reserved=True):
        if f.topic == AMBIENT_TOPIC and AMBIENT_TAG in f.tags:
            try:
                data = json.loads(f.statement)
            except (TypeError, ValueError):
                data = {}
            return Ambient(
                identity=list(data.get("identity") or []),
                active_projects=list(data.get("active_projects") or []),
                open_loops=list(data.get("open_loops") or []),
                narrative=str(data.get("narrative") or ""),
                source_event_id=f.source_event_id,
            )
    return Ambient()


# ---------------------------------------------------------------------------
# Orchestrator + render
# ---------------------------------------------------------------------------
@dataclass
class CuratedBrief:
    """The assembled brief: ambient + cued lines, stamped and explainable."""

    policy_version: str
    graph_high_water: str
    ambient: Ambient
    lines: List[BriefLine] = field(default_factory=list)
    cue: Optional[Cue] = None

    def is_empty(self) -> bool:
        return self.ambient.is_empty() and not self.lines

    def render(self, *, with_receipts: bool = False, ambient_budget: int = 280) -> str:
        """Human-facing render. Receipts are invisible by default (Decision 8);
        ``with_receipts=True`` appends ``[source_event_id]`` for the on-demand
        explainability view."""
        out: List[str] = []
        amb = self.ambient.render(ambient_budget)
        if amb:
            out.append(amb)
            out.append("")
        body: List[str] = []
        for section in _SECTION_ORDER:
            section_lines = [ln for ln in self.lines if ln.section == section]
            if not section_lines:
                continue
            body.append(_SECTION_TITLES[section])
            for ln in section_lines:
                suffix = f" [{ln.source_event_id or 'no-receipt'}]" if with_receipts else ""
                body.append(f"  - {ln.text}{suffix}")
        if body:
            out.append("Memory (assembled from the event ledger):")
            out.extend(body)
        return "\n".join(out).rstrip()

    def receipts(self) -> List[Dict[str, Any]]:
        """On-demand explainability: every line's score breakdown + receipt."""
        return [
            {
                "section": ln.section,
                "key": ln.key,
                "detail": ln.detail,
                "score": ln.score,
                "breakdown": ln.breakdown,
                "source_event_id": ln.source_event_id,
            }
            for ln in self.lines
        ]


async def graph_high_water(graph: MemoryGraph) -> str:
    """A deterministic snapshot id: the max created_at across live graph nodes.

    Stamped on the brief so a replay can prove which graph state produced it.
    """
    hw = ""
    for d in await graph.current_decisions():
        if d.created_at > hw:
            hw = d.created_at
    for f in await graph.current_facts():
        if f.created_at > hw:
            hw = f.created_at
    for loop in await graph.open_loops(states=[LOOP_OPEN]):
        if loop.created_at > hw:
            hw = loop.created_at
    return hw


async def curate(
    graph: MemoryGraph,
    cue: Cue,
    *,
    budget: Optional[Budget] = None,
    weights: Optional[RankWeights] = None,
    policy_version: str = POLICY_VERSION,
    repo_id: Optional[str] = None,
) -> CuratedBrief:
    """Pure, versioned curation: ``brief = curate(graph, cue, budget, version)``.

    No wall-clock, no randomness, no LLM. Given the same graph snapshot, cue,
    budget and policy_version, the rendered brief is byte-identical — the
    contract the golden snapshot tests pin.
    """
    budget = budget or Budget()
    weights = weights or RankWeights()
    repo_id = repo_id if repo_id is not None else cue.repo_id

    ambient = await load_ambient(graph, repo_id=repo_id)
    candidates = await gather_candidates(graph, cue, repo_id)
    ranked = Ranker(weights).rank(cue, candidates)
    lines = Budgeter(budget).select(ranked)
    hw = await graph_high_water(graph)
    return CuratedBrief(
        policy_version=policy_version,
        graph_high_water=hw,
        ambient=ambient,
        lines=lines,
        cue=cue,
    )


# ---------------------------------------------------------------------------
# Optional cue-expansion seam (honest-unavailable)
# ---------------------------------------------------------------------------
class CueExpander:
    """Optional LLM cue-expansion seam (Decision 7): may EXPAND THE CUE only.

    It rewrites an oblique ask into extra *query terms* — it never selects facts.
    In 3c.0 it is honest-unavailable: with no model configured it returns the cue
    unchanged (deterministic fallback). When a model IS configured the call would
    happen here; either way the added terms are logged on the spine via
    :meth:`expansion_log` so a replay can see exactly what the cue became.
    """

    def __init__(self, settings: Any = None, model_router: Any = None):
        self._settings = settings
        self._mr = model_router
        self._configured = bool(getattr(settings, "curation_cue_expansion", "") or "") if settings else False

    @property
    def available(self) -> bool:
        return self._configured

    async def expand(self, cue: Cue) -> Cue:
        """Return a (possibly) term-expanded cue. Deterministic fallback = no-op.

        No model call is made in 3c.0 even when configured — the seam exists,
        honest-unavailable, so the read path stays deterministic until 3c.1 wires
        a real expander behind it.
        """
        # Honest-unavailable: the seam is wired, but the deterministic fallback
        # (return the cue unchanged) is what runs until a real expander lands.
        return cue

    def expansion_log(self, cue: Cue) -> Dict[str, Any]:
        """The spine record of what expansion did — terms + provenance."""
        return {
            "available": self.available,
            "expansion_terms": list(cue.expansion_terms),
            "alias_hits": list(cue.alias_hits),
            "anaphora_terms": list(cue.anaphora_terms),
            "hop_terms": list(cue.hop_terms),
        }


# ---------------------------------------------------------------------------
# E. Instrumentation — curation.miss / curation.waste
# ---------------------------------------------------------------------------
def _norm_topics(texts: Sequence[str]) -> set:
    out: set = set()
    for t in texts:
        out |= set(_tokens(t))
    return out


def compute_miss_waste(
    brief: CuratedBrief,
    graph_candidates: Sequence[Candidate],
    turn_text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic miss/waste detection against a turn's resulting transcript.

    - ``miss``: a live graph item whose topic tokens appear in the turn transcript
      but which was NOT included in the brief (a fact the turn needed, unsurfaced).
    - ``waste``: an included line whose topic tokens never appear in the transcript
      (surfaced but unused).

    Returns ``(misses, wastes)`` as receipt-bearing dicts for the spine. The
    replay harness (3c.1) consumes these; 3c.0 only needs the events to exist.
    """
    turn_tokens = set(_tokens(turn_text))
    included_keys = {ln.key for ln in brief.lines}

    misses: List[Dict[str, Any]] = []
    for c in graph_candidates:
        if c.key in included_keys:
            continue
        item_tokens = set(_tokens(c.topic) + _tokens(c.text))
        if item_tokens and (item_tokens & turn_tokens):
            misses.append(
                {
                    "key": c.key,
                    "item_type": c.item_type,
                    "topic": c.topic,
                    "source_event_id": c.source_event_id,
                    "matched_terms": sorted(item_tokens & turn_tokens),
                }
            )

    wastes: List[Dict[str, Any]] = []
    for ln in brief.lines:
        line_tokens = set(_tokens(ln.text))
        if line_tokens and not (line_tokens & turn_tokens):
            wastes.append(
                {
                    "key": ln.key,
                    "section": ln.section,
                    "source_event_id": ln.source_event_id,
                    "score": ln.score,
                }
            )

    misses.sort(key=lambda m: m["key"])
    wastes.sort(key=lambda w: w["key"])
    return misses, wastes


def curation_breakdown_payload(brief: CuratedBrief) -> Dict[str, Any]:
    """Structured curation receipt for stamping on the delegation event."""
    return {
        "policy_version": brief.policy_version,
        "graph_high_water": brief.graph_high_water,
        "lines": brief.receipts(),
        "ambient_source_event_id": brief.ambient.source_event_id,
    }


# ---------------------------------------------------------------------------
# Live-path orchestrator
# ---------------------------------------------------------------------------
class Curator:
    """Wires the deterministic curation pipeline into the live brief path.

    This is the object the coordinator holds (see
    :meth:`Coordinator.build_delegation_brief`). It builds a :class:`Cue` from
    turn-time signals, runs the optional honest-unavailable cue-expansion seam,
    calls the pure :func:`curate`, and returns the :class:`CuratedBrief` plus the
    candidate set (so the caller can compute miss/waste against the resulting
    turn). Config (budget/weights/expansion seam) is read once from settings.
    """

    def __init__(self, graph: MemoryGraph, settings: Any = None, model_router: Any = None):
        self._graph = graph
        self._settings = settings
        self._budget = Budget.from_settings(settings) if settings is not None else Budget()
        self._weights = RankWeights.from_settings(settings) if settings is not None else RankWeights()
        self._cue_builder = CueBuilder(graph)
        self._expander = CueExpander(settings, model_router)

    @property
    def expander(self) -> "CueExpander":
        return self._expander

    async def assemble(
        self,
        utterance: str,
        *,
        repo_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        recent_turns: Optional[Sequence[str]] = None,
        active_files: Optional[Sequence[str]] = None,
        active_task: Optional[str] = None,
    ) -> Tuple[CuratedBrief, List[Candidate], Cue]:
        cue = await self._cue_builder.build(
            utterance,
            thread_id=thread_id,
            repo_id=repo_id,
            recent_turns=recent_turns,
            active_files=active_files,
            active_task=active_task,
        )
        cue = await self._expander.expand(cue)
        candidates = await gather_candidates(self._graph, cue, repo_id)
        brief = await curate(
            self._graph,
            cue,
            budget=self._budget,
            weights=self._weights,
            repo_id=repo_id,
        )
        return brief, candidates, cue
