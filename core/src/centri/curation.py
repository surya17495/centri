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

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

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
POLICY_VERSION = "3c.2"

# Unit 2 (semantic leg ON): when the embedding-similarity feature carries a
# POSITIVE weight, briefs can change shape (a paraphrase with zero token overlap
# can now surface), so the policy identity must change. This is the deliberate
# POLICY_VERSION bump the 3c.1 contract called for. The default weight is still
# 0.0, so the default render stays on POLICY_VERSION "3c.0" with its existing
# golden; only an explicitly-enabled positive embedding weight selects this
# version (and its own golden). The active version is chosen by
# :func:`active_policy_version` from the weights, never silently.
POLICY_VERSION_EMBED = "3c.2-embed"


def active_policy_version(weights: "RankWeights") -> str:
    """The policy version implied by the active ranker weights.

    A positive ``embedding_similarity`` weight is a deliberate, shape-changing
    policy (semantic recall can now move a score), so it selects
    :data:`POLICY_VERSION_EMBED`; otherwise the byte-identical pre-embedding
    policy :data:`POLICY_VERSION`. Pure function of the weights — no wall-clock,
    no config read here — so a replay re-derives the same stamp.
    """
    return POLICY_VERSION_EMBED if weights.embedding_similarity > 0.0 else POLICY_VERSION

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

# Tags marking typed-graph nodes as legacy-provenance — ingested from HAL,
# Hermes, or migrated mempalace memory. When Centri is the active memory
# provider these are suppressed from the brief unless the cue explicitly
# references the legacy system, so stale imported memory does not crowd out
# live Centri state. Audit history is never deleted: the rows stay in the
# graph and remain visible to fact_history / open_loops queries.
LEGACY_TAGS = frozenset({"hermes", "hal", "mempalace"})

# Cue tokens that opt into legacy surfacing. When any of these appear in the
# cue's term set, legacy items are NOT suppressed — the user is asking about
# old memory and should see it.
_LEGACY_CUE_TOKENS = frozenset({"hal", "hermes", "mempalace", "hindsight"})

# Text tokens that mark a line as legacy-mentioning. Only prose that names
# HAL/mempalace/Hindsight by name is dropped.
_LEGACY_TEXT_TOKENS = frozenset({"hal", "mempalace", "hindsight"})


def _is_legacy_tags(tags: Sequence[str]) -> bool:
    return bool(LEGACY_TAGS & {t.lower() for t in tags})


def _is_legacy_source(source: str) -> bool:
    s = (source or "").lower()
    return "mempalace" in s or "hal." in s or s == "hal" or "hindsight" in s


def _cue_asks_for_legacy(cue: "Cue") -> bool:
    """The user is explicitly asking about a legacy system.

    Based on the user's own utterance plus anaphora-resolved recent turns (what
    ``it``/``that`` pointed at) — NOT on graph-hop neighbor tokens. A legacy
    decision that merely shares a token with the cue must not flip the brief
    into legacy-surfacing mode on its own, or every neighbor of a legacy node
    would unsuppress HAL/Hindsight and the ambient header would leak again.
    """
    raw_lower = (cue.raw or "").lower()
    if "old memory stack" in raw_lower:
        return True
    user_terms = set(_tokens(cue.raw)) | set(cue.anaphora_terms)
    return bool(_LEGACY_CUE_TOKENS & user_terms)


def _text_mentions_legacy(text: str) -> bool:
    """True if ``text`` names a legacy system (HAL/Hermes/mempalace/Hindsight).

    Tag-based suppression catches legacy-provenance graph nodes, but two leak
    paths remain for the rendered brief: (1) the ambient digest is a
    denormalized summary whose strings carry no tags, and (2) consolidation may
    synthesize HAL-mentioning decisions/conventions from legacy transcripts with
    non-legacy tags (e.g. ``hal.skill``). This text check is what keeps those
    out of the brief for non-legacy cues without touching the stored rows.

    Whole-word matching handles "HAL namespace", "Hindsight", and dotted forms
    like "hal.skill" (the dot splits the token). Fused CamelCase such as
    "HALMemory" lowercases to one token with no boundary, so HAL is also matched
    as a capitalized leading run followed by another uppercase letter.
    """
    if not text:
        return False
    if _LEGACY_TEXT_TOKENS & set(re.findall(r"[a-z0-9]+", text.lower())):
        return True
    return bool(re.search(r"\b(?:HAL|Hindsight)(?=[A-Z])", text))


@dataclass(frozen=True)
class RankWeights:
    """Linear-ranker feature weights (spec B). Config-overridable; the defaults
    are the ratified policy for ``POLICY_VERSION``."""

    overlap: float = 1.0          # entity/cue lexical overlap (BM25-ish)
    type_prior: float = 0.6       # decision > convention > fact > observation
    open_loop_boost: float = 0.5  # open loop whose cue the turn touches
    thread_affinity: float = 0.4  # thread-local above global background
    recency: float = 0.05         # TIEBREAK ONLY — deliberately tiny
    # Stored-vector semantic similarity (3c.1). DEFAULT 0.0 so the pre-embedding
    # golden brief is byte-identical until embeddings are deliberately turned on
    # (a POLICY_VERSION bump). With weight 0.0 the feature is computed and shown
    # in the breakdown for explainability but cannot move a score.
    embedding_similarity: float = 0.0

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
            embedding_similarity=_f("curation_w_embedding_similarity", cls.embedding_similarity),
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


# ---------------------------------------------------------------------------
# Token counting (deterministic, pinned, swappable)
# ---------------------------------------------------------------------------
# The budgeter measures every item in *real* tokens, not a word-count proxy.
# Counting is deterministic and the active counter's identity is stamped into
# the brief: changing the tokenizer changes the stamp, so a re-pinned golden is
# a deliberate act, never silent drift. The interface lets a per-hand/per-model
# tokenizer be configured later; the default is a pinned tiktoken encoding, and
# the only honest fallback (word count) is recorded in the stamp when tiktoken
# is unavailable at runtime.

# Pinned encoding for the default counter. This is part of the curation policy —
# bumping it must also bump the brief stamp.
DEFAULT_ENCODING = "o200k_base"


class TokenCounter:
    """Deterministic token counter behind a small interface.

    ``stamp`` is the tokenizer identity recorded on every brief (e.g.
    ``tiktoken:o200k_base``); ``count(text)`` returns the token count.
    """

    stamp: str = "abstract"

    def count(self, text: str) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class WordCountCounter(TokenCounter):
    """Honest fallback: whitespace word count. Used only when tiktoken is
    unavailable at runtime; its presence is visible in the brief stamp so the
    degraded path is never silent."""

    stamp = "wordcount:v1"

    def count(self, text: str) -> int:
        return len(re.findall(r"\S+", text or ""))


class TiktokenCounter(TokenCounter):
    """Real token counts from a PINNED tiktoken encoding (default policy).

    The encoding name is baked into ``stamp`` so the tokenizer version is part
    of the policy identity. Construction raises if tiktoken / the encoding is
    unavailable so the resolver can fall back honestly.
    """

    def __init__(self, encoding: str = DEFAULT_ENCODING):
        import tiktoken  # raises ImportError when unavailable

        self._enc = tiktoken.get_encoding(encoding)
        self._encoding = encoding
        self.stamp = f"tiktoken:{encoding}"

    def count(self, text: str) -> int:
        return len(self._enc.encode(text or ""))


_DEFAULT_COUNTER: Optional[TokenCounter] = None


def default_token_counter() -> TokenCounter:
    """The process-wide default counter: pinned tiktoken, word-count fallback.

    Cached so the encoding is loaded once. The chosen counter's ``stamp`` is
    what lands on the brief, so callers never need to know which path won.
    """
    global _DEFAULT_COUNTER
    if _DEFAULT_COUNTER is None:
        try:
            _DEFAULT_COUNTER = TiktokenCounter()
        except Exception:  # tiktoken missing or encoding unavailable
            _DEFAULT_COUNTER = WordCountCounter()
    return _DEFAULT_COUNTER


# ---------------------------------------------------------------------------
# Write-time embeddings (3c.1) — pinned model, pure-arithmetic read, honest fallback
# ---------------------------------------------------------------------------
# Embeddings are computed when a candidate is WRITTEN (the model name is pinned
# and recorded in the policy stamp, exactly like the tokenizer). At READ time the
# ranker only does pure cosine arithmetic over stored vectors — no model call, no
# network — so the curate() purity / golden-snapshot contract is preserved. When
# no embedding model is configured the provider is honest-unavailable: it yields
# no vectors and stamps ``embedding:unavailable``, the cosine feature is 0.0, and
# (with weight 0.0 by default) briefs are byte-identical to the pre-embedding
# policy. Turning embeddings on is therefore a deliberate POLICY_VERSION bump.


class EmbeddingProvider:
    """Deterministic write-time embedding behind a small interface.

    ``stamp`` is the embedding-model identity recorded on the brief (e.g.
    ``embedding:Qwen/Qwen3-Embedding-8B`` or ``embedding:unavailable``);
    ``embed(text)`` returns a vector or ``None`` when unavailable. Read time
    never calls this — vectors are stored on the candidate at write time.
    """

    stamp: str = "embedding:abstract"

    @property
    def available(self) -> bool:  # pragma: no cover - interface
        return False

    def embed(self, text: str) -> Optional[List[float]]:  # pragma: no cover - interface
        raise NotImplementedError


class NullEmbeddingProvider(EmbeddingProvider):
    """Honest-unavailable embedding: no model, no vectors, visible stamp.

    Used when no embedding model is configured (the default). Its presence is
    recorded in the brief stamp so the degraded path is never silent, and it
    yields ``None`` so the cosine feature contributes nothing.
    """

    stamp = "embedding:unavailable"

    @property
    def available(self) -> bool:
        return False

    def embed(self, text: str) -> Optional[List[float]]:
        return None


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local pinned-model embedding via ``fastembed`` (ONNX MiniLM-class).

    Preferred provider (Unit 2): no network at read OR write time once the model
    is cached, and the model name is pinned + recorded in the stamp like the
    tokenizer. ``fastembed`` is an OPTIONAL dependency — if it (or its model
    cache) is unavailable the constructor raises and the resolver falls back, so
    the offline default stays :class:`NullEmbeddingProvider` and tests stay green.
    """

    def __init__(self, model_name: str):
        # Imported lazily so the package is never a hard dependency. A missing
        # package / model cache raises here and the resolver degrades honestly.
        from fastembed import TextEmbedding  # type: ignore

        self._model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        self.stamp = f"embedding:local:{model_name}"

    @property
    def available(self) -> bool:
        return True

    def embed(self, text: str) -> Optional[List[float]]:
        if not (text or "").strip():
            return None
        try:
            vecs = list(self._model.embed([text]))
        except Exception:  # noqa: BLE001 — a runtime failure must not poison writes
            return None
        if not vecs:
            return None
        return [float(x) for x in vecs[0]]


class LiteLLMEmbeddingProvider(EmbeddingProvider):
    """Network embedding via the existing :class:`ModelRouter` (LiteLLM route).

    Fallback provider (Unit 2) reusing the ratified provider-key resolution
    (``CENTRI_*`` env → OpenCode auth fallback) — it never configures a provider
    twice (Decision 5). Used only when local fastembed is unavailable and an
    embedding model is configured. The model name is pinned by config and
    recorded in the stamp.
    """

    def __init__(self, model_router: Any, model_name: str):
        self._mr = model_router
        self._model_name = model_name
        self.stamp = f"embedding:litellm:{model_name}"

    @property
    def available(self) -> bool:
        return self._mr is not None

    def embed(self, text: str) -> Optional[List[float]]:
        if not (text or "").strip() or self._mr is None:
            return None
        try:
            out = self._mr.embed([text], model=self._model_name)
        except Exception:  # noqa: BLE001
            return None
        if not out or out[0] is None:
            return None
        return [float(x) for x in out[0]]


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic offline embedding for BENCH FIXTURES ONLY — clearly labeled.

    NOT a semantic model: it maps the stemmed token *set* of a text into a fixed
    bag-of-words vector over a salted hash space, so two paraphrases that share
    LEMMA-level vocabulary (e.g. "database"/"databases", "mock"/"mocks") land
    near each other while unrelated texts stay orthogonal — enough to exercise
    the embedding ranker feature and the quality-per-token bench offline, with no
    network and no model download. The stamp says ``embedding:hashing-stub`` so
    nothing mistakes it for a real model. Pure + deterministic (re-derivable).
    """

    def __init__(self, dim: int = 64):
        self._dim = dim
        self.stamp = f"embedding:hashing-stub:d{dim}"

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def _stem(tok: str) -> str:
        # Tiny deterministic stemmer so trivial morphology (plurals/verb forms)
        # does not split a concept across dimensions. Bench-only heuristic.
        for suf in ("ing", "ed", "es", "s"):
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                return tok[: -len(suf)]
        return tok

    def embed(self, text: str) -> Optional[List[float]]:
        toks = {self._stem(t) for t in _tokens(text)}
        if not toks:
            return None
        vec = [0.0] * self._dim
        for t in toks:
            h = int(hashlib.sha1(t.encode("utf-8")).hexdigest(), 16)
            vec[h % self._dim] += 1.0
        return vec


def resolve_embedding_provider(settings: Any = None, model_router: Any = None) -> EmbeddingProvider:
    """The configured embedding provider, honest-unavailable by default.

    Preference order (Unit 2): (a) a local pinned fastembed model when
    ``CENTRI_EMBEDDING_LOCAL_MODEL`` is set and the package/model is installable;
    (b) the LiteLLM route via :class:`ModelRouter` when an embedding model is
    configured (``model_embeddings`` / ``CENTRI_EMBEDDING_*``), reusing the
    existing key resolution; otherwise (c) :class:`NullEmbeddingProvider` — the
    offline default that keeps CI/tests green. Enabling embeddings is therefore
    explicit config; nothing turns on by accident.
    """
    if settings is None:
        return NullEmbeddingProvider()

    local_model = getattr(settings, "embedding_local_model", "") or ""
    if local_model:
        try:
            return LocalEmbeddingProvider(local_model)
        except Exception:  # noqa: BLE001 — package/model absent → degrade honestly
            pass

    network_model = getattr(settings, "embedding_model", "") or getattr(settings, "model_embeddings", "") or ""
    if network_model and getattr(settings, "embedding_enabled", False):
        mr = model_router
        if mr is None:
            try:
                from centri.model_router import ModelRouter

                mr = ModelRouter()
            except Exception:  # noqa: BLE001
                mr = None
        if mr is not None:
            return LiteLLMEmbeddingProvider(mr, network_model)

    return NullEmbeddingProvider()


def cosine_similarity(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    """Pure cosine similarity in [0, 1], clamped. 0.0 when either side is absent.

    Read-time arithmetic only — no model call. Negative cosines clamp to 0.0 so
    the feature only ever *adds* evidence, never subtracts (keeping the linear
    ranker's features non-negative like the others).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    sim = dot / (na * nb)
    if sim <= 0.0:
        return 0.0
    return round(min(1.0, sim), 6)


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
    # 3c.1: the cue's own embedding, computed write-time-style at cue build when a
    # provider is available. ``None`` (default) -> the cosine feature is 0.0, so
    # the deterministic lexical path is unchanged when embeddings are off.
    vector: Optional[List[float]] = None

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
        # drop out and the nouns they referred to remain.  Broadened: we always
        # enrich when recent_turns are available, not just for explicit pronouns
        # — "continue" or "fix that too" also need prior-turn context.
        anaphora_terms: List[str] = []
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
            "embedding_similarity": self._w.embedding_similarity,
        }[name]

    def _features(self, cue: Cue, cue_terms: set, c: Candidate) -> Dict[str, float]:
        hay = set(_tokens(c.topic) + _tokens(c.text) + [t.lower() for t in c.tags])
        overlap_n = len(cue_terms & hay)
        # BM25-ish saturation so a flood of repeats doesn't dominate.
        overlap = round(overlap_n / (overlap_n + 1.0), 6) if overlap_n else 0.0
        type_prior = _TYPE_PRIOR.get(c.item_type, 0.25)
        open_loop_boost = 1.0 if (c.item_type == "open_loop" and c.touches_cue) else 0.0
        # Thread affinity: same-thread items get the full boost; cross-thread
        # same-repo items get partial credit (prior session on the same project
        # is still relevant context); different repo gets nothing.
        if c.thread_id and cue.thread_id and c.thread_id == cue.thread_id:
            thread_affinity = 1.0
        elif c.repo_id and cue.repo_id and c.repo_id == cue.repo_id:
            thread_affinity = 0.5
        else:
            thread_affinity = 0.0
        recency = _recency_score(c.created_at)
        # Stored-vector semantic similarity — pure cosine, no model call. 0.0 when
        # either vector is absent (the default / honest-unavailable path).
        embedding_similarity = cosine_similarity(cue.vector, c.vector)
        return {
            "overlap": overlap,
            "type_prior": type_prior,
            "open_loop_boost": open_loop_boost,
            "thread_affinity": thread_affinity,
            "recency": recency,
            "embedding_similarity": embedding_similarity,
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

    section: str          # decisions | rejections | conventions | open_loops | photographic_recall
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

    def __init__(self, budget: Optional[Budget] = None, counter: Optional[TokenCounter] = None):
        self._b = budget or Budget()
        self._counter = counter or default_token_counter()

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
            full_cost = self._counter.count(full)
            digest = _render_item(sc.candidate, "digest")
            digest_cost = self._counter.count(digest)

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


_SECTION_ORDER = ["decisions", "rejections", "conventions", "open_loops", "photographic_recall"]
_SECTION_TITLES = {
    "decisions": "Decisions already made (do not relitigate):",
    "rejections": "Approaches already REJECTED (do not re-propose without stating what changed):",
    "conventions": "Project conventions / current facts:",
    "open_loops": "Open loops / alternatives still on the table:",
    "photographic_recall": "Photographic recall (found in raw event history):",
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
                vector=d.vector,
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
                vector=f.vector,
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

    # Obsolete project history filtering: when Centri is the active memory provider, items
    # ingested from HAL/Hermes/mempalace are treated as obsolete and filtered
    # from the candidate pool to maintain current context relevance, unless the cue
    # explicitly mentions them. The tag check catches obsolete-provenance nodes; the
    # text check also drops items whose statement/topic names an obsolete system (e.g.
    # synthesis decisions tagged ``hal.skill`` or carrying HAL/Hindsight prose), so
    # the rendered "Decisions already made / Open loops" sections stay clean.
    if not _cue_asks_for_legacy(cue):
        cands = [
            c
            for c in cands
            if not _is_legacy_tags(c.tags)
            and not _text_mentions_legacy(c.text)
            and not _text_mentions_legacy(c.topic)
        ]

    return cands
AMBIENT_TOPIC = "ambient-standing-context"

# A fact tagged with this is the consolidation-maintained ambient digest. Stored
# in the graph (with a receipt) so it is re-derivable and supersedable like any
# other node.
AMBIENT_TAG = "ambient"


@dataclass
class Ambient:
    user_profile: Dict[str, str] = field(default_factory=dict)
    identity: List[str] = field(default_factory=list)
    active_projects: List[str] = field(default_factory=list)
    open_loops: List[str] = field(default_factory=list)
    narrative: str = ""
    continuity_capsule: Dict[str, Any] = field(default_factory=dict)
    # Receipt to the most recent spine event the digest was derived from, plus a
    # bounded list of the source events it summarized — so the standing self is
    # auditable back to the verbatim events that produced it (master plan §2.8).
    source_event_id: Optional[str] = None
    derived_from: List[str] = field(default_factory=list)
    derived_at: str = ""

    def is_empty(self) -> bool:
        return not (
            self.user_profile
            or self.identity
            or self.active_projects
            or self.open_loops
            or self.narrative
            or self.continuity_capsule
        )

    def render(self, budget: int, counter: Optional[TokenCounter] = None,
               cue_terms: Optional[set] = None) -> str:
        counter = counter or default_token_counter()
        lines: List[str] = ["Standing self (continuity):"]
        if self.user_profile:
            lines.append("User Profile:")
            for k, v in self.user_profile.items():
                lines.append(f"  {k}: {v}")
        if self.identity:
            lines.append("Who/conventions: " + "; ".join(self.identity))
        if self.active_projects:
            lines.append("Active: " + "; ".join(self.active_projects))
        if self.narrative:
            lines.append("Current work: " + self.narrative)
        if self.open_loops:
            ranked = list(self.open_loops)
            if cue_terms:
                ranked.sort(
                    key=lambda s: len(set(_tokens(s)) & cue_terms),
                    reverse=True,
                )
            top = ranked[:3]
            lines.append("Top open loops: " + "; ".join(top))
        capsule = self.continuity_capsule or {}
        if capsule:
            continuity_parts = []
            time_ctx = capsule.get("current_time_context") or {}
            relative = time_ctx.get("relative_label")
            if relative:
                continuity_parts.append(f"time={relative}")
            last_decision = capsule.get("last_decision") or {}
            if last_decision.get("topic") and last_decision.get("statement"):
                continuity_parts.append(
                    f"last decision={last_decision['topic']}: {last_decision['statement']}"
                )
            suggested_next = capsule.get("suggested_next_action")
            if suggested_next:
                continuity_parts.append(f"next={suggested_next}")
            if continuity_parts:
                lines.append("Continuity: " + "; ".join(continuity_parts))
        block = "\n".join(lines)
        if block == "Standing self (continuity):":
            block = ""
        # Trim to budget deterministically: drop whole words from the end until
        # the real token count fits. Word boundaries keep the trim readable while
        # the counter (not the word count) decides when we are under budget.
        if counter.count(block) > budget:
            words = re.findall(r"\S+", block)
            while words and counter.count(" ".join(words) + "…") > budget:
                words.pop()
            block = (" ".join(words) + "…") if words else ""
        return block


async def load_ambient(graph: MemoryGraph, repo_id: Optional[str] = None) -> Ambient:
    """Read the consolidation-maintained ambient digest from the graph."""
    await graph.ensure_tables()
    try:
        user_profile = await graph.get_profile()
    except Exception:
        user_profile = {}

    for f in await graph.current_facts(repo_id=repo_id, include_reserved=True):
        if f.topic == AMBIENT_TOPIC and AMBIENT_TAG in f.tags:
            try:
                data = json.loads(f.statement)
            except (TypeError, ValueError):
                data = {}
            return Ambient(
                user_profile=user_profile,
                identity=list(data.get("identity") or []),
                active_projects=list(data.get("active_projects") or []),
                open_loops=list(data.get("open_loops") or []),
                narrative=str(data.get("narrative") or ""),
                continuity_capsule=dict(data.get("continuity_capsule") or {}),
                source_event_id=f.source_event_id,
                derived_from=list(data.get("derived_from") or []),
                derived_at=str(data.get("derived_at") or ""),
            )
    return Ambient(user_profile=user_profile)


def _suppress_ambient_legacy(ambient: Ambient) -> Ambient:
    """Return ``ambient`` with legacy-mentioning header lines dropped.

    The ambient digest is a denormalized summary that consolidation writes from
    the live graph: ``identity`` (convention strings), ``open_loops`` (intent
    strings), ``user_profile`` values, and a ``narrative`` count. Legacy nodes
    can land in those strings even though the cued candidate pool is tag- and
    text-filtered, so when Centri is the active provider and the cue is not
    asking about legacy systems, those lines are dropped here at read time. The
    stored graph rows (audit history) are never deleted — this only changes what
    the per-turn brief prepends. ``active_projects`` holds repo ids, not memory
    content, so it is passed through unchanged.
    """
    return Ambient(
        user_profile={
            k: v
            for k, v in ambient.user_profile.items()
            if not _text_mentions_legacy(k) and not _text_mentions_legacy(v)
        },
        identity=[s for s in ambient.identity if not _text_mentions_legacy(s)],
        active_projects=list(ambient.active_projects),
        open_loops=[s for s in ambient.open_loops if not _text_mentions_legacy(s)],
        narrative=ambient.narrative if not _text_mentions_legacy(ambient.narrative) else "",
        continuity_capsule=dict(ambient.continuity_capsule),
        source_event_id=ambient.source_event_id,
        derived_from=list(ambient.derived_from),
        derived_at=ambient.derived_at,
    )


# ---------------------------------------------------------------------------
# Orchestrator + render
# ---------------------------------------------------------------------------
@dataclass
class VerbatimMatch:
    text: str
    type: str
    source: str
    event_id: str
    thread_id: Optional[str] = None


# Source-priority ordering for verbatim recall. User utterances are the most
# important signal (what the person actually said), followed by assistant
# responses, then session activity, then everything else. Tool output and
# system events sort last — they are reference material, not conversation.
_VERBATIM_SOURCE_PRIORITY = {
    "user": 0,
    "utterance": 0,
    "assistant": 1,
    "transcript": 1,
    "message": 1,
    "session": 2,
    "tool": 3,
    "system": 3,
}


def _verbatim_source_priority(source: str) -> int:
    """Sort key for verbatim matches: lower = higher priority."""
    s = (source or "").lower()
    for keyword, priority in _VERBATIM_SOURCE_PRIORITY.items():
        if keyword in s:
            return priority
    return 2


@dataclass
class CuratedBrief:
    """The assembled brief: ambient + cued lines, stamped and explainable."""

    policy_version: str
    graph_high_water: str
    ambient: Ambient
    lines: List[BriefLine] = field(default_factory=list)
    cue: Optional[Cue] = None
    # Tokenizer identity (e.g. ``tiktoken:o200k_base`` or ``wordcount:v1``). Part
    # of the policy stamp: a tokenizer change changes this string, so a re-pinned
    # golden is deliberate, never silent drift.
    tokenizer_stamp: str = ""
    # Embedding-model identity (e.g. ``embedding:Qwen/Qwen3-Embedding-8B`` or
    # ``embedding:unavailable``). Also part of the policy stamp — the write-time
    # embedding model is pinned exactly like the tokenizer.
    embedding_stamp: str = "embedding:unavailable"
    verbatim: List[VerbatimMatch] = field(default_factory=list)

    def is_empty(self) -> bool:
        return self.ambient.is_empty() and not self.lines

    def render(
        self,
        *,
        with_receipts: bool = False,
        ambient_budget: int = 280,
        counter: Optional[TokenCounter] = None,
    ) -> str:
        """Human-facing render. Receipts are invisible by default (Decision 8);
        ``with_receipts=True`` appends ``[source_event_id]`` for the on-demand
        explainability view."""
        out: List[str] = []
        cue_terms = self.cue.term_set() if self.cue else None
        amb = self.ambient.render(ambient_budget, counter, cue_terms=cue_terms)
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
        if self.verbatim:
            out.append("")
            out.append("Verbatim context:")
            for m in self.verbatim:
                quoted = "\n".join(f"> {line}" for line in m.text.splitlines())
                out.append(f"{quoted} (source: {m.source})")
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
    counter: Optional[TokenCounter] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
    thread_id: Optional[str] = None,
) -> CuratedBrief:
    """Pure, versioned curation: ``brief = curate(graph, cue, budget, version)``.

    No wall-clock, no randomness, no LLM at read time. Given the same graph
    snapshot, cue, budget, policy_version, tokenizer and stored vectors, the
    rendered brief is byte-identical — the contract the golden snapshot tests
    pin. The token counter's ``stamp`` and the embedding provider's ``stamp`` are
    recorded on the brief so both are part of the policy identity.
    """
    budget = budget or Budget()
    weights = weights or RankWeights()
    counter = counter or default_token_counter()
    embedding_provider = embedding_provider or NullEmbeddingProvider()
    repo_id = repo_id if repo_id is not None else cue.repo_id

    ambient = await load_ambient(graph, repo_id=repo_id)
    # Filter obsolete HAL/Hermes/mempalace/Hindsight history from the ambient header
    # (User Profile / Who/conventions / Top open loops / Recent memory) unless
    # the cue is asking about them. Maintain current context relevance by default.
    # Mirrors gather_candidates' tag filter for the cued section; the stored digest
    # rows stay as audit history.
    if not _cue_asks_for_legacy(cue):
        ambient = _suppress_ambient_legacy(ambient)
    candidates = await gather_candidates(graph, cue, repo_id)
    ranked = Ranker(weights).rank(cue, candidates)
    lines = Budgeter(budget, counter).select(ranked)

    if cue.raw:
        clean_words = []
        for word in cue.raw.split():
            w = word.replace('"', '').replace('*', '').replace(':', '').replace('-', '').replace('+', '').replace('^', '').strip()
            if w:
                clean_words.append(f'"{w}"')
        fts_query = " OR ".join(clean_words)
        if fts_query:
            raw_events = []
            try:
                cur = await graph._db._execute(
                    """
                    SELECT events.id, events.type, events.source, events.ts, events.payload_json
                    FROM event_fts
                    JOIN events ON events.rowid = event_fts.rowid
                    WHERE event_fts MATCH ?
                      AND events.source NOT LIKE 'consolidation%'
                      AND events.source NOT LIKE 'memory%'
                    ORDER BY bm25(event_fts) ASC
                    LIMIT 5
                    """,
                    (fts_query,),
                )
                raw_events = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                logger.warning("FTS recall fallback failed, trying LIKE query: %s", e)
                try:
                    cur = await graph._db._execute(
                        """
                        SELECT id, type, source, ts, payload_json
                        FROM events
                        WHERE payload_json LIKE ?
                          AND source NOT LIKE 'consolidation%'
                          AND source NOT LIKE 'memory%'
                        LIMIT 5
                        """,
                        (f"%{cue.raw}%",),
                    )
                    raw_events = [dict(row) for row in cur.fetchall()]
                except Exception as e2:
                    logger.error("LIKE fallback also failed: %s", e2)
            
            for ev in raw_events:
                payload_str = ev.get("payload_json", "{}")
                try:
                    payload = json.loads(payload_str)
                except Exception:
                    payload = {}
                
                text_val = ""
                for key in ("text", "content", "message", "stdout", "output", "summary", "line"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.strip():
                        text_val = val.strip()
                        break
                if not text_val:
                    text_val = json.dumps(payload)
                
                lines.append(
                    BriefLine(
                        section="photographic_recall",
                        text=text_val,
                        detail="full",
                        score=1.0,
                        breakdown={"fts_score": 1.0},
                        source_event_id=ev.get("id"),
                        key=f"photo-{ev.get('id')}",
                    )
                )

    hw = await graph_high_water(graph)

    verbatim = []
    if cue.raw:
        clean_words = []
        for word in cue.raw.split():
            w = word.replace('"', '').replace('*', '').replace(':', '').replace('-', '').replace('+', '').replace('^', '').strip()
            if w:
                clean_words.append(f'"{w}"')
        fts_query = " OR ".join(clean_words)
        if fts_query:
            try:
                results = await graph._db.search_events(fts_query, limit=5)
                for r in results:
                    verbatim.append(
                        VerbatimMatch(
                            text=r["text"],
                            type=r["type"],
                            source=r["source"],
                            event_id=r["event_id"],
                            thread_id=r.get("thread_id"),
                        )
                    )
            except Exception:
                pass

    # Exclude verbatim matches from the current thread — they are already in
    # context (circular recall). Also apply obsolete-history filtering.
    if not _cue_asks_for_legacy(cue):
        verbatim = [
            v
            for v in verbatim
            if not _is_legacy_source(v.source) and not _text_mentions_legacy(v.text)
        ]

    # Dedup by first 200 chars — same text appears in multiple event types
    # (hermes.user.message, user.utterance, hermes.tool.result). Keep first.
    _seen = set()
    verbatim = [
        v for v in verbatim
        if (hash(v.text[:200]) not in _seen and not _seen.add(hash(v.text[:200])))
    ]

    # Source-priority sort: user messages rank above assistant, which ranks
    # above system/tool output. Within the same tier, BM25 order is preserved.
    verbatim.sort(key=lambda v: _verbatim_source_priority(v.source))

    return CuratedBrief(
        policy_version=policy_version,
        graph_high_water=hw,
        ambient=ambient,
        lines=lines,
        cue=cue,
        tokenizer_stamp=counter.stamp,
        embedding_stamp=embedding_provider.stamp,
        verbatim=verbatim,
    )


# ---------------------------------------------------------------------------
# Verbatim recall as a first-class turn capability (master plan §2.10)
# ---------------------------------------------------------------------------
@dataclass
class VerbatimRecall:
    """One exact, receipted recall of an original utterance from the spine.

    Unlike the passive ``VerbatimMatch`` folded into a brief, this is the result
    a TURN gets when it calls :func:`recall_verbatim` on demand. ``text`` is the
    original wording byte-for-byte (never paraphrased/distilled); ``source_event_id``
    is the receipt that resolves to the originating spine event; ``thread_id`` is
    the session that event belongs to (so a cross-session recall is provable).
    """

    text: str
    source_event_id: str
    type: str
    source: str
    thread_id: Optional[str] = None


def _verbatim_fts_query(query: str) -> str:
    """Build an OR-of-quoted-terms FTS5 query, stripping operator characters.

    Mirrors the cleaning ``curate()`` applies before ``search_events`` so a raw
    user query can never inject FTS5 syntax (which would raise and silently drop
    recall). An empty result means there were no usable terms.
    """
    clean_words = []
    for word in (query or "").split():
        w = word.replace('"', "").replace("*", "").replace(":", "").replace("-", "")
        w = w.replace("+", "").replace("^", "").strip()
        if w:
            clean_words.append(f'"{w}"')
    return " OR ".join(clean_words)


async def recall_verbatim(
    db: Any,
    query: str,
    *,
    scope: str = "global",
    limit: int = 5,
    exclude_thread_id: Optional[str] = None,
) -> List[VerbatimRecall]:
    """On-demand exact recall of original utterances over the lossless spine.

    This is the capability distillation-only incumbents structurally cannot offer
    (master plan §2.10): the verbatim original is always present in the spine, so
    exact recall is always *possible*. Reuses the existing FTS5 plumbing
    (:meth:`Database.search_events`) and the same source-priority sort + legacy
    filtering + dedup that ``curate()`` applies to its passive verbatim section —
    no duplicate storage, no second index.

    ``scope='global'`` searches the whole spine (memory is global; a session is a
    view). ``exclude_thread_id`` drops matches from the calling session so a turn
    pages in the OTHER sessions' originals, not its own already-in-context text.

    Honest degradation (master plan §2.13): if the FTS5 query errors, fall back to
    a LIKE scan; if that also fails, return ``[]`` (an honest empty result, never a
    fabricated answer). The caller emits the auditable ``recall.verbatim`` event.
    """
    fts_query = _verbatim_fts_query(query)
    if not fts_query:
        return []

    rows: List[Dict[str, Any]] = []
    try:
        rows = await db.search_events(fts_query, limit=max(limit * 3, limit))
    except Exception:
        logger.debug("recall_verbatim FTS search failed; trying LIKE fallback", exc_info=True)
        rows = await _recall_verbatim_like(db, query, limit=max(limit * 3, limit))

    matches: List[VerbatimRecall] = []
    for r in rows:
        thread_id = r.get("thread_id")
        if exclude_thread_id and thread_id == exclude_thread_id:
            continue
        text = r.get("text") or ""
        source = r.get("source") or ""
        # Same obsolete-history filtering curate() applies: never surface a legacy
        # system's text as a current verbatim recall.
        if _is_legacy_source(source) or _text_mentions_legacy(text):
            continue
        matches.append(
            VerbatimRecall(
                text=text,
                source_event_id=r.get("event_id") or r.get("id") or "",
                type=r.get("type") or "",
                source=source,
                thread_id=thread_id,
            )
        )

    # Dedup by first 200 chars (the same text appears under several event types).
    seen: set = set()
    deduped: List[VerbatimRecall] = []
    for m in matches:
        key = hash(m.text[:200])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)

    # Source-priority sort: user utterances rank above assistant, above tool/system.
    deduped.sort(key=lambda m: _verbatim_source_priority(m.source))
    return deduped[:limit]


async def _recall_verbatim_like(db: Any, query: str, *, limit: int) -> List[Dict[str, Any]]:
    """LIKE-scan fallback when FTS5 is unavailable (honest degradation, §2.13).

    Returns rows shaped like :meth:`Database.search_events` output so the caller is
    agnostic to which path produced them. Excludes the memory system's own events
    (``consolidation.*`` / ``memory.*``) exactly as the FTS path does.
    """
    terms = [w for w in (query or "").split() if w.strip()]
    if not terms:
        return []
    likes = " OR ".join("events.payload_json LIKE ?" for _ in terms)
    params = [f"%{t}%" for t in terms]
    try:
        cur = await db._execute(  # noqa: SLF001 — fallback path needs a raw scan
            f"""
            SELECT events.id AS event_id, events.type, events.source, events.thread_id,
                   events.payload_json
            FROM events
            WHERE ({likes})
              AND events.importance IN ('normal','high')
              AND events.source NOT LIKE 'consolidation%'
              AND events.source NOT LIKE 'memory%'
            ORDER BY events.ts DESC
            LIMIT ?
            """,
            (*params, limit),
        )
    except Exception:
        logger.debug("recall_verbatim LIKE fallback failed", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        d = dict(row)
        try:
            payload = json.loads(d.get("payload_json") or "{}")
        except Exception:
            payload = {}
        text = ""
        for key in ("text", "content", "statement", "intent", "description", "message"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                text = val
                break
        d["text"] = text
        out.append(d)
    return out


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
        "tokenizer_stamp": brief.tokenizer_stamp,
        "embedding_stamp": brief.embedding_stamp,
        "graph_high_water": brief.graph_high_water,
        "lines": brief.receipts(),
        "ambient_source_event_id": brief.ambient.source_event_id,
        "ambient_derived_from": list(brief.ambient.derived_from),
        "ambient_derived_at": brief.ambient.derived_at,
        "ambient_continuity_capsule": dict(brief.ambient.continuity_capsule),
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
        self._counter = default_token_counter()
        self._embeddings = resolve_embedding_provider(settings, model_router)
        self._policy_version = active_policy_version(self._weights)
        self._cue_builder = CueBuilder(graph)
        self._expander = CueExpander(settings, model_router)

    @property
    def expander(self) -> "CueExpander":
        return self._expander

    @property
    def embeddings(self) -> "EmbeddingProvider":
        return self._embeddings

    async def assemble(
        self,
        utterance: str,
        *,
        repo_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        recent_turns: Optional[Sequence[str]] = None,
        active_files: Optional[Sequence[str]] = None,
        active_task: Optional[str] = None,
        budget: Optional[Budget] = None,
    ) -> Tuple[CuratedBrief, List[Candidate], Cue]:
        # Working memory: read short-lived thread context so mid-session
        # continuity doesn't depend on re-deriving state from the full spine.
        # If the caller didn't supply active_files or active_task but working
        # memory has them from a prior turn, use those.
        if thread_id:
            try:
                wm = await self._graph._db.get_working_context(thread_id)
                if not active_files and wm.get("active_files"):
                    active_files = [f for f in wm["active_files"].split("\n") if f]
                if not active_task and wm.get("active_task"):
                    active_task = wm["active_task"]
            except Exception:
                pass

        # P4: Auto-fetch recent thread events when the caller didn't supply
        # recent_turns but did supply a thread_id. This makes the existing
        # anaphora resolution actually fire — without it, "it"/"that"/"continue"
        # get zero context lift because recent_turns is always None via the API.
        if recent_turns is None and thread_id:
            try:
                raw_events = await self._graph._db.recent_events(
                    thread_id=thread_id, limit=6
                )
                turns: List[str] = []
                for ev in reversed(raw_events):  # chronological order
                    pj = ev.get("payload_json", "")
                    if not pj:
                        continue
                    try:
                        payload = json.loads(pj) if isinstance(pj, str) else pj
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        for key in ("text", "content", "statement", "message", "cue"):
                            val = payload.get(key)
                            if val and isinstance(val, str):
                                turns.append(val)
                                break
                recent_turns = turns[-3:] if turns else None
            except Exception:
                pass

        cue = await self._cue_builder.build(
            utterance,
            thread_id=thread_id,
            repo_id=repo_id,
            recent_turns=recent_turns,
            active_files=active_files,
            active_task=active_task,
        )
        cue = await self._expander.expand(cue)
        # Embed the cue write-time-style (no-op when honest-unavailable). The
        # ranker then does pure cosine over stored candidate vectors at read.
        if self._embeddings.available and cue.vector is None:
            cue.vector = self._embeddings.embed(cue.raw)
        candidates = await gather_candidates(self._graph, cue, repo_id)
        brief = await curate(
            self._graph,
            cue,
            budget=budget or self._budget,
            weights=self._weights,
            policy_version=self._policy_version,
            repo_id=repo_id,
            counter=self._counter,
            embedding_provider=self._embeddings,
            thread_id=thread_id,
        )

        # Write working memory: store the current utterance and active state so
        # the next turn can pick up where this one left off without re-scanning
        # the full spine.
        if thread_id:
            try:
                if utterance:
                    await self._graph._db.set_working_context(thread_id, "last_utterance", utterance)
                if active_task:
                    await self._graph._db.set_working_context(thread_id, "active_task", active_task)
                if active_files:
                    await self._graph._db.set_working_context(thread_id, "active_files", "\n".join(active_files))
            except Exception:
                pass

        return brief, candidates, cue
