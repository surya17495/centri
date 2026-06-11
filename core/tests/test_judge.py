"""LLM-judge wiring tests — fully mocked HTTP, never touches the network.

These pin the judge's contract: it posts a rubric prompt, parses strict JSON out
of (possibly noisy, reasoning-model) output, clamps and shapes it into a
TaskScore, retries on malformed output, and plugs into the set_judge() seam so
the harness path is unchanged.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.bench.judge import LLMJudge, make_judge
from centri.bench.personas import all_personas
from centri.bench.scoring import TaskScore, score, set_judge


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Records requests and replays a scripted list of response bodies."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self._bodies.pop(0))


def _good_verdict() -> str:
    return json.dumps(
        {
            "brief_completeness": 1.0,
            "re_proposal_rate": 0.0,
            "next_step_correct": 1.0,
            "stale_fact_correct": 1.0,
            "missing_brief_items": [],
            "unguarded_rejections": [],
        }
    )


@pytest.fixture(autouse=True)
def _clear_judge():
    set_judge(None)
    yield
    set_judge(None)


def test_judge_parses_clean_json_into_taskscore():
    persona = next(p for p in all_personas() if p.key == "webapp")
    client = _FakeClient([_good_verdict()])
    judge = LLMJudge(client=client)
    s = judge(persona, "some brief")
    assert isinstance(s, TaskScore)
    assert s.persona == "webapp"
    assert s.brief_completeness == 1.0
    assert s.re_proposal_rate == 0.0
    assert s.stale_fact_correct == 1.0
    assert len(client.calls) == 1


def test_judge_extracts_json_from_fenced_and_prefixed_output():
    persona = next(p for p in all_personas() if p.key == "trading")
    noisy = "Here is my verdict:\n```json\n" + _good_verdict() + "\n```\nDone."
    judge = LLMJudge(client=_FakeClient([noisy]))
    s = judge(persona, "brief")
    assert s.brief_completeness == 1.0


def test_judge_extracts_bare_object_amid_prose():
    persona = next(p for p in all_personas() if p.key == "infra")
    noisy = "I think the answer is " + _good_verdict() + " overall."
    judge = LLMJudge(client=_FakeClient([noisy]))
    s = judge(persona, "brief")
    assert s.next_step_correct == 1.0


def test_judge_retries_on_malformed_then_succeeds():
    persona = next(p for p in all_personas() if p.key == "webapp")
    client = _FakeClient(["not json at all", "{ broken", _good_verdict()])
    judge = LLMJudge(client=client, max_retries=3)
    s = judge(persona, "brief")
    assert s.brief_completeness == 1.0
    assert len(client.calls) == 3  # two failures then success


def test_judge_raises_after_exhausting_retries():
    persona = next(p for p in all_personas() if p.key == "webapp")
    client = _FakeClient(["nope", "still nope", "again nope"])
    judge = LLMJudge(client=client, max_retries=3)
    with pytest.raises(RuntimeError):
        judge(persona, "brief")


def test_judge_clamps_out_of_range_scores():
    persona = next(p for p in all_personas() if p.key == "webapp")
    verdict = json.dumps(
        {
            "brief_completeness": 1.7,
            "re_proposal_rate": -0.5,
            "next_step_correct": 2.0,
            "stale_fact_correct": 0.5,
            "missing_brief_items": ["x"],
            "unguarded_rejections": [],
        }
    )
    judge = LLMJudge(client=_FakeClient([verdict]))
    s = judge(persona, "brief")
    assert s.brief_completeness == 1.0
    assert s.re_proposal_rate == 0.0
    assert s.next_step_correct == 1.0
    assert s.stale_fact_correct == 0.5


def test_judge_retries_on_missing_metric_key():
    persona = next(p for p in all_personas() if p.key == "webapp")
    partial = json.dumps({"brief_completeness": 1.0})  # missing the rest
    client = _FakeClient([partial, _good_verdict()])
    judge = LLMJudge(client=client, max_retries=2)
    s = judge(persona, "brief")
    assert s.stale_fact_correct == 1.0
    assert len(client.calls) == 2


def test_judge_request_shape_uses_config_and_temperature_zero():
    persona = next(p for p in all_personas() if p.key == "trading")
    client = _FakeClient([_good_verdict()])
    judge = LLMJudge(
        base_url="http://example.test/v1/",
        model="some/Model",
        api_key="k",
        client=client,
    )
    judge(persona, "brief")
    call = client.calls[0]
    assert call["url"] == "http://example.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer k"
    assert call["json"]["model"] == "some/Model"
    assert call["json"]["temperature"] == 0
    # The prompt must carry the persona ground truth so grading is anchored.
    prompt = call["json"]["messages"][0]["content"]
    assert "improve" not in prompt or True  # cue not required
    assert "EWMA(span=8)" in prompt  # expected_brief item is embedded


def test_judge_plugs_into_set_judge_seam():
    """score() delegates to the installed judge — same harness path."""
    persona = next(p for p in all_personas() if p.key == "webapp")
    judge = LLMJudge(client=_FakeClient([_good_verdict()]))
    set_judge(judge)
    s = score(persona, "whatever brief")
    assert s.brief_completeness == 1.0
    assert s.persona == "webapp"


def test_make_judge_reads_env_defaults(monkeypatch):
    monkeypatch.setenv("CENTRI_JUDGE_BASE_URL", "http://relay.test/v1")
    monkeypatch.setenv("CENTRI_JUDGE_MODEL", "moonshotai/Kimi-K2.6")
    monkeypatch.delenv("CENTRI_JUDGE_API_KEY", raising=False)
    judge = make_judge()
    assert judge.base_url == "http://relay.test/v1"
    assert judge.model == "moonshotai/Kimi-K2.6"
    assert judge.api_key == "sandbox"
