"""Phase 2 typed memory graph tests — supersession, not accumulation.

Proves the semantic + prospective index: decisions/facts carry receipts, new
truth supersedes old (live view shows only current, history stays answerable),
and rejections are first-class for the re-proposal guard.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.memory_graph import (
    LOOP_DONE,
    LOOP_OPEN,
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


class TestReceipts:
    async def test_every_object_carries_a_receipt(self, graph):
        await graph.add_fact(Fact(id="f1", topic="db", statement="uses SQLite", source_event_id="evt-7"))
        facts = await graph.current_facts()
        assert facts[0].source_event_id == "evt-7"


class TestSupersession:
    async def test_new_fact_supersedes_old_on_same_topic(self, graph):
        await graph.supersede_fact(Fact(id="f1", topic="auth service", statement="named authsvc", source_event_id="e1"))
        await graph.supersede_fact(Fact(id="f2", topic="auth service", statement="renamed to identity", source_event_id="e2"))
        current = await graph.current_facts()
        # Live view shows only the new truth.
        assert len(current) == 1
        assert current[0].id == "f2"
        # But history (true-in-March) is still answerable.
        history = await graph.fact_history("auth service")
        assert {h.id for h in history} == {"f1", "f2"}
        old = next(h for h in history if h.id == "f1")
        assert old.superseded_by == "f2" and old.invalidated_at is not None

    async def test_rejection_and_adoption_are_distinct_claims(self, graph):
        # Rejecting approach A does not erase it when we later adopt approach B.
        await graph.supersede_decision(
            Decision(id="d1", topic="signal", statement="use raw SMA", stance=STANCE_REJECTED, source_event_id="e1")
        )
        await graph.supersede_decision(
            Decision(id="d2", topic="signal", statement="use EWMA", stance=STANCE_ADOPTED, source_event_id="e2")
        )
        rejected = await graph.rejected_approaches()
        adopted = await graph.current_decisions(stance=STANCE_ADOPTED)
        assert [d.id for d in rejected] == ["d1"]
        assert [d.id for d in adopted] == ["d2"]

    async def test_decision_reversal_supersedes_same_stance(self, graph):
        await graph.supersede_decision(
            Decision(id="d1", topic="db engine", statement="use Postgres", stance=STANCE_ADOPTED, source_event_id="e1")
        )
        await graph.supersede_decision(
            Decision(id="d2", topic="db engine", statement="use SQLite", stance=STANCE_ADOPTED,
                     rationale="desktop footprint", source_event_id="e2")
        )
        live = await graph.current_decisions(stance=STANCE_ADOPTED)
        assert len(live) == 1 and live[0].id == "d2"


class TestOpenLoops:
    async def test_open_loops_listed_and_resolved(self, graph):
        await graph.add_open_loop(OpenLoop(id="l1", intent="benchmark Letta adapter", source_event_id="e1"))
        loops = await graph.open_loops()
        assert [loop.id for loop in loops] == ["l1"]
        await graph.set_loop_state("l1", LOOP_DONE)
        assert await graph.open_loops(states=[LOOP_OPEN]) == []

    async def test_touch_revives_dormant_loop(self, graph):
        await graph.add_open_loop(OpenLoop(id="l1", intent="ship voice", state="dormant", source_event_id="e1"))
        await graph.touch_loop("l1")
        loop = await graph.get_open_loop("l1")
        assert loop.state == LOOP_OPEN
