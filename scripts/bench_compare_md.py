#!/usr/bin/env python3
"""Render a markdown head-to-head comparison from centri-bench JSON outputs.

Usage:
  bench_compare_md.py <deterministic.json> [judge.json] > comparison.md

Reads the ``--json`` payloads emitted by ``python -m centri.bench.run`` (one for
the deterministic rubric, optionally one for the LLM judge) and prints a single
markdown document: a headline table per grader plus the per-persona breakdown,
labelled with the exact backend identity (so ``letta-adapter[letta_http]`` vs
``[local_projection]`` is never silently conflated).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

METRICS = [
    ("brief_completeness", "brief completeness ↑"),
    ("re_proposal_rate", "re-proposal rate ↓"),
    ("next_step_correct", "next-step correct ↑"),
    ("stale_fact_correct", "stale-fact correct ↑"),
    ("composite", "composite ↑"),
]


def _load(path: str) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _headline_table(results: List[Dict[str, Any]]) -> str:
    backends = [r["backend"] for r in results]
    head = "| Metric | " + " | ".join(backends) + " |"
    sep = "|" + "---|" * (len(backends) + 1)
    rows = [head, sep]
    for key, label in METRICS:
        cells = [f"{r['headline'].get(key, float('nan')):.2f}" for r in results]
        bold = key == "composite"
        lab = f"**{label}**" if bold else label
        cells = [f"**{c}**" if bold else c for c in cells]
        rows.append(f"| {lab} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _per_persona_table(results: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for r in results:
        lines.append(f"\n#### {r['backend']}  (dormancy_ok={r.get('dormancy_ok')})\n")
        head = "| persona | completeness | re-proposal | next-step | stale-fact |"
        sep = "|---|---|---|---|---|"
        lines += [head, sep]
        for p in r.get("per_persona", []):
            lines.append(
                f"| {p['persona']} | {p['brief_completeness']:.2f} | "
                f"{p['re_proposal_rate']:.2f} | {p['next_step_correct']:.2f} | "
                f"{p['stale_fact_correct']:.2f} |"
            )
    return "\n".join(lines)


def _section(title: str, payload: Dict[str, Any]) -> str:
    results = payload["results"]
    out = [f"## {title}\n", _headline_table(results), "\n", _per_persona_table(results)]
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    det = _load(sys.argv[1])
    judge: Optional[Dict[str, Any]] = _load(sys.argv[2]) if len(sys.argv) > 2 else None

    personas = ", ".join(det.get("personas", []))
    print("# CENTRI native vs real Letta — head-to-head\n")
    print(f"Personas: {personas}\n")
    print(
        "Both arms grade against the same Nebius Token Factory models. Arm A is "
        "CENTRI's typed memory graph with write-time embeddings on; arm B is a "
        "real Letta server (archival passages, pgvector similarity, no typed "
        "supersession). The thesis: native wins specifically on stale-fact "
        "supersession.\n"
    )
    print(_section("Deterministic rubric", det))
    if judge is not None:
        print("\n")
        print(_section("LLM judge", judge))


if __name__ == "__main__":
    main()
