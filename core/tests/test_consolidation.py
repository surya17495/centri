"""Phase 2 consolidation worker tests — the sleep cycle.

Proves the worker folds typed event hints into the graph, resolves conflicts by
supersession, never confabulates outcomes, and re-derives the whole graph from
the ledger.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.db import Database
from centri.memory_graph import LOOP_DONE, LOOP_OPEN, STANCE_REJECTED, MemoryGraph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def setup():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    yield Consolidator(db, graph), graph, db
    await db.close()


class TestConsolidation:
    async def test_folds_decision_and_fact_hints(self, setup):
        worker, graph, _ = setup
        n = await worker.consume_events([
            {"id": "e1", "type": "task.completed", "payload": {
                "decision": {"topic": "signal", "statement": "use EWMA", "stance": "adopted"},
                "fact": {"topic": "data source", "statement": "Binance funding API"},
            }},
        ])
        assert n == 2
        decisions = await graph.current_decisions()
        facts = await graph.current_facts()
        assert decisions[0].statement == "use EWMA"
        assert decisions[0].source_event_id == "e1"  # receipt
        assert facts[0].topic == "data source"

    async def test_rejected_approach_is_captured(self, setup):
        worker, graph, _ = setup
        await worker.consume_events([
            {"id": "e1", "type": "task.failed", "payload": {
                "decision": {"topic": "cache", "statement": "Redis sidecar", "stance": "rejected",
                             "rationale": "breaks desktop footprint"},
            }},
        ])
        rejected = await graph.rejected_approaches()
        assert len(rejected) == 1
        assert rejected[0].rationale == "breaks desktop footprint"

    async def test_supersession_across_batches(self, setup):
        worker, graph, _ = setup
        await worker.consume_events([
            {"id": "e1", "type": "task.completed", "payload": {
                "fact": {"topic": "auth service", "statement": "named authsvc"}}},
        ])
        await worker.consume_events([
            {"id": "e2", "type": "task.completed", "payload": {
                "fact": {"topic": "auth service", "statement": "renamed to identity"}}},
        ])
        current = await graph.current_facts()
        assert len(current) == 1 and current[0].statement == "renamed to identity"

    async def test_open_loop_opened_then_resolved(self, setup):
        worker, graph, _ = setup
        await worker.consume_events([
            {"id": "e1", "type": "task.started", "payload": {
                "open_loop": {"id": "loop-x", "intent": "evaluate Letta adapter head-to-head"}}},
        ])
        assert len(await graph.open_loops(states=[LOOP_OPEN])) == 1
        await worker.consume_events([
            {"id": "e2", "type": "task.completed", "payload": {
                "loop_resolution": {"loop_id": "loop-x", "resolution": "done"}}},
        ])
        assert await graph.open_loops(states=[LOOP_OPEN]) == []
        loop = await graph.get_open_loop("loop-x")
        assert loop.state == LOOP_DONE

    async def test_never_confabulates_missing_fields(self, setup):
        worker, graph, _ = setup
        # A hint missing topic/statement is dropped, not invented.
        n = await worker.consume_events([
            {"id": "e1", "type": "task.completed", "payload": {"decision": {"topic": "x"}}},
        ])
        assert n == 0
        assert await graph.current_decisions() == []

    async def test_emits_memory_synthesized(self, setup):
        worker, graph, db = setup
        await worker.consume_events([
            {"id": "e1", "type": "task.completed", "payload": {
                "fact": {"topic": "t", "statement": "s"}}},
        ])
        events = await db.recent_events(limit=50)
        assert any(e["type"] == "memory.synthesized" for e in events)


class TestRebuild:
    async def test_rebuild_from_events_is_re_derivable(self, setup):
        worker, graph, db = setup
        await db.append_event("e1", "task.completed", "jobs", _now(), payload={
            "fact": {"topic": "auth service", "statement": "named authsvc"}})
        await db.append_event("e2", "task.completed", "jobs", _now(), payload={
            "fact": {"topic": "auth service", "statement": "renamed to identity"},
            "decision": {"topic": "signal", "statement": "use EWMA", "stance": "adopted"}})
        written = await worker.rebuild_from_events()
        assert written >= 2
        facts = await graph.current_facts()
        assert len(facts) == 1 and facts[0].statement == "renamed to identity"
        # Wiping and rebuilding again is deterministic.
        await graph.clear()
        await worker.rebuild_from_events()
        facts2 = await graph.current_facts()
        assert len(facts2) == 1 and facts2[0].statement == "renamed to identity"


class TestTranscriptHints:
    """Phase 3b.1 — hand.transcript events carry deterministic fact hints."""

    async def test_transcript_fact_hint_writes_fact(self, setup):
        worker, graph, _ = setup
        long_text = "Refactored the auth module into authsvc. " * 10
        n = await worker.consume_events([
            {"id": "ev-t1", "type": "hand.transcript", "payload": {
                "session_uid": "sess-9",
                "intent": "refactor auth",
                "text": long_text,
                "fact": {
                    "topic": "delegated-session:sess-9",
                    "statement": f"Delegated session for 'refactor auth' (completed/end_turn): {long_text[:400]}",
                    "tags": ["hand", "transcript", "acp"],
                },
            }},
        ])
        assert n == 1
        facts = await graph.current_facts()
        match = [f for f in facts if f.topic == "delegated-session:sess-9"]
        assert len(match) == 1
        assert match[0].source_event_id == "ev-t1"  # receipt back to the spine
        assert "refactor auth" in match[0].statement

    async def test_transcript_without_hint_writes_nothing(self, setup):
        worker, graph, _ = setup
        n = await worker.consume_events([
            {"id": "ev-t2", "type": "hand.transcript", "payload": {
                "session_uid": "sess-10", "text": ""}},
        ])
        assert n == 0
        assert await graph.current_facts() == []
