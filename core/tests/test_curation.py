"""3c.0 deterministic context curation tests.

Covers spec section F: golden snapshot (fixture spine + cue → byte-identical
brief per policy_version) plus unit tests for every feature — hard filters,
scoring, knapsack/digest fallback, ambient assembly, miss/waste emission, and
cue building (aliases + anaphora + one graph hop).
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.curation import (
    AMBIENT_TAG,
    AMBIENT_TOPIC,
    DEFAULT_ENCODING,
    POLICY_VERSION,
    Budget,
    Budgeter,
    Candidate,
    CueBuilder,
    CueExpander,
    Curator,
    RankWeights,
    Ranker,
    TiktokenCounter,
    TokenCounter,
    WordCountCounter,
    compute_miss_waste,
    curate,
    curation_breakdown_payload,
    default_token_counter,
    gather_candidates,
    load_ambient,
)
from centri.db import Database
from centri.memory_graph import (
    STANCE_ADOPTED,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)


@pytest.fixture
async def graph():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    g = MemoryGraph(db)
    await g.ensure_tables()
    yield g
    await db.close()


async def _seed(g: MemoryGraph) -> None:
    """A small fixed graph with stable created_at for deterministic snapshots."""
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
    await g.add_fact(
        Fact(
            id="f2",
            topic="layout",
            statement="backend lives under core/src/centri",
            source_event_id="evt-f2",
            created_at="2026-01-04T00:00:00+00:00",
            tags=[],
        )
    )
    await g.add_open_loop(
        OpenLoop(
            id="l1",
            intent="wire jwt refresh rotation into the gateway",
            source_event_id="evt-l1",
            cue="jwt refresh",
            created_at="2026-01-05T00:00:00+00:00",
            tags=["auth"],
        )
    )


# ---------------------------------------------------------------------------
# A. Cue builder
# ---------------------------------------------------------------------------
class TestCueBuilder:
    async def test_tokenizes_and_drops_stopwords(self, graph):
        cue = await CueBuilder(graph).build("please fix the jwt refresh bug")
        assert "jwt" in cue.terms and "refresh" in cue.terms
        assert "please" not in cue.terms and "the" not in cue.terms

    async def test_alias_expansion_from_graph_fact(self, graph):
        # An alias is a fact tagged "alias": phrase -> canonical term.
        await graph.add_fact(
            Fact(id="a1", topic="the auth thing", statement="jwt refresh",
                 source_event_id="evt-a1", tags=["alias"])
        )
        cue = await CueBuilder(graph).build("can we revisit the auth thing")
        assert "jwt refresh" in cue.alias_hits
        assert "jwt" in cue.terms and "refresh" in cue.terms
        assert any(e.startswith("alias:") for e in cue.expansion_terms)

    async def test_anaphora_resolves_against_recent_turns(self, graph):
        cue = await CueBuilder(graph).build(
            "let's revisit that",
            recent_turns=["how should we handle jwt refresh tokens"],
        )
        assert "jwt" in cue.anaphora_terms or "refresh" in cue.anaphora_terms
        assert "jwt" in cue.terms

    async def test_recent_turns_always_enrich(self, graph):
        # Broadened: we always lift recent-turn tokens, not just for pronouns.
        # "configure the database" with prior context about jwt should still
        # pick up jwt/refresh as context.
        cue = await CueBuilder(graph).build(
            "configure the database",
            recent_turns=["how should we handle jwt refresh tokens"],
        )
        assert "jwt" in cue.anaphora_terms or "refresh" in cue.anaphora_terms

    async def test_graph_hop_pulls_neighbor_topics(self, graph):
        await _seed(graph)
        # "jwt" matches topic "jwt refresh"; the one graph hop adds the neighbor
        # topic's other token ("refresh") that wasn't in the base cue.
        cue = await CueBuilder(graph).build("jwt")
        assert "refresh" in cue.hop_terms


# ---------------------------------------------------------------------------
# B. Ranker
# ---------------------------------------------------------------------------
class TestRanker:
    def _cand(self, **kw):
        base = dict(key="k", item_type="fact", topic="", text="", source_event_id="e",
                    created_at="2026-01-01T00:00:00+00:00")
        base.update(kw)
        return Candidate(**base)

    async def test_overlap_drives_score(self, graph):
        cue = await CueBuilder(graph).build("jwt refresh tokens")
        hit = self._cand(key="hit", topic="jwt refresh", text="rotating jwt refresh")
        miss = self._cand(key="miss", topic="logging", text="structured logs")
        ranked = Ranker().rank(cue, [miss, hit])
        assert ranked[0].candidate.key == "hit"
        assert ranked[0].breakdown["overlap"] > ranked[1].breakdown["overlap"]

    async def test_type_prior_breaks_equal_overlap(self, graph):
        cue = await CueBuilder(graph).build("auth")
        dec = self._cand(key="dec", item_type="decision", topic="auth", text="auth")
        fact = self._cand(key="fact", item_type="fact", topic="auth", text="auth")
        ranked = Ranker().rank(cue, [fact, dec])
        assert ranked[0].candidate.key == "dec"

    async def test_open_loop_boost_only_when_cue_touched(self, graph):
        cue = await CueBuilder(graph).build("auth")
        touched = self._cand(key="t", item_type="open_loop", topic="auth", text="auth", touches_cue=True)
        bd = Ranker()._features(cue, cue.term_set(), touched)
        assert bd["open_loop_boost"] == 1.0
        untouched = self._cand(key="u", item_type="open_loop", topic="auth", text="auth", touches_cue=False)
        assert Ranker()._features(cue, cue.term_set(), untouched)["open_loop_boost"] == 0.0

    async def test_thread_affinity(self, graph):
        cue = await CueBuilder(graph).build("auth", thread_id="th-1")
        local = self._cand(key="l", topic="auth", text="auth", thread_id="th-1")
        other = self._cand(key="o", topic="auth", text="auth", thread_id="th-2")
        assert Ranker()._features(cue, cue.term_set(), local)["thread_affinity"] == 1.0
        assert Ranker()._features(cue, cue.term_set(), other)["thread_affinity"] == 0.0

    async def test_recency_is_tiebreak_only_no_wallclock(self, graph):
        cue = await CueBuilder(graph).build("auth")
        older = self._cand(key="older", topic="auth", text="auth", created_at="2026-01-01T00:00:00+00:00")
        newer = self._cand(key="newer", topic="auth", text="auth", created_at="2026-06-01T00:00:00+00:00")
        ranked = Ranker().rank(cue, [older, newer])
        # Equal overlap/type → newer edges out older purely on the ISO string.
        assert ranked[0].candidate.key == "newer"
        # Recency weight is tiny: a real overlap difference always wins over it.
        strong_miss = self._cand(key="strong", topic="auth tokens jwt refresh",
                                  text="auth tokens jwt refresh", created_at="2020-01-01T00:00:00+00:00")
        ranked2 = Ranker().rank(await CueBuilder(graph).build("auth tokens jwt refresh"),
                                [strong_miss, newer])
        assert ranked2[0].candidate.key == "strong"  # overlap beats recency despite older ts

    async def test_superseded_never_enters(self, graph):
        await _seed(graph)
        # Supersede the adopted decision; the live view (what gather reads) drops it.
        await graph.supersede_decision(
            Decision(id="d1b", topic="jwt refresh", statement="adopt opaque session tokens",
                     stance=STANCE_ADOPTED, source_event_id="evt-d1b",
                     created_at="2026-02-01T00:00:00+00:00", tags=["auth"])
        )
        cue = await CueBuilder(graph).build("jwt refresh")
        cands = await gather_candidates(graph, cue, None)
        statements = {c.text for c in cands}
        assert not any("rotating refresh tokens" in s for s in statements)


# ---------------------------------------------------------------------------
# C. Budgeter
# ---------------------------------------------------------------------------
class TestBudgeter:
    def _sc(self, ranker_out):
        return ranker_out

    async def test_digest_fallback_when_full_too_big(self, graph):
        cue = await CueBuilder(graph).build("auth")
        long_text = "auth " + " ".join(f"word{i}" for i in range(50))
        c = Candidate(key="k", item_type="fact", topic="auth", text=long_text,
                      source_event_id="e", created_at="2026-01-01T00:00:00+00:00")
        ranked = Ranker().rank(cue, [c])
        # In real tiktoken tokens the 14-word digest costs ~30 and the full text
        # ~103, so a budget of 40 fits the digest but forces the full text down.
        lines = Budgeter(Budget(total=40, ambient=0, floor_decisions=0, floor_rejections=0)).select(ranked)
        assert len(lines) == 1 and lines[0].detail == "digest"

    async def test_dropped_when_cannot_afford_digest(self, graph):
        cue = await CueBuilder(graph).build("auth")
        c = Candidate(key="k", item_type="fact", topic="auth", text="auth tokens rotate often",
                      source_event_id="e", created_at="2026-01-01T00:00:00+00:00")
        ranked = Ranker().rank(cue, [c])
        lines = Budgeter(Budget(total=0, ambient=0, floor_decisions=0, floor_rejections=0)).select(ranked)
        assert lines == []

    async def test_decision_floor_reserves_space(self, graph):
        cue = await CueBuilder(graph).build("auth")
        dec = Candidate(key="d", item_type="decision", topic="auth", text="adopt rotating tokens for auth",
                        source_event_id="e", created_at="2026-01-02T00:00:00+00:00")
        # Total budget of 0 but a decision floor lets the decision through.
        ranked = Ranker().rank(cue, [dec])
        lines = Budgeter(Budget(total=0, ambient=0, floor_decisions=50, floor_rejections=0)).select(ranked)
        assert len(lines) == 1 and lines[0].section == "decisions"


# ---------------------------------------------------------------------------
# D. Ambient layer
# ---------------------------------------------------------------------------
class TestAmbient:
    async def test_empty_when_no_digest(self, graph):
        amb = await load_ambient(graph)
        assert amb.is_empty()

    async def test_loads_and_renders_digest(self, graph):
        import json
        await graph.add_fact(
            Fact(id="amb", topic=AMBIENT_TOPIC,
                 statement=json.dumps({
                     "identity": ["tests hit a real db"],
                     "active_projects": ["centri"],
                     "open_loops": ["wire jwt rotation"],
                     "narrative": "Recent memory: 2 decisions.",
                 }),
                 source_event_id="evt-amb", tags=[AMBIENT_TAG])
        )
        amb = await load_ambient(graph)
        assert not amb.is_empty()
        rendered = amb.render(280)
        assert "centri" in rendered and "jwt rotation" in rendered
        assert amb.source_event_id == "evt-amb"

    async def test_ambient_excluded_from_cued_candidates(self, graph):
        import json
        await _seed(graph)
        await graph.add_fact(
            Fact(id="amb", topic=AMBIENT_TOPIC, statement=json.dumps({"narrative": "x"}),
                 source_event_id="evt-amb", tags=[AMBIENT_TAG])
        )
        cue = await CueBuilder(graph).build("jwt refresh")
        cands = await gather_candidates(graph, cue, None)
        assert not any(c.topic == AMBIENT_TOPIC for c in cands)


# ---------------------------------------------------------------------------
# E. Instrumentation
# ---------------------------------------------------------------------------
class TestMissWaste:
    async def test_miss_when_needed_fact_unsurfaced(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("logging")  # touches nothing in the graph
        brief = await curate(graph, cue, budget=Budget(total=0, ambient=0, floor_decisions=0, floor_rejections=0))
        cands = await gather_candidates(graph, cue, None)
        # The turn transcript mentions jwt refresh, which exists but wasn't surfaced.
        misses, wastes = compute_miss_waste(brief, cands, "we need to revisit jwt refresh rotation")
        assert any("jwt" in m["topic"] or "refresh" in m["topic"] for m in misses)

    async def test_waste_when_surfaced_unused(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("jwt refresh")
        brief = await curate(graph, cue)
        cands = await gather_candidates(graph, cue, None)
        # Turn transcript about something unrelated → surfaced lines are waste.
        _misses, wastes = compute_miss_waste(brief, cands, "deploying the marketing site today")
        assert wastes != []


# ---------------------------------------------------------------------------
# Cue expander seam (honest-unavailable)
# ---------------------------------------------------------------------------
class TestCueExpander:
    async def test_unconfigured_is_noop(self, graph):
        cue = await CueBuilder(graph).build("jwt refresh")
        exp = CueExpander(settings=None)
        assert exp.available is False
        out = await exp.expand(cue)
        assert out.terms == cue.terms
        log = exp.expansion_log(cue)
        assert log["available"] is False and "expansion_terms" in log


# ---------------------------------------------------------------------------
# Token counting (deterministic, pinned, swappable; fallback recorded in stamp)
# ---------------------------------------------------------------------------
class TestTokenCounter:
    def test_tiktoken_counts_are_deterministic_and_stamped(self):
        c = TiktokenCounter(DEFAULT_ENCODING)
        assert c.stamp == f"tiktoken:{DEFAULT_ENCODING}"
        n1 = c.count("adopt rotating refresh tokens")
        n2 = TiktokenCounter(DEFAULT_ENCODING).count("adopt rotating refresh tokens")
        assert n1 == n2 and n1 > 0
        # Real subword tokenization: more tokens than whitespace words here.
        assert n1 >= len("adopt rotating refresh tokens".split())

    def test_wordcount_fallback_counts_words_and_stamps(self):
        c = WordCountCounter()
        assert c.stamp == "wordcount:v1"
        assert c.count("one two three") == 3
        assert c.count("") == 0

    def test_default_counter_prefers_tiktoken_when_available(self):
        c = default_token_counter()
        assert isinstance(c, TokenCounter)
        # tiktoken is a hard dependency, so the default resolves to the pinned
        # encoding; the fallback path still records its own honest stamp.
        assert c.stamp in (f"tiktoken:{DEFAULT_ENCODING}", "wordcount:v1")

    def test_changing_tokenizer_changes_the_stamp(self):
        assert TiktokenCounter(DEFAULT_ENCODING).stamp != WordCountCounter().stamp

    async def test_fallback_counter_changes_brief_stamp(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("jwt refresh")
        tik = await curate(graph, cue, counter=TiktokenCounter(DEFAULT_ENCODING))
        wc = await curate(graph, cue, counter=WordCountCounter())
        assert tik.tokenizer_stamp == f"tiktoken:{DEFAULT_ENCODING}"
        assert wc.tokenizer_stamp == "wordcount:v1"
        # The stamp travels into the spine payload so the degraded path is visible.
        assert curation_breakdown_payload(wc)["tokenizer_stamp"] == "wordcount:v1"


# ---------------------------------------------------------------------------
# Golden snapshot — byte-identical brief per policy_version
# ---------------------------------------------------------------------------
GOLDEN = """\
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


class TestGoldenSnapshot:
    async def test_brief_is_byte_identical(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("what's our jwt refresh and testing setup")
        brief = await curate(graph, cue)
        assert brief.policy_version == POLICY_VERSION
        assert brief.graph_high_water == "2026-01-05T00:00:00+00:00"
        # The tokenizer identity is part of the policy stamp; the default counter
        # is the pinned tiktoken encoding when available (word-count fallback only
        # records its own stamp, never silently substitutes).
        assert brief.tokenizer_stamp in (f"tiktoken:{DEFAULT_ENCODING}", "wordcount:v1")
        assert brief.render() == GOLDEN

    async def test_render_is_deterministic_across_runs(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("what's our jwt refresh and testing setup")
        b1 = await curate(graph, cue)
        b2 = await curate(graph, cue)
        assert b1.render() == b2.render()

    async def test_receipts_invisible_by_default_visible_on_demand(self, graph):
        await _seed(graph)
        cue = await CueBuilder(graph).build("jwt refresh")
        brief = await curate(graph, cue)
        assert "[evt-" not in brief.render()
        assert "[evt-" in brief.render(with_receipts=True)
        receipts = brief.receipts()
        assert receipts and all("breakdown" in r and "source_event_id" in r for r in receipts)


# ---------------------------------------------------------------------------
# Curator live-path orchestrator
# ---------------------------------------------------------------------------
class TestCurator:
    async def test_assemble_returns_brief_candidates_cue(self, graph):
        await _seed(graph)
        curator = Curator(graph)
        brief, candidates, cue = await curator.assemble("jwt refresh", repo_id=None)
        assert brief.policy_version == POLICY_VERSION
        assert candidates and cue.raw == "jwt refresh"
        assert brief.render() != ""

    async def test_from_settings_reads_policy_knobs(self):
        class S:
            curation_budget_total = 123
            curation_w_overlap = 2.0
        b = Budget.from_settings(S())
        w = RankWeights.from_settings(S())
        assert b.total == 123 and w.overlap == 2.0


# ---------------------------------------------------------------------------
# Obsolete project history filtering — HAL/Hermes/mempalace items treated as obsolete
# ---------------------------------------------------------------------------
class TestObsoleteProjectHistoryFiltering:
    """When Centri is the active memory provider, items ingested from HAL,
    Hermes, or mempalace are filtered out of the brief to maintain current context
    relevance, unless the cue explicitly mentions them."""

    async def test_obsolete_fact_filtered_from_brief(self, graph):
        await graph.add_fact(
            Fact(
                id="lf1",
                topic="payment flow",
                statement="Hermes used Stripe webhooks for payment sync",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hermes", "hal", "transcript"],
            )
        )
        await graph.add_fact(
            Fact(
                id="lf2",
                topic="payment flow",
                statement="Centri uses event-sourced payment ledger",
                source_event_id="evt-lf2",
                created_at="2026-01-02T00:00:00+00:00",
                tags=["convention"],
            )
        )
        cue = await CueBuilder(graph).build("payment flow setup")
        brief = await curate(graph, cue)
        rendered = brief.render()
        assert "Centri uses event-sourced" in rendered
        assert "Hermes" not in rendered

    async def test_obsolete_fact_surfaces_when_cue_mentions_system(self, graph):
        await graph.add_fact(
            Fact(
                id="lf1",
                topic="payment flow",
                statement="Hermes used Stripe webhooks for payment sync",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hermes", "hal", "transcript"],
            )
        )
        cue = await CueBuilder(graph).build("what did hermes do for payment")
        brief = await curate(graph, cue)
        rendered = brief.render()
        assert "Hermes" in rendered

    async def test_obsolete_fact_excluded_from_candidates(self, graph):
        await graph.add_fact(
            Fact(
                id="lf1",
                topic="auth",
                statement="hal stored tokens in localStorage",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hal", "ingest"],
            )
        )
        await graph.add_fact(
            Fact(
                id="f1",
                topic="auth",
                statement="Centri stores tokens in httpOnly cookies",
                source_event_id="evt-f1",
                created_at="2026-01-02T00:00:00+00:00",
                tags=["convention"],
            )
        )
        cue = await CueBuilder(graph).build("auth tokens")
        cands = await gather_candidates(graph, cue, None)
        keys = [c.key for c in cands]
        assert not any("lf1" in k for k in keys)
        assert any("f1" in k for k in keys)

    async def test_obsolete_fact_included_when_cue_mentions_hal(self, graph):
        await graph.add_fact(
            Fact(
                id="lf1",
                topic="auth",
                statement="hal stored tokens in localStorage",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hal", "ingest"],
            )
        )
        cue = await CueBuilder(graph).build("check hal auth setup")
        cands = await gather_candidates(graph, cue, None)
        assert any("lf1" in c.key for c in cands)

    async def test_mempalace_tag_filtered(self, graph):
        await graph.add_fact(
            Fact(
                id="mp1",
                topic="deploy",
                statement="mempalace recorded deploy via fly.io",
                source_event_id="evt-mp1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["mempalace", "memory"],
            )
        )
        cue = await CueBuilder(graph).build("deploy setup")
        brief = await curate(graph, cue)
        assert "mempalace" not in brief.render().lower()

    async def test_non_obsolete_ingest_not_filtered(self, graph):
        # The "ingest" tag alone is NOT obsolete — opencode/claude_code/cursor all
        # use it. Only hermes/hal/mempalace mark obsolete provenance.
        await graph.add_fact(
            Fact(
                id="oc1",
                topic="deploy",
                statement="opencode recorded deploy via fly.io",
                source_event_id="evt-oc1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["opencode", "ingest", "transcript"],
            )
        )
        cue = await CueBuilder(graph).build("deploy setup")
        brief = await curate(graph, cue)
        assert "fly.io" in brief.render()

    async def test_golden_snapshot_unaffected(self, graph):
        # The golden seed has no obsolete-tagged items, so the brief is unchanged.
        await _seed(graph)
        cue = await CueBuilder(graph).build("what's our jwt refresh and testing setup")
        brief = await curate(graph, cue)
        assert brief.render() == GOLDEN


# ---------------------------------------------------------------------------
# Current context relevance and obsolete project history filtering in the ambient header
# ---------------------------------------------------------------------------
class TestCurrentContextRelevanceAmbientHeader:
    """Regression: /memory/recall builds its header from the ambient digest
    (User Profile / Who-conventions / Top open loops / Recent memory) and then
    the cued Decisions / Open-loops sections. Obsolete project history filtering
    should ensure the header stays clean of obsolete systems unless the cue
    explicitly asks for them by name.
    """

    _AMBIENT_DIGEST = {
        # Written by consolidation._refresh_ambient from convention facts; the
        # HAL one must be dropped for a non-explicit cue, the clean one kept.
        "identity": [
            "HAL namespace convention: HAL skills live under hal.skill",
            "testing: integration tests hit a real database, never mocks",
        ],
        "active_projects": ["centri"],
        "open_loops": [
            "Investigate potential issues in HAL memory code",
            "Determine HALMemory initialization path",
            "Centralize richer HAL event payload",
            "wire jwt refresh rotation into the gateway",
        ],
        "narrative": "Recent memory: 5 decisions, 2 facts, 3 open loops on record.",
    }

    async def _seed(self, g: MemoryGraph) -> Curator:
        # Stored ambient digest (exactly what consolidation writes to the graph).
        await g.add_fact(
            Fact(
                id="ambient-standing-context",
                topic=AMBIENT_TOPIC,
                statement=json.dumps(self._AMBIENT_DIGEST, sort_keys=True),
                source_event_id="evt-amb",
                created_at="2026-01-01T00:00:00+00:00",
                tags=[AMBIENT_TAG],
            )
        )
        # Obsolete-provenance decision (tagged) — already caught by the tag filter;
        # included so the full render path is exercised end-to-end.
        await g.add_decision(
            Decision(
                id="ld1",
                topic="auth provider",
                statement="HAL helper/provider wraps the legacy Hermes auth client",
                stance=STANCE_ADOPTED,
                rationale="kept HAL provider as a compatibility shim",
                source_event_id="evt-ld1",
                created_at="2026-01-02T00:00:00+00:00",
                tags=["hermes", "hal", "transcript"],
            )
        )
        # Synthesized HAL decision whose tag is NOT in LEGACY_TAGS (``hal.skill``
        # != ``hal``) and whose topic shares a token with memory cues. The tag
        # filter misses it; the text filter must drop it, and its topic must not
        # flip the cue into obsolete-retrieval mode via the graph hop.
        await g.add_decision(
            Decision(
                id="ld2",
                topic="hal memory init",
                statement="Determine HALMemory initialization path",
                stance=STANCE_ADOPTED,
                rationale="centralize richer HAL event payload",
                source_event_id="evt-ld2",
                created_at="2026-01-03T00:00:00+00:00",
                tags=["hal.skill"],
            )
        )
        # Obsolete open loop (tagged mempalace).
        await g.add_open_loop(
            OpenLoop(
                id="ll1",
                intent="review mempalace memory schema before deprecation",
                source_event_id="evt-ll1",
                cue="memory",
                created_at="2026-01-04T00:00:00+00:00",
                tags=["mempalace"],
            )
        )
        # Hindsight-named convention with NO legacy tag — text filter must catch.
        await g.add_fact(
            Fact(
                id="lf2",
                topic="replay layer",
                statement="Hindsight replay layer snapshots every turn",
                source_event_id="evt-lf2",
                created_at="2026-01-05T00:00:00+00:00",
                tags=["convention"],
            )
        )
        # Clean Centri memory content that SHOULD still surface for a memory cue.
        await g.add_decision(
            Decision(
                id="cd1",
                topic="memory continuity",
                statement="use event-sourced spine for memory continuity",
                stance=STANCE_ADOPTED,
                rationale="durable replay without a separate store",
                source_event_id="evt-cd1",
                created_at="2026-01-06T00:00:00+00:00",
                tags=["memory"],
            )
        )
        return Curator(g, settings=None)

    async def test_non_obsolete_cue_omits_hal_from_header_and_sections(self, graph):
        curator = await self._seed(graph)
        # The actual /memory/recall path: Curator.assemble -> CuratedBrief.
        brief, _cands, _cue = await curator.assemble(
            "futures-agent current work live sim memory continuity"
        )
        md = brief.render()
        # The cue never names an obsolete system explicitly, so the brief must not leak any.
        for needle in (
            "HALMemory",
            "Hindsight",
            "hal.skill",
            "HAL namespace",
            "HAL helper",
            "HAL memory",
            "HAL event",
            "mempalace",
            "Hermes",
        ):
            assert needle not in md, f"obsolete leakage {needle!r} in brief:\n{md}"
        # Ambient header is still rendered (clean identity + open loop survive).
        assert "Who/conventions:" in md, md
        assert "integration tests hit a real database" in md, md
        assert "wire jwt refresh rotation" in md, md
        # Clean Centri memory content still surfaces in the cued section.
        assert "event-sourced spine" in md, md

    async def test_explicit_hal_cue_surfaces_obsolete_history(self, graph):
        curator = await self._seed(graph)
        brief, _cands, _cue = await curator.assemble("tell me about the HAL memory setup")
        md = brief.render()
        # The user explicitly asked about HAL, so the ambient header keeps the
        # HAL open loops.
        assert "HALMemory" in md, md

    async def test_graph_hop_neighbor_does_not_unfilter_obsolete(self, graph):
        # An obsolete decision whose topic shares "memory" with the cue must NOT
        # pull "hal" into the cue's explicit-mention set and unfilter obsolete history.
        curator = await self._seed(graph)
        brief, _cands, cue = await curator.assemble("memory continuity")
        # Sanity: the graph hop DID pull the neighbor topic token into terms…
        assert "hal" in cue.terms or "init" in cue.terms, cue.terms
        # …yet the brief still filters obsolete history because the user did not name it explicitly.
        md = brief.render()
        for needle in ("HALMemory", "hal.skill", "Hindsight", "HAL namespace"):
            assert needle not in md, f"obsolete leakage {needle!r} in brief:\n{md}"

    async def test_complaining_about_suppress_legacy_comment_does_not_unhide_legacy(self, graph):
        curator = await self._seed(graph)
        # The user complains using the word "legacy" but not explicit system names.
        brief, _cands, _cue = await curator.assemble(
            "Fix it and also wtf is that supress legacy comment, what does it make sense for people, it should just work continuosly and update itself"
        )
        md = brief.render()
        # Verify no obsolete systems/components are returned.
        for needle in (
            "HALMemory",
            "Hindsight",
            "hal.skill",
            "HAL namespace",
            "HAL helper",
            "HAL memory",
            "HAL event",
            "mempalace",
            "Hermes",
        ):
            assert needle not in md, f"obsolete leakage {needle!r} in brief:\n{md}"

    async def test_explicit_query_can_retrieve_obsolete_history(self, graph):
        curator = await self._seed(graph)
        brief, _cands, _cue = await curator.assemble("HAL memory old integration issue")
        md = brief.render()
        # Explicit query contains HAL, so HAL history should be retrieved.
        assert "HALMemory" in md or "HAL namespace" in md, f"Missing HAL history in brief:\n{md}"


# ---------------------------------------------------------------------------
# MemoryBriefAssembler obsolete history filtering (fallback path)
# ---------------------------------------------------------------------------
class TestMemoryBriefObsoleteProjectHistoryFiltering:
    async def test_obsolete_fact_filtered_in_fallback_assembler(self, graph):
        from centri.memory_brief import MemoryBriefAssembler

        await graph.add_fact(
            Fact(
                id="lf1",
                topic="deploy",
                statement="hermes deployed via fly.io",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hermes", "hal", "transcript"],
            )
        )
        await graph.add_fact(
            Fact(
                id="f1",
                topic="deploy",
                statement="Centri deploys via docker compose",
                source_event_id="evt-f1",
                created_at="2026-01-02T00:00:00+00:00",
                tags=["convention"],
            )
        )
        section = await MemoryBriefAssembler(graph).assemble("deploy setup")
        rendered = section.render()
        assert "docker compose" in rendered
        assert "hermes" not in rendered.lower()

    async def test_obsolete_fact_surfaces_in_fallback_when_cue_mentions_system(self, graph):
        from centri.memory_brief import MemoryBriefAssembler

        await graph.add_fact(
            Fact(
                id="lf1",
                topic="deploy",
                statement="hermes deployed via fly.io",
                source_event_id="evt-lf1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hermes", "hal", "transcript"],
            )
        )
        section = await MemoryBriefAssembler(graph).assemble("what did hermes deploy")
        assert "hermes" in section.render().lower()


# ---------------------------------------------------------------------------
# Obsolete open-loop reconciliation
# ---------------------------------------------------------------------------
class TestReconcileObsoleteLoops:
    async def test_obsolete_loops_closed_non_destructive(self, graph):
        from centri.memory_graph import LOOP_DONE, LOOP_OPEN

        await graph.add_open_loop(
            OpenLoop(
                id="ll1",
                intent="migrate hermes payment webhooks",
                source_event_id="evt-ll1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["hermes", "hal"],
            )
        )
        await graph.add_open_loop(
            OpenLoop(
                id="ll2",
                intent="review mempalace memory schema",
                source_event_id="evt-ll2",
                created_at="2026-01-02T00:00:00+00:00",
                tags=["mempalace"],
            )
        )
        await graph.add_open_loop(
            OpenLoop(
                id="nl1",
                intent="wire jwt refresh rotation",
                source_event_id="evt-nl1",
                created_at="2026-01-03T00:00:00+00:00",
                tags=["auth"],
            )
        )
        closed = await graph.reconcile_legacy_loops()
        assert closed == 2

        # Non-destructive: rows still exist, just state changed.
        legacy1 = await graph.get_open_loop("ll1")
        legacy2 = await graph.get_open_loop("ll2")
        assert legacy1.state == LOOP_DONE
        assert legacy2.state == LOOP_DONE

        # Non-obsolete loop untouched.
        normal = await graph.get_open_loop("nl1")
        assert normal.state == LOOP_OPEN

    async def test_no_obsolete_loops_returns_zero(self, graph):
        await graph.add_open_loop(
            OpenLoop(
                id="nl1",
                intent="wire jwt refresh rotation",
                source_event_id="evt-nl1",
                created_at="2026-01-01T00:00:00+00:00",
                tags=["auth"],
            )
        )
        closed = await graph.reconcile_legacy_loops()
        assert closed == 0
