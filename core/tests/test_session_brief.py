"""Session-start push briefing (Increment 2).

On session start CENTRI builds the deterministic, LLM-free ProactiveBrief and
surfaces it unprompted: a ``brief.session_start`` spine event (so connected
shells render it) plus a one-shot prepend into the FIRST turn's curated context.
These tests drive the Coordinator directly with a real Database + MemoryGraph +
ProactiveBriefBuilder — no model, no network, fully offline.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.coordinator import Coordinator
from centri.db import Database
from centri.memory_brief import ProactiveBriefBuilder
from centri.memory_graph import LOOP_DORMANT, MemoryGraph, OpenLoop
from centri.schemas import ContextPacket


class _StubMemory:
    async def recall(self, *a, **k):
        return []


class _StubContextAssembler:
    def __init__(self, recent_events=None, repo_id=None):
        self._recent = recent_events or []
        self._repo_id = repo_id

    async def assemble(self, *a, **k):
        return ContextPacket(repo_id=self._repo_id), []


class _StubModelRouter:
    def complete(self, *a, **k):
        return None

    async def acomplete(self, *a, **k):
        return None


class _CapturingBus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


def _make_coordinator(db, graph, *, session_brief_enabled=True, event_bus=None):
    builder = ProactiveBriefBuilder(db, graph)
    return Coordinator(
        db=db,
        model_router=_StubModelRouter(),
        memory=_StubMemory(),
        context_assembler=_StubContextAssembler(),
        permissions=None,
        hands=None,
        jobs=None,
        artifacts=None,
        event_bus=event_bus,
        hot_cache=None,
        briefing_builder=None,
        memory_brief=None,
        curator=None,
        proactive_brief=builder,
        session_brief_enabled=session_brief_enabled,
    )


async def _events_of(db, type_):
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


async def _seed(db, g):
    """Seed one of each section so the brief is non-empty and deterministic."""
    from datetime import datetime, timezone
    # changed <- user.utterance event
    await db.append_event(
        event_id="evt-syn-1",
        type="user.utterance",
        source="memory",
        ts=datetime.now(timezone.utc).isoformat(),
        payload={"text": "switched deploy to Caddy"},
    )
    # blocked <- task.failed event
    await db.append_event(
        event_id="evt-fail-1",
        type="task.failed",
        source="jobs",
        ts="2026-01-02T00:00:00+00:00",
        payload={"error": "migration 0042 failed: column already exists"},
    )
    # next_steps <- open loop
    await g.add_open_loop(
        OpenLoop(
            id="loop-1",
            intent="wire up the embeddings backfill endpoint",
            source_event_id="evt-loop-1",
            created_at="2026-01-03T00:00:00+00:00",
        )
    )
    # dormancy_questions <- dormant loop
    await g.add_open_loop(
        OpenLoop(
            id="loop-2",
            intent="rewrite the auth middleware",
            state=LOOP_DORMANT,
            source_event_id="evt-loop-2",
            created_at="2026-01-04T00:00:00+00:00",
        )
    )


@pytest.mark.asyncio
async def test_emit_session_brief_records_event_with_receipts(graph_db):
    db, g = graph_db
    await _seed(db, g)
    coord = _make_coordinator(db, g)

    payload = await coord.emit_session_brief()

    assert payload is not None
    assert payload["is_empty"] is False
    assert payload["changed_count"] == 1
    assert payload["blocked_count"] == 1
    assert payload["next_count"] == 1
    assert payload["dormancy_count"] == 1

    events = await _events_of(db, "brief.session_start")
    assert len(events) == 1
    ev = events[0]
    # The rendered brief is persisted in the payload so the event is replayable.
    assert "switched deploy to Caddy" in ev["payload"]["summary"]
    assert ev["payload"]["changed_count"] == 1
    assert ev["payload"]["is_empty"] is False


@pytest.mark.asyncio
async def test_emit_session_brief_content_covers_all_sections(graph_db):
    db, g = graph_db
    await _seed(db, g)
    coord = _make_coordinator(db, g)

    payload = await coord.emit_session_brief()
    rendered = payload["summary"]

    assert "switched deploy to Caddy" in rendered
    assert "migration 0042 failed" in rendered
    assert "wire up the embeddings backfill endpoint" in rendered
    assert "rewrite the auth middleware" in rendered


@pytest.mark.asyncio
async def test_empty_brief_still_emits_event_with_zero_counts(graph_db):
    db, g = graph_db
    coord = _make_coordinator(db, g)

    payload = await coord.emit_session_brief()

    assert payload is not None
    assert payload["is_empty"] is True
    assert payload["changed_count"] == 0
    assert payload["blocked_count"] == 0
    assert payload["next_count"] == 0
    assert payload["dormancy_count"] == 0

    events = await _events_of(db, "brief.session_start")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_flag_off_disables_emission(graph_db):
    db, g = graph_db
    await _seed(db, g)
    coord = _make_coordinator(db, g, session_brief_enabled=False)

    payload = await coord.emit_session_brief()

    assert payload is None
    events = await _events_of(db, "brief.session_start")
    assert events == []


@pytest.mark.asyncio
async def test_first_turn_injection_is_stashed_then_one_shot(graph_db):
    db, g = graph_db
    await _seed(db, g)
    coord = _make_coordinator(db, g)

    await coord.emit_session_brief()
    # After emission the rendered brief is stashed for the first turn to prepend.
    assert coord._pending_session_brief is not None
    assert "switched deploy to Caddy" in coord._pending_session_brief

    # Exercise the exact handle_utterance injection block (lines after
    # _build_context_parallel): prepend once, then clear so later turns are not
    # re-briefed.
    packet = ContextPacket(relevant_recall=["existing recall line"])
    if coord._pending_session_brief:
        packet.relevant_recall = [coord._pending_session_brief] + list(
            packet.relevant_recall or []
        )
        coord._pending_session_brief = None

    assert "switched deploy to Caddy" in packet.relevant_recall[0]
    assert "existing recall line" in packet.relevant_recall
    # One-shot: a second turn finds nothing pending.
    assert coord._pending_session_brief is None


@pytest.mark.asyncio
async def test_published_event_mirrors_summary_for_shells(graph_db):
    db, g = graph_db
    await _seed(db, g)
    bus = _CapturingBus()
    coord = _make_coordinator(db, g, event_bus=bus)

    await coord.emit_session_brief()

    published = [e for e in bus.published if e.get("type") == "brief.session_start"]
    assert len(published) == 1
    # Shells render the brief off the top-level "summary" mirror on the bus event.
    assert "switched deploy to Caddy" in published[0]["summary"]


@pytest.mark.asyncio
async def test_empty_brief_does_not_stash_for_injection(graph_db):
    db, g = graph_db
    coord = _make_coordinator(db, g)

    await coord.emit_session_brief()

    # Nothing to prepend when there is nothing pending.
    assert coord._pending_session_brief is None


@pytest.mark.asyncio
async def test_proactive_brief_includes_hermes_messages(graph_db):
    db, g = graph_db
    from datetime import datetime, timezone
    
    # Seed hermes.user.message
    await db.append_event(
        event_id="evt-hermes-user",
        type="hermes.user.message",
        source="hermes_turn_sync",
        ts=datetime.now(timezone.utc).isoformat(),
        payload={"text": "user message via hermes"},
    )
    # Seed hermes.assistant.message
    await db.append_event(
        event_id="evt-hermes-asst",
        type="hermes.assistant.message",
        source="hermes_turn_sync",
        ts=datetime.now(timezone.utc).isoformat(),
        payload={"text": "assistant message via hermes"},
    )
    
    builder = ProactiveBriefBuilder(db, g)
    brief = await builder.build()
    
    assert "user message via hermes" in brief.changed
    assert "assistant message via hermes" in brief.changed
