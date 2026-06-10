"""CENTRI memory_store tests — memory is a derived, re-derivable index.

Proves the core contract: synthesis folds events into memory, and
rebuild_from_events reconstructs that memory purely from the event ledger.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.memory_store import CORE_BLOCKS, ArchivalFact, SqliteMemoryStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def store():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield SqliteMemoryStore(db), db
    await db.close()


class TestCoreBlocks:
    async def test_set_and_get_block(self, store):
        ms, _ = store
        await ms.set_block("priorities", "ship phase 0")
        assert await ms.get_block("priorities") == "ship phase 0"

    async def test_all_blocks_returns_full_set(self, store):
        ms, _ = store
        blocks = await ms.all_blocks()
        assert set(blocks.keys()) == set(CORE_BLOCKS)


class TestArchivalFacts:
    async def test_insert_and_search(self, store):
        ms, _ = store
        await ms.insert_fact(ArchivalFact(id="f1", text="user prefers terse replies"))
        hits = await ms.search_facts("terse")
        assert len(hits) == 1 and hits[0].id == "f1"


class TestSynthesisAndRebuild:
    async def test_consume_events_tracks_active_project(self, store):
        ms, _ = store
        n = await ms.consume_events([
            {"id": "evt-1", "type": "task.started", "payload": {"description": "build the spine"}},
        ])
        assert n == 1
        assert await ms.get_block("active_project") == "build the spine"

    async def test_rebuild_from_events_is_re_derivable(self, store):
        ms, db = store
        # Write events to the ledger (the source of truth).
        await db.append_event("evt-a", "task.started", "coordinator", _now(), payload={"description": "task A"})
        await db.append_event("evt-b", "task.completed", "jobs", _now(), payload={"summary": "task A done"})
        # Rebuild memory purely from the ledger.
        written = await ms.rebuild_from_events()
        assert written == 2
        assert await ms.get_block("active_project") == "task A"
        hits = await ms.search_facts("done")
        assert any("done" in f.text for f in hits)

        # Wiping memory and rebuilding again yields the same result.
        await ms.set_block("active_project", "STALE")
        await ms.rebuild_from_events()
        assert await ms.get_block("active_project") == "task A"
