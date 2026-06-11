"""LettaMemoryStore HTTP-mode wiring tests — fully mocked, no network or SDK.

These pin the contract that, when a Letta server is configured, the store routes
its archival facts to the remote agent's *passages* (insert_passage /
search_passages / reset) instead of the local SQLite projection — and that the
local projection still works when no client is injected.

A fake client stands in for ``LettaHTTPClient`` so the tests exercise the
wiring without an SDK, a server, or the relay.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.letta_memory_store import LettaMemoryStore
from centri.memory_store import ArchivalFact


class _FakeLettaClient:
    """In-memory stand-in for LettaHTTPClient: records calls, stores passages."""

    def __init__(self):
        self.passages = []  # list of (text, tags)
        self.agents_created = 0
        self.resets = 0
        self.agent_id = None

    def ensure_agent(self):
        self.agents_created += 1
        self.agent_id = f"agent-{self.agents_created}"
        return self.agent_id

    def reset(self):
        self.resets += 1
        self.passages = []
        self.agent_id = None

    def insert_passage(self, text, tags=None):
        self.passages.append((text, list(tags or [])))

    def search_passages(self, query, limit=12):
        # Naive substring match to mimic retrieval, newest first; returns
        # (text, passage_id) tuples like the real client. No supersession.
        q = (query or "").lower()
        hits = [
            (text, f"passage-{i}")
            for i, (text, _tags) in enumerate(self.passages)
            if not q or q in text.lower()
        ]
        return list(reversed(hits))[:limit]


@pytest.fixture
async def http_store():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    client = _FakeLettaClient()
    yield LettaMemoryStore(db, letta_client=client), db, client
    await db.close()


@pytest.fixture
async def local_store():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield LettaMemoryStore(db), db
    await db.close()


class TestModeSelection:
    def test_injected_client_is_http_mode(self, http_store_sync):
        store, client = http_store_sync
        assert store.available() is True
        assert store.mode() == "letta_http"

    def test_no_client_is_local_projection(self, local_store_sync):
        store = local_store_sync
        assert store.available() is False
        assert store.mode() == "local_projection"


# Sync fixtures for the non-async mode tests above.
@pytest.fixture
def http_store_sync():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    client = _FakeLettaClient()
    return LettaMemoryStore(db, letta_client=client), client


@pytest.fixture
def local_store_sync():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    return LettaMemoryStore(db)


class TestHTTPRouting:
    async def test_insert_fact_routes_to_passages(self, http_store):
        store, _db, client = http_store
        await store.insert_fact(ArchivalFact(id="f1", text="authsvc owns login", tags=["fact"]))
        assert client.passages == [("authsvc owns login", ["fact"])]
        # Agent is created lazily on first write.
        assert client.agents_created == 1

    async def test_insert_does_not_write_sqlite(self, http_store):
        store, db, _client = http_store
        await store.insert_fact(ArchivalFact(id="f1", text="remote only"))
        # The local archival table should never be created/used in HTTP mode.
        cur = await db._execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='letta_archival'"
        )
        assert cur.fetchone() is None

    async def test_search_facts_reads_passages(self, http_store):
        store, _db, client = http_store
        await store.insert_fact(ArchivalFact(id="f1", text="authsvc owns login"))
        await store.insert_fact(ArchivalFact(id="f2", text="identity-gateway owns login"))
        hits = await store.search_facts("login", limit=12)
        texts = [h.text for h in hits]
        # No supersession: both the stale and current note come back.
        assert "authsvc owns login" in texts
        assert "identity-gateway owns login" in texts

    async def test_search_passage_ids_become_fact_ids(self, http_store):
        store, _db, _client = http_store
        await store.insert_fact(ArchivalFact(id="f1", text="hello world"))
        hits = await store.search_facts("hello")
        assert hits and all(h.id for h in hits)

    async def test_rebuild_resets_remote_agent(self, http_store):
        store, db, client = http_store
        await db.append_event(
            "evt-a", "decision.made", "bench", "2026-01-01T00:00:00Z",
            payload={"decision": {"topic": "auth", "statement": "use authsvc", "stance": "adopted"}},
        )
        written = await store.rebuild_from_events()
        assert written == 1
        # A reset drops the prior agent; a fresh agent is created for the rebuild.
        assert client.resets == 1
        assert client.agents_created >= 1
        assert len(client.passages) == 1

    async def test_rebuild_is_re_derivable(self, http_store):
        store, db, client = http_store
        await db.append_event(
            "evt-a", "fact.recorded", "bench", "2026-01-01T00:00:00Z",
            payload={"fact": {"topic": "db", "statement": "postgres in prod"}},
        )
        await store.rebuild_from_events()
        first = list(client.passages)
        # Rebuilding again from the same ledger yields the same passages.
        await store.rebuild_from_events()
        assert client.passages == first
        assert client.resets == 2


class TestLocalFallback:
    async def test_insert_and_search_local(self, local_store):
        store, _db = local_store
        await store.insert_fact(ArchivalFact(id="f1", text="user prefers terse replies"))
        hits = await store.search_facts("terse")
        assert len(hits) == 1 and hits[0].id == "f1"

    async def test_rebuild_local_projection(self, local_store):
        store, db = local_store
        await db.append_event(
            "evt-a", "fact.recorded", "bench", "2026-01-01T00:00:00Z",
            payload={"fact": {"topic": "db", "statement": "postgres in prod"}},
        )
        written = await store.rebuild_from_events()
        assert written == 1
        hits = await store.search_facts("postgres")
        assert any("postgres" in h.text for h in hits)
