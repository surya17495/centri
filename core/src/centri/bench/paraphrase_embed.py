"""Paraphrase-cue embedding bench — measures the semantic leg, offline (Unit 2).

The work order asks us to *measure, don't assert*: extend the bench fixtures with
**paraphrase-style cues** (cues that share NO lexical tokens with the target fact)
and record before/after **quality-per-token** when the embedding-similarity ranker
feature is turned on.

This bench is fully offline and deterministic. It uses the clearly-labeled
:class:`centri.curation.HashingEmbeddingProvider` stub for the *fixture* — a real
network/local model is NOT needed to show the mechanism, and the stub is honest
about being a stub (its stamp says ``hashing-stub``). The numbers below therefore
demonstrate the ranker plumbing on a synthetic embedding space; with a real
embedding model the same code path applies, only the vectors change.

Run: ``python -m centri.bench.paraphrase_embed`` (add ``--json`` for machine output).

Each fixture is a (cue, graph) pair where the cue is a paraphrase of exactly one
"needed" fact with zero token overlap. We score quality-per-token with the
embedding weight OFF (baseline 3c.0) and ON (3c.1-embed) and report the deltas.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from centri.curation import (
    POLICY_VERSION,
    POLICY_VERSION_EMBED,
    Budget,
    HashingEmbeddingProvider,
    RankWeights,
    active_policy_version,
    compute_miss_waste,
    curate,
    default_token_counter,
    gather_candidates,
)
from centri.curation_replay import quality_per_token
from centri.db import Database
from centri.memory_graph import Fact, MemoryGraph


@dataclass
class Fixture:
    name: str
    cue_text: str          # a paraphrase: shares no lexical tokens with the target
    target_topic: str
    target_statement: str
    distractors: List[Tuple[str, str]]  # (topic, statement) facts that must NOT win


# Fixtures: each cue is a paraphrase of the target with deliberately disjoint
# surface tokens but overlapping *concept* vocabulary (so a semantic vector can
# bridge the gap while lexical overlap is ~0).
FIXTURES: List[Fixture] = [
    Fixture(
        name="auth-token-rotation",
        cue_text="rotating the tokens frequently",
        target_topic="auth strategy",
        target_statement="we rotate access token on a short interval",
        distractors=[
            ("deployment", "the build pipeline ships nightly to staging"),
            ("ui", "the sidebar collapses on small viewports"),
        ],
    ),
    Fixture(
        name="db-testing-convention",
        cue_text="mocking databases in our tests",
        target_topic="testing",
        target_statement="integration test hit a real database, never mocked",
        distractors=[
            ("logging", "structured logs are emitted as JSON lines"),
            ("auth strategy", "we rotate access token on a short interval"),
        ],
    ),
    Fixture(
        name="layout-module-home",
        cue_text="where backends are living",
        target_topic="layout",
        target_statement="backend services live under the core source tree",
        distractors=[
            ("ui", "the sidebar collapses on small viewports"),
            ("deployment", "the build pipeline ships nightly to staging"),
        ],
    ),
]


async def _build_graph(fx: Fixture, prov: HashingEmbeddingProvider) -> Tuple[MemoryGraph, Database]:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "bench.db")
    g = MemoryGraph(db)
    await g.ensure_tables()

    def emb(topic: str, statement: str):
        return prov.embed(f"{topic}: {statement}")

    # The needed fact.
    await g.add_fact(
        Fact(
            id=f"{fx.name}-target",
            topic=fx.target_topic,
            statement=fx.target_statement,
            source_event_id=f"evt-{fx.name}-target",
            created_at="2026-03-01T00:00:00+00:00",
            vector=emb(fx.target_topic, fx.target_statement),
        )
    )
    for i, (topic, statement) in enumerate(fx.distractors):
        await g.add_fact(
            Fact(
                id=f"{fx.name}-distract-{i}",
                topic=topic,
                statement=statement,
                source_event_id=f"evt-{fx.name}-distract-{i}",
                created_at="2026-03-02T00:00:00+00:00",
                vector=emb(topic, statement),
            )
        )
    return g, db


async def _score_one(fx: Fixture, prov: HashingEmbeddingProvider, weight: float) -> Dict:
    from centri.curation import CueBuilder

    g, db = await _build_graph(fx, prov)
    try:
        cue = await CueBuilder(g).build(fx.cue_text)
        cue.vector = prov.embed(fx.cue_text)
        w = RankWeights(embedding_similarity=weight)
        candidates = await gather_candidates(g, cue, None)
        # A deliberately tight budget: room for ONE fact line, so the single slot
        # goes to whichever candidate the ranker scores highest. This is what makes
        # the bench *measure* the semantic leg instead of admitting everything —
        # under a loose budget all three facts surface and the legs can't be told
        # apart. Distractors are newer than the target, so with embeddings OFF the
        # recency leg pulls a distractor into the slot (miss); with embeddings ON
        # the paraphrase's cosine pulls the target in (hit).
        budget = Budget(total=18, ambient=0, floor_decisions=0, floor_rejections=0)
        brief = await curate(
            g,
            cue,
            budget=budget,
            weights=w,
            policy_version=active_policy_version(w),
            embedding_provider=prov,
        )
        # Ground truth: the turn "needed" the target fact, so the turn transcript
        # is the target's own statement. compute_miss_waste then derives miss/waste
        # by real token-overlap against that transcript — the SAME signal the live
        # path records (a needed-but-unsurfaced item is a miss; a surfaced line
        # whose tokens never appear is waste). The cue is a paraphrase with zero
        # lexical overlap, so only the embedding leg can bridge to the target.
        turn_text = f"{fx.target_topic}: {fx.target_statement}"
        misses, wastes = compute_miss_waste(brief, candidates, turn_text)
        included = [ln.key for ln in brief.lines]
        counter = default_token_counter()
        tokens = sum(counter.count(ln.text) for ln in brief.lines) or 1
        q = quality_per_token(
            included_keys=included,
            miss_count=len(misses),
            waste_count=len(wastes),
            tokens=tokens,
        )
        surfaced = f"fact:{fx.name}-target" in included
        return {
            "fixture": fx.name,
            "policy_version": brief.policy_version,
            "surfaced": surfaced,
            "miss": len(misses),
            "waste": len(wastes),
            "tokens": tokens,
            "quality_per_token": q.quality_per_token,
            "recall": q.recall,
        }
    finally:
        await db.close()


async def run() -> Dict:
    prov = HashingEmbeddingProvider(dim=256)
    off: List[Dict] = []
    on: List[Dict] = []
    for fx in FIXTURES:
        off.append(await _score_one(fx, prov, weight=0.0))
        on.append(await _score_one(fx, prov, weight=2.0))

    def agg(rows: List[Dict]) -> Dict:
        n = len(rows) or 1
        return {
            "surfaced_rate": round(sum(1 for r in rows if r["surfaced"]) / n, 4),
            "mean_recall": round(sum(r["recall"] for r in rows) / n, 4),
            "mean_quality_per_token": round(sum(r["quality_per_token"] for r in rows) / n, 8),
            "total_misses": sum(r["miss"] for r in rows),
        }

    return {
        "provider_stamp": prov.stamp,
        "policy_off": POLICY_VERSION,
        "policy_on": POLICY_VERSION_EMBED,
        "off": agg(off),
        "on": agg(on),
        "per_fixture_off": off,
        "per_fixture_on": on,
    }


def report(out: Dict) -> str:
    lines = []
    lines.append("centri paraphrase-embedding bench (offline, hashing-stub provider)")
    lines.append("=" * 72)
    lines.append(f"provider: {out['provider_stamp']}  (FIXTURE STUB — not a real model)")
    lines.append(f"policy OFF: {out['policy_off']}   policy ON: {out['policy_on']}")
    lines.append("")
    o, n = out["off"], out["on"]
    lines.append(f"{'metric':<26}{'OFF (w=0.0)':>16}{'ON (w=2.0)':>16}")
    lines.append("-" * 58)
    lines.append(f"{'paraphrase surfaced rate':<26}{o['surfaced_rate']:>16}{n['surfaced_rate']:>16}")
    lines.append(f"{'mean recall':<26}{o['mean_recall']:>16}{n['mean_recall']:>16}")
    lines.append(
        f"{'mean quality-per-token':<26}{o['mean_quality_per_token']:>16}{n['mean_quality_per_token']:>16}"
    )
    lines.append(f"{'total misses':<26}{o['total_misses']:>16}{n['total_misses']:>16}")
    return "\n".join(lines)


def main() -> None:
    out = asyncio.run(run())
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        print(report(out))


if __name__ == "__main__":
    main()
