#!/usr/bin/env python3
"""Live verification of the LLM consolidation tier against a real model.

This is NOT a test — it makes a real network call and is excluded from the
offline suite. It exists so a human (or CI with credentials) can confirm the
proposal-contract tier behaves against a live OpenAI-compatible endpoint.

It seeds a throwaway spine with realistic UNHINTED ML-training stdout events
(epoch lines, a checkpoint path, then a later checkpoint that should supersede
the first), runs exactly ONE real consolidation batch, and prints:

  - the ops the model proposed,
  - which were applied / rejected and why (provenance receipts),
  - the resulting live typed nodes (facts / decisions / open loops),
  - the token usage reported by the API.

Configuration (honest-unavailable — exits nonzero if unset):

  CENTRI_CONSOLIDATION_BASE_URL   e.g. https://api.tokenfactory.nebius.com/v1/
  CENTRI_CONSOLIDATION_API_KEY    injected at runtime by the orchestrator/proxy
  CENTRI_CONSOLIDATION_MODEL      e.g. Qwen/Qwen3-30B-A3B-Instruct-2507

Usage:
    python scripts/live_consolidation_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Make the core package importable when run from the repo root.
_CORE_SRC = Path(__file__).resolve().parent.parent / "core" / "src"
sys.path.insert(0, str(_CORE_SRC))

from centri.consolidation import ConsolidationLLMTier  # noqa: E402
from centri.consolidation_llm import resolve_consolidation_client  # noqa: E402
from centri.db import Database  # noqa: E402
from centri.memory_graph import LOOP_OPEN, MemoryGraph  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Settings:
    """Minimal settings shim sourced from the CENTRI_CONSOLIDATION_* env vars."""

    consolidation_base_url = os.getenv("CENTRI_CONSOLIDATION_BASE_URL", "")
    consolidation_api_key = os.getenv("CENTRI_CONSOLIDATION_API_KEY", "")
    consolidation_model = os.getenv("CENTRI_CONSOLIDATION_MODEL", "")


# Realistic UNHINTED ML-training stdout — no synthesis hints, so only the LLM
# tier touches it. The second checkpoint should drive a supersede of the first.
def _seed_events() -> list[dict]:
    return [
        {"id": "ev-1", "type": "hand.stdout", "source": "hand", "payload": {
            "text": "Epoch 10/100 done — val_loss 0.124, checkpoint saved to "
                    "/mnt/data/run42/epoch-10.pt"}},
        {"id": "ev-2", "type": "hand.stdout", "source": "hand", "payload": {
            "text": "Resuming training from epoch 11; ETA ~3h, LR 1e-4."}},
        {"id": "ev-3", "type": "hand.stdout", "source": "hand", "payload": {
            "text": "Epoch 25/100 done — val_loss 0.087 (new best), checkpoint saved to "
                    "/mnt/data/run42/epoch-25.pt; superseding epoch-10."}},
        {"id": "ev-4", "type": "hand.stdout", "source": "hand", "payload": {
            "text": "Data loaded from the Binance funding-rate API; 1.2M rows."}},
    ]


async def _run() -> int:
    client = resolve_consolidation_client(_Settings())
    if client is None:
        print(
            "UNAVAILABLE: set CENTRI_CONSOLIDATION_BASE_URL and "
            "CENTRI_CONSOLIDATION_MODEL (and API key) to run the live check.",
            file=sys.stderr,
        )
        return 2

    print(f"Model:     {client.model}")
    print(f"Base URL:  {_Settings.consolidation_base_url}")
    print(f"API key:   {'set' if _Settings.consolidation_api_key else 'unset'}")
    print()

    tmpdir = tempfile.mkdtemp(prefix="centri-live-consolidation-")
    db = Database(Path(tmpdir) / "state.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()

    events = _seed_events()
    # Persist to the spine so receipts have a real ledger to write into.
    for ev in events:
        await db.append_event(
            event_id=ev["id"],
            type=ev["type"],
            source=ev.get("source", "hand"),
            ts=_now(),
            payload=ev["payload"],
        )

    tier = ConsolidationLLMTier(db, graph, client=client, batch_threshold=1)

    print(f"Seeding {len(events)} unhinted events; running ONE live batch...\n")
    try:
        res = await tier.consume_unhinted(events, force=True)
    except Exception as exc:  # noqa: BLE001 — surface any transport failure
        print(f"FAILED: live consolidation raised: {exc}", file=sys.stderr)
        await db.close()
        return 1

    if not res.get("ran"):
        print("FAILED: tier did not run (unavailable or empty batch).", file=sys.stderr)
        await db.close()
        return 1

    # ---- Proposals + receipts --------------------------------------------
    print(f"Proposed:  {res.get('proposed', 0)}")
    print(f"Applied:   {res.get('applied', 0)}")
    print(f"Rejected:  {res.get('rejected', 0)}")
    if res.get("reasons"):
        print("Rejection reasons:")
        for reason in res["reasons"]:
            print(f"  - {reason}")
    print()

    applied_receipts = [
        e for e in await db.recent_events(limit=200)
        if e["type"] == "consolidation.proposal.applied"
    ]
    if applied_receipts:
        print("Applied ops (from provenance receipts):")
        for r in reversed(applied_receipts):
            payload = json.loads(r["payload_json"])
            op = payload.get("op", {})
            print(f"  - {op.get('op')}: {json.dumps({k: v for k, v in op.items() if k != 'op'})}"
                  f"  [source={payload.get('source_event_ids')}]")
        print()

    # ---- Resulting live typed nodes --------------------------------------
    facts = await graph.current_facts()
    decisions = await graph.current_decisions()
    loops = await graph.open_loops(states=[LOOP_OPEN])

    print(f"Live facts ({len(facts)}):")
    for f in facts:
        print(f"  - [{f.topic}] {f.statement}  (src={f.source_event_id})")
    print(f"Live decisions ({len(decisions)}):")
    for d in decisions:
        print(f"  - [{d.topic}] {d.statement}")
    print(f"Open loops ({len(loops)}):")
    for loop in loops:
        print(f"  - {loop.intent}")
    print()

    # ---- Token usage ------------------------------------------------------
    usage = res.get("usage") or {}
    print("Token usage:")
    print(f"  prompt_tokens     = {usage.get('prompt_tokens', 0)}")
    print(f"  completion_tokens = {usage.get('completion_tokens', 0)}")
    print(f"  total_tokens      = {usage.get('total_tokens', 0)}")

    await db.close()

    # Success criterion: the model proposed at least one op and the gatekeeper
    # applied at least one typed node. A run that proposes nothing usable is a
    # red flag worth a nonzero exit.
    if res.get("applied", 0) < 1:
        print("\nFAILED: no proposals were applied.", file=sys.stderr)
        return 1
    print("\nOK: live consolidation produced applied typed memory.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
