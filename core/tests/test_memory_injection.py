"""Phase 2 cue-driven injection + dormancy + proactive briefing tests.

Proves the 'injection without asking' half of the zero-spoonfeed test: at a
delegation cue, relevant decisions/rejections/conventions/open-loops are
assembled and pushed into the brief; dormant loops surface exactly one yes/no
line; the proactive brief reports what changed/blocked/next.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.db import Database
from centri.memory_brief import MemoryBriefAssembler, ProactiveBriefBuilder
from centri.memory_graph import (
    LOOP_OPEN,
    STANCE_ADOPTED,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)
from centri.scheduler import Scheduler


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def env():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    yield db, graph
    await db.close()


class TestCueInjection:
    async def test_assembles_relevant_decisions_and_rejections(self, env):
        _, graph = env
        await graph.supersede_decision(Decision(id="d1", topic="funding rate signal", statement="use EWMA smoothing", stance=STANCE_ADOPTED, source_event_id="e1"))
        await graph.supersede_decision(Decision(id="d2", topic="funding rate signal", statement="raw SMA window", stance=STANCE_REJECTED, rationale="too noisy", source_event_id="e2"))
        await graph.supersede_fact(Fact(id="f1", topic="data source", statement="Binance funding API", source_event_id="e3"))
        await graph.add_open_loop(OpenLoop(id="l1", intent="try a Kalman filter on the funding signal", source_event_id="e4"))

        section = await MemoryBriefAssembler(graph).assemble("improve the funding-rate signal")
        assert any("EWMA" in d.statement for d in section.decisions)
        assert any("SMA" in d.statement for d in section.rejections)
        rendered = section.render()
        assert "REJECTED" in rendered
        assert "[e2]" in rendered  # receipt is carried into the brief

    async def test_empty_graph_yields_empty_section(self, env):
        _, graph = env
        section = await MemoryBriefAssembler(graph).assemble("anything")
        assert section.is_empty()

    async def test_irrelevant_cue_still_falls_back_not_silent(self, env):
        # When the cue matches nothing lexically, decisions still surface (recency
        # fallback) rather than the brief going silently empty.
        _, graph = env
        await graph.supersede_decision(Decision(id="d1", topic="deployment", statement="deploy via fly.io", stance=STANCE_ADOPTED, source_event_id="e1"))
        section = await MemoryBriefAssembler(graph).assemble("zzzz qqqq")
        assert len(section.decisions) == 1


class TestDormancy:
    async def test_stale_loop_surfaces_once_then_marked(self, env):
        db, graph = env
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        await graph.add_open_loop(OpenLoop(id="l1", intent="evaluate Letta head-to-head", source_event_id="e1", last_touched_at=old, created_at=old))
        sched = Scheduler(db, jobs=None, memory=None, observability=None, memory_graph=graph, dormancy_days=7.0)
        surfaced = await sched.detect_dormant_loops()
        assert surfaced == ["l1"]
        # Second pass does not nag again.
        surfaced2 = await sched.detect_dormant_loops()
        assert surfaced2 == []
        loop = await graph.get_open_loop("l1")
        assert loop.dormancy_asked_at is not None and loop.state == "dormant"

    async def test_fresh_loop_not_surfaced(self, env):
        db, graph = env
        await graph.add_open_loop(OpenLoop(id="l1", intent="recent thing", source_event_id="e1"))
        sched = Scheduler(db, jobs=None, memory=None, observability=None, memory_graph=graph, dormancy_days=7.0)
        assert await sched.detect_dormant_loops() == []


class TestSchedulerConsolidation:
    async def test_tick_consolidation_high_water_mark(self, env):
        db, graph = env
        await db.append_event("e1", "task.completed", "jobs", _now(), payload={"fact": {"topic": "t", "statement": "s1"}})
        worker = Consolidator(db, graph)
        sched = Scheduler(db, jobs=None, memory=None, observability=None, consolidator=worker, memory_graph=graph)
        n1 = await sched.run_consolidation()
        assert n1 == 1
        # No new events -> nothing re-consolidated.
        assert await sched.run_consolidation() == 0
        # A new event is picked up.
        await db.append_event("e2", "task.completed", "jobs", _now(), payload={"fact": {"topic": "t", "statement": "s2"}})
        assert await sched.run_consolidation() == 1
        facts = await graph.current_facts()
        assert len(facts) == 1 and facts[0].statement == "s2"


class TestProactiveBrief:
    async def test_reports_changed_blocked_next(self, env):
        db, graph = env
        await db.append_event("e1", "memory.synthesized", "memory", _now(), payload={"summary": "fact:data source"})
        await db.append_event("e2", "task.failed", "jobs", _now(), payload={"error": "backtest crashed on NaN"})
        await graph.add_open_loop(OpenLoop(id="l1", intent="add walk-forward validation", source_event_id="e3"))
        brief = await ProactiveBriefBuilder(db, graph).build()
        assert any("data source" in c for c in brief.changed)
        assert any("NaN" in b for b in brief.blocked)
        assert any("walk-forward" in n for n in brief.next_steps)
        text = brief.render()
        assert "What changed:" in text and "What's blocked:" in text and "What's next:" in text
