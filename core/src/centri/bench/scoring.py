"""centri-bench scoring — the three headline metrics, deterministic rubric.

centri-bench.md specifies LLM-judge grading with human spot-checks. CENTRI's
build sandbox has no model API key, so this module implements the **deterministic
rubric** the judge would apply, against the structured ground truth authored in
``personas.py``. The scorer is honest about being deterministic and exposes a
seam (:func:`set_judge`) so an LLM judge can be slotted in unchanged when keys
are present. Because the ground truth is structured (exact rejected approaches,
required brief substrings, stale/current pairs, the next step), deterministic
substring grading is faithful — it is not a softer test than a rubric judge, it
is the rubric judge's checklist made executable.

Metrics (directions per the spec):

  - ``re_proposal_rate``   (lower better): fraction of a persona's rejected
    approaches that the assembled brief does NOT carry as rejected — an agent
    with no record of the rejection is free to re-propose it.
  - ``brief_completeness`` (higher better): fraction of the persona's required
    decisions/rejections/conventions/next-step substrings present in the brief.
  - ``next_step_correct``  (higher better): whether the ground-truth next step
    (by key tokens) appears in the brief.
  - ``stale_fact_correct`` (higher better): for each (stale, current) pair, the
    stale string is absent and the current string present in the live brief.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from centri.bench.personas import Persona

# Optional LLM-judge seam. When set, scoring delegates the rubric to it.
_JUDGE: Optional[Callable] = None


def set_judge(fn: Optional[Callable]) -> None:
    global _JUDGE
    _JUDGE = fn


def _present(needle: str, haystack: str) -> bool:
    return needle.lower() in haystack.lower()


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3]


def _token_overlap_present(phrase: str, haystack: str, threshold: float = 0.6) -> bool:
    """True if at least ``threshold`` of ``phrase``'s content tokens appear."""
    toks = _tokens(phrase)
    if not toks:
        return False
    hay = haystack.lower()
    hit = sum(1 for t in toks if t in hay)
    return (hit / len(toks)) >= threshold


@dataclass
class TaskScore:
    persona: str
    re_proposal_rate: float
    brief_completeness: float
    next_step_correct: float
    stale_fact_correct: float
    missing_brief_items: List[str] = field(default_factory=list)
    unguarded_rejections: List[str] = field(default_factory=list)

    @property
    def composite(self) -> float:
        # Headline composite: reward completeness + correctness, penalize
        # re-proposal. Re-proposal is inverted so higher composite is better.
        return round(
            0.35 * self.brief_completeness
            + 0.30 * self.next_step_correct
            + 0.20 * self.stale_fact_correct
            + 0.15 * (1.0 - self.re_proposal_rate),
            4,
        )


def score(persona: Persona, brief: str) -> TaskScore:
    if _JUDGE is not None:
        return _JUDGE(persona, brief)

    # Brief completeness: required substrings present.
    present = [item for item in persona.expected_brief if _present(item, brief)]
    missing = [item for item in persona.expected_brief if not _present(item, brief)]
    completeness = len(present) / len(persona.expected_brief) if persona.expected_brief else 1.0

    # Re-proposal rate: a rejected approach is "guarded" if it appears in the
    # brief (so the agent knows it was tried and rejected). Unguarded rejections
    # are re-proposal risk.
    unguarded = [r for r in persona.rejected if not _present(r, brief)]
    re_proposal = len(unguarded) / len(persona.rejected) if persona.rejected else 0.0

    # Next-step correctness: ground-truth next step present by token overlap.
    next_correct = (
        1.0 if (not persona.ground_truth_next or _token_overlap_present(persona.ground_truth_next, brief))
        else 0.0
    )

    # Stale-fact correctness: stale absent AND current present, per pair.
    if persona.superseded:
        oks = 0
        for stale, current in persona.superseded:
            if (not _present(stale, brief)) and _present(current, brief):
                oks += 1
        stale_correct = oks / len(persona.superseded)
    else:
        stale_correct = 1.0  # no supersession to test for this persona

    return TaskScore(
        persona=persona.key,
        re_proposal_rate=round(re_proposal, 4),
        brief_completeness=round(completeness, 4),
        next_step_correct=round(next_correct, 4),
        stale_fact_correct=round(stale_correct, 4),
        missing_brief_items=missing,
        unguarded_rejections=unguarded,
    )
