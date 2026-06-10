"""centri-bench harness tests — the benchmark is itself under test.

These assert the benchmark's *methodology* is sound and deterministic, and that
it reproduces the central thesis: typed supersession (native) beats prose
accumulation (Letta) specifically on the stale-fact task. They do NOT hard-code
the headline numbers (those are reported honestly by the harness), but they pin
the relationships the spec claims.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.bench.backends import LettaBackend, NativeBackend
from centri.bench.harness import run
from centri.bench.personas import all_personas
from centri.bench.scoring import score, set_judge


@pytest.fixture(autouse=True)
def _clear_judge():
    # The deterministic rubric is the default; ensure no test leaks an LLM judge.
    set_judge(None)
    yield
    set_judge(None)


async def _brief_for(backend, persona) -> str:
    await backend.ingest(persona)
    brief = await backend.brief(persona.cue, persona.repo_id)
    await backend.close()
    return brief


@pytest.mark.asyncio
async def test_native_brief_carries_rejections_and_current_facts():
    persona = next(p for p in all_personas() if p.key == "webapp")
    brief = await _brief_for(NativeBackend(), persona)
    s = score(persona, brief)
    # Native must be perfect on this persona: it has typed supersession.
    assert s.brief_completeness == 1.0, s.missing_brief_items
    assert s.re_proposal_rate == 0.0, s.unguarded_rejections
    assert s.stale_fact_correct == 1.0
    assert s.next_step_correct == 1.0


@pytest.mark.asyncio
async def test_native_brief_drops_superseded_fact():
    persona = next(p for p in all_personas() if p.key == "webapp")
    brief = await _brief_for(NativeBackend(), persona)
    # The renamed service: stale name gone, current name present.
    assert "authsvc" not in brief.lower()
    assert "identity-gateway" in brief.lower()


@pytest.mark.asyncio
async def test_letta_resurfaces_stale_fact():
    """The escape hatch's failure mode: prose accumulation keeps the stale name."""
    persona = next(p for p in all_personas() if p.key == "webapp")
    brief = await _brief_for(LettaBackend(), persona)
    # Letta has no supersession, so the old name is still in the archival prose.
    assert "authsvc" in brief.lower()
    s = score(persona, brief)
    assert s.stale_fact_correct < 1.0  # fails the stale-fact task by design


@pytest.mark.asyncio
async def test_native_applies_convention_regardless_of_cue():
    """Task 5 (procedural): convention-tagged facts inject even when the cue
    doesn't lexically mention them."""
    persona = next(p for p in all_personas() if p.key == "infra")
    brief = await _brief_for(NativeBackend(), persona)
    assert "terraform plan+apply" in brief.lower()
    assert "vault" in brief.lower()


@pytest.mark.asyncio
async def test_harness_native_beats_letta_on_supersession():
    """Central thesis, end-to-end: across the panel, native's stale-fact score
    is strictly better than Letta's."""
    out = await run()
    by_name = {r.backend: r for r in out["results"]}
    native = next(r for n, r in by_name.items() if n.startswith("centri-native"))
    letta = next(r for n, r in by_name.items() if n.startswith("letta"))

    nh = native.headline()
    lh = letta.headline()
    assert nh["stale_fact_correct"] > lh["stale_fact_correct"]
    assert nh["composite"] >= lh["composite"]
    # Native should be flawless on the headline composite given perfect ground
    # truth; this guards against a regression in assembly.
    assert nh["composite"] == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_dormancy_surfaces_once():
    """Task 6 (silent-abandonment) reported as a native capability."""
    out = await run()
    native = next(r for r in out["results"] if r.backend.startswith("centri-native"))
    assert native.dormancy_ok is True


@pytest.mark.asyncio
async def test_scoring_is_deterministic():
    persona = next(p for p in all_personas() if p.key == "trading")
    backend = NativeBackend()
    await backend.ingest(persona)
    brief = await backend.brief(persona.cue, persona.repo_id)
    await backend.close()
    s1 = score(persona, brief)
    s2 = score(persona, brief)
    assert vars(s1) == vars(s2)


@pytest.mark.asyncio
async def test_judge_seam_overrides_rubric():
    persona = next(p for p in all_personas() if p.key == "trading")
    sentinel = score(persona, "")  # any TaskScore object
    set_judge(lambda p, b: sentinel)
    assert score(persona, "anything") is sentinel
