"""centri-bench LLM judge — the real rubric grader behind the ``set_judge()`` seam.

``scoring.py`` ships a deterministic rubric that is faithful to the structured
ground truth in ``personas.py``. This module is the *LLM-judge* the spec
(``docs/centri-bench.md``) actually calls for: it hands the assembled brief and
the persona ground truth to a chat model and asks it to grade the same three
headline metrics plus the supporting ones, returning strict JSON.

It is wired in via :func:`centri.bench.scoring.set_judge`, so the harness path is
unchanged — ``score(persona, brief)`` simply delegates to the judge when one is
installed.

Config is env-driven (BYOK; no keys live in code):

  - ``CENTRI_JUDGE_BASE_URL``  default ``http://127.0.0.1:8901/v1`` (the relay)
  - ``CENTRI_JUDGE_MODEL``     default ``moonshotai/Kimi-K2.6``
  - ``CENTRI_JUDGE_API_KEY``   any non-empty string; the relay injects the real
    key upstream. Defaults to ``"sandbox"`` so the relay path works keyless.

The model is a reasoning model: responses may carry a ``reasoning`` field and the
content may be wrapped in prose or fences, so the parser is tolerant about
*locating* the JSON object but strict about its *shape*. Malformed output is
retried (default 3 attempts) with the temperature pinned to 0 for determinism.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from centri.bench.personas import Persona
from centri.bench.scoring import TaskScore

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - httpx is a hard dependency
    httpx = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "http://127.0.0.1:8901/v1"
DEFAULT_MODEL = "moonshotai/Kimi-K2.6"

_RUBRIC = """You are a strict benchmark judge for an agent-memory system.

You are given a persona's GROUND TRUTH (authored before the system was built) and
the BRIEF the memory system assembled for a cold-start cue. Grade how well the
brief reflects the ground truth on these metrics. Output ONLY a JSON object.

Metrics (all floats in [0,1] unless noted):
- "brief_completeness": fraction of the REQUIRED items that the brief conveys
  (semantically, not just verbatim). REQUIRED items: {expected}
- "re_proposal_rate" (LOWER is better): fraction of the REJECTED approaches that
  the brief FAILS to flag as already-tried-and-rejected. An approach the brief
  does not mention as rejected is unguarded and counts toward this rate.
  REJECTED approaches: {rejected}
- "next_step_correct": 1.0 if the brief's implied next step matches the
  GROUND-TRUTH NEXT STEP, else 0.0. GROUND-TRUTH NEXT STEP: {next_step}
- "stale_fact_correct": for each (stale, current) pair, score 1.0 only if the
  brief states the CURRENT value and does NOT assert the STALE value as true;
  average over pairs. If there are no pairs, score 1.0. PAIRS: {pairs}

Also return:
- "missing_brief_items": list of REQUIRED items the brief omits.
- "unguarded_rejections": list of REJECTED approaches the brief fails to flag.

BRIEF:
\"\"\"
{brief}
\"\"\"

Respond with ONLY this JSON object and nothing else:
{{"brief_completeness": <float>, "re_proposal_rate": <float>,
"next_step_correct": <float>, "stale_fact_correct": <float>,
"missing_brief_items": [<string>...], "unguarded_rejections": [<string>...]}}"""


class LLMJudge:
    """A callable rubric judge: ``judge(persona, brief) -> TaskScore``."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_retries: int = 3,
        timeout: float = 120.0,
        client: Any = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("CENTRI_JUDGE_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.getenv("CENTRI_JUDGE_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("CENTRI_JUDGE_API_KEY", "sandbox")
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = client  # injectable httpx.Client for tests

    # -- prompt -------------------------------------------------------------
    def _prompt(self, persona: Persona, brief: str) -> str:
        pairs = [{"stale": s, "current": c} for s, c in persona.superseded]
        return _RUBRIC.format(
            expected=json.dumps(persona.expected_brief),
            rejected=json.dumps(persona.rejected),
            next_step=json.dumps(persona.ground_truth_next or ""),
            pairs=json.dumps(pairs),
            brief=brief,
        )

    # -- transport ----------------------------------------------------------
    def _post(self, prompt: str) -> str:
        if self._client is None:
            if httpx is None:  # pragma: no cover
                raise RuntimeError("httpx is required for the LLM judge")
            self._client = httpx.Client(timeout=self.timeout)
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Locate the first balanced JSON object in possibly-noisy model output."""
        text = text.strip()
        # Strip code fences if present.
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        start = text.find("{")
        if start == -1:
            raise ValueError("no JSON object in judge output")
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
        raise ValueError("unbalanced JSON object in judge output")

    @staticmethod
    def _to_score(persona: Persona, obj: Dict[str, Any]) -> TaskScore:
        def f(key: str) -> float:
            val = float(obj[key])  # KeyError/ValueError -> retry
            return round(max(0.0, min(1.0, val)), 4)

        return TaskScore(
            persona=persona.key,
            re_proposal_rate=f("re_proposal_rate"),
            brief_completeness=f("brief_completeness"),
            next_step_correct=f("next_step_correct"),
            stale_fact_correct=f("stale_fact_correct"),
            missing_brief_items=[str(x) for x in obj.get("missing_brief_items", [])],
            unguarded_rejections=[str(x) for x in obj.get("unguarded_rejections", [])],
        )

    # -- entrypoint ---------------------------------------------------------
    def __call__(self, persona: Persona, brief: str) -> TaskScore:
        prompt = self._prompt(persona, brief)
        last_err: Optional[Exception] = None
        for _ in range(self.max_retries):
            try:
                raw = self._post(prompt)
                obj = self._extract_json(raw)
                return self._to_score(persona, obj)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                last_err = exc
                continue
        raise RuntimeError(
            f"LLM judge failed to produce valid JSON after {self.max_retries} attempts: {last_err}"
        )


def make_judge(**kwargs: Any) -> LLMJudge:
    """Construct a judge from env defaults, overridable via kwargs."""
    return LLMJudge(**kwargs)
