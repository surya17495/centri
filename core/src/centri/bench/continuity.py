"""centri-bench 3e continuity suites — the Hermes-failure regression gate.

ROADMAP Phase 3e: the owner's real-world Hermes sessions exposed four *continuity*
failure modes that the headline benchmark (``personas.py``) does not isolate.
This module turns each into a falsifiable suite, scored by the same deterministic
rubric ideas as ``scoring.py`` and runnable as a regression gate via
``python -m centri.bench.run --suite continuity``.

The four suites (motivated by ROADMAP 3e):

  1. ``cross_session_awareness`` — an open loop from a prior session is surfaced
     *unprompted* at a cold cue that does not lexically mention it. Hermes lost
     in-flight work across sessions; CENTRI must push prospective memory.
  2. ``supersession_under_churn`` — a fact superseded multiple times under config
     churn must resolve to the LATEST value only; every stale value must be
     absent. Hermes re-surfaced stale config after a rename chain.
  3. ``cold_start_recall`` — on a genuinely fresh client (new Database + graph,
     memory rebuilt purely ``rebuild_from_events``), the brief must still carry
     the project's decisions/conventions. Proves re-derivability, not warm cache.
  4. ``delegated_session_awareness`` — work done in a *delegated* coding session
     (a hand's ``task.completed`` carrying typed hints) must surface in the next
     brief. Hermes forgot what a sub-session had already accomplished.

ANTI-GAMING: per ``docs/centri-bench.md`` the ground truth is authored BEFORE the
implementation is tuned, and the harness reports HONEST scores. A failing
continuity suite is a *finding* (a real 3e gap), never something to hide by
softening the assertion or tuning the assembler to the test.

STUB STATUS: the personas below are synthetic scaffolding shaped like the real
thing. They are explicitly marked ``# TODO(owner)`` and must be replaced with
material drawn from the owner's actual Hermes transcripts before the 3e numbers
are quoted as evidence. The *methodology* (suite shapes, scoring, gate wiring) is
real and under test; the *content* is a placeholder.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.bench.personas import Persona
from centri.consolidation import Consolidator
from centri.db import Database
from centri.memory_brief import MemoryBriefAssembler
from centri.memory_graph import MemoryGraph


# ----------------------------------------------------------------------
# Continuity persona: a Persona plus the extra ground truth the continuity
# suites score against (prior-session loop, stale-value chain, delegated work).
# ----------------------------------------------------------------------
@dataclass
class ContinuityPersona:
    persona: Persona
    # Suite 1: a loop opened in a PRIOR session that must surface unprompted.
    prior_session_loop: str = ""
    # Suite 2: every stale value in a supersession chain that must be ABSENT,
    # plus the single current value that must be PRESENT.
    stale_values: List[str] = field(default_factory=list)
    current_value: str = ""
    # Suite 4: substrings of delegated-session work that must surface next brief.
    delegated_brief_items: List[str] = field(default_factory=list)


def _ts(day: int) -> str:
    base = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(days=day, minutes=day * 11)).isoformat()


def _ev(eid: str, etype: str, day: int, repo: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": eid, "type": etype, "ts": _ts(day), "source": "bench", "repo_id": repo, "payload": payload}


# ----------------------------------------------------------------------
# TODO(owner): replace with real Hermes transcript material. The shapes below
# are synthetic placeholders that exercise the four 3e failure modes; the
# numbers they produce are only meaningful once seeded from real sessions.
# ----------------------------------------------------------------------
def _hermes_like() -> ContinuityPersona:
    """A multi-session project that touches all four continuity failure modes.

    # TODO(owner): replace events/cue/ground-truth with a real Hermes session
    # chain. Keep the four-mode coverage: a stale prior-session loop, a
    # multi-step supersession chain, conventions that must survive cold start,
    # and a delegated coding session whose result must surface next turn.
    """
    repo = "repo-continuity"
    events = [
        # --- Session 1: decisions + conventions + an open loop left dangling. ---
        _ev("cnt-1", "task.completed", 0, repo, {
            "fact": {"topic": "deploy convention",
                     "statement": "all deploys go through the CI pipeline, never manual kubectl",
                     "tags": ["convention"]}}),
        _ev("cnt-2", "task.failed", 1, repo, {
            "decision": {"topic": "queue backend", "statement": "use an in-memory queue for jobs",
                         "stance": "rejected", "rationale": "loses jobs on restart"}}),
        _ev("cnt-3", "task.completed", 2, repo, {
            "decision": {"topic": "queue backend", "statement": "use a durable Postgres-backed job queue",
                         "stance": "adopted", "rationale": "survives restarts, no extra infra"}}),
        _ev("cnt-4", "task.started", 3, repo, {
            "open_loop": {"id": "loop-metrics", "intent": "wire up Prometheus metrics for the job queue",
                          "cue": "queue observability"}}),
        # --- Session 2 (days later): config churn renames the same fact thrice. ---
        _ev("cnt-5", "task.completed", 6, repo, {
            "fact": {"topic": "queue table name", "statement": "the job queue table is called jobs_v1"}}),
        _ev("cnt-6", "task.completed", 9, repo, {
            "fact": {"topic": "queue table name", "statement": "the job queue table is called jobs_v2"}}),
        _ev("cnt-7", "task.completed", 12, repo, {
            "fact": {"topic": "queue table name", "statement": "the job queue table is called work_items"}}),
        # --- Session 3: a DELEGATED coding session completes real work. The hand
        #     reports a task.completed carrying typed hints (the same shape a real
        #     hand's events_to_record produces); it must surface next brief. ---
        _ev("cnt-8", "task.completed", 15, repo, {
            "summary": "delegated coding session added retry/backoff to the queue worker",
            "session_uid": "ses-delegated-1",
            "decision": {"topic": "queue retries",
                         "statement": "retry failed jobs with exponential backoff capped at 5 attempts",
                         "stance": "adopted", "rationale": "delegated session validated it against the load test"}}),
    ]
    persona = Persona(
        key="continuity",
        title="Multi-session job-queue project (Hermes-like)",
        repo_id=repo,
        events=events,
        cue="continue work on the job queue",
        rejected=["in-memory queue"],
        expected_brief=["durable Postgres-backed job queue", "in-memory queue",
                        "CI pipeline", "work_items"],
        superseded=[("jobs_v1", "work_items"), ("jobs_v2", "work_items")],
        ground_truth_next="wire up Prometheus metrics for the job queue",
        dormant_loops=["wire up Prometheus metrics for the job queue"],
    )
    return ContinuityPersona(
        persona=persona,
        prior_session_loop="wire up Prometheus metrics for the job queue",
        stale_values=["jobs_v1", "jobs_v2"],
        current_value="work_items",
        delegated_brief_items=["exponential backoff", "retry"],
    )


def all_continuity_personas() -> List[ContinuityPersona]:
    # TODO(owner): grow this list from real Hermes sessions; one persona is
    # enough to exercise all four modes but not to characterize the system.
    return [_hermes_like()]


# ----------------------------------------------------------------------
# Result types — one row per (suite, persona), honest pass/fail + detail.
# ----------------------------------------------------------------------
@dataclass
class SuiteScore:
    suite: str
    persona: str
    passed: bool
    detail: str = ""


@dataclass
class ContinuityResult:
    scores: List[SuiteScore] = field(default_factory=list)

    def pass_rate(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(1 for s in self.scores if s.passed) / len(self.scores), 4)


# ----------------------------------------------------------------------
# Shared helper: rebuild a brief from a persona's ledger on a FRESH client.
# Every suite uses this (re-derivability is the common spine), so the suites
# differ only in what ground truth they check, never in the assembly path.
# ----------------------------------------------------------------------
async def _fresh_brief(cp: ContinuityPersona, *, cue: Optional[str] = None) -> str:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / f"{cp.persona.key}.db")
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    for ev in cp.persona.events:
        await db.append_event(
            event_id=ev["id"], type=ev["type"], source=ev.get("source", "bench"),
            ts=ev["ts"], repo_id=ev.get("repo_id"), payload=ev.get("payload", {}),
        )
    # Production consolidation path, re-derived purely from the ledger.
    await Consolidator(db, graph).rebuild_from_events()
    section = await MemoryBriefAssembler(graph).assemble(cue or cp.persona.cue, repo_id=cp.persona.repo_id)
    brief = section.render()
    await db.close()
    return brief


def _present(needle: str, haystack: str) -> bool:
    return needle.lower() in haystack.lower()


# ----------------------------------------------------------------------
# Suite 1 — unprompted cross-session awareness.
# A prior-session open loop must surface even though the cold cue does not name
# it. The cue deliberately avoids the loop's keywords ("Prometheus/metrics").
# ----------------------------------------------------------------------
async def _suite_cross_session_awareness(cp: ContinuityPersona) -> SuiteScore:
    brief = await _fresh_brief(cp, cue="continue work on the job queue")
    loop = cp.prior_session_loop
    passed = _present(loop, brief)
    detail = "" if passed else f"prior-session loop not surfaced unprompted: {loop!r}"
    return SuiteScore("cross_session_awareness", cp.persona.key, passed, detail)


# ----------------------------------------------------------------------
# Suite 2 — fact supersession under config churn.
# A fact renamed multiple times must resolve to the LATEST value only; EVERY
# stale value must be absent from the live brief.
# ----------------------------------------------------------------------
async def _suite_supersession_under_churn(cp: ContinuityPersona) -> SuiteScore:
    brief = await _fresh_brief(cp)
    leaked = [v for v in cp.stale_values if _present(v, brief)]
    current_ok = _present(cp.current_value, brief)
    passed = not leaked and current_ok
    if leaked:
        detail = f"stale values leaked after churn: {leaked}"
    elif not current_ok:
        detail = f"current value missing: {cp.current_value!r}"
    else:
        detail = ""
    return SuiteScore("supersession_under_churn", cp.persona.key, passed, detail)


# ----------------------------------------------------------------------
# Suite 3 — cold-start recall on a fresh client.
# A brand-new client (new DB+graph, memory rebuilt only from events) must still
# carry the project's decisions and conventions — no warm cache to lean on.
# ----------------------------------------------------------------------
async def _suite_cold_start_recall(cp: ContinuityPersona) -> SuiteScore:
    brief = await _fresh_brief(cp)
    required = cp.persona.expected_brief
    missing = [item for item in required if not _present(item, brief)]
    passed = not missing
    detail = "" if passed else f"cold-start brief missing: {missing}"
    return SuiteScore("cold_start_recall", cp.persona.key, passed, detail)


# ----------------------------------------------------------------------
# Suite 4 — awareness of delegated-session work.
# Typed hints recorded by a delegated coding session (a hand's task.completed)
# must surface in the next brief, so the next turn knows what was already done.
# ----------------------------------------------------------------------
async def _suite_delegated_session_awareness(cp: ContinuityPersona) -> SuiteScore:
    brief = await _fresh_brief(cp)
    missing = [item for item in cp.delegated_brief_items if not _present(item, brief)]
    passed = not missing
    detail = "" if passed else f"delegated-session work not surfaced: {missing}"
    return SuiteScore("delegated_session_awareness", cp.persona.key, passed, detail)


_SUITES = (
    _suite_cross_session_awareness,
    _suite_supersession_under_churn,
    _suite_cold_start_recall,
    _suite_delegated_session_awareness,
)


async def run_continuity() -> ContinuityResult:
    """Run all four continuity suites across all continuity personas.

    Honest scoring: each suite returns a real pass/fail; nothing is tuned to
    pass. A failure is a 3e finding to triage, not a benchmark bug.
    """
    result = ContinuityResult()
    for cp in all_continuity_personas():
        for suite in _SUITES:
            result.scores.append(await suite(cp))
    return result


def report_continuity(result: ContinuityResult) -> str:
    lines: List[str] = []
    lines.append("centri-bench 3e continuity suites")
    lines.append("=" * 72)
    lines.append("STUB PERSONAS — # TODO(owner): seed from real Hermes transcripts before")
    lines.append("quoting these numbers as 3e evidence. Methodology is real; content is not.")
    lines.append("")
    for s in result.scores:
        mark = "PASS" if s.passed else "FAIL"
        line = f"  [{mark}] {s.suite:28s} {s.persona}"
        if s.detail:
            line += f"  -- {s.detail}"
        lines.append(line)
    lines.append("")
    lines.append(f"Continuity pass rate: {result.pass_rate():.2f} "
                 f"({sum(1 for s in result.scores if s.passed)}/{len(result.scores)})")
    lines.append("Failing suites are 3e FINDINGS, not bugs to hide (anti-gaming rule).")
    return "\n".join(lines)
