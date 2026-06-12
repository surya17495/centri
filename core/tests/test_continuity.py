"""Piece B2 — 3e continuity bench scaffold tests.

These pin the *methodology* of the continuity gate (``centri.bench.continuity``):
the four suites exist, each scores a real pass/fail through the production
cold-start path (rebuild_from_events -> assemble), and the gate is wired into
``python -m centri.bench.run --suite continuity``.

Per the anti-gaming rule we do NOT assert specific pass/fail outcomes against the
stub personas — those are findings to be re-derived once the owner seeds real
Hermes material. We assert the gate is sound and honest: it runs, it produces one
score per (suite, persona), and each suite genuinely exercises the cold-start
assembly (not a benchmark-only shortcut).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.bench.continuity import (  # noqa: E402
    all_continuity_personas,
    report_continuity,
    run_continuity,
    _fresh_brief,
)

_SUITE_NAMES = {
    "cross_session_awareness",
    "supersession_under_churn",
    "cold_start_recall",
    "delegated_session_awareness",
}


@pytest.mark.asyncio
async def test_all_four_continuity_suites_run_per_persona():
    """The gate produces exactly one honest score per (suite, persona)."""
    result = await run_continuity()
    personas = all_continuity_personas()
    assert len(result.scores) == 4 * len(personas)
    # All four named 3e failure modes are covered.
    assert {s.suite for s in result.scores} == _SUITE_NAMES


@pytest.mark.asyncio
async def test_scores_are_real_booleans_with_detail_on_failure():
    """Honest scoring: passed is a bool, and any failure carries a detail string
    explaining the finding (never a silent fail)."""
    result = await run_continuity()
    for s in result.scores:
        assert isinstance(s.passed, bool)
        if not s.passed:
            assert s.detail, f"{s.suite} failed without an explanatory detail"


@pytest.mark.asyncio
async def test_continuity_is_deterministic():
    """Same ledger -> same scores. The gate must be a stable regression signal."""
    a = await run_continuity()
    b = await run_continuity()
    assert [vars(s) for s in a.scores] == [vars(s) for s in b.scores]


@pytest.mark.asyncio
async def test_cold_start_brief_is_rebuilt_purely_from_events():
    """The shared spine rebuilds memory only from the ledger (re-derivability):
    a fresh client with no warm cache still produces a non-empty brief carrying
    a known decision."""
    cp = all_continuity_personas()[0]
    brief = await _fresh_brief(cp)
    assert brief.strip(), "cold-start brief must not be empty"
    # A decision adopted in the persona's history must be recoverable cold.
    assert "durable postgres-backed job queue" in brief.lower()


@pytest.mark.asyncio
async def test_supersession_suite_actually_checks_stale_absence():
    """Guard the suite's own validity: the persona's stale values and current
    value are distinct, so the suite is a real test and not vacuously true."""
    cp = all_continuity_personas()[0]
    assert cp.stale_values, "supersession suite needs stale values to test"
    assert cp.current_value
    assert cp.current_value not in cp.stale_values


@pytest.mark.asyncio
async def test_cross_session_loop_is_not_lexically_in_the_cue():
    """Guard suite 1's validity: the prior-session loop must be surfaced
    *unprompted*, so its key token must NOT appear in the cue (otherwise the
    suite would pass by lexical cue-match, not by prospective surfacing)."""
    cp = all_continuity_personas()[0]
    cue = cp.persona.cue.lower()
    # The loop's distinctive token ("prometheus"/"metrics") must be absent from
    # the cue, so a pass means the loop surfaced unprompted.
    assert "prometheus" not in cue and "metrics" not in cue


def test_report_renders_and_flags_stub_status():
    """The report must visibly flag that personas are stubs (owner honesty)."""

    async def _go():
        return await run_continuity()

    import asyncio

    text = report_continuity(asyncio.run(_go()))
    assert "TODO(owner)" in text
    assert "continuity" in text.lower()
    assert "pass rate" in text.lower()
