"""Unit 2 — semantic leg ON: embedding providers, write-time embedding,
idempotent backfill, the POLICY_VERSION bump under a positive weight, and a new
golden, plus a paraphrase cue (no token overlap) surfacing via cosine.

All offline: the default provider is NullEmbeddingProvider (honest-unavailable),
and the paraphrase fixtures use the clearly-labeled deterministic
HashingEmbeddingProvider stub so the suite needs no network or model download.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.curation import (
    POLICY_VERSION,
    POLICY_VERSION_EMBED,
    Budget,
    Candidate,
    CueBuilder,
    Curator,
    HashingEmbeddingProvider,
    NullEmbeddingProvider,
    RankWeights,
    Ranker,
    active_policy_version,
    cosine_similarity,
    curate,
    gather_candidates,
    resolve_embedding_provider,
)
from centri.db import Database
from centri.memory_graph import (
    STANCE_ADOPTED,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
)


@pytest.fixture
async def graph():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    g = MemoryGraph(db)
    await g.ensure_tables()
    yield g, db
    await db.close()


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class TestProviders:
    def test_null_is_honest_unavailable(self):
        p = NullEmbeddingProvider()
        assert p.available is False
        assert p.stamp == "embedding:unavailable"
        assert p.embed("anything") is None

    def test_hashing_stub_is_deterministic_and_labeled(self):
        p = HashingEmbeddingProvider(dim=64)
        assert p.available is True
        assert "hashing-stub" in p.stamp  # never mistaken for a real model
        v1 = p.embed("rotating refresh tokens")
        v2 = p.embed("rotating refresh tokens")
        assert v1 == v2 and len(v1) == 64

    def test_hashing_stub_paraphrase_is_near_unrelated_is_far(self):
        p = HashingEmbeddingProvider(dim=128)
        # Shared lemma vocabulary (token/tokens, refresh/refreshing) → high cosine.
        a = p.embed("we adopted rotating refresh tokens")
        b = p.embed("the refreshing token rotation we adopted")
        # No shared vocabulary at all → low/zero cosine.
        c = p.embed("the kitchen sink plumbing schedule")
        assert cosine_similarity(a, b) > cosine_similarity(a, c)

    def test_resolve_default_is_null(self):
        assert isinstance(resolve_embedding_provider(None), NullEmbeddingProvider)

    def test_resolve_null_when_unconfigured_settings(self):
        class S:
            embedding_enabled = False
            embedding_local_model = ""
            embedding_model = ""
            model_embeddings = ""

        assert isinstance(resolve_embedding_provider(S()), NullEmbeddingProvider)

    def test_resolve_litellm_when_enabled_with_router(self):
        from centri.curation import LiteLLMEmbeddingProvider

        class S:
            embedding_enabled = True
            embedding_local_model = ""
            embedding_model = "text-embedding-3-small"
            model_embeddings = ""

        class FakeRouter:
            def __init__(self):
                self.seen_model = None

            def embed(self, texts, model=None):
                self.seen_model = model
                return [[1.0, 0.0, 0.0] for _ in texts]

        router = FakeRouter()
        prov = resolve_embedding_provider(S(), router)
        assert isinstance(prov, LiteLLMEmbeddingProvider)
        assert prov.available is True
        assert prov.embed("hi") == [1.0, 0.0, 0.0]
        # The provider's configured model is threaded through to the router so a
        # CENTRI_EMBEDDING_MODEL-only config still resolves a model.
        assert router.seen_model == "text-embedding-3-small"
        assert prov.embed("") is None


# ---------------------------------------------------------------------------
# Policy version under a positive embedding weight
# ---------------------------------------------------------------------------
class TestPolicyVersion:
    def test_zero_weight_keeps_base_version(self):
        assert active_policy_version(RankWeights(embedding_similarity=0.0)) == POLICY_VERSION

    def test_positive_weight_selects_embed_version(self):
        assert (
            active_policy_version(RankWeights(embedding_similarity=0.3))
            == POLICY_VERSION_EMBED
        )

    async def test_curate_stamps_base_version_by_default(self, graph):
        g, _ = graph
        await _seed(g)
        cue = await CueBuilder(g).build("jwt refresh")
        brief = await curate(g, cue)
        assert brief.policy_version == POLICY_VERSION
        assert brief.embedding_stamp == "embedding:unavailable"


# ---------------------------------------------------------------------------
# Write-time embedding through consolidation
# ---------------------------------------------------------------------------
class TestWriteTimeEmbedding:
    async def test_null_provider_writes_no_vector(self, graph):
        g, db = graph
        con = Consolidator(db, g)  # default null provider
        await con.consume_events(
            [
                {
                    "id": "evt-1",
                    "type": "fact.observed",
                    "payload": {"fact": {"topic": "testing", "statement": "use real db"}},
                }
            ]
        )
        facts = await g.current_facts()
        assert facts and all(f.vector is None for f in facts)

    async def test_real_provider_writes_vector_at_write_time(self, graph):
        g, db = graph
        con = Consolidator(db, g, embedding_provider=HashingEmbeddingProvider())
        await con.consume_events(
            [
                {
                    "id": "evt-1",
                    "type": "decision.made",
                    "payload": {
                        "decision": {
                            "topic": "jwt refresh",
                            "statement": "adopt rotating refresh tokens",
                            "stance": STANCE_ADOPTED,
                        }
                    },
                }
            ]
        )
        decisions = await g.current_decisions()
        assert decisions and decisions[0].vector is not None
        assert len(decisions[0].vector) == 64


# ---------------------------------------------------------------------------
# Idempotent backfill
# ---------------------------------------------------------------------------
class TestBackfill:
    async def test_backfill_is_idempotent(self, graph):
        g, db = graph
        await _seed(g)  # nodes written with NO vector
        con = Consolidator(db, g, embedding_provider=HashingEmbeddingProvider())
        first = await con.backfill_embeddings()
        assert first["embedded"] == first["total"] > 0
        # Re-run: every node already has a vector → nothing to do.
        second = await con.backfill_embeddings()
        assert second["total"] == 0
        assert second["embedded"] == 0

    async def test_backfill_honest_unavailable_embeds_nothing(self, graph):
        g, db = graph
        await _seed(g)
        con = Consolidator(db, g)  # null provider
        result = await con.backfill_embeddings()
        assert result["embedded"] == 0
        assert result["total"] > 0
        facts = await g.current_facts()
        assert all(f.vector is None for f in facts)

    async def test_backfill_emits_progress_events(self, graph):
        g, db = graph
        await _seed(g)
        con = Consolidator(db, g, embedding_provider=HashingEmbeddingProvider())
        await con.backfill_embeddings()
        rows = await db.recent_events(limit=100)
        kinds = {r.get("type") for r in rows}
        assert "embedding.backfill.started" in kinds
        assert "embedding.backfill.completed" in kinds


# ---------------------------------------------------------------------------
# Paraphrase recall: semantic leg surfaces a no-token-overlap fact
# ---------------------------------------------------------------------------
class TestParaphraseRecall:
    async def test_positive_weight_surfaces_paraphrase_with_no_overlap(self, graph):
        g, db = graph
        prov = HashingEmbeddingProvider(dim=256)
        # Target fact uses vocabulary the cue will NOT share lexically, but the
        # embedding vocabulary overlaps (rotation/rotating, token/tokens).
        target = Fact(
            id="f-target",
            topic="auth strategy",
            statement="we rotate access tokens on a short interval",
            source_event_id="evt-target",
            created_at="2026-02-01T00:00:00+00:00",
            tags=[],
        )
        target.vector = prov.embed(f"{target.topic}: {target.statement}")
        await g.add_fact(target)
        # A distractor fact with no shared embedding vocabulary.
        distractor = Fact(
            id="f-distract",
            topic="deployment",
            statement="the build pipeline ships nightly to staging",
            source_event_id="evt-distract",
            created_at="2026-02-02T00:00:00+00:00",
            tags=[],
        )
        distractor.vector = prov.embed(f"{distractor.topic}: {distractor.statement}")
        await g.add_fact(distractor)

        # Paraphrase cue: zero lexical overlap with the target statement words.
        cue_text = "rotating the token"
        cue = await CueBuilder(g).build(cue_text)
        cue.vector = prov.embed(cue_text)

        candidates = await gather_candidates(g, cue, None)
        # Baseline (weight 0.0): embedding cannot move the score.
        base = Ranker(RankWeights(embedding_similarity=0.0)).rank(cue, candidates)
        # Semantic ON (positive weight): the paraphrase target outranks distractor.
        semantic = Ranker(RankWeights(embedding_similarity=2.0)).rank(cue, candidates)

        def score_of(ranked, key):
            return next(s.score for s in ranked if s.candidate.key == key)

        # With the semantic leg ON, the target gets a strictly higher score than
        # it did with embeddings OFF (the leg adds evidence a paraphrase needs).
        assert score_of(semantic, "fact:f-target") > score_of(base, "fact:f-target")
        # And the target outranks the unrelated distractor under the semantic leg.
        assert score_of(semantic, "fact:f-target") > score_of(semantic, "fact:f-distract")


# ---------------------------------------------------------------------------
# Golden snapshot for the embed-active policy (POLICY_VERSION_EMBED)
# ---------------------------------------------------------------------------
# The pre-embedding golden lives in test_curation.py keyed to POLICY_VERSION.
# This is the SEPARATE golden for the semantic-leg-ON policy: a positive
# embedding weight + a (deterministic stub) provider. The render is byte-stable
# given the same graph + vectors, exactly like the base golden.
GOLDEN_EMBED = """\
Memory (assembled from the event ledger):
Decisions already made (do not relitigate):
  - adopt rotating refresh tokens — short-lived access tokens limit blast radius
Approaches already REJECTED (do not re-propose without stating what changed):
  - store refresh tokens in localStorage — XSS exfiltration risk
Project conventions / current facts:
  - testing: integration tests hit a real database, never mocks
  - layout: backend lives under core/src/centri
Open loops / alternatives still on the table:
  - wire jwt refresh rotation into the gateway (cue: jwt refresh)"""


class TestEmbedGoldenSnapshot:
    async def test_embed_policy_brief_is_byte_identical(self, graph):
        g, _ = graph
        prov = HashingEmbeddingProvider(dim=256)

        def emb(topic, statement):
            return prov.embed(f"{topic}: {statement}")

        await _seed_full(g, emb)
        cue = await CueBuilder(g).build("what is our jwt refresh and testing setup")
        cue.vector = prov.embed("what is our jwt refresh and testing setup")
        w = RankWeights(embedding_similarity=0.5)
        brief = await curate(
            g, cue, weights=w, policy_version=active_policy_version(w), embedding_provider=prov
        )
        assert brief.policy_version == POLICY_VERSION_EMBED
        assert brief.embedding_stamp == "embedding:hashing-stub:d256"
        assert brief.render() == GOLDEN_EMBED

    async def test_embed_policy_render_is_deterministic(self, graph):
        g, _ = graph
        prov = HashingEmbeddingProvider(dim=256)
        await _seed_full(g, lambda t, s: prov.embed(f"{t}: {s}"))
        cue = await CueBuilder(g).build("jwt refresh and testing setup")
        cue.vector = prov.embed("jwt refresh and testing setup")
        w = RankWeights(embedding_similarity=0.5)
        b1 = await curate(g, cue, weights=w, policy_version=active_policy_version(w), embedding_provider=prov)
        b2 = await curate(g, cue, weights=w, policy_version=active_policy_version(w), embedding_provider=prov)
        assert b1.render() == b2.render()


async def _seed_full(g: MemoryGraph, emb) -> None:
    """The base golden's seed, with stored vectors for the embed-active golden."""
    from centri.memory_graph import OpenLoop

    await g.add_decision(
        Decision(
            id="d1", topic="jwt refresh", statement="adopt rotating refresh tokens",
            stance=STANCE_ADOPTED, rationale="short-lived access tokens limit blast radius",
            source_event_id="evt-d1", created_at="2026-01-01T00:00:00+00:00", tags=["auth"],
            vector=emb("jwt refresh", "adopt rotating refresh tokens"),
        )
    )
    await g.add_decision(
        Decision(
            id="d2", topic="jwt refresh", statement="store refresh tokens in localStorage",
            stance=STANCE_REJECTED, rationale="XSS exfiltration risk",
            source_event_id="evt-d2", created_at="2026-01-02T00:00:00+00:00", tags=["auth"],
            vector=emb("jwt refresh", "store refresh tokens in localStorage"),
        )
    )
    await g.add_fact(
        Fact(
            id="f1", topic="testing", statement="integration tests hit a real database, never mocks",
            source_event_id="evt-f1", created_at="2026-01-03T00:00:00+00:00", tags=["convention"],
            vector=emb("testing", "integration tests hit a real database, never mocks"),
        )
    )
    await g.add_fact(
        Fact(
            id="f2", topic="layout", statement="backend lives under core/src/centri",
            source_event_id="evt-f2", created_at="2026-01-04T00:00:00+00:00", tags=[],
            vector=emb("layout", "backend lives under core/src/centri"),
        )
    )
    await g.add_open_loop(
        OpenLoop(
            id="l1", intent="wire jwt refresh rotation into the gateway",
            source_event_id="evt-l1", cue="jwt refresh",
            created_at="2026-01-05T00:00:00+00:00", tags=["auth"],
        )
    )


async def _seed(g: MemoryGraph) -> None:
    await g.add_decision(
        Decision(
            id="d1",
            topic="jwt refresh",
            statement="adopt rotating refresh tokens",
            stance=STANCE_ADOPTED,
            rationale="short-lived access tokens limit blast radius",
            source_event_id="evt-d1",
            created_at="2026-01-01T00:00:00+00:00",
            tags=["auth"],
        )
    )
    await g.add_decision(
        Decision(
            id="d2",
            topic="jwt refresh",
            statement="store refresh tokens in localStorage",
            stance=STANCE_REJECTED,
            rationale="XSS exfiltration risk",
            source_event_id="evt-d2",
            created_at="2026-01-02T00:00:00+00:00",
            tags=["auth"],
        )
    )
    await g.add_fact(
        Fact(
            id="f1",
            topic="testing",
            statement="integration tests hit a real database, never mocks",
            source_event_id="evt-f1",
            created_at="2026-01-03T00:00:00+00:00",
            tags=["convention"],
        )
    )
