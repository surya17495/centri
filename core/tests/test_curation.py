"""3c.0 deterministic context curation tests.

Covers spec section F: golden snapshot (fixture spine + cue → byte-identical
brief per policy_version) plus unit tests for every feature — hard filters,
scoring, knapsack/digest fallback, ambient assembly, miss/waste emission, and
cue building (aliases + anaphora + one graph hop).
"""

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

    async def test_no_anaphora_no_recent_lift(self, graph):
        cue = await CueBuilder(graph).build(
            "configure the database",
            recent_turns=["how should we handle jwt refresh tokens"],
        )
        # No anaphoric token in the utterance → recent turns are not lifted.
        assert cue.anaphora_terms == []

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
# Legacy suppression — HAL/Hermes/mempalace items treated as legacy
# ---------------------------------------------------------------------------
class TestLegacySuppression:
    """When Centri is the active memory provider, items ingested from HAL,
    Hermes, or mempalace are suppressed from the brief unless the cue
    explicitly mentions them."""

    async def test_legacy_fact_suppressed_from_brief(self, graph):
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

    async def test_legacy_fact_surfaces_when_cue_mentions_legacy(self, graph):
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

    async def test_legacy_fact_excluded_from_candidates(self, graph):
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

    async def test_legacy_fact_included_when_cue_mentions_hal(self, graph):
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

    async def test_mempalace_tag_suppressed(self, graph):
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

    async def test_non_legacy_ingest_not_suppressed(self, graph):
        # The "ingest" tag alone is NOT legacy — opencode/claude_code/cursor all
        # use it. Only hermes/hal/mempalace mark legacy provenance.
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
        # The golden seed has no legacy-tagged items, so the brief is unchanged.
        await _seed(graph)
        cue = await CueBuilder(graph).build("what's our jwt refresh and testing setup")
        brief = await curate(graph, cue)
        assert brief.render() == GOLDEN


# ---------------------------------------------------------------------------
# MemoryBriefAssembler legacy suppression (fallback path)
# ---------------------------------------------------------------------------
class TestMemoryBriefLegacySuppression:
    async def test_legacy_fact_suppressed_in_fallback_assembler(self, graph):
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

    async def test_legacy_fact_surfaces_in_fallback_when_cue_mentions_legacy(self, graph):
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
# Legacy open-loop reconciliation
# ---------------------------------------------------------------------------
class TestReconcileLegacyLoops:
    async def test_legacy_loops_closed_non_destructive(self, graph):
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

        # Non-legacy loop untouched.
        normal = await graph.get_open_loop("nl1")
        assert normal.state == LOOP_OPEN

    async def test_no_legacy_loops_returns_zero(self, graph):
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
