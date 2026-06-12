"""ModelRouter supports LiteLLM proxy aliases and direct Nebius/OpenAI roles."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import centri.config as config_module
import centri.model_router as model_router_module
from centri.config import Settings
from centri.model_router import ModelRouter


class _FakeChoice:
    def __init__(self, content: str):
        self.message = type("Message", (), {"content": content})()


class _FakeCompletionResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeLiteLLM:
    def __init__(self):
        self.completion_calls = []
        self.embedding_calls = []

    def completion(self, **kwargs):
        self.completion_calls.append(kwargs)
        return _FakeCompletionResponse("coding_task")

    def embedding(self, **kwargs):
        self.embedding_calls.append(kwargs)
        return {"data": [{"embedding": [0.1, 0.2]} for _ in kwargs["input"]]}


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> _FakeLiteLLM:
    fake = _FakeLiteLLM()
    monkeypatch.setattr(model_router_module, "litellm", fake)
    return fake


def test_model_router_uses_litellm_proxy_alias_for_roles(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="http://localhost:4000/v1",
            litellm_api_key="proxy-key",
            model_intent="fast-intent",
        ),
    )
    router = ModelRouter()

    assert router.classify_intent("Could you inspect this?") == "coding_task"
    call = fake_litellm.completion_calls[-1]
    assert call["model"] == "fast-intent"
    assert call["api_base"] == "http://localhost:4000/v1"
    assert call["api_key"] == "proxy-key"


def test_model_router_supports_direct_openai_roles(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="",
            litellm_api_key="",
            openai_api_key="open-key",
            model_narration="gpt-4o-mini",
        ),
    )
    router = ModelRouter()

    router.narrate("Task finished.")
    call = fake_litellm.completion_calls[-1]
    assert call["model"] == "openai/gpt-4o-mini"
    assert call["api_base"] == "https://api.openai.com/v1"
    assert call["api_key"] == "open-key"


def test_model_router_supports_direct_nebius_roles(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="",
            litellm_api_key="",
            nebius_api_key="neb-key",
            nebius_base_url="https://api.studio.nebius.ai/v1",
            model_reasoning="deepseek-ai/DeepSeek-V3",
        ),
    )
    router = ModelRouter()

    router.reason("Explain the failure.")
    call = fake_litellm.completion_calls[-1]
    assert call["model"] == "nebius/deepseek-ai/DeepSeek-V3"
    assert call["api_base"] == "https://api.studio.nebius.ai/v1"
    assert call["api_key"] == "neb-key"


def test_model_router_role_models_reports_resolved_transport(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="http://localhost:4000/v1",
            litellm_api_key="proxy-key",
            model_intent="fast-intent",
            model_embeddings="embed-small",
        ),
    )
    router = ModelRouter()

    roles = router.role_models()
    assert roles["intent"] == {
        "configured": True,
        "model": "fast-intent",
        "via_proxy": True,
        "api_base": "http://localhost:4000/v1",
    }
    assert roles["embeddings"] == {
        "configured": True,
        "model": "embed-small",
        "via_proxy": True,
        "api_base": "http://localhost:4000/v1",
    }


def test_embed_auto_prefixes_bare_model_for_custom_base(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    model_router_module._EMBED_CACHE.clear()
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="https://api.tokenfactory.nebius.com/v1",
            litellm_api_key="tf-key",
            model_embeddings="Qwen/Qwen3-Embedding-8B",
        ),
    )
    router = ModelRouter()

    router.embed(["custom-base text"])
    assert fake_litellm.embedding_calls[-1]["model"] == "openai/Qwen/Qwen3-Embedding-8B"


def test_embed_leaves_already_prefixed_model_unchanged(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    model_router_module._EMBED_CACHE.clear()
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="https://api.tokenfactory.nebius.com/v1",
            litellm_api_key="tf-key",
            model_embeddings="openai/Qwen/Qwen3-Embedding-8B",
        ),
    )
    router = ModelRouter()

    router.embed(["already prefixed text"])
    assert fake_litellm.embedding_calls[-1]["model"] == "openai/Qwen/Qwen3-Embedding-8B"


def test_embed_does_not_prefix_without_custom_base(
    monkeypatch: pytest.MonkeyPatch, fake_litellm: _FakeLiteLLM
):
    model_router_module._EMBED_CACHE.clear()
    monkeypatch.setattr(
        config_module,
        "_settings",
        Settings(
            litellm_base_url="",
            litellm_api_key="",
            nebius_api_key="neb-key",
            nebius_base_url="https://api.studio.nebius.ai/v1",
            model_embeddings="nebius/Qwen/Qwen3-Embedding-8B",
        ),
    )
    router = ModelRouter()

    router.embed(["no custom base text"])
    assert fake_litellm.embedding_calls[-1]["model"] == "nebius/Qwen/Qwen3-Embedding-8B"
