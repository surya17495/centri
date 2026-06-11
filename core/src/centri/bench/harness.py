"""centri-bench harness — run a memory backend across the personas and score.

The harness is the reproducible methodology of ``docs/centri-bench.md``: seed
each persona's scripted history into a fresh ledger, fold it into the backend's
memory, open a cold-start session with the terse cue, assemble the brief through
the production cue-driven path, and score the three headline metrics plus the
taxonomy tasks.

It runs a panel of backends (CENTRI native + the Letta escape-hatch adapter).
Incumbents (Hermes, Claude Code, Cursor) are named in the spec but cannot ingest
the typed event ledger and are out of scope for an in-process harness; the spec
documents that handicap honestly and so do we (see ``report()``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from typing import Any, Dict, List

from centri.bench.backends import LettaBackend, NativeBackend
from centri.bench.personas import Persona, all_personas
from centri.bench.scoring import TaskScore, score
from centri.consolidation import Consolidator
from centri.db import Database
from centri.memory_graph import LOOP_OPEN, MemoryGraph
from centri.scheduler import Scheduler

# centri-bench.md task taxonomy (#, name, memory system).
TAXONOMY = [
    (1, "Re-proposal avoidance", "semantic+supersession"),
    (2, "Episodic recall w/ receipts", "episodic"),
    (3, "Stale-fact supersession", "semantic+supersession"),
    (4, "Open-loop surfacing", "prospective"),
    (5, "Procedural application", "procedural"),
    (6, "Silent-abandonment handling", "prospective"),
]


@dataclass
class BackendResult:
    backend: str
    scores: List[TaskScore] = field(default_factory=list)
    dormancy_ok: bool = False

    def headline(self) -> Dict[str, float]:
        n = len(self.scores) or 1
        return {
            "re_proposal_rate": round(sum(s.re_proposal_rate for s in self.scores) / n, 4),
            "brief_completeness": round(sum(s.brief_completeness for s in self.scores) / n, 4),
            "next_step_correct": round(sum(s.next_step_correct for s in self.scores) / n, 4),
            "stale_fact_correct": round(sum(s.stale_fact_correct for s in self.scores) / n, 4),
            "composite": round(sum(s.composite for s in self.scores) / n, 4),
        }


async def _run_backend(backend: Any, personas: List[Persona]) -> BackendResult:
    res = BackendResult(backend=backend.name)
    for persona in personas:
        await backend.ingest(persona)
        brief = await backend.brief(persona.cue, persona.repo_id)
        res.scores.append(score(persona, brief))
        await backend.close()
    res.dormancy_ok = await _dormancy_probe()
    return res


async def _dormancy_probe() -> bool:
    """Exercise task 6 (silent-abandonment): a stale loop surfaces exactly once.

    Backend-independent — it tests the native prospective machinery (scheduler +
    graph) that only the native backend has, so it is reported as a native
    capability, not a per-backend score.
    """
    from centri.memory_graph import OpenLoop

    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "dorm.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await graph.add_open_loop(OpenLoop(id="probe", intent="ship the thing", source_event_id="e1",
                                       last_touched_at=old, created_at=old))
    sched = Scheduler(db, jobs=None, memory=None, observability=None, memory_graph=graph, dormancy_days=7.0)
    first = await sched.detect_dormant_loops()
    second = await sched.detect_dormant_loops()
    await db.close()
    return first == ["probe"] and second == []


async def run() -> Dict[str, Any]:
    personas = all_personas()
    backends = [NativeBackend(), LettaBackend()]
    results = [await _run_backend(b, personas) for b in backends]
    return {
        "personas": [p.key for p in personas],
        "taxonomy": TAXONOMY,
        "results": results,
    }


def report(out: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("centri-bench results")
    lines.append("=" * 72)
    lines.append(f"Personas: {', '.join(out['personas'])}")
    lines.append("")
    lines.append("Task taxonomy (centri-bench.md):")
    for num, name, sysname in out["taxonomy"]:
        lines.append(f"  {num}. {name} [{sysname}]")
    lines.append("")
    for res in out["results"]:
        h = res.headline()
        lines.append(f"Backend: {res.backend}")
        lines.append(f"  re-proposal rate   (lower better): {h['re_proposal_rate']:.2f}")
        lines.append(f"  brief completeness (higher better): {h['brief_completeness']:.2f}")
        lines.append(f"  next-step correct  (higher better): {h['next_step_correct']:.2f}")
        lines.append(f"  stale-fact correct (higher better): {h['stale_fact_correct']:.2f}")
        lines.append(f"  HEADLINE COMPOSITE (higher better): {h['composite']:.2f}")
        lines.append(f"  dormancy (task 6, surface-once): {'PASS' if res.dormancy_ok else 'n/a'}")
        for s in res.scores:
            extra = ""
            if s.missing_brief_items:
                extra = f"  missing: {s.missing_brief_items}"
            lines.append(
                f"    - {s.persona:8s} complete={s.brief_completeness:.2f} "
                f"reproposal={s.re_proposal_rate:.2f} next={s.next_step_correct:.0f} "
                f"stale={s.stale_fact_correct:.2f}{extra}"
            )
        lines.append("")
    lines.append("Honest handicap note: incumbents (Hermes, Claude Code, Cursor) cannot")
    lines.append("ingest the typed event ledger, so they are out of scope for this")
    lines.append("in-process harness. Per centri-bench.md that handicap IS the point —")
    lines.append("event-level capture is the differentiator.")
    letta_http = any("letta_http" in r.backend for r in out["results"])
    if letta_http:
        lines.append("The Letta adapter ran in letta_http mode against a real Letta server")
        lines.append("(pgvector-backed archival passages); it has no typed supersession.")
    else:
        lines.append("The Letta adapter ran in local-projection mode (Letta server not")
        lines.append("configured); it models Letta's prose-archival storage, which has no")
        lines.append("typed supersession. Point CENTRI_LETTA_URL at a server for letta_http.")
    return "\n".join(lines)
