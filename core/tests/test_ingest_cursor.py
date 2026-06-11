"""Phase 3b.4 — Cursor ingestion adapter tests.

Cursor keeps chat in a SQLite ``state.vscdb`` key-value table (``ItemTable`` /
``cursorDiskKV``) under JSON values whose shape varies between releases. These
tests prove the adapter harvests messages tolerantly into
``ingest.cursor.message`` events (idempotent, incremental, redacted), folds
assistant turns only, counts in discovery, and **degrades honestly** (skips with
a reason) when no chat table is present.
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
from centri.ingest import CursorIngestor
from centri.memory_graph import MemoryGraph


def _make_vscdb(path: Path, items, table: str = "ItemTable") -> None:
    """Build a state.vscdb with a (key, value) KV table. ``items`` = {key: value}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE {table} (key TEXT PRIMARY KEY, value BLOB)")
    conn.executemany(
        f"INSERT INTO {table} (key, value) VALUES (?, ?)",
        [(k, v if isinstance(v, (bytes, str)) else json.dumps(v)) for k, v in items.items()],
    )
    conn.commit()
    conn.close()


def _chat_blob(messages):
    return json.dumps({"messages": messages})


@pytest.fixture
async def env():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    ingestor = CursorIngestor(db)
    yield ingestor, db, graph, Path(tmpdir)
    await db.close()


class TestCursorIngest:
    async def test_ingests_chat_messages(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        _make_vscdb(vscdb, {
            "workbench.panel.aichat.view.aichat.chatdata": _chat_blob([
                {"id": "b1", "role": "user", "text": "build the parser",
                 "timestamp": "2026-06-01T10:00:00Z"},
                {"id": "b2", "role": "assistant", "text": "Wrote a recursive-descent parser.",
                 "timestamp": "2026-06-01T10:01:00Z"},
            ]),
            "some.unrelated.key": json.dumps({"foo": "bar"}),
        })
        result = await ingestor.ingest(vscdb, source="cur-test")
        assert result["ingested"] == 2
        assert result["available"] is True
        assert result["agent"] == "cursor"
        events = [e for e in await db.recent_events(limit=50)
                  if e["type"] == "ingest.cursor.message"]
        assert len(events) == 2
        assert all(e["source"] == "ingest.cursor" for e in events)

    async def test_rerun_idempotent(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        _make_vscdb(vscdb, {
            "chatdata": _chat_blob([
                {"id": "b1", "role": "assistant", "text": "one", "timestamp": "2026-06-01T10:00:00Z"},
            ]),
        })
        assert (await ingestor.ingest(vscdb, source="cur-test"))["ingested"] == 1
        assert (await ingestor.ingest(vscdb, source="cur-test"))["ingested"] == 0
        events = [e for e in await db.recent_events(limit=50)
                  if e["type"] == "ingest.cursor.message"]
        assert len(events) == 1

    async def test_incremental_new_message(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        _make_vscdb(vscdb, {
            "chatdata": _chat_blob([
                {"id": "b1", "role": "assistant", "text": "one", "timestamp": "2026-06-01T10:00:00Z"},
            ]),
        })
        assert (await ingestor.ingest(vscdb, source="cur-test"))["ingested"] == 1
        # Rewrite the KV value with an extra message (Cursor mutates the blob).
        conn = sqlite3.connect(str(vscdb))
        conn.execute(
            "UPDATE ItemTable SET value=? WHERE key='chatdata'",
            (_chat_blob([
                {"id": "b1", "role": "assistant", "text": "one", "timestamp": "2026-06-01T10:00:00Z"},
                {"id": "b2", "role": "assistant", "text": "two", "timestamp": "2026-06-01T11:00:00Z"},
            ]),),
        )
        conn.commit()
        conn.close()
        result = await ingestor.ingest(vscdb, source="cur-test")
        assert result["ingested"] == 1
        texts = [e["payload_json"] for e in await db.recent_events(limit=50)
                 if e["type"] == "ingest.cursor.message"]
        assert any("two" in t for t in texts)

    async def test_redaction_applied(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        _make_vscdb(vscdb, {
            "chatdata": _chat_blob([
                {"id": "b1", "role": "assistant", "text": "secret ghp_abcdefghijklmnop end",
                 "timestamp": "2026-06-01T10:00:00Z"},
            ]),
        })
        await ingestor.ingest(vscdb, source="cur-test")
        rows = [e["payload_json"] for e in await db.recent_events(limit=50)
                if e["type"] == "ingest.cursor.message"]
        assert rows and "ghp_abcdefghijklmnop" not in rows[0]

    async def test_schema_tolerance_alt_table_and_keys(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        # cursorDiskKV table; "type" instead of "role"; "content" instead of "text".
        _make_vscdb(vscdb, {
            "composer.composerData": json.dumps({
                "conversation": [
                    {"bubbleId": "x1", "type": "assistant",
                     "content": "alt-shaped message", "createdAt": 1717236000000},
                ],
            }),
        }, table="cursorDiskKV")
        result = await ingestor.ingest(vscdb, source="cur-alt")
        assert result["ingested"] == 1
        rows = [json.loads(e["payload_json"]) for e in await db.recent_events(limit=50)
                if e["type"] == "ingest.cursor.message"]
        assert rows and rows[0]["text"] == "alt-shaped message"

    async def test_missing_kv_table_degrades_honestly(self, env):
        ingestor, db, _, tmp = env
        vscdb = tmp / "state.vscdb"
        # A valid SQLite db with no key-value table at all.
        conn = sqlite3.connect(str(vscdb))
        conn.execute("CREATE TABLE other (a TEXT)")
        conn.commit()
        conn.close()
        result = await ingestor.ingest(vscdb, source="cur-test")
        assert result["available"] is False
        assert result["ingested"] == 0

    async def test_missing_store_unavailable(self, env):
        ingestor, _, _, tmp = env
        result = await ingestor.ingest(tmp / "nope.vscdb", source="cur-test")
        assert result["available"] is False

    async def test_workspace_storage_dir_scanned(self, env):
        ingestor, db, _, tmp = env
        ws = tmp / "workspaceStorage"
        _make_vscdb(ws / "hashA" / "state.vscdb", {
            "chatdata": _chat_blob([
                {"id": "a1", "role": "assistant", "text": "from A", "timestamp": "2026-06-01T10:00:00Z"},
            ]),
        })
        _make_vscdb(ws / "hashB" / "state.vscdb", {
            "chatdata": _chat_blob([
                {"id": "b1", "role": "assistant", "text": "from B", "timestamp": "2026-06-01T10:00:00Z"},
            ]),
        })
        result = await ingestor.ingest(ws, source="cur-ws")
        assert result["ingested"] == 2
        texts = [e["payload_json"] for e in await db.recent_events(limit=50)
                 if e["type"] == "ingest.cursor.message"]
        joined = " ".join(texts)
        assert "from A" in joined and "from B" in joined

    async def test_discover_counts(self, env):
        ingestor, _, _, tmp = env
        vscdb = tmp / "state.vscdb"
        _make_vscdb(vscdb, {
            "chatdata": _chat_blob([
                {"id": "b1", "role": "assistant", "text": "one", "timestamp": "t"},
                {"id": "b2", "role": "assistant", "text": "two", "timestamp": "t"},
            ]),
        })
        found = ingestor.discover(extra_paths=[str(vscdb)])
        assert any(s.available and s.count == 2 for s in found)
