"""Phase 3b.5 — OpenCode config/auth reuse (single LLM config, Decision 5).

OpenCode's provider auth is the source of truth: CENTRI reads it read-only,
surfaces *which* providers are configured (never key material), and the model
router resolves a provider key from it as a fallback when no CENTRI_* env key is
set — env always wins.

Honesty: OpenCode's auth.json/opencode.json shapes are *fixture-verified only*;
the reader is built tolerant across the shapes seen in the wild but is not proven
against a real OpenCode install.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.opencode_config import OpenCodeConfig


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


class TestOpenCodeConfig:
    def test_reads_auth_shapes_tolerantly(self, tmp_path):
        d = tmp_path / "opencode"
        _write(d / "auth.json", {
            "openai": {"type": "api", "key": "sk-openai-xxx"},
            "anthropic": {"apiKey": "sk-ant-yyy"},
            "openrouter": "sk-or-zzz",
        })
        cfg = OpenCodeConfig(extra_dirs=[str(d)], use_defaults=False)
        providers = {p.provider: p for p in cfg.discovered_providers()}
        assert {"openai", "anthropic", "openrouter"} <= set(providers)
        assert all(p.has_key for p in providers.values())

    def test_resolve_provider_key_returns_credential_internally(self, tmp_path):
        d = tmp_path / "opencode"
        _write(d / "auth.json", {"openai": {"key": "sk-secret-123"}})
        cfg = OpenCodeConfig(extra_dirs=[str(d)], use_defaults=False)
        assert cfg.resolve_provider_key("openai") == "sk-secret-123"
        assert cfg.resolve_provider_key("absent") is None

    def test_discovered_providers_never_leak_key_material(self, tmp_path):
        d = tmp_path / "opencode"
        _write(d / "auth.json", {"openai": {"key": "sk-secret-123"}})
        cfg = OpenCodeConfig(extra_dirs=[str(d)], use_defaults=False)
        serialized = json.dumps([p.as_dict() for p in cfg.discovered_providers()])
        assert "sk-secret-123" not in serialized
        assert '"has_key": true' in serialized

    def test_config_models_surface_for_display(self, tmp_path):
        d = tmp_path / "opencode"
        _write(d / "opencode.json", {
            "provider": {"openai": {"models": {"gpt-4o": {}, "gpt-4o-mini": {}}}}
        })
        cfg = OpenCodeConfig(extra_dirs=[str(d)], use_defaults=False)
        providers = {p.provider: p for p in cfg.discovered_providers()}
        assert "openai" in providers
        assert set(providers["openai"].models) == {"gpt-4o", "gpt-4o-mini"}
        # No auth.json → has_key is honestly False.
        assert providers["openai"].has_key is False

    def test_missing_dirs_degrade_honestly(self, tmp_path):
        cfg = OpenCodeConfig(extra_dirs=[str(tmp_path / "nope")], use_defaults=False)
        assert cfg.discovered_providers() == []
        assert cfg.resolve_provider_key("openai") is None


class TestModelRouterFallback:
    """Env keys win; OpenCode auth is the fallback; honest-unavailable otherwise."""

    def _router(self, monkeypatch, *, openai_env="", opencode_key=None):
        import centri.config as config_mod
        from centri.config import Settings
        from centri.model_router import ModelRouter

        # Direct-provider path (no LiteLLM proxy) so _provider_credentials runs.
        settings = Settings(
            litellm_base_url="",
            openai_api_key=openai_env,
            model_fast_reply="gpt-4o-mini",  # openai-looking => direct openai path
        )
        monkeypatch.setattr(config_mod, "_settings", settings)

        class FakeOC:
            def resolve_provider_key(self, provider):
                return opencode_key if provider == "openai" else None

        return ModelRouter(opencode_config=FakeOC())

    def test_env_key_wins_over_opencode(self, monkeypatch):
        router = self._router(monkeypatch, openai_env="env-key", opencode_key="oc-key")
        key, _base = router._provider_credentials("openai")
        assert key == "env-key"

    def test_opencode_key_used_when_env_absent(self, monkeypatch):
        router = self._router(monkeypatch, openai_env="", opencode_key="oc-key")
        key, _base = router._provider_credentials("openai")
        assert key == "oc-key"

    def test_honest_unavailable_when_neither(self, monkeypatch):
        router = self._router(monkeypatch, openai_env="", opencode_key=None)
        key, _base = router._provider_credentials("openai")
        assert key is None
