"""Phase 1.1 — verbatim recall as a first-class turn capability (master plan §2.10).

The differentiator distillation-only incumbents structurally cannot offer: from a
DIFFERENT session, recall the EXACT original wording a user said earlier, byte-equal
(not paraphrased / distilled), with a receipt (``source_event_id``) that resolves to
the originating event.

These tests drive the Coordinator directly with a real Database + MemoryGraph — no
model, no network, fully offline. Cross-session boundaries are modelled by the spine
``thread_id`` (a thread/session is just a view over the one global memory; recall is
global). The on-demand page-in must itself be auditable on the spine via a
``recall.verbatim`` event (master plan §2.8).
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.coordinator import Coordinator
from centri.db import Database
from centri.memory_graph import MemoryGraph
from centri.schemas import ContextPacket


class _StubMemory:
    async def recall(self, *a, **k):
        return []


class _StubContextAssembler:
    async def assemble(self, *a, **k):
        return ContextPacket(), []


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


def _make_coordinator(db, *, event_bus=None):
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
    )


@pytest.fixture
async def db():
    tmpdir = tempfile.mkdtemp()
    database = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(database)
    await graph.ensure_tables()
    yield database
    await database.close()


# The exact sentence said in session A. Recall must return this byte-for-byte.
_VERBATIM = "We decided to name the auth service authsvc and front it with a gateway at /auth."


async def _seed_session_a(db, *, thread_id="thread-A", event_id="evt-session-a-1"):
    """Store one verbatim user utterance in session A and return its event id."""
    await db.append_event(
        event_id=event_id,
        type="user.utterance",
        source="text",
        ts="2026-06-01T10:00:00+00:00",
        thread_id=thread_id,
        payload={"text": _VERBATIM, "user_id": "u1", "thread_id": thread_id},
    )
    return event_id


@pytest.mark.asyncio
async def test_recall_verbatim_returns_exact_text_cross_session(db):
    """Session B recalls the EXACT sentence said in session A (byte-equal)."""
    src_event_id = await _seed_session_a(db)

    coord = _make_coordinator(db)
    # The recalling turn lives in a DIFFERENT session (thread-B). Memory is global,
    # so the query must reach across the session boundary.
    matches = await coord.recall_verbatim("authsvc gateway", scope="global")

    assert matches, "expected at least one verbatim match across the session boundary"
    top = matches[0]
    # Byte-equal, not a paraphrase or distilled summary.
    assert top.text == _VERBATIM
    # Receipt resolves to session A's originating event.
    assert top.source_event_id == src_event_id
    # The match came from session A, proving the boundary was crossed.
    assert top.thread_id == "thread-A"


@pytest.mark.asyncio
async def test_recall_verbatim_receipt_resolves_to_real_event(db):
    """The returned source_event_id resolves to the actual spine event, byte-equal."""
    src_event_id = await _seed_session_a(db)
    coord = _make_coordinator(db)

    matches = await coord.recall_verbatim("authsvc", scope="global")
    assert matches
    receipt = matches[0].source_event_id

    rows = await db.recent_events(limit=200)
    by_id = {r["id"]: r for r in rows}
    assert receipt in by_id, "receipt must resolve to a real spine event"
    import json

    payload = json.loads(by_id[receipt]["payload_json"])
    assert payload["text"] == _VERBATIM


@pytest.mark.asyncio
async def test_recall_verbatim_emits_auditable_event(db):
    """The on-demand page-in is itself recorded on the spine + bus (§2.8)."""
    await _seed_session_a(db)
    bus = _CapturingBus()
    coord = _make_coordinator(db, event_bus=bus)

    await coord.recall_verbatim("authsvc gateway", scope="global")

    # Published to connected shells.
    published = [e for e in bus.published if e.get("type") == "recall.verbatim"]
    assert len(published) == 1
    body = published[0]
    assert body["payload"]["query"] == "authsvc gateway"
    assert body["payload"]["scope"] == "global"
    assert body["payload"]["match_count"] >= 1
    # The receipts of the matches are on the event so the page-in is auditable.
    assert body["payload"]["source_event_ids"]

    # Persisted to the append-only spine.
    rows = await db.recent_events(limit=200)
    persisted = [r for r in rows if r["type"] == "recall.verbatim"]
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_recall_verbatim_honest_empty_when_no_match(db):
    """No match → honest empty result (no fabricated answer), still auditable."""
    await _seed_session_a(db)
    bus = _CapturingBus()
    coord = _make_coordinator(db, event_bus=bus)

    matches = await coord.recall_verbatim("kubernetes helm chart rollout", scope="global")
    assert matches == []

    published = [e for e in bus.published if e.get("type") == "recall.verbatim"]
    assert len(published) == 1
    assert published[0]["payload"]["match_count"] == 0


@pytest.mark.asyncio
async def test_recall_verbatim_not_paraphrased_distinct_from_distillation(db):
    """Guard: recall returns the ORIGINAL words, not a consolidated/distilled fact.

    A distilled fact about the same topic must not be mistaken for the verbatim
    original. The verbatim path returns the raw utterance text exactly.
    """
    src_event_id = await _seed_session_a(db)
    # A distilled/paraphrased restatement of the same topic also lives on the spine
    # (the kind of lossy summary incumbents keep). It must NOT be returned as the
    # verbatim original.
    await db.append_event(
        event_id="evt-distilled-1",
        type="memory.synthesized",
        source="memory",
        ts="2026-06-02T10:00:00+00:00",
        thread_id="thread-A",
        payload={"text": "Auth service is called authsvc."},
    )
    coord = _make_coordinator(db)

    matches = await coord.recall_verbatim("authsvc", scope="global")
    assert matches
    # The top match is the byte-equal original, and its receipt is the utterance,
    # not the synthesized distillation.
    assert matches[0].text == _VERBATIM
    assert matches[0].source_event_id == src_event_id
    # The system's own memory.synthesized event is never surfaced as verbatim recall.
    assert all(m.source_event_id != "evt-distilled-1" for m in matches)
