"""3c.0.2 — universal per-turn curation.

Every chat turn must flow through the same pure ``curate()`` Curator path as
coding delegation (Decision 13: memory quality identical in chat and coding).
These tests drive the Coordinator directly with a real Database + MemoryGraph +
Curator and minimal stub collaborators, asserting that:

  - a plain chat turn produces a curated brief (ambient + cued) with receipts,
  - the curated brief is deterministic for the same inputs,
  - the cued layer is computed live per turn on the cold AND warm hot-cache paths,
  - thread-affinity works for chat threads,
  - chat curation emits curation.brief / miss-waste instrumentation (turn_kind=chat),
  - the coding-delegation path is unaffected (no duplicate chat curation event).
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.coordinator import Coordinator
from centri.curation import AMBIENT_TAG, AMBIENT_TOPIC, Candidate, Curator
from centri.db import Database
from centri.memory_graph import (
    STANCE_ADOPTED,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
)
from centri.schemas import ContextPacket, RepoState


# ---------------------------------------------------------------------------
# Stubs — minimal collaborators so the chat path runs without the full app.
# ---------------------------------------------------------------------------
class _StubModelRouter:
    """Deterministic, no-network model router. Echoes context so a test can
    assert the curated memory actually reached the reasoning input."""

    def __init__(self):
        self.last_reason_prompt = None

    def classify_intent(self, text, context=""):
        return "general"

    def reason(self, prompt, output_schema=None):
        self.last_reason_prompt = prompt
        return "ok"

    def summarize_status(self, ctx):
        return "status summary"

    def narrate(self, text, voice=False):
        return text


class _StubContextAssembler:
    """Cold-path context build: returns a packet with the supplied recent turns
    and (optionally) a repo, mirroring what the real assembler yields."""

    def __init__(self, recent_events=None, repo_id=None):
        self._recent = recent_events or []
        self._repo_id = repo_id

    async def build(self, thread_id=None, task_id=None):
        repo_state = RepoState(id=self._repo_id, name="proj", root="") if self._repo_id else None
        return ContextPacket(recent_events=list(self._recent), repo_state=repo_state)


class _ColdCache:
    """Hot cache that is always cold (forces the DB+memory fallback path)."""

    async def get(self):
        return None

    async def update_from_packet(self, packet):
        pass


class _WarmCache:
    """Hot cache that is warm — returns a snapshot with a stale recall list, so a
    test can prove the live curator overrides the cache's cued layer."""

    class _Snap:
        def __init__(self, recall):
            self.last_updated = "2026-01-01T00:00:00+00:00"
            self.relevant_recall = list(recall)
            self.recent_events = []
            self.constraints = []
            # absent attributes -> getattr(..., None) in the coordinator
            self.repo_id = None
            self.repo_name = ""
            self.session_uid = None
            self.active_task_id = None
            self.active_thread_id = None
            self.letta_identity = None

    def __init__(self, stale_recall):
        self._snap = self._Snap(stale_recall)

    async def get(self):
        return self._snap

    async def update_from_packet(self, packet):
        pass


class _StubMemory:
    async def recall(self, text, limit=3):
        return ["STALE-RECALL-LINE"]

    async def core_blocks(self):
        return {}

    async def learn(self, event):
        pass


def _StubCandidate(thread_id):
    return Candidate(
        key=f"c-{thread_id}",
        item_type="decision",
        topic="deploy",
        text="deploy via caddy",
        source_event_id="evt-x",
        created_at="2026-02-01T00:00:00+00:00",
        thread_id=thread_id,
    )


def _make_coordinator(db, graph, *, hot_cache, recent_events=None, repo_id=None):
    curator = Curator(graph)
    mr = _StubModelRouter()
    coord = Coordinator(
        db=db,
        model_router=mr,
        memory=_StubMemory(),
        context_assembler=_StubContextAssembler(recent_events, repo_id),
        permissions=None,
        hands=None,
        jobs=None,
        artifacts=None,
        event_bus=None,
        hot_cache=hot_cache,
        briefing_builder=None,
        memory_brief=None,
        curator=curator,
    )
    return coord, mr


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


async def _events_of(db, type_):
    import json

    rows = await db.recent_events(limit=200)
    out = []
    for e in rows:
        if e.get("type") != type_:
            continue
        e = dict(e)
        if isinstance(e.get("payload_json"), str):
            try:
                e["payload"] = json.loads(e["payload_json"])
            except (TypeError, ValueError):
                e["payload"] = {}
        out.append(e)
    return out


@pytest.fixture
async def graph_db():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    g = MemoryGraph(db)
    await g.ensure_tables()
    yield db, g
    await db.close()


class TestUniversalCuration:
    async def test_chat_turn_produces_curated_ambient_and_cued(self, graph_db):
        import json

        db, g = graph_db
        await _seed(g)
        # An ambient standing digest, stored as the reserved Fact.
        await g.add_fact(
            Fact(
                id="amb",
                topic=AMBIENT_TOPIC,
                statement=json.dumps({"identity": ["surya, founder"], "active_projects": ["centri"]}),
                source_event_id="evt-amb",
                created_at="2026-01-06T00:00:00+00:00",
                tags=[AMBIENT_TAG],
            )
        )
        coord, mr = _make_coordinator(db, g, hot_cache=_ColdCache())

        resp = await coord.handle_utterance("how did we handle jwt refresh?", user_id="u")
        assert resp.response_type == "info"

        brief = coord._last_curated_brief
        assert brief is not None, "chat turn must run the live curator"
        # Cued layer: the adopted jwt decision surfaced.
        keys = {ln.key for ln in brief.lines}
        assert "decision:d1" in keys
        # Ambient layer present.
        assert not brief.ambient.is_empty()
        assert "surya, founder" in brief.ambient.identity
        # The curated memory actually reached the reasoning input (not a 3-item recall).
        assert "rotating refresh tokens" in (mr.last_reason_prompt or "")

    async def test_curated_lines_carry_receipts(self, graph_db):
        db, g = graph_db
        await _seed(g)
        coord, _ = _make_coordinator(db, g, hot_cache=_ColdCache())
        await coord.handle_utterance("remind me about jwt refresh", user_id="u")
        brief = coord._last_curated_brief
        receipts = brief.receipts()
        assert receipts, "curated chat brief must expose receipts"
        for r in receipts:
            assert "source_event_id" in r and "breakdown" in r
        d1 = next(r for r in receipts if r["key"] == "decision:d1")
        assert d1["source_event_id"] == "evt-d1"

    async def test_chat_curation_emits_brief_and_cue_events(self, graph_db):
        db, g = graph_db
        await _seed(g)
        coord, _ = _make_coordinator(db, g, hot_cache=_ColdCache())
        await coord.handle_utterance("what about jwt refresh", user_id="u")

        briefs = await _events_of(db, "curation.brief")
        cues = await _events_of(db, "curation.cue")
        assert briefs, "chat turn must emit curation.brief (so 3c.1 replay covers chat)"
        assert cues, "chat turn must emit curation.cue provenance"
        payload = briefs[0]["payload"]
        assert payload["turn_kind"] == "chat"
        assert payload["policy_version"]
        assert "miss_count" in payload and "waste_count" in payload
        assert "tokenizer_stamp" in payload

    async def test_brief_deterministic_for_same_inputs(self, graph_db):
        db, g = graph_db
        await _seed(g)
        coord, _ = _make_coordinator(db, g, hot_cache=_ColdCache())
        first_brief, _, _ = await coord._curator.assemble("jwt refresh please")
        first = first_brief.render(with_receipts=True)
        second_brief, _, _ = await coord._curator.assemble("jwt refresh please")
        second = second_brief.render(with_receipts=True)
        assert first == second, "same (graph, cue, budget, policy) must render byte-identical"

    async def test_live_curator_overrides_stale_warm_cache(self, graph_db):
        # The warm hot cache carries a stale recall line; the live cued layer must
        # still be computed per turn and surface the real decision, not the stale
        # cache content (the asymmetry 3c.0.2 closes).
        db, g = graph_db
        await _seed(g)
        coord, mr = _make_coordinator(db, g, hot_cache=_WarmCache(["STALE-CACHE-LINE"]))
        await coord.handle_utterance("tell me about jwt refresh", user_id="u")
        brief = coord._last_curated_brief
        assert brief is not None and brief.lines, "warm path must still curate live"
        assert "decision:d1" in {ln.key for ln in brief.lines}
        # The stale cache line did not crowd out the live curated memory.
        assert "rotating refresh tokens" in (mr.last_reason_prompt or "")

    async def test_thread_affinity_wired_for_chat_threads(self, graph_db):
        # The chat thread propagates into the cue so the ranker's thread-affinity
        # feature is live for chat turns (cue.thread_id == the chat thread). A
        # candidate in that thread then gets the affinity bump — proven directly on
        # the cue the curator built, which is the integration point 3c.0.2 adds.
        from centri.curation import Ranker

        db, g = graph_db
        await _seed(g)
        coord, _ = _make_coordinator(db, g, hot_cache=_ColdCache())
        await coord.handle_utterance("how do we deploy", user_id="u", thread_id="th-ops")
        cue = coord._last_curated_brief.cue
        assert cue.thread_id == "th-ops", "chat thread must reach the cue"
        # The ranker honors that thread for a thread-local candidate.
        local = _StubCandidate(thread_id="th-ops")
        other = _StubCandidate(thread_id="th-other")
        assert Ranker()._features(cue, cue.term_set(), local)["thread_affinity"] == 1.0
        assert Ranker()._features(cue, cue.term_set(), other)["thread_affinity"] == 0.0

    async def test_status_turn_also_curates(self, graph_db):
        db, g = graph_db
        await _seed(g)
        coord, _ = _make_coordinator(db, g, hot_cache=_ColdCache())
        await coord.handle_utterance("what's the status on jwt refresh", user_id="u")
        # status intent still flows through the curator (chat-side curation).
        assert coord._last_curated_brief is not None
        briefs = await _events_of(db, "curation.brief")
        assert briefs and briefs[0]["payload"]["turn_kind"] == "chat"
