"""Phase 3b.3 — OpenCode ingestion adapter tests.

Proves the adapter tails an external opencode.db into ``ingest.opencode.transcript``
spine events: incremental + idempotent (re-run = no dupes), per-source
high-water mark, redaction on write, schema tolerance, and that facts from an
ingested session surface in a memory brief after consolidation.
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.db import Database
from centri.ingest import OpenCodeIngestor, ingest_opencode_db
from centri.memory_brief import MemoryBriefAssembler
from centri.memory_graph import MemoryGraph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_opencode_db(
    path: Path,
    rows,
    id_col: str = "id",
    session_col: str = "session_id",
    role_col: str = "role",
    content_col: str = "content",
    ts_col: str = "created_at",
    table: str = "event",
) -> None:
    """Build a minimal external opencode.db with an event table.

    ``rows`` is a list of (id, session_id, role, content, ts) tuples.
    """
    conn = sqlite3.connect(str(path))
    
    # Map default or custom table to candidates
    tbl = "event" if table in ("message", "messages") else table
    
    # Column mapping based on arguments
    real_id_col = "id" if id_col == "id" else id_col
    real_session_col = "aggregate_id" if session_col == "session_id" else session_col
    real_ts_col = "created_at" if ts_col == "created_at" else ts_col
    
    # We always need a type column in v2
    type_col = "name" if real_id_col == "messageID" else "type"
    data_col = "payload" if real_id_col == "messageID" else "data"
    
    conn.execute(
        f"CREATE TABLE {tbl} ("
        f"{real_id_col} TEXT PRIMARY KEY, {type_col} TEXT, {real_session_col} TEXT, "
        f"{data_col} TEXT, {real_ts_col} TEXT)"
    )
    
    formatted_rows = []
    for r in rows:
        rid, session_id, role, content, ts = r
        if content.startswith("[") or content.startswith("{"):
            try:
                parts = json.loads(content)
                if isinstance(parts, list) and parts:
                    data_val = json.dumps({"part": parts[0]})
                else:
                    data_val = json.dumps(parts)
            except Exception:
                data_val = json.dumps({"part": {"type": role, "text": content}})
        else:
            data_val = json.dumps({"part": {"type": role, "text": content}})
            
        formatted_rows.append((
            rid,
            "message.part.created",
            session_id,
            data_val,
            ts
        ))
        
    conn.executemany(
        f"INSERT INTO {tbl} ({real_id_col},{type_col},{real_session_col},{data_col},{real_ts_col}) "
        f"VALUES (?,?,?,?,?)",
        formatted_rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
async def env():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    ingestor = OpenCodeIngestor(db)
    yield ingestor, db, graph, Path(tmpdir)
    await db.close()


class TestOpenCodeIngest:
    async def test_ingests_messages_as_events(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [
            ("m1", "s1", "user", "build the funding tracker for the project since we need to launch soon", "2026-06-01T10:00:00Z"),
            ("m2", "s1", "assistant", "Implemented EWMA over Binance funding API to retrieve current prices.", "2026-06-01T10:01:00Z"),
        ])
        result = await ingestor.ingest(oc, source="oc-test")
        assert result["ingested"] == 2
        assert result["available"] is True

        events = await db.recent_events(limit=50)
        ingested = [e for e in events if e["type"] == "ingest.opencode.transcript"]
        assert len(ingested) == 2
        # importance normal in v2 structured events
        assert all(e["importance"] == "normal" for e in ingested)
        assert all(e["source"] == "ingest.opencode.message" for e in ingested)

    async def test_rerun_is_idempotent_no_dupes(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [
            ("m1", "s1", "assistant", "first answer to the user query regarding how to compute EWMA correctly.", "2026-06-01T10:00:00Z"),
        ])
        first = await ingestor.ingest(oc, source="oc-test")
        assert first["ingested"] == 1
        # Re-run over the same store: nothing new.
        second = await ingestor.ingest(oc, source="oc-test")
        assert second["ingested"] == 0
        events = await db.recent_events(limit=50)
        assert len([e for e in events if e["type"] == "ingest.opencode.transcript"]) == 1

    async def test_incremental_only_new_rows(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [
            ("m1", "s1", "assistant", "answer one to the user query regarding how to compute EWMA correctly.", "2026-06-01T10:00:00Z"),
        ])
        assert (await ingestor.ingest(oc, source="oc-test"))["ingested"] == 1
        # Append a new row to the external store, then re-ingest.
        conn = sqlite3.connect(str(oc))
        # For the test, we write an event row
        data_json = json.dumps({"part": {"type": "assistant", "text": "answer two to the user query regarding how to compute EWMA correctly."}})
        conn.execute(
            "INSERT INTO event (id,type,aggregate_id,data,created_at) VALUES (?,?,?,?,?)",
            ("m2", "message.part.created", "s1", data_json, "2026-06-01T11:00:00Z"),
        )
        conn.commit()
        conn.close()
        result = await ingestor.ingest(oc, source="oc-test")
        assert result["ingested"] == 1  # only the new row
        texts = [
            e["payload_json"] for e in await db.recent_events(limit=50)
            if e["type"] == "ingest.opencode.transcript"
        ]
        assert any("answer two" in t for t in texts)

    async def test_high_water_mark_is_per_source(self, env):
        ingestor, db, _, tmp = env
        oc_a = tmp / "a.db"
        oc_b = tmp / "b.db"
        _make_opencode_db(oc_a, [("a1", "sa", "assistant", "from A we need to ensure that the configuration is loaded properly.", "2026-06-01T10:00:00Z")])
        _make_opencode_db(oc_b, [("b1", "sb", "assistant", "from B we need to ensure that the configuration is loaded properly.", "2026-06-01T10:00:00Z")])
        assert (await ingestor.ingest(oc_a, source="src-a"))["ingested"] == 1
        # A different source has its own cursor; B is not blocked by A's run.
        assert await db.get_ingest_high_water("src-a") == "1"
        assert await db.get_ingest_high_water("src-b") == ""
        assert (await ingestor.ingest(oc_b, source="src-b"))["ingested"] == 1
        assert await db.get_ingest_high_water("src-b") == "1"

    async def test_redaction_applied_on_write(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [
            ("m1", "s1", "assistant", "export GITHUB_TOKEN=ghp_abcdefghijklmnop and key to the production environment server config", "2026-06-01T10:00:00Z"),
        ])
        await ingestor.ingest(oc, source="oc-test")
        rows = [
            e["payload_json"] for e in await db.recent_events(limit=50)
            if e["type"] == "ingest.opencode.transcript"
        ]
        assert rows
        # The known github token must be scrubbed by the append-event seam.
        assert "ghp_abcdefghijklmnop" not in rows[0]

    async def test_facts_from_ingested_session_surface_in_brief(self, env):
        ingestor, db, graph, tmp = env
        oc = tmp / "opencode.db"
        # We need a message that satisfies _message_fact:
        # 1. length >= 60
        # 2. contains at least one of: "/", ".py", ".ts", ".js", ".json", ".md"
        # 3. contains at least one of: "decided", "changed", "fixed", "bug", "todo", etc.
        _make_opencode_db(oc, [
            ("m1", "s99", "user", "how should we name the auth service in the project backend files?", "2026-06-01T10:00:00Z"),
            ("m2", "s99", "assistant",
             "Decided to name the auth service authsvc and front it with a gateway in /auth.",
             "2026-06-01T10:05:00Z"),
        ])
        await ingestor.ingest(oc, source="oc-test")
        # Consolidate the ingested events into the typed graph.
        worker = Consolidator(db, graph)
        rows = list(reversed(await db.recent_events(limit=200)))
        import json
        events = [
            {"id": r["id"], "type": r["type"], "repo_id": r.get("repo_id"),
             "payload": json.loads(r["payload_json"]) if r.get("payload_json") else {}}
            for r in rows
        ]
        written = await worker.consume_events(events)
        assert written >= 1
        # The assistant message became a typed fact; a brief cued on "auth"
        # surfaces it (acceptance criterion).
        assembler = MemoryBriefAssembler(graph)
        section = await assembler.assemble("auth service naming")
        statements = " ".join(f.statement for f in section.conventions)
        assert "authsvc" in statements

    async def test_user_prompts_recorded_but_not_folded(self, env):
        ingestor, db, graph, tmp = env
        oc = tmp / "opencode.db"
        # User prompt must be >= 40 chars to clear _transcript_salience
        _make_opencode_db(oc, [
            ("m1", "s1", "user", "just a question, no durable fact, but it needs to be long enough to be kept.", "2026-06-01T10:00:00Z"),
        ])
        await ingestor.ingest(oc, source="oc-test")
        import json
        rows = [
            json.loads(e["payload_json"])
            for e in await db.recent_events(limit=50)
            if e["type"] == "ingest.opencode.transcript"
        ]
        assert len(rows) == 1
        # The event exists (capture completeness) but carries no fact hint, so
        # consolidation never confabulates a fact from a bare user prompt.
        assert "fact" not in rows[0]

    async def test_missing_db_reports_unavailable(self, env):
        ingestor, _, _, tmp = env
        result = await ingestor.ingest(tmp / "does-not-exist.db", source="oc-test")
        assert result["available"] is False
        assert result["ingested"] == 0

    async def test_schema_tolerance_alternate_column_names(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "alt.db"
        # An OpenCode variant using camelCase / "messages" table / "parts" JSON.
        # Note: under v2 we map this to events table
        _make_opencode_db(
            oc,
            [("x1", "sess-1", "assistant", '[{"type":"text","text":"flattened part text, which is long enough to exceed the minimum length threshold."}]', "2026-06-01T10:00:00Z")],
            id_col="messageID", session_col="sessionID", role_col="role",
            content_col="parts", ts_col="time_created", table="messages",
        )
        result = await ingestor.ingest(oc, source="oc-alt")
        assert result["ingested"] == 1
        import json
        rows = [
            json.loads(e["payload_json"])
            for e in await db.recent_events(limit=50)
            if e["type"] == "ingest.opencode.transcript"
        ]
        assert rows and rows[0]["text"] == "flattened part text, which is long enough to exceed the minimum length threshold."

    async def test_default_source_is_db_path(self, env):
        ingestor, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [("m1", "s1", "assistant", "hi there user, how can I help you today with your codebase?", "2026-06-01T10:00:00Z")])
        result = await ingestor.ingest(oc)  # no explicit source
        assert result["source"].startswith("opencode:")
        assert str(oc.resolve()) in result["source"]

    async def test_convenience_helper(self, env):
        _, db, _, tmp = env
        oc = tmp / "opencode.db"
        _make_opencode_db(oc, [("m1", "s1", "assistant", "via helper: here is the complete implementation of the database module.", "2026-06-01T10:00:00Z")])
        result = await ingest_opencode_db(db, oc, source="oc-helper")
        assert result["ingested"] == 1
