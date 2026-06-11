"""Phase 3b.4 — Claude Code ingestion adapter tests.

Proves the adapter tails ``~/.claude`` session JSONL into
``ingest.claude_code.message`` spine events with the same guarantees as the
OpenCode adapter: incremental + idempotent (re-run = no dupes), per-source
high-water mark, redaction on write, schema tolerance, assistant fold / user
no-fold, discovery counts, missing-store honesty.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import Consolidator
from centri.db import Database
from centri.ingest import ClaudeCodeIngestor
from centri.memory_brief import MemoryBriefAssembler
from centri.memory_graph import MemoryGraph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
async def env():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    ingestor = ClaudeCodeIngestor(db)
    yield ingestor, db, graph, Path(tmpdir)
    await db.close()


class TestClaudeCodeIngest:
    async def test_ingests_jsonl_as_events(self, env):
        ingestor, db, _, tmp = env
        proj = tmp / "projects" / "myrepo"
        _write_jsonl(proj / "sess1.jsonl", [
            {"uuid": "u1", "sessionId": "s1", "role": "user",
             "content": "add a funding tracker", "timestamp": "2026-06-01T10:00:00Z"},
            {"uuid": "u2", "sessionId": "s1", "role": "assistant",
             "content": "Added EWMA over the funding API.", "timestamp": "2026-06-01T10:01:00Z"},
        ])
        result = await ingestor.ingest(tmp / "projects", source="cc-test")
        assert result["ingested"] == 2
        assert result["available"] is True
        assert result["agent"] == "claude_code"
        events = await db.recent_events(limit=50)
        ingested = [e for e in events if e["type"] == "ingest.claude_code.message"]
        assert len(ingested) == 2
        assert all(e["importance"] == "low" for e in ingested)
        assert all(e["source"] == "ingest.claude_code" for e in ingested)

    async def test_rerun_is_idempotent(self, env):
        ingestor, db, _, tmp = env
        proj = tmp / "projects" / "r"
        _write_jsonl(proj / "s.jsonl", [
            {"uuid": "u1", "sessionId": "s1", "role": "assistant",
             "content": "first", "timestamp": "2026-06-01T10:00:00Z"},
        ])
        assert (await ingestor.ingest(tmp / "projects", source="cc-test"))["ingested"] == 1
        assert (await ingestor.ingest(tmp / "projects", source="cc-test"))["ingested"] == 0
        events = [e for e in await db.recent_events(limit=50)
                  if e["type"] == "ingest.claude_code.message"]
        assert len(events) == 1

    async def test_incremental_only_new_lines(self, env):
        ingestor, db, _, tmp = env
        proj = tmp / "projects" / "r"
        f = proj / "s.jsonl"
        _write_jsonl(f, [
            {"uuid": "u1", "sessionId": "s1", "role": "assistant",
             "content": "one", "timestamp": "2026-06-01T10:00:00Z"},
        ])
        assert (await ingestor.ingest(tmp / "projects", source="cc-test"))["ingested"] == 1
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"uuid": "u2", "sessionId": "s1", "role": "assistant",
                                 "content": "two", "timestamp": "2026-06-01T11:00:00Z"}) + "\n")
        result = await ingestor.ingest(tmp / "projects", source="cc-test")
        assert result["ingested"] == 1
        texts = [e["payload_json"] for e in await db.recent_events(limit=50)
                 if e["type"] == "ingest.claude_code.message"]
        assert any("two" in t for t in texts)

    async def test_redaction_applied(self, env):
        ingestor, db, _, tmp = env
        proj = tmp / "projects" / "r"
        _write_jsonl(proj / "s.jsonl", [
            {"uuid": "u1", "sessionId": "s1", "role": "assistant",
             "content": "token is ghp_abcdefghijklmnop here", "timestamp": "2026-06-01T10:00:00Z"},
        ])
        await ingestor.ingest(tmp / "projects", source="cc-test")
        rows = [e["payload_json"] for e in await db.recent_events(limit=50)
                if e["type"] == "ingest.claude_code.message"]
        assert rows and "ghp_abcdefghijklmnop" not in rows[0]

    async def test_schema_tolerance_nested_and_parts(self, env):
        ingestor, db, _, tmp = env
        proj = tmp / "projects" / "r"
        # type instead of role; content as a parts array; nested under message.
        _write_jsonl(proj / "s.jsonl", [
            {"id": "x1", "session_id": "sess-1",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "flattened part text"}]},
             "ts": "2026-06-01T10:00:00Z"},
        ])
        result = await ingestor.ingest(tmp / "projects", source="cc-alt")
        assert result["ingested"] == 1
        rows = [json.loads(e["payload_json"]) for e in await db.recent_events(limit=50)
                if e["type"] == "ingest.claude_code.message"]
        assert rows and rows[0]["text"] == "flattened part text"

    async def test_user_prompts_not_folded_assistant_folded(self, env):
        ingestor, db, graph, tmp = env
        proj = tmp / "projects" / "r"
        _write_jsonl(proj / "s.jsonl", [
            {"uuid": "u1", "sessionId": "s9", "role": "user",
             "content": "what should we name it?", "timestamp": "2026-06-01T10:00:00Z"},
            {"uuid": "u2", "sessionId": "s9", "role": "assistant",
             "content": "Decided to name the cache layer hotcache for clarity.",
             "timestamp": "2026-06-01T10:05:00Z"},
        ])
        await ingestor.ingest(tmp / "projects", source="cc-test")
        rows = [json.loads(e["payload_json"]) for e in await db.recent_events(limit=50)
                if e["type"] == "ingest.claude_code.message"]
        by_role = {r["role"]: r for r in rows}
        assert "fact" not in by_role["user"]
        assert "fact" in by_role["assistant"]
        # The assistant fact surfaces in a cued brief after consolidation.
        worker = Consolidator(db, graph)
        evrows = list(reversed(await db.recent_events(limit=200)))
        events = [{"id": r["id"], "type": r["type"], "repo_id": r.get("repo_id"),
                   "payload": json.loads(r["payload_json"]) if r.get("payload_json") else {}}
                  for r in evrows]
        assert await worker.consume_events(events) >= 1
        section = await MemoryBriefAssembler(graph).assemble("cache layer name")
        statements = " ".join(f.statement for f in section.conventions)
        assert "hotcache" in statements

    async def test_missing_store_unavailable(self, env):
        ingestor, _, _, tmp = env
        result = await ingestor.ingest(tmp / "nope", source="cc-test")
        assert result["available"] is False
        assert result["ingested"] == 0

    async def test_discover_counts_lines(self, env):
        ingestor, _, _, tmp = env
        proj = tmp / "projects" / "r"
        _write_jsonl(proj / "s.jsonl", [
            {"uuid": "u1", "role": "assistant", "content": "a", "timestamp": "t"},
            {"uuid": "u2", "role": "assistant", "content": "b", "timestamp": "t"},
        ])
        found = ingestor.discover(extra_paths=[str(tmp / "projects")])
        assert any(s.available and s.count == 2 for s in found)
