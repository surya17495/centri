"""Phase 3b.5 — generic config-driven fallback ingestion adapter.

For unknown agents whose store is a plain JSONL file or a SQLite chat table, the
generic adapter ingests via config (field/column names) while reusing the
registry's HWM / idempotency / redaction / fact-hint core. These tests prove both
shapes import once (idempotent), honor configured field names, fold assistant
turns, and degrade honestly when nothing is present.
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.ingest import GenericAdapterConfig, GenericIngestor
from centri.memory_graph import MemoryGraph


@pytest.fixture
async def db():
    tmpdir = tempfile.mkdtemp()
    database = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(database)
    await graph.ensure_tables()
    yield database, Path(tmpdir)
    await database.close()


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


class TestGenericJsonl:
    async def test_imports_with_custom_fields(self, db):
        database, tmp = db
        f = tmp / "acme.jsonl"
        _write_jsonl(f, [
            {"mid": "1", "convo": "s1", "speaker": "assistant", "utterance": "hello", "created": "2026-06-01T10:00:00Z"},
            {"mid": "2", "convo": "s1", "speaker": "user", "utterance": "hi", "created": "2026-06-01T10:01:00Z"},
        ])
        cfg = GenericAdapterConfig(
            agent="acme", kind="jsonl",
            id_field="mid", session_field="convo",
            role_field="speaker", content_field="utterance", ts_field="created",
        )
        adapter = GenericIngestor(database, cfg)
        result = await adapter.ingest(f, source="acme-test")
        assert result["ingested"] == 2 and result["available"] is True

        events = await database.recent_events(limit=20)
        acme = [e for e in events if e["type"] == "ingest.acme.message"]
        assert len(acme) == 2
        payloads = [json.loads(e["payload_json"]) for e in acme]
        # Assistant turn folds a fact hint; user turn does not.
        assistant = next(p for p in payloads if p["role"] == "assistant")
        user = next(p for p in payloads if p["role"] == "user")
        assert "fact" in assistant and "fact" not in user

    async def test_idempotent_rerun(self, db):
        database, tmp = db
        f = tmp / "acme.jsonl"
        _write_jsonl(f, [{"mid": "1", "speaker": "assistant", "utterance": "x", "created": "2026-06-01T10:00:00Z"}])
        cfg = GenericAdapterConfig(agent="acme", kind="jsonl",
                                   id_field="mid", role_field="speaker",
                                   content_field="utterance", ts_field="created")
        adapter = GenericIngestor(database, cfg)
        first = await adapter.ingest(f, source="acme-test")
        second = await adapter.ingest(f, source="acme-test")
        assert first["ingested"] == 1 and second["ingested"] == 0

    async def test_falls_back_to_candidate_field_names(self, db):
        database, tmp = db
        # No explicit fields configured: rely on the built-in candidate lists.
        f = tmp / "x.jsonl"
        _write_jsonl(f, [{"id": "1", "role": "assistant", "content": "y", "timestamp": "2026-06-01T10:00:00Z"}])
        cfg = GenericAdapterConfig(agent="acme2", kind="jsonl")
        adapter = GenericIngestor(database, cfg)
        result = await adapter.ingest(f, source="acme2-test")
        assert result["ingested"] == 1


class TestGenericSqlite:
    async def test_imports_with_custom_columns(self, db):
        database, tmp = db
        store = tmp / "chat.db"
        conn = sqlite3.connect(str(store))
        conn.execute("CREATE TABLE chat_messages (mid TEXT, sender TEXT, body TEXT, created_at TEXT)")
        conn.executemany(
            "INSERT INTO chat_messages (mid, sender, body, created_at) VALUES (?,?,?,?)",
            [("1", "assistant", "answer", "2026-06-01T10:00:00Z")],
        )
        conn.commit()
        conn.close()
        cfg = GenericAdapterConfig(
            agent="acme", kind="sqlite", table="chat_messages",
            id_field="mid", role_field="sender", content_field="body", ts_field="created_at",
        )
        adapter = GenericIngestor(database, cfg)
        result = await adapter.ingest(store, source="acme-sql")
        assert result["ingested"] == 1

    async def test_auto_resolves_table(self, db):
        database, tmp = db
        store = tmp / "chat2.db"
        conn = sqlite3.connect(str(store))
        conn.execute("CREATE TABLE whatever (id TEXT, role TEXT, content TEXT, ts TEXT)")
        conn.execute("INSERT INTO whatever VALUES ('1','assistant','hi','2026-06-01T10:00:00Z')")
        conn.commit()
        conn.close()
        cfg = GenericAdapterConfig(agent="acme3", kind="sqlite")  # no table => auto-resolve
        adapter = GenericIngestor(database, cfg)
        result = await adapter.ingest(store, source="acme3-sql")
        assert result["ingested"] == 1


class TestGenericHonesty:
    async def test_missing_store_unavailable(self, db):
        database, tmp = db
        cfg = GenericAdapterConfig(agent="acme", kind="jsonl")
        adapter = GenericIngestor(database, cfg)
        result = await adapter.ingest(tmp / "nope.jsonl", source="acme-missing")
        assert result["available"] is False and result["ingested"] == 0
