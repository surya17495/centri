"""`centri memory rebuild [--embed]` — re-derive the typed graph from the spine.

Offline: the embed path uses the configured provider, which is the
honest-unavailable NullEmbeddingProvider with no model set, so the rebuild
succeeds and writes no vectors. The deterministic HashingEmbeddingProvider
covers the vector-writing path in test_embeddings.py; here we pin the CLI
contract (dispatch, event emission, re-derivability, honest degradation).
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import centri.config as config
from centri.cli import _memory_rebuild, run
from centri.db import Database


@pytest.fixture
def temp_db(monkeypatch):
    """Point CENTRI_DB_PATH at a temp file and reset the settings cache."""
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "state.db"
    monkeypatch.setenv("CENTRI_DB_PATH", str(db_path))
    # Ensure embeddings stay off (offline default) regardless of ambient env.
    monkeypatch.setenv("CENTRI_EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("CENTRI_EMBEDDING_LOCAL_MODEL", "")
    monkeypatch.setenv("CENTRI_EMBEDDING_MODEL", "")
    config._settings = None
    yield db_path
    config._settings = None


async def _seed_spine(db: Database) -> None:
    await db.append_event(
        event_id="evt-d1",
        type="decision.made",
        source="test",
        ts="2026-01-01T00:00:00+00:00",
        payload={
            "decision": {
                "id": "d1",
                "topic": "jwt refresh",
                "statement": "adopt rotating refresh tokens",
                "stance": "adopted",
            }
        },
    )
    await db.append_event(
        event_id="evt-f1",
        type="fact.observed",
        source="test",
        ts="2026-01-02T00:00:00+00:00",
        payload={
            "fact": {
                "id": "f1",
                "topic": "testing",
                "statement": "integration tests hit a real database",
            }
        },
    )


class TestMemoryRebuild:
    async def test_rebuild_rederives_graph_from_spine(self, temp_db):
        db = Database(temp_db)
        await _seed_spine(db)
        await db.close()

        rc = await _memory_rebuild(embed=False)
        assert rc == 0

        db2 = Database(temp_db)
        from centri.memory_graph import MemoryGraph

        graph = MemoryGraph(db2)
        decisions = await graph.current_decisions()
        facts = await graph.current_facts()
        assert any(d.id == "d1" for d in decisions)
        assert any(f.id == "f1" for f in facts)
        await db2.close()

    async def test_rebuild_emits_receipt_event(self, temp_db):
        db = Database(temp_db)
        await _seed_spine(db)
        await db.close()

        await _memory_rebuild(embed=True)

        db2 = Database(temp_db)
        rows = await db2.recent_events(limit=200)
        rebuilds = [r for r in rows if r.get("type") == "memory.rebuild"]
        assert rebuilds, "memory.rebuild receipt event must be emitted"
        import json

        payload = json.loads(rebuilds[0]["payload_json"])
        assert payload["embed"] is True
        # No model configured in the offline test → honest-unavailable.
        assert payload["embedding_available"] is False
        assert payload["embedding_stamp"] == "embedding:unavailable"
        assert payload["counts"]["decisions"] >= 1
        assert payload["counts"]["facts"] >= 1
        await db2.close()

    async def test_rebuild_honest_unavailable_writes_no_vectors(self, temp_db):
        db = Database(temp_db)
        await _seed_spine(db)
        await db.close()

        await _memory_rebuild(embed=True)

        db2 = Database(temp_db)
        from centri.memory_graph import MemoryGraph

        graph = MemoryGraph(db2)
        facts = await graph.current_facts()
        decisions = await graph.current_decisions()
        assert facts and all(f.vector is None for f in facts)
        assert decisions and all(d.vector is None for d in decisions)
        await db2.close()


class TestCliDispatch:
    def test_no_args_would_serve(self, monkeypatch):
        called = {}
        monkeypatch.setattr("centri.cli.serve", lambda: called.setdefault("served", True))
        run([])
        assert called.get("served") is True

    def test_memory_no_subcommand_prints_help(self, capsys):
        # Should not raise; prints help and returns.
        run(["memory"])
        out = capsys.readouterr().out
        assert "rebuild" in out
