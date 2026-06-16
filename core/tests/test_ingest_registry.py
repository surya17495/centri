"""Phase 3b.4 — ingestion registry, discovery, and bootstrap tests.

The registry fans out to per-agent adapters sharing one HWM/idempotency core.
These tests prove discovery probes configured paths across agents (honest counts,
honest-unavailable when absent), and that bootstrap is *first tick*: a one-time
full import across discovered sources that emits progress events on the spine and
is idempotent on re-run.
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.ingest import DiscoveredSource, IngestConfig, IngestRegistry
from centri.memory_graph import MemoryGraph


def _make_opencode_db(path: Path, rows) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE event (id TEXT PRIMARY KEY, type TEXT, aggregate_id TEXT, data TEXT, created_at TEXT)"
    )
    formatted_rows = []
    for r in rows:
        rid, session_id, role, content, ts = r
        data_json = json.dumps({"part": {"type": role, "text": content}})
        formatted_rows.append((rid, "message.part.created", session_id, data_json, ts))
    conn.executemany(
        "INSERT INTO event (id,type,aggregate_id,data,created_at) VALUES (?,?,?,?,?)",
        formatted_rows
    )
    conn.commit()
    conn.close()


def _make_cursor_db(path: Path, messages) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("chatdata", json.dumps({"messages": messages})),
    )
    conn.commit()
    conn.close()


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
async def env(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    # Isolate discovery from the real machine: point Path.home() at an empty tmp
    # home so adapters' default_locations() find nothing real. Tests then supply
    # fixture stores via IngestConfig.extra_paths only — fixture-verified, never
    # reading the sandbox's actual ~/.claude / Cursor state.
    home = Path(tmpdir) / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    yield db, graph, Path(tmpdir)
    await db.close()


def _registry_with_sources(db, tmp):
    """Build a registry whose three agents point at fixture stores via config."""
    oc = tmp / "opencode.db"
    _make_opencode_db(oc, [("m1", "s1", "assistant", "oc answer to the user query regarding how to compute EWMA correctly.", "2026-06-01T10:00:00Z")])
    cc = tmp / "claude" / "projects"
    _write_jsonl(cc / "s.jsonl", [
        {"uuid": "u1", "sessionId": "s1", "role": "assistant",
         "content": "cc answer", "timestamp": "2026-06-01T10:00:00Z"},
    ])
    cur = tmp / "state.vscdb"
    _make_cursor_db(cur, [
        {"id": "b1", "role": "assistant", "text": "cursor answer", "timestamp": "2026-06-01T10:00:00Z"},
    ])
    config = IngestConfig(extra_paths={
        "opencode": [str(oc)],
        "claude_code": [str(cc)],
        "cursor": [str(cur)],
    })
    return IngestRegistry(db, config=config)


class TestDiscovery:
    async def test_discover_finds_all_three_with_counts(self, env):
        db, _, tmp = env
        reg = _registry_with_sources(db, tmp)
        summary = reg.discover_summary()
        agents = {s["agent"] for s in summary["sources"] if s["available"]}
        assert {"opencode", "claude_code", "cursor"} <= agents
        # Each fixture has exactly one message.
        assert summary["total_messages"] == 3
        assert summary["available_count"] == 3

    async def test_disabled_agent_skipped(self, env):
        db, _, tmp = env
        reg = _registry_with_sources(db, tmp)
        reg._config.disabled = {"cursor"}
        summary = reg.discover_summary()
        agents = {s["agent"] for s in summary["sources"]}
        assert "cursor" not in agents
        assert "cursor" not in summary["agents"]

    async def test_discover_honest_unavailable_when_nothing(self, env):
        db, _, tmp = env
        # No fixtures, no real default stores expected in the sandbox home.
        reg = IngestRegistry(db, config=IngestConfig(extra_paths={
            "opencode": [str(tmp / "absent.db")],
        }))
        summary = reg.discover_summary()
        # Absent configured path produces no available source (skipped silently),
        # and no fabricated counts.
        assert summary["available_count"] == 0 or all(
            not s["available"] for s in summary["sources"] if s["agent"] == "opencode"
        )


class TestBootstrap:
    async def test_bootstrap_imports_all_and_emits_progress(self, env):
        db, _, tmp = env
        reg = _registry_with_sources(db, tmp)
        result = await reg.bootstrap()
        assert result["imported"] == 3
        events = await db.recent_events(limit=100)
        types = [e["type"] for e in events]
        assert "ingest.bootstrap.started" in types
        assert "ingest.bootstrap.completed" in types
        assert types.count("ingest.bootstrap.progress") == 3
        # The actual messages landed as their per-agent ingest events.
        msg_types = {e["type"] for e in events if e["type"].startswith("ingest.") and (e["type"].endswith(".message") or e["type"].endswith(".transcript"))}
        assert {"ingest.opencode.transcript", "ingest.claude_code.message", "ingest.cursor.message"} <= msg_types

    async def test_bootstrap_idempotent(self, env):
        db, _, tmp = env
        reg = _registry_with_sources(db, tmp)
        first = await reg.bootstrap()
        assert first["imported"] == 3
        second = await reg.bootstrap()
        assert second["imported"] == 0  # HWM means first tick already drained

    async def test_bootstrap_explicit_sources(self, env):
        db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [("m1", "s1", "assistant", "only this message from the user session is expected to be processed.", "2026-06-01T10:00:00Z")])
        reg = IngestRegistry(db, config=IngestConfig())
        sources = [DiscoveredSource(agent="opencode", path=str(oc), available=True)]
        result = await reg.bootstrap(sources=sources)
        assert result["imported"] == 1
        assert result["source_count"] == 1
