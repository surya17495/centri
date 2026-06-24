"""Piece B1 — 3c.0.2 chat/coding curation PARITY verification.

ROADMAP 3c.0.2 / Decision 13: memory curation quality must be IDENTICAL in chat
and coding. Both must flow through the same pure ``curate()`` Curator path
(``Coordinator._curate_into_packet``), produce receipt-bearing briefs, and emit
``curation.brief``/``curation.cue`` instrumentation — the only difference being
the ``turn_kind`` stamp ("chat" vs "delegation") that the 3c.1 replay harness
partitions on.

``test_universal_curation.py`` proves the chat side in depth. This file proves
the *parity*: the two entry points draw from the same curation path and render a
byte-identical brief for the same ``(graph, cue, budget, policy)``, and a coding
turn emits exactly ONE (delegation-side) ``curation.brief`` — never double-counted
by also chat-curating it.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.briefing import BriefingBuilder  # noqa: E402
from centri.config import Settings  # noqa: E402
from centri.coordinator import Coordinator  # noqa: E402
from centri.curation import AMBIENT_TAG, AMBIENT_TOPIC, Curator  # noqa: E402
from centri.db import Database  # noqa: E402
from centri.memory_graph import STANCE_ADOPTED, STANCE_REJECTED, Decision, Fact, MemoryGraph  # noqa: E402
from centri.permissions import Permissions  # noqa: E402
from centri.schemas import ContextPacket, RepoState  # noqa: E402


class _StubModelRouter:
    def classify_intent(self, text, context=""):
        return "general"

    def reason(self, prompt, output_schema=None):
        return "ok"

    def summarize_status(self, ctx):
        return "status"

    def narrate(self, text, voice=False):
        return text


class _StubContextAssembler:
    def __init__(self, repo_id=None):
        self._repo_id = repo_id

    async def build(self, thread_id=None, task_id=None):
        repo_state = RepoState(id=self._repo_id, name="proj", root="") if self._repo_id else None
        return ContextPacket(repo_state=repo_state)


class _ColdCache:
    async def get(self):
        return None

    async def update_from_packet(self, packet):
        pass


class _StubMemory:
    async def recall(self, text, limit=3):
        return []

    async def core_blocks(self):
        return {}

    async def learn(self, event):
        pass


class _StubJobs:
    """Records started handoffs; returns a job id so the coding path completes."""

    def __init__(self):
        self.started = []

    async def start(self, handoff, task_id):
        self.started.append((handoff, task_id))
        return f"job-{task_id}"


async def _seed(g: MemoryGraph) -> None:
    await g.add_decision(Decision(
        id="d1", topic="jwt refresh", statement="adopt rotating refresh tokens",
        stance=STANCE_ADOPTED, rationale="limits blast radius",
        source_event_id="evt-d1", created_at="2026-01-01T00:00:00+00:00", tags=["auth"],
    ))
    await g.add_decision(Decision(
        id="d2", topic="jwt refresh", statement="store refresh tokens in localStorage",
        stance=STANCE_REJECTED, rationale="XSS exfiltration risk",
        source_event_id="evt-d2", created_at="2026-01-02T00:00:00+00:00", tags=["auth"],
    ))
    await g.add_fact(Fact(
        id="f1", topic="testing", statement="integration tests hit a real database, never mocks",
        source_event_id="evt-f1", created_at="2026-01-03T00:00:00+00:00", tags=["convention"],
    ))
    await g.add_fact(Fact(
        id="ambient-1",
        topic=AMBIENT_TOPIC,
        statement=json.dumps({
            "identity": ["build small, test, then push"],
            "active_projects": ["centri"],
            "open_loops": ["wire standing-self receipts into runtime hydration"],
            "narrative": "Current work is Centri continuity.",
            "continuity_capsule": {
                "current_time_context": {
                    "generated_at": "2026-06-21T03:30:00+00:00",
                    "latest_event_at": "2026-01-03T00:00:00+00:00",
                    "relative_label": "previous work",
                },
                "active_shared_work": "Current work is Centri continuity.",
                "last_decision": {
                    "topic": "jwt refresh",
                    "statement": "adopt rotating refresh tokens",
                    "source_event_id": "evt-d1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
                "open_loops": [
                    {
                        "intent": "wire standing-self receipts into runtime hydration",
                        "source_event_id": "evt-f1",
                        "created_at": "2026-01-03T00:00:00+00:00",
                    },
                ],
                "source_event_ids": ["evt-d1", "evt-f1"],
                "suggested_next_action": "wire standing-self receipts into runtime hydration",
            },
            "derived_from": ["evt-d1", "evt-f1"],
            "derived_at": "2026-06-21T03:30:00+00:00",
        }),
        source_event_id="evt-ambient-highwater",
        created_at="2026-06-21T03:30:00+00:00",
        tags=[AMBIENT_TAG],
    ))


async def _events_of(db, type_):
    rows = await db.recent_events(limit=200)
    out = []
    for e in rows:
        if e.get("type") != type_:
            continue
        e = dict(e)
        try:
            e["payload"] = json.loads(e.get("payload_json") or "{}")
        except (TypeError, ValueError):
            e["payload"] = {}
        out.append(e)
    return out


def _make_coordinator(db, graph, *, jobs=None, autonomy="autonomous_local", briefing_builder=None):
    return Coordinator(
        db=db,
        model_router=_StubModelRouter(),
        memory=_StubMemory(),
        context_assembler=_StubContextAssembler(),
        permissions=Permissions(Settings(autonomy_level=autonomy)),
        hands=None,
        jobs=jobs,
        artifacts=None,
        event_bus=None,
        hot_cache=_ColdCache(),
        briefing_builder=briefing_builder,
        memory_brief=None,
        curator=Curator(graph),
    )


@pytest.fixture
async def graph_db():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    g = MemoryGraph(db)
    await g.ensure_tables()
    yield db, g
    await db.close()


class TestCurationParity:
    async def test_chat_and_coding_briefs_are_byte_identical(self, graph_db):
        """The same cue against the same graph must render an identical curated
        brief whether it arrives via the chat path or the delegation path — proof
        they draw from one curation function, not two."""
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)
        cue = "how did we handle jwt refresh?"

        # Chat path entry point.
        chat_packet = ContextPacket()
        injected_chat = await coord._curate_chat_context(chat_packet, cue, chat_thread=None)
        assert injected_chat is True
        chat_brief = coord._last_curated_brief.render(with_receipts=True)

        # Delegation path entry point.
        deleg_packet = ContextPacket()
        await coord.build_delegation_brief(deleg_packet, cue)
        deleg_brief = coord._last_curated_brief.render(with_receipts=True)

        assert chat_brief == deleg_brief, "chat and coding must render the SAME curated brief"
        assert "rotating refresh tokens" in chat_brief

    async def test_both_paths_emit_brief_with_matching_policy_identity(self, graph_db):
        """Both turn kinds carry the same policy_version + tokenizer_stamp +
        embedding_stamp (the policy identity), differing only in turn_kind."""
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)

        await coord._curate_chat_context(ContextPacket(), "jwt refresh", chat_thread=None)
        await coord.build_delegation_brief(ContextPacket(), "jwt refresh")

        briefs = await _events_of(db, "curation.brief")
        kinds = {b["payload"]["turn_kind"] for b in briefs}
        assert kinds == {"chat", "delegation"}
        # Policy identity is shared across both turn kinds.
        for field in ("policy_version", "tokenizer_stamp", "embedding_stamp"):
            values = {b["payload"].get(field) for b in briefs}
            assert len(values) == 1, f"{field} must be identical across chat & delegation: {values}"

    async def test_both_paths_carry_receipts(self, graph_db):
        """Every curated line carries a source_event_id receipt in both paths."""
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)

        await coord._curate_chat_context(ContextPacket(), "jwt refresh", chat_thread=None)
        chat_receipts = coord._last_curated_brief.receipts()
        await coord.build_delegation_brief(ContextPacket(), "jwt refresh")
        deleg_receipts = coord._last_curated_brief.receipts()

        assert chat_receipts and deleg_receipts
        chat_d1 = next(r for r in chat_receipts if r["key"] == "decision:d1")
        deleg_d1 = next(r for r in deleg_receipts if r["key"] == "decision:d1")
        assert chat_d1["source_event_id"] == deleg_d1["source_event_id"] == "evt-d1"

    async def test_both_paths_stamp_standing_self_derivation_receipts(self, graph_db):
        """The ambient standing-self is part of runtime hydration, not a hidden
        summary blob: chat turns and delegation/spawn briefs both stamp the same
        ambient high-water receipt and bounded derivation receipts on the
        ``curation.brief`` event, so either path can expand the standing-self back
        to verbatim source events.
        """
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)

        await coord._curate_chat_context(ContextPacket(), "jwt refresh", chat_thread="thread-chat")
        await coord.build_delegation_brief(ContextPacket(), "jwt refresh")

        briefs = await _events_of(db, "curation.brief")
        by_kind = {b["payload"]["turn_kind"]: b["payload"] for b in briefs}
        assert set(by_kind) == {"chat", "delegation"}

        for payload in by_kind.values():
            assert payload["ambient_source_event_id"] == "evt-ambient-highwater"
            assert payload["ambient_derived_from"] == ["evt-d1", "evt-f1"]
            assert payload["ambient_derived_at"] == "2026-06-21T03:30:00+00:00"
            capsule = payload["ambient_continuity_capsule"]
            assert capsule["current_time_context"]["relative_label"] == "previous work"
            assert capsule["last_decision"]["source_event_id"] == "evt-d1"
            assert capsule["source_event_ids"] == ["evt-d1", "evt-f1"]
            assert capsule["suggested_next_action"] == (
                "wire standing-self receipts into runtime hydration"
            )

    async def test_delegation_handoff_preserves_standing_self_preamble(self, graph_db):
        """Spawned/coding hands receive continuity as an explicit preamble.

        The curation path already computes the standing self; this verifies the
        final hands-facing prompt preserves it by name instead of burying it in a
        generic, truncated recall bullet.
        """
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g, briefing_builder=BriefingBuilder(max_chars=2500))

        briefing = await coord.build_delegation_brief(ContextPacket(), "continue jwt refresh work")

        assert "Relevant memory:" in briefing
        assert "Standing self (continuity):" in briefing
        assert "Current work: Current work is Centri continuity." in briefing
        assert "Continuity: time=previous work" in briefing
        assert "next=wire standing-self receipts into runtime hydration" in briefing

    async def test_coding_turn_emits_exactly_one_brief_no_double_count(self, graph_db):
        """A coding-intent utterance must curate ONCE on the delegation side and
        NOT also be chat-curated (which would double-count the same turn). The
        single emitted brief is turn_kind=delegation."""
        db, g = graph_db
        await _seed(g)
        jobs = _StubJobs()
        coord = _make_coordinator(db, g, jobs=jobs)

        # "implement ..." trips the coding_task heuristic (see _classify_intent).
        resp = await coord.handle_utterance("implement jwt refresh token rotation", user_id="u")
        assert resp.response_type == "task_created"
        assert jobs.started, "coding turn must have delegated to a hand"

        briefs = await _events_of(db, "curation.brief")
        assert len(briefs) == 1, f"a coding turn must emit exactly one curation.brief, got {len(briefs)}"
        assert briefs[0]["payload"]["turn_kind"] == "delegation"

    async def test_delegation_brief_returns_a_string_not_a_coroutine(self, graph_db):
        """Regression: when the curator injects, build_delegation_brief must
        await _finish_brief and return the rendered string — not a coroutine that
        would land in the HandoffRequest.user_intent verbatim."""
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)
        brief = await coord.build_delegation_brief(ContextPacket(), "jwt refresh")
        assert isinstance(brief, str), f"brief must be a str, got {type(brief)}"

    async def test_chat_turn_emits_exactly_one_chat_brief(self, graph_db):
        """A non-coding utterance emits exactly one chat-side brief (the mirror of
        the no-double-count invariant)."""
        db, g = graph_db
        await _seed(g)
        coord = _make_coordinator(db, g)

        resp = await coord.handle_utterance("what did we decide about jwt refresh?", user_id="u")
        assert resp.response_type == "info"
        briefs = await _events_of(db, "curation.brief")
        assert len(briefs) == 1
        assert briefs[0]["payload"]["turn_kind"] == "chat"
