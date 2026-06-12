"""3c.1 — replay harness, quality-per-token bench, and tiered digests.

This module sits *beside* :mod:`centri.curation` so the golden-pinned read-path
surface in that module stays stable. It adds the three measurable-memory pieces
of Phase 1 (VISION "Memory completion"):

  - **Tiered digests** (:class:`DigestBuilder`) — daily -> weekly roll-ups of the
    typed graph, as DERIVED VIEWS over a lossless spine (Decision 13). Nothing is
    deleted; a digest is a read-time presentation of nodes grouped by their
    ``created_at`` window. Summarization is deterministic by default (a stable
    truncated join), with an optional LLM seam that is honest-unavailable and
    never invents.
  - **Replay harness** (:class:`ReplayHarness`) — re-run any curation policy over
    the historical ``curation.brief`` ledger and score miss/waste, so a policy
    change is measured against recorded turns, not asserted.
  - **Quality-per-token bench** (:func:`quality_per_token`) — precision/recall of
    the facts a turn actually needed, per token spent: the headline 3c metric.

Everything here is pure given its inputs (the recorded ledger + a policy); no
wall-clock, no randomness, no network. The optional LLM digest seam, when
unconfigured, falls back deterministically so the suite runs offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from centri.curation import (
    POLICY_VERSION,
    Candidate,
    TokenCounter,
    _digest,
    default_token_counter,
)


# ---------------------------------------------------------------------------
# Tiered digests — derived views over a lossless spine (Decision 13)
# ---------------------------------------------------------------------------
def _day_key(created_at: str) -> str:
    """The YYYY-MM-DD bucket of an ISO timestamp (lexical prefix, no parsing)."""
    return (created_at or "")[:10]


def _iso_week_key(created_at: str) -> str:
    """A deterministic weekly bucket label from an ISO date.

    Pure string arithmetic over the YYYY-MM-DD prefix — no calendar library, so
    it is reproducible and never depends on locale/wall-clock. The label is
    ``YYYY-Www`` where ``ww`` is the ordinal week within the year derived from the
    day-of-year, which is good enough to GROUP nodes into stable weekly tiers (the
    digest is a presentation view, not a date authority — receipts point at the
    exact node).
    """
    date = (created_at or "")[:10]
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date)
    if not m:
        return "undated"
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Cumulative days before each month (non-leap baseline; the +leap correction
    # keeps week boundaries stable enough for grouping). Deterministic.
    cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    leap = 1 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) and month > 2 else 0
    doy = cum[month - 1] + day + leap
    week = (doy - 1) // 7 + 1
    return f"{year}-W{week:02d}"


class DigestSummarizer:
    """Optional LLM digest-summarizer seam (honest-unavailable).

    A digest line is produced from a bucket's member texts. The deterministic
    fallback (no model configured) is a stable truncated join — exactly the kind
    of presentation collapse Decision 13 calls a read-time policy. A real
    summarizer slots in behind :meth:`summarize` without changing callers; it may
    only condense, never invent (the member receipts remain the ground truth).
    """

    def __init__(self, settings: Any = None, model_router: Any = None):
        self._settings = settings
        self._mr = model_router
        self._configured = bool(getattr(settings, "model_summarization", "") or "") and model_router is not None

    @property
    def available(self) -> bool:
        # 3c.1 keeps the read path deterministic: even when a summarization model
        # is configured we do NOT call it here, so digests stay re-derivable and
        # offline. The seam is wired for a later prose pass (Phase 2).
        return False

    def summarize(self, member_texts: Sequence[str], limit_words: int = 18) -> str:
        """Deterministic digest line: a stable, length-bounded join of members."""
        joined = "; ".join(t.strip() for t in member_texts if t and t.strip())
        return _digest(joined, limit_words=limit_words)


@dataclass
class DigestTier:
    """One digest bucket: a window label, a one-line summary, and receipts."""

    tier: str            # "daily" | "weekly"
    window: str          # bucket label (e.g. "2026-01-03" or "2026-W01")
    summary: str         # the derived, length-bounded digest line
    member_keys: List[str] = field(default_factory=list)
    receipts: List[Optional[str]] = field(default_factory=list)  # source_event_ids

    def is_empty(self) -> bool:
        return not self.member_keys


class DigestBuilder:
    """Builds tiered (daily -> weekly) digests from typed graph candidates.

    A digest is a DERIVED VIEW: the spine and graph are untouched, the builder
    only groups live nodes by their ``created_at`` window and emits a summary
    line per window with the member receipts. Deterministic given the same
    candidates + summarizer. Buckets and members are sorted so the output is
    byte-stable across runs (the re-derivability invariant).
    """

    def __init__(self, summarizer: Optional[DigestSummarizer] = None):
        self._summarizer = summarizer or DigestSummarizer()

    def build(self, candidates: Sequence[Candidate], *, tier: str = "daily") -> List[DigestTier]:
        key_fn = _day_key if tier == "daily" else _iso_week_key
        buckets: Dict[str, List[Candidate]] = {}
        for c in candidates:
            buckets.setdefault(key_fn(c.created_at), []).append(c)

        tiers: List[DigestTier] = []
        for window in sorted(buckets):
            members = sorted(buckets[window], key=lambda c: (c.created_at, c.key))
            summary = self._summarizer.summarize([self._member_text(c) for c in members])
            tiers.append(
                DigestTier(
                    tier=tier,
                    window=window,
                    summary=summary,
                    member_keys=[c.key for c in members],
                    receipts=[c.source_event_id for c in members],
                )
            )
        return tiers

    @staticmethod
    def _member_text(c: Candidate) -> str:
        topic = (c.topic or "").strip()
        body = (c.text or "").strip()
        return f"{topic}: {body}" if topic and topic.lower() not in body.lower() else body


# ---------------------------------------------------------------------------
# Quality-per-token — the headline 3c metric
# ---------------------------------------------------------------------------
@dataclass
class QualityScore:
    """Precision/recall of the facts a turn actually needed, per token spent."""

    needed: int          # facts the turn needed (included-hits + misses)
    included_hits: int   # brief lines the turn actually used
    misses: int          # needed facts the brief did NOT surface
    wastes: int          # brief lines the turn never used
    tokens: int          # token cost of the brief body
    precision: float     # included_hits / (included_hits + wastes)
    recall: float        # included_hits / (included_hits + misses)
    quality_per_token: float  # f1 / tokens (the headline ratio)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "needed": self.needed,
            "included_hits": self.included_hits,
            "misses": self.misses,
            "wastes": self.wastes,
            "tokens": self.tokens,
            "precision": self.precision,
            "recall": self.recall,
            "quality_per_token": self.quality_per_token,
        }


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0.0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)


def quality_per_token(
    *,
    included_keys: Sequence[str],
    miss_count: int,
    waste_count: int,
    tokens: int,
) -> QualityScore:
    """Score one curated turn from its miss/waste ledger + token cost.

    A line is a *hit* if it was included and not counted as waste; a *miss* is a
    needed fact left out. Precision = hits/(hits+waste), recall = hits/(hits+miss),
    and quality-per-token = F1 / tokens. Pure arithmetic over recorded counts, so
    a replay can re-derive it identically.
    """
    included = len(included_keys)
    included_hits = max(0, included - waste_count)
    needed = included_hits + miss_count
    precision = round(included_hits / included, 6) if included else 0.0
    recall = round(included_hits / needed, 6) if needed else (1.0 if included_hits == 0 else 0.0)
    f1 = _f1(precision, recall)
    qpt = round(f1 / tokens, 8) if tokens else 0.0
    return QualityScore(
        needed=needed,
        included_hits=included_hits,
        misses=miss_count,
        wastes=waste_count,
        tokens=max(0, tokens),
        precision=precision,
        recall=recall,
        quality_per_token=qpt,
    )


# ---------------------------------------------------------------------------
# Replay harness — re-score recorded turns; measure, don't assert
# ---------------------------------------------------------------------------
@dataclass
class ReplayTurn:
    """One recorded curated turn pulled from the ``curation.brief`` ledger."""

    event_id: Optional[str]
    turn_kind: str
    policy_version: str
    tokenizer_stamp: str
    embedding_stamp: str
    graph_high_water: str
    included_keys: List[str]
    miss_count: int
    waste_count: int
    tokens: int


@dataclass
class ReplayReport:
    """Aggregate quality-per-token over a replayed ledger, partitioned by kind."""

    policy_version: str
    turns: int
    chat_turns: int
    delegation_turns: int
    total_misses: int
    total_wastes: int
    total_tokens: int
    mean_precision: float
    mean_recall: float
    mean_quality_per_token: float
    per_turn: List[QualityScore] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "turns": self.turns,
            "chat_turns": self.chat_turns,
            "delegation_turns": self.delegation_turns,
            "total_misses": self.total_misses,
            "total_wastes": self.total_wastes,
            "total_tokens": self.total_tokens,
            "mean_precision": self.mean_precision,
            "mean_recall": self.mean_recall,
            "mean_quality_per_token": self.mean_quality_per_token,
        }


def _line_text_tokens(line: Dict[str, Any]) -> int:
    """Token estimate for one recorded brief line (word count fallback).

    The recorded receipt does not store the rendered text length, so the harness
    approximates a line's cost by the section/key it carries. This keeps the
    harness self-contained over the ledger; the live path measures real tokens.
    """
    text = " ".join(str(line.get(k, "")) for k in ("key", "section", "detail"))
    return len(re.findall(r"\S+", text))


class ReplayHarness:
    """Replay recorded curation turns and score quality-per-token.

    The harness reads ``curation.brief`` events from a :class:`centri.db.Database`
    (the lossless spine), reconstructs each turn's included-key set + miss/waste
    counts + token cost, and scores them with :func:`quality_per_token`. It does
    NOT need to re-run :func:`curate` because the brief receipts already record
    the policy's output; this makes a policy comparison a pure re-scoring of the
    ledger (the replay invariant: measure against recorded turns).

    A live re-curation variant can be layered on top by passing the historical
    cue back through :func:`curate`; 3c.1 ships the ledger re-scoring path, which
    is the one the miss/waste instrumentation was designed to feed.
    """

    def __init__(self, db: Any, counter: Optional[TokenCounter] = None):
        self._db = db
        self._counter = counter or default_token_counter()

    async def load_turns(
        self, *, policy_version: Optional[str] = None, limit: int = 10_000
    ) -> List[ReplayTurn]:
        import json

        rows = await self._db.recent_events(limit=limit)
        turns: List[ReplayTurn] = []
        for r in rows:
            if r.get("type") != "curation.brief":
                continue
            payload = r.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if isinstance(r.get("payload_json"), str) and not payload:
                try:
                    payload = json.loads(r["payload_json"])
                except (TypeError, ValueError):
                    payload = {}
            if policy_version and payload.get("policy_version") != policy_version:
                continue
            lines = payload.get("lines") or []
            included_keys = [ln.get("key") for ln in lines if isinstance(ln, dict)]
            tokens = sum(_line_text_tokens(ln) for ln in lines if isinstance(ln, dict))
            turns.append(
                ReplayTurn(
                    event_id=r.get("id"),
                    turn_kind=str(payload.get("turn_kind") or "unknown"),
                    policy_version=str(payload.get("policy_version") or ""),
                    tokenizer_stamp=str(payload.get("tokenizer_stamp") or ""),
                    embedding_stamp=str(payload.get("embedding_stamp") or ""),
                    graph_high_water=str(payload.get("graph_high_water") or ""),
                    included_keys=[k for k in included_keys if k],
                    miss_count=int(payload.get("miss_count") or 0),
                    waste_count=int(payload.get("waste_count") or 0),
                    tokens=tokens,
                )
            )
        # Deterministic order: oldest-first by event id (recent_events is newest-first).
        turns.sort(key=lambda t: (t.event_id or ""))
        return turns

    def score_turns(self, turns: Sequence[ReplayTurn]) -> ReplayReport:
        scores: List[QualityScore] = []
        for t in turns:
            scores.append(
                quality_per_token(
                    included_keys=t.included_keys,
                    miss_count=t.miss_count,
                    waste_count=t.waste_count,
                    tokens=t.tokens,
                )
            )
        n = len(scores) or 1
        chat = sum(1 for t in turns if t.turn_kind == "chat")
        deleg = sum(1 for t in turns if t.turn_kind == "delegation")
        policy = turns[0].policy_version if turns else POLICY_VERSION
        return ReplayReport(
            policy_version=policy,
            turns=len(turns),
            chat_turns=chat,
            delegation_turns=deleg,
            total_misses=sum(s.misses for s in scores),
            total_wastes=sum(s.wastes for s in scores),
            total_tokens=sum(s.tokens for s in scores),
            mean_precision=round(sum(s.precision for s in scores) / n, 6),
            mean_recall=round(sum(s.recall for s in scores) / n, 6),
            mean_quality_per_token=round(sum(s.quality_per_token for s in scores) / n, 8),
            per_turn=scores,
        )

    async def run(self, *, policy_version: Optional[str] = None) -> ReplayReport:
        turns = await self.load_turns(policy_version=policy_version)
        return self.score_turns(turns)


def report(rep: ReplayReport) -> str:
    """Human-readable replay summary (mirrors bench.harness.report style)."""
    lines: List[str] = []
    lines.append("centri curation replay — quality-per-token")
    lines.append("=" * 72)
    lines.append(f"policy_version: {rep.policy_version}")
    lines.append(f"turns: {rep.turns} (chat={rep.chat_turns}, delegation={rep.delegation_turns})")
    lines.append(f"total misses: {rep.total_misses}   total wastes: {rep.total_wastes}")
    lines.append(f"total tokens spent: {rep.total_tokens}")
    lines.append(f"mean precision:        {rep.mean_precision:.3f}")
    lines.append(f"mean recall:           {rep.mean_recall:.3f}")
    lines.append(f"mean quality-per-token: {rep.mean_quality_per_token:.6f}")
    return "\n".join(lines)
