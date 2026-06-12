"""3c.1 — replay harness, quality-per-token bench, tiered digests, embeddings.

These tests cover the Phase-1 "memory completion" pieces that sit beside the
golden-pinned curation read path:

  - tiered digests are DERIVED VIEWS over a lossless spine (Decision 13): grouping
    live nodes by created_at window, deterministic + re-derivable, with receipts;
  - quality-per-token scores precision/recall of the facts a turn needed per token;
  - the replay harness re-scores the recorded ``curation.brief`` ledger so a policy
    is measured, not asserted — and it partitions chat vs delegation turns;
  - write-time embeddings slot in as a pure-arithmetic ranker feature that is
    honest-unavailable by default (no network/keys) and cannot move the golden
    brief while its weight is 0.0.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.curation import (
    POLICY_VERSION,
    Candidate,
    NullEmbeddingProvider,
    RankWeights,
    Ranker,
    Cue,
    cosine_similarity,
    curate,
    resolve_embedding_provider,
)
from centri.curation_replay import (
    DigestBuilder,
    DigestSummarizer,
    ReplayHarness,
    _iso_week_key,
    quality_per_token,
    report,
)
from centri.db import Database
from centri.memory_graph import (
    Fact,
    MemoryGraph,
)


def _cand(key, topic, text, created_at, *, vector=None, item_type="fact", eid=None):
    return Candidate(
        key=key,
        item_type=item_type,
        topic=topic,
        text=text,
        source_event_id=eid or f"evt-{key}",
        created_at=created_at,
        vector=vector,
    )


@pytest.fixture
async def db():
    tmpdir = tempfile.mkdtemp()
    database = Database(Path(tmpdir) / "state.db")
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# Tiered digests
# ---------------------------------------------------------------------------
class TestTieredDigests:
    def test_daily_buckets_group_by_day_with_receipts(self):
        cands = [
            _cand("fact:a", "deploy", "use caddy", "2026-01-01T09:00:00+00:00"),
            _cand("fact:b", "deploy", "pin tls", "2026-01-01T18:00:00+00:00"),
            _cand("fact:c", "testing", "real db", "2026-01-02T10:00:00+00:00"),
        ]
        tiers = DigestBuilder().build(cands, tier="daily")
        windows = [t.window for t in tiers]
        assert windows == ["2026-01-01", "2026-01-02"]  # sorted, one per day
        day1 = tiers[0]
        assert day1.member_keys == ["fact:a", "fact:b"]
        assert day1.receipts == ["evt-fact:a", "evt-fact:b"]
        assert day1.summary  # a non-empty derived line
        # The digest is a view: it carries receipts back to the spine, not deletion.
        assert all(r is not None for r in day1.receipts)

    def test_weekly_tier_rolls_days_into_weeks(self):
        cands = [
            _cand("fact:a", "x", "one", "2026-01-01T00:00:00+00:00"),
            _cand("fact:b", "y", "two", "2026-01-03T00:00:00+00:00"),
            _cand("fact:c", "z", "three", "2026-01-10T00:00:00+00:00"),
        ]
        tiers = DigestBuilder().build(cands, tier="weekly")
        # Jan 1 and Jan 3 fall in the same ISO-ish week; Jan 10 in a later week.
        assert len(tiers) == 2
        assert tiers[0].member_keys == ["fact:a", "fact:b"]
        assert tiers[1].member_keys == ["fact:c"]

    def test_digest_is_deterministic_and_rederivable(self):
        cands = [
            _cand("fact:b", "deploy", "pin tls everywhere", "2026-01-01T18:00:00+00:00"),
            _cand("fact:a", "deploy", "use caddy as the reverse proxy", "2026-01-01T09:00:00+00:00"),
        ]
        first = DigestBuilder().build(cands, tier="daily")
        # Same inputs in a different order -> identical buckets/summaries.
        second = DigestBuilder().build(list(reversed(cands)), tier="daily")
        assert [t.window for t in first] == [t.window for t in second]
        assert [t.summary for t in first] == [t.summary for t in second]
        assert [t.member_keys for t in first] == [t.member_keys for t in second]

    def test_summarizer_seam_is_honest_unavailable(self):
        # No model => not available => deterministic truncated-join fallback runs.
        s = DigestSummarizer(settings=None, model_router=None)
        assert s.available is False
        line = s.summarize(["alpha beta", "gamma delta"], limit_words=3)
        assert line  # deterministic, non-empty
        assert "…" in line or len(line.split()) <= 3

    def test_week_key_undated_is_safe(self):
        assert _iso_week_key("") == "undated"
        assert _iso_week_key("garbage") == "undated"


# ---------------------------------------------------------------------------
# Quality-per-token
# ---------------------------------------------------------------------------
class TestQualityPerToken:
    def test_perfect_turn_scores_high(self):
        q = quality_per_token(included_keys=["a", "b"], miss_count=0, waste_count=0, tokens=10)
        assert q.precision == 1.0 and q.recall == 1.0
        assert q.quality_per_token == round(1.0 / 10, 8)

    def test_misses_lower_recall(self):
        q = quality_per_token(included_keys=["a", "b"], miss_count=2, waste_count=0, tokens=10)
        # 2 hits, 2 missed -> recall 0.5, precision 1.0
        assert q.precision == 1.0
        assert q.recall == 0.5

    def test_wastes_lower_precision(self):
        q = quality_per_token(included_keys=["a", "b", "c"], miss_count=0, waste_count=2, tokens=10)
        # 1 hit, 2 waste -> precision 1/3
        assert q.included_hits == 1
        assert round(q.precision, 3) == 0.333

    def test_zero_tokens_is_zero_qpt(self):
        q = quality_per_token(included_keys=["a"], miss_count=0, waste_count=0, tokens=0)
        assert q.quality_per_token == 0.0


# ---------------------------------------------------------------------------
# Replay harness over the recorded ledger
# ---------------------------------------------------------------------------
class TestReplayHarness:
    async def _record_brief(self, db, *, eid, turn_kind, lines, misses, wastes):
        await db.append_event(
            event_id=eid,
            type="curation.brief",
            source="curation",
            ts=eid,  # ts ordering not needed; harness sorts by event id
            payload={
                "policy_version": POLICY_VERSION,
                "tokenizer_stamp": "tiktoken:o200k_base",
                "embedding_stamp": "embedding:unavailable",
                "graph_high_water": "2026-01-05T00:00:00+00:00",
                "lines": [{"key": k, "section": "decisions", "detail": "full"} for k in lines],
                "turn_kind": turn_kind,
                "miss_count": misses,
                "waste_count": wastes,
                "misses": [],
                "wastes": [],
            },
        )

    async def test_replay_scores_recorded_turns(self, db):
        await self._record_brief(db, eid="b1", turn_kind="chat", lines=["decision:d1"], misses=0, wastes=0)
        await self._record_brief(db, eid="b2", turn_kind="delegation", lines=["decision:d1", "fact:f1"], misses=1, wastes=1)
        rep = await ReplayHarness(db).run()
        assert rep.turns == 2
        assert rep.chat_turns == 1 and rep.delegation_turns == 1
        assert rep.total_misses == 1 and rep.total_wastes == 1
        assert 0.0 <= rep.mean_precision <= 1.0
        assert 0.0 <= rep.mean_recall <= 1.0
        # report() renders a human summary without raising.
        text = report(rep)
        assert "quality-per-token" in text and POLICY_VERSION in text

    async def test_replay_filters_by_policy_version(self, db):
        await self._record_brief(db, eid="b1", turn_kind="chat", lines=["decision:d1"], misses=0, wastes=0)
        # A turn from a different policy must be excluded when filtering.
        await db.append_event(
            event_id="b2",
            type="curation.brief",
            source="curation",
            ts="b2",
            payload={"policy_version": "other", "lines": [{"key": "x"}], "turn_kind": "chat",
                     "miss_count": 0, "waste_count": 0},
        )
        rep = await ReplayHarness(db).run(policy_version=POLICY_VERSION)
        assert rep.turns == 1

    async def test_replay_empty_ledger_is_safe(self, db):
        rep = await ReplayHarness(db).run()
        assert rep.turns == 0
        assert rep.mean_quality_per_token == 0.0


# ---------------------------------------------------------------------------
# Write-time embeddings
# ---------------------------------------------------------------------------
class TestEmbeddings:
    def test_cosine_basics(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
        # Opposed vectors clamp to 0.0 (features stay non-negative).
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == 0.0
        # Absent or mismatched -> 0.0.
        assert cosine_similarity(None, [1.0]) == 0.0
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_null_provider_is_honest_unavailable(self):
        p = resolve_embedding_provider(settings=None)
        assert isinstance(p, NullEmbeddingProvider)
        assert p.available is False
        assert p.embed("anything") is None
        assert p.stamp == "embedding:unavailable"

    def test_embedding_feature_present_but_zero_weighted_by_default(self):
        cue = Cue(raw="deploy", terms=["deploy"], vector=[1.0, 0.0])
        c = _cand("fact:a", "deploy", "use caddy", "2026-01-01T00:00:00+00:00", vector=[1.0, 0.0])
        ranker = Ranker()  # default weights: embedding_similarity weight 0.0
        feats = ranker._features(cue, cue.term_set(), c)
        assert feats["embedding_similarity"] == 1.0  # computed
        scored = ranker.rank(cue, [c])[0]
        # Weight 0.0 -> the perfect cosine contributes nothing to the score.
        assert "embedding_similarity" in scored.breakdown
        contribution = RankWeights().embedding_similarity * feats["embedding_similarity"]
        assert contribution == 0.0

    def test_embedding_weight_can_reorder_when_enabled(self):
        # With a positive weight a vector-similar candidate outranks a lexical tie.
        w = RankWeights(overlap=0.0, type_prior=0.0, embedding_similarity=1.0)
        cue = Cue(raw="x", terms=[], vector=[1.0, 0.0])
        near = _cand("fact:near", "t", "body", "2026-01-01T00:00:00+00:00", vector=[1.0, 0.0])
        far = _cand("fact:far", "t", "body", "2026-01-02T00:00:00+00:00", vector=[0.0, 1.0])
        ranked = Ranker(w).rank(cue, [far, near])
        assert ranked[0].candidate.key == "fact:near"

    async def test_brief_carries_embedding_stamp(self, db):
        g = MemoryGraph(db)
        await g.ensure_tables()
        await g.add_fact(Fact(id="f1", topic="testing", statement="real db",
                              source_event_id="evt-f1", created_at="2026-01-01T00:00:00+00:00",
                              tags=["convention"]))
        cue = Cue(raw="testing", terms=["testing"])
        brief = await curate(g, cue, embedding_provider=NullEmbeddingProvider())
        assert brief.embedding_stamp == "embedding:unavailable"

    async def test_vector_round_trips_through_graph(self, db):
        g = MemoryGraph(db)
        await g.ensure_tables()
        await g.add_fact(Fact(id="f1", topic="t", statement="s", source_event_id="e",
                              created_at="2026-01-01T00:00:00+00:00", vector=[0.1, 0.2, 0.3]))
        facts = await g.current_facts()
        assert facts[0].vector == [0.1, 0.2, 0.3]
