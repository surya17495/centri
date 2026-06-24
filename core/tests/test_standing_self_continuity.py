"""Phase 1.2 (foundational slice) — the standing self is ONE global truth,
derived from the spine, receipt-backed, and current after every consolidation.

The master-plan architecture: there is one global memory; sessions/threads are
only *views*; spawns/parallel tasks are *producers* of spine events; the ambient
standing-self is *derived* from the spine/graph (not session-scoped) and must be
re-hydratable before any turn or spawn.

These tests drive the deterministic consolidation worker directly with a real
Database + MemoryGraph — no model, no network, fully offline. They prove three
behaviors the standing self must have:

  (a) MID-SESSION: a second consolidation in the same thread updates the digest
      and advances its derivation receipt — awareness moves as work progresses,
      not only at session start.
  (b) CROSS-SESSION: events produced under different ``thread_id``s fold into the
      SAME global digest, readable with no thread filter — continuity carries
      across sessions because memory is global.
  (c) PARALLEL SAME-SESSION PRODUCERS: two concurrent producers (distinct
      ``task_id``s in one thread, modelling parallel tasks/spawns) both land in
      the one global digest — parallel work in a single session is reflected
      without needing parallel sessions.

The digest must be RECEIPT-BACKED: it carries ``source_event_id`` (the latest
spine event it was derived from) plus a bounded ``derived_from`` receipt list, so
the standing self is auditable back to the verbatim events that produced it
(honest, bounded, no fabrication).
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.curation import load_ambient
from centri.db import Database
from centri.memory_graph import MemoryGraph


@pytest.fixture
async def setup():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    yield Consolidator(db, graph), graph, db
    await db.close()


def _decision_event(eid, topic, statement, *, thread_id=None, task_id=None):
    return {
        "id": eid,
        "type": "task.completed",
        "thread_id": thread_id,
        "task_id": task_id,
        "payload": {
            "decision": {"topic": topic, "statement": statement, "stance": "adopted"},
        },
    }


def _open_loop_event(eid, intent, *, thread_id=None, task_id=None):
    return {
        "id": eid,
        "type": "task.started",
        "thread_id": thread_id,
        "task_id": task_id,
        "payload": {
            "open_loop": {"intent": intent, "cue": intent},
        },
    }


@pytest.mark.asyncio
async def test_ambient_is_receipt_backed(setup):
    """The derived standing self carries a receipt to the spine event it summarizes."""
    worker, graph, _ = setup
    await worker.consume_events([
        _decision_event("evt-1", "auth service", "named authsvc"),
    ])

    ambient = await load_ambient(graph)
    assert not ambient.is_empty()
    # The digest resolves to the originating spine event, not None.
    assert ambient.source_event_id == "evt-1"
    # And the bounded provenance list also carries the receipt.
    assert "evt-1" in ambient.derived_from


@pytest.mark.asyncio
async def test_mid_session_update_advances_receipt(setup):
    """A later consolidation in the same thread updates the digest + its receipt.

    Standing self must move as work progresses mid-session, not freeze at the
    value computed at session start.
    """
    worker, graph, _ = setup
    await worker.consume_events([
        _decision_event("evt-early", "auth service", "named authsvc", thread_id="thread-A"),
    ])
    first = await load_ambient(graph)
    assert first.source_event_id == "evt-early"

    # More work happens later in the SAME session.
    await worker.consume_events([
        _decision_event("evt-late", "gateway", "front auth with a gateway at /auth",
                        thread_id="thread-A"),
    ])
    second = await load_ambient(graph)

    # The receipt advanced to the newest derivation input.
    assert second.source_event_id == "evt-late"
    # The newer work is reflected in the rolling narrative.
    assert "gateway" in second.narrative
    # Both events are auditable in the provenance list.
    assert "evt-early" in second.derived_from
    assert "evt-late" in second.derived_from


@pytest.mark.asyncio
async def test_cross_session_folds_into_one_global_digest(setup):
    """Events from DIFFERENT threads fold into ONE global standing self.

    Memory is global; a session is just a view. The ambient load path takes no
    thread filter — work done in session A is visible to the standing self read
    from session B.
    """
    worker, graph, _ = setup
    await worker.consume_events([
        _decision_event("evt-a", "auth service", "named authsvc", thread_id="thread-A"),
    ])
    await worker.consume_events([
        _decision_event("evt-b", "deploy target", "ship to fly.io", thread_id="thread-B"),
    ])

    # Read with no thread scoping at all — this is the global standing self.
    ambient = await load_ambient(graph)
    blob = ambient.narrative + " " + " ".join(ambient.open_loops)
    # Both sessions' work is present in the one digest.
    assert "auth service" in blob
    assert "deploy target" in blob
    # Both receipts are auditable; the latest is the digest's headline receipt.
    assert "evt-a" in ambient.derived_from
    assert "evt-b" in ambient.derived_from


@pytest.mark.asyncio
async def test_parallel_same_session_producers_both_land(setup):
    """Two concurrent producers in ONE session both reach the global digest.

    Models parallel tasks/spawns within a single session (distinct task_ids,
    same thread). After both consolidations settle, the standing self reflects
    both — no parallel session required.
    """
    worker, graph, _ = setup

    # Two producers run concurrently, each folding its own batch. They share the
    # one global graph; consolidation is serialized by the single connection but
    # the producers are launched in parallel.
    await asyncio.gather(
        worker.consume_events([
            _decision_event("evt-p1", "indexer", "use FTS5 triggers",
                            thread_id="thread-A", task_id="task-1"),
        ]),
        worker.consume_events([
            _decision_event("evt-p2", "ranker", "bm25 with source priority",
                            thread_id="thread-A", task_id="task-2"),
        ]),
    )

    ambient = await load_ambient(graph)
    blob = ambient.narrative
    assert "indexer" in blob
    assert "ranker" in blob
    # Both parallel producers' events are receipted in the provenance.
    assert "evt-p1" in ambient.derived_from
    assert "evt-p2" in ambient.derived_from


@pytest.mark.asyncio
async def test_receipts_resolve_to_real_spine_events(setup):
    """Every receipt in the standing self resolves to an actual spine event.

    Honest + auditable: the provenance is not fabricated — each id is a real
    appended event. (The deterministic worker folds in-memory batches, so we
    assert the receipts equal the ids that were fed in, which are the spine ids.)
    """
    worker, graph, _ = setup
    fed = ["evt-1", "evt-2"]
    await worker.consume_events([
        _decision_event("evt-1", "auth service", "named authsvc"),
    ])
    await worker.consume_events([
        _decision_event("evt-2", "gateway", "front with a gateway"),
    ])

    ambient = await load_ambient(graph)
    # No fabricated receipts — every derived_from id was actually a source event.
    assert ambient.derived_from
    assert all(r in fed for r in ambient.derived_from)
    # The headline receipt is one of the real inputs.
    assert ambient.source_event_id in fed


@pytest.mark.asyncio
async def test_continuity_capsule_tracks_time_shared_work_and_open_loop_receipts(setup):
    """The standing self carries an explicit continuity capsule for the orchestrator.

    This is the "don't feel fresh" contract: the runtime ambient layer should
    know what shared work is active, when it was derived, what the latest
    decision was, and which open loops/receipts it came from.
    """
    worker, graph, _ = setup
    await worker.consume_events([
        _decision_event(
            "evt-decision",
            "standing self",
            "make continuity a deterministic ambient capsule",
            thread_id="thread-A",
        ),
        _open_loop_event(
            "evt-loop",
            "push the continuity capsule increment to dev",
            thread_id="thread-A",
            task_id="task-parallel-1",
        ),
    ])

    ambient = await load_ambient(graph)
    capsule = ambient.continuity_capsule

    assert capsule["active_shared_work"] == ambient.narrative
    assert capsule["last_decision"]["topic"] == "standing self"
    assert capsule["last_decision"]["source_event_id"] == "evt-decision"
    assert capsule["open_loops"][0]["intent"] == "push the continuity capsule increment to dev"
    assert capsule["open_loops"][0]["source_event_id"] == "evt-loop"
    assert set(capsule["source_event_ids"]) == {"evt-decision", "evt-loop"}
    assert capsule["current_time_context"]["generated_at"] == ambient.derived_at
    assert capsule["current_time_context"]["relative_label"] in {
        "earlier today",
        "yesterday",
        "previous work",
    }
    rendered = ambient.render(budget=280)
    assert "Current work:" in rendered
    assert "Continuity:" in rendered
    assert "last decision=standing self" in rendered
