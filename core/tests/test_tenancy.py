"""Phase A tenancy-key migration tests (Decision 9).

Every spine/graph row carries a ``tenant_id`` (default ``"local"``). These tests
prove three things:

  - a DB created *before* the column existed migrates in place, preserving its
    existing rows (which inherit the ``"local"`` default);
  - new events and graph nodes carry ``tenant_id`` end to end;
  - the single-tenant default means no behavior change (the 202 baseline holds).

pytest: python -m pytest tests/test_tenancy.py -v
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import DEFAULT_TENANT, Database
from centri.memory_graph import Decision, Fact, MemoryGraph, OpenLoop


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixture-DB migration: a pre-Phase-A schema upgrades in place
# ---------------------------------------------------------------------------
def _legacy_events_schema(path: Path) -> None:
    """Create an `events` table WITHOUT tenant_id and seed one row, the way a
    DB from before Decision 9 would look on disk."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE events (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            ts TEXT NOT NULL,
            thread_id TEXT,
            task_id TEXT,
            repo_id TEXT,
            importance TEXT DEFAULT 'low',
            payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        "INSERT INTO events (id, type, source, ts, payload_json) VALUES (?,?,?,?,?)",
        ("legacy-1", "user.utterance", "test", _now(), "{}"),
    )
    # A legacy derived graph table, also missing tenant_id.
    conn.execute(
        """CREATE TABLE mem_facts (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            statement TEXT NOT NULL,
            source_event_id TEXT,
            repo_id TEXT,
            created_at TEXT NOT NULL,
            superseded_by TEXT,
            invalidated_at TEXT,
            tags TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT INTO mem_facts (id, topic, statement, created_at) VALUES (?,?,?,?)",
        ("legacy-f1", "layout", "backend lives under core/src/centri", _now()),
    )
    conn.commit()
    conn.close()


class TestMigration:
    async def test_existing_db_migrates_and_preserves_rows(self):
        tmpdir = tempfile.mkdtemp()
        path = Path(tmpdir) / "state.db"
        _legacy_events_schema(path)

        # Opening the DB through the wrapper runs the additive migration.
        db = Database(path)

        # The legacy event row survived and inherited the default tenant.
        events = await db.recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["id"] == "legacy-1"
        assert events[0]["tenant_id"] == DEFAULT_TENANT

        # The legacy graph fact survived; reading it through MemoryGraph stamps
        # the default tenant onto the object.
        g = MemoryGraph(db)
        await g.ensure_tables()
        facts = await g.current_facts()
        assert any(f.id == "legacy-f1" for f in facts)
        assert all(f.tenant_id == DEFAULT_TENANT for f in facts)
        await db.close()

    async def test_migration_is_idempotent(self):
        tmpdir = tempfile.mkdtemp()
        path = Path(tmpdir) / "state.db"
        _legacy_events_schema(path)
        # Open twice — the second open must not error on an already-present column.
        Database(path)
        db = Database(path)
        cols = {
            r[1]
            for r in (await db._execute("PRAGMA table_info(events)")).fetchall()
        }
        assert "tenant_id" in cols
        await db.close()


# ---------------------------------------------------------------------------
# New rows carry tenant_id end to end
# ---------------------------------------------------------------------------
class TestNewRowsCarryTenant:
    async def test_append_event_defaults_to_local(self):
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        await db.append_event("e1", "user.utterance", "test", _now())
        events = await db.recent_events(limit=10)
        assert events[0]["tenant_id"] == DEFAULT_TENANT
        await db.close()

    async def test_append_event_honors_explicit_tenant(self):
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        await db.append_event("e-acme", "user.utterance", "test", _now(), tenant_id="acme")
        await db.append_event("e-local", "user.utterance", "test", _now())
        # recent_events filters by tenant; the default view sees only "local".
        local = await db.recent_events(limit=10)
        assert [e["id"] for e in local] == ["e-local"]
        acme = await db.recent_events(limit=10, tenant_id="acme")
        assert [e["id"] for e in acme] == ["e-acme"]
        await db.close()

    async def test_graph_nodes_carry_tenant(self):
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        g = MemoryGraph(db)
        await g.ensure_tables()
        await g.add_decision(
            Decision(id="d1", topic="auth", statement="adopt rotating tokens",
                     source_event_id="evt-d1", created_at=_now())
        )
        await g.add_fact(
            Fact(id="f1", topic="testing", statement="real db, no mocks",
                 source_event_id="evt-f1", created_at=_now())
        )
        await g.add_open_loop(
            OpenLoop(id="l1", intent="wire rotation", source_event_id="evt-l1",
                     cue="auth", created_at=_now())
        )
        decs = await g.current_decisions()
        facts = await g.current_facts()
        loops = await g.open_loops()
        assert decs[0].tenant_id == DEFAULT_TENANT
        assert facts[0].tenant_id == DEFAULT_TENANT
        assert loops[0].tenant_id == DEFAULT_TENANT
        await db.close()

    async def test_explicit_tenant_round_trips_through_graph(self):
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        g = MemoryGraph(db)
        await g.ensure_tables()
        await g.add_fact(
            Fact(id="f-acme", topic="layout", statement="acme layout",
                 source_event_id="evt", created_at=_now(), tenant_id="acme")
        )
        facts = await g.current_facts()
        assert facts[0].tenant_id == "acme"
        await db.close()
