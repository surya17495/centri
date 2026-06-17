"""Increment 3 — LLM consolidation tier (proposal contract) tests.

All offline: a scripted fake LLM client returns canned op-array responses, so the
deterministic gatekeeper (validate → apply/reject → provenance receipt) is what is
actually under test. No network. Covers the spec's required scenarios: ML-training
fixture (add_fact + open_loop), supersede flow, every rejection path, the skip
path, the hint-events-never-reach-the-tier guarantee, and provenance receipts.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.consolidation import (
    ConsolidationLLMTier,
    event_has_hints,
)
from centri.consolidation_llm import ChatResult, resolve_consolidation_client
from centri.consolidation_prompt import (
    build_messages,
    op_schema_summary,
    parse_ops,
)
from centri.db import Database
from centri.memory_graph import LOOP_OPEN, MemoryGraph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeLLM:
    """Scripted OpenAI-compatible client. Returns queued responses in order."""

    def __init__(self, responses, model="fake/consolidator", usage=None):
        self._responses = list(responses)
        self.model = model
        self._usage = usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        content = self._responses.pop(0) if self._responses else "[{\"op\":\"finish\"}]"
        return ChatResult(content=content, usage=dict(self._usage), model=self.model)


def _ops_json(*ops):
    return json.dumps(list(ops) + [{"op": "finish"}])


@pytest.fixture
async def setup():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    yield db, graph
    await db.close()


def _tier(db, graph, llm, **kw):
    return ConsolidationLLMTier(db, graph, client=llm, batch_threshold=kw.pop("batch_threshold", 1), **kw)


# Realistic unhinted ML-training stdout events.
def _epoch_events():
    return [
        {"id": "ev-1", "type": "hand.stdout", "payload": {
            "text": "Epoch 10/100 completed, val_loss: 0.12 — checkpoint saved to /mnt/data/epoch-10.pt"}},
        {"id": "ev-2", "type": "hand.stdout", "payload": {
            "text": "Resuming training from epoch 11; ETA 3h."}},
    ]


class TestProposalContract:
    async def test_ml_training_add_fact_and_open_loop_applied(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "training checkpoint",
             "statement": "Best checkpoint at epoch 10 saved to /mnt/data/epoch-10.pt (val_loss 0.12)",
             "tags": ["ml", "checkpoint"]},
            {"op": "open_loop", "intent": "continue model training past epoch 10"},
        )])
        tier = _tier(db, graph, llm)
        res = await tier.consume_unhinted(_epoch_events())
        assert res["applied"] == 2 and res["rejected"] == 0
        facts = await graph.current_facts()
        assert any("epoch-10.pt" in f.statement for f in facts)
        assert facts[0].source_event_id == "ev-1"  # provenance to the first source event
        loops = await graph.open_loops(states=[LOOP_OPEN])
        assert any("continue model training" in loop.intent for loop in loops)

    async def test_supersede_flow_old_node_superseded(self, setup):
        db, graph = setup
        # First batch establishes the epoch-10 fact.
        llm1 = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "training checkpoint",
             "statement": "Best checkpoint at epoch 10: /mnt/data/epoch-10.pt"},
        )])
        tier1 = _tier(db, graph, llm1)
        await tier1.consume_unhinted([{"id": "ev-1", "type": "hand.stdout", "payload": {"text": "epoch 10 saved"}}])
        fact = (await graph.current_facts())[0]

        # Second batch: epoch-11 supersedes the epoch-10 fact by node id.
        llm2 = FakeLLM([_ops_json(
            {"op": "supersede", "node_id": fact.id, "kind": "fact",
             "new_statement": "Best checkpoint at epoch 11: /mnt/data/epoch-11.pt"},
        )])
        tier2 = _tier(db, graph, llm2)
        res = await tier2.consume_unhinted([{"id": "ev-2", "type": "hand.stdout", "payload": {"text": "epoch 11 saved"}}])
        assert res["applied"] == 1
        live = await graph.current_facts()
        assert len(live) == 1 and "epoch-11.pt" in live[0].statement
        # Old node retained with a superseded_by pointer.
        history = await graph.fact_history("training checkpoint")
        old = next(f for f in history if f.id == fact.id)
        assert old.superseded_by is not None


class TestRejectionPaths:
    async def test_malformed_json_rejected_graph_untouched(self, setup):
        db, graph = setup
        llm = FakeLLM(["this is not json at all"])
        tier = _tier(db, graph, llm)
        res = await tier.consume_unhinted([{"id": "e1", "type": "hand.stdout", "payload": {"text": "noise"}}])
        assert res["applied"] == 0 and res["rejected"] == 1
        assert await graph.current_facts() == []
        events = await db.recent_events(limit=50)
        assert any(e["type"] == "consolidation.proposal.rejected" for e in events)

    async def test_unknown_supersede_target_rejected(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json(
            {"op": "supersede", "node_id": "does-not-exist", "kind": "fact", "new_statement": "x on 2026-01-01"},
        )])
        tier = _tier(db, graph, llm)
        res = await tier.consume_unhinted([{"id": "e1", "type": "hand.stdout", "payload": {"text": "noise"}}])
        assert res["applied"] == 0 and res["rejected"] == 1
        assert "not live" in " ".join(res["reasons"])
        assert await graph.current_facts() == []

    async def test_duplicate_fact_rejected(self, setup):
        db, graph = setup
        # Seed a fact, then propose a near-identical one.
        llm1 = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "data source", "statement": "Funding rates pulled from the Binance API"},
        )])
        await _tier(db, graph, llm1).consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        llm2 = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "data source", "statement": "Funding rates pulled from the Binance API"},
        )])
        res = await _tier(db, graph, llm2).consume_unhinted([{"id": "e2", "type": "x", "payload": {"text": "b"}}])
        assert res["applied"] == 0 and res["rejected"] == 1
        assert "duplicate" in " ".join(res["reasons"])
        assert len(await graph.current_facts()) == 1

    async def test_relative_date_statement_rejected(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "milestone", "statement": "We shipped the parser today"},
        )])
        res = await _tier(db, graph, llm).consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        assert res["applied"] == 0 and res["rejected"] == 1
        assert "relative-time" in " ".join(res["reasons"])
        assert await graph.current_facts() == []

    async def test_unknown_op_and_missing_field_rejected(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json(
            {"op": "delete_everything"},
            {"op": "add_fact", "topic": "t"},  # missing statement
        )])
        res = await _tier(db, graph, llm).consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        assert res["applied"] == 0 and res["rejected"] == 2
        joined = " ".join(res["reasons"])
        assert "unknown op" in joined and "missing required field: statement" in joined


class TestSkipAndHints:
    async def test_skip_path_finish_only(self, setup):
        db, graph = setup
        llm = FakeLLM(["[{\"op\":\"finish\"}]"])
        res = await _tier(db, graph, llm).consume_unhinted(
            [{"id": "e1", "type": "chat", "payload": {"text": "hey how's it going"}}]
        )
        assert res["ran"] is True and res["applied"] == 0 and res["rejected"] == 0
        assert await graph.current_facts() == []
        assert await graph.current_decisions() == []

    async def test_hinted_events_never_reach_the_tier(self, setup):
        db, graph = setup
        # If the tier somehow saw this hinted event it would call the LLM; assert
        # it filters them out so the LLM is never invoked and nothing is written.
        llm = FakeLLM([_ops_json({"op": "add_fact", "topic": "x", "statement": "y on 2026-01-01"})])
        tier = _tier(db, graph, llm)
        hinted = {"id": "e1", "type": "task.completed", "payload": {
            "fact": {"topic": "auth", "statement": "named authsvc"}}}
        res = await tier.consume_unhinted([hinted])
        assert res["ran"] is False  # no candidates -> no call
        assert llm.calls == []
        assert await graph.current_facts() == []

    async def test_event_has_hints_helper(self):
        assert event_has_hints({"fact": {"topic": "a", "statement": "b"}}) is True
        assert event_has_hints({"decision": [{"topic": "a", "statement": "b"}]}) is True
        assert event_has_hints({"text": "raw stdout"}) is False
        assert event_has_hints({"fact": {}}) is False  # empty hint is not a hint


class TestProvenanceReceipts:
    async def test_applied_op_emits_receipt_with_source_and_model(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json({"op": "add_fact", "topic": "ckpt", "statement": "saved to /m/e.pt"})],
                      model="Qwen/Qwen3-30B")
        await _tier(db, graph, llm).consume_unhinted(
            [{"id": "ev-7", "type": "hand.stdout", "payload": {"text": "saved"}}]
        )
        events = await db.recent_events(limit=50)
        applied = [e for e in events if e["type"] == "consolidation.proposal.applied"]
        assert len(applied) == 1
        payload = json.loads(applied[0]["payload_json"])
        assert payload["model"] == "Qwen/Qwen3-30B"
        assert "ev-7" in payload["source_event_ids"]
        assert payload["op"]["op"] == "add_fact"

    async def test_batch_receipt_carries_token_usage(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json({"op": "add_fact", "topic": "t", "statement": "s on 2026-02-02"})],
                      usage={"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366})
        res = await _tier(db, graph, llm).consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        assert res["usage"]["total_tokens"] == 366
        events = await db.recent_events(limit=50)
        batch = [e for e in events if e["type"] == "consolidation.batch"]
        assert len(batch) == 1
        assert json.loads(batch[0]["payload_json"])["usage"]["total_tokens"] == 366


class TestHonestUnavailable:
    async def test_no_client_does_nothing(self, setup):
        db, graph = setup
        tier = ConsolidationLLMTier(db, graph, client=None, batch_threshold=1)
        assert tier.available is False
        res = await tier.consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        assert res["available"] is False and res["ran"] is False
        assert await graph.current_facts() == []

    def test_resolve_returns_none_when_unconfigured(self):
        class S:
            consolidation_base_url = ""
            consolidation_model = ""
            consolidation_api_key = ""
        assert resolve_consolidation_client(S()) is None

    def test_resolve_builds_client_when_configured(self):
        class S:
            consolidation_base_url = "https://api.tokenfactory.nebius.com/v1/"
            consolidation_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
            consolidation_api_key = "k"
        client = resolve_consolidation_client(S())
        assert client is not None and client.model.startswith("Qwen/")


class TestBatchThresholdAndDeterminism:
    async def test_below_threshold_waits_unless_forced(self, setup):
        db, graph = setup
        llm = FakeLLM([_ops_json({"op": "add_fact", "topic": "t", "statement": "s on 2026-03-03"})])
        tier = ConsolidationLLMTier(db, graph, client=llm, batch_threshold=8)
        res = await tier.consume_unhinted([{"id": "e1", "type": "x", "payload": {"text": "a"}}])
        assert res["ran"] is False  # one event < threshold 8
        assert llm.calls == []

    def test_parse_ops_extracts_fenced_array(self):
        ops, err = parse_ops("Here you go:\n```json\n[{\"op\":\"finish\"}]\n```\nDone.")
        assert err is None and ops == [{"op": "finish"}]

    def test_parse_ops_rejects_object(self):
        ops, err = parse_ops('{"op":"finish"}')
        assert err is not None

    def test_op_schema_summary_lists_all_ops(self):
        ops = {row["op"] for row in op_schema_summary()}
        assert {"add_fact", "add_decision", "open_loop", "close_loop", "supersede", "finish"} <= ops

    def test_build_messages_includes_digest_and_batch(self):
        from centri.consolidation_prompt import LiveDigest
        digest = LiveDigest(facts=[{"id": "f1", "topic": "x", "statement": "y"}])
        msgs = build_messages([{"id": "e1", "type": "hand.stdout", "payload": {"text": "stdout line"}}], digest)
        assert msgs[0]["role"] == "system"
        assert "f1" in msgs[1]["content"] and "stdout line" in msgs[1]["content"]
        assert "ABSOLUTE DATES ONLY" in msgs[0]["content"]


class MockEmbeddingProvider:
    def __init__(self, vectors=None):
        self.vectors = vectors or {}
        self.available = True
        self.stamp = "embedding:mock"

    def embed(self, text):
        return self.vectors.get(text, None)


class TestTargetedImprovements:
    async def test_pre_filter_with_embeddings(self, setup):
        db, graph = setup
        from centri.memory_graph import Fact

        provider = MockEmbeddingProvider({
            "User auth uses JWT tokens": [1.0, 0.0],
            "auth: User auth uses JWT tokens": [1.0, 0.0],
        })

        fact = Fact(
            id="f1",
            topic="auth",
            statement="User auth uses JWT tokens",
            source_event_id=None,
            vector=[1.0, 0.0]
        )
        await graph.supersede_fact(fact)

        llm = FakeLLM([])
        tier = ConsolidationLLMTier(db, graph, client=llm, batch_threshold=1, embedding_provider=provider)

        ev = {"id": "ev-1", "type": "hand.stdout", "payload": {"text": "User auth uses JWT tokens"}}
        res = await tier.consume_unhinted([ev])

        assert res["ran"] is False
        assert res["batch_size"] == 0
        assert len(llm.calls) == 0

    async def test_semantic_dedup_in_gatekeeper(self, setup):
        db, graph = setup
        from centri.memory_graph import Fact

        provider = MockEmbeddingProvider({
            "auth: User auth uses JWT tokens": [1.0, 0.0],
            "auth: User authorization uses JWT tokens": [0.95, 0.3122],
        })

        fact = Fact(
            id="f1",
            topic="auth",
            statement="User auth uses JWT tokens",
            source_event_id=None,
            vector=[1.0, 0.0]
        )
        await graph.supersede_fact(fact)

        llm = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "auth", "statement": "User authorization uses JWT tokens"}
        )])
        tier = ConsolidationLLMTier(db, graph, client=llm, batch_threshold=1, embedding_provider=provider)

        res = await tier.consume_unhinted([{"id": "ev-1", "type": "x", "payload": {"text": "dummy"}}])

        assert res["applied"] == 0
        assert res["rejected"] == 1
        assert "semantic duplicate of fact f1" in res["reasons"]

    async def test_semantic_dedup_text_fallback(self, setup):
        db, graph = setup
        from centri.curation import NullEmbeddingProvider
        from centri.memory_graph import Fact

        provider = NullEmbeddingProvider()

        fact = Fact(
            id="f1",
            topic="auth",
            statement="User authentication uses jwt tokens.",
            source_event_id=None
        )
        await graph.supersede_fact(fact)

        llm = FakeLLM([_ops_json(
            {"op": "add_fact", "topic": "auth", "statement": "user authentication   uses JWT tokens!!!"}
        )])
        tier = ConsolidationLLMTier(db, graph, client=llm, batch_threshold=1, embedding_provider=provider)

        res = await tier.consume_unhinted([{"id": "ev-1", "type": "x", "payload": {"text": "dummy"}}])

        assert res["applied"] == 0
        assert res["rejected"] == 1
        assert "semantic duplicate of fact f1" in res["reasons"]

    async def test_digest_budget_expansion(self, setup):
        db, graph = setup
        from centri.memory_graph import Fact, Decision, STANCE_ADOPTED

        for i in range(150):
            fact = Fact(id=f"f-{i}", topic="t", statement=f"statement {i}", source_event_id=None)
            await graph.add_fact(fact)
        for i in range(120):
            dec = Decision(id=f"d-{i}", topic="t", statement=f"statement {i}", stance=STANCE_ADOPTED, source_event_id=None)
            await graph.add_decision(dec)

        tier = ConsolidationLLMTier(db, graph, client=FakeLLM([]))
        digest = await tier._build_digest([])

        assert len(digest.facts) == 150
        assert len(digest.decisions) == 100

