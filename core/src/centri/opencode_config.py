"""Read OpenCode's local provider config/auth (ROADMAP 3b.5, Decision 5).

Decision 5 ("single LLM config") makes OpenCode's own provider configuration the
source of truth for model access: a user who already set up providers in OpenCode
should never have to configure them again in CENTRI. This module probes OpenCode's
well-known config/auth files **read-only**, surfaces *which providers are
configured* (never the key material), and resolves a provider's key for the model
router as a fallback when no ``CENTRI_*`` env key is present.

Two files matter, both schema-tolerant because OpenCode's layout has shifted
across versions:

  - ``auth.json`` — maps provider → credentials. Shapes seen in the wild::

        {"openai": {"type": "api", "key": "sk-..."}}
        {"anthropic": {"apiKey": "sk-ant-..."}}
        {"openrouter": "sk-or-..."}

    We read tolerantly: a provider's value may be a dict (look for
    ``key``/``apiKey``/``api_key``/``token``/``value``) or a bare string.
  - ``opencode.json`` — non-secret config; we read only the ``provider`` block to
    learn configured provider/model names for display.

**Never write, never log key material.** :func:`discovered_providers` returns
presence only (``has_key`` boolean); the raw key is returned solely by
:func:`resolve_provider_key` to the in-process model router and is never placed in
an event payload. Anything that *is* surfaced in an event still passes through the
redaction seam.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_KEY_FIELDS = ("key", "apiKey", "api_key", "token", "value", "secret")


def default_config_dirs() -> List[Path]:
    """Well-known OpenCode config/state directories (macOS/Linux)."""
    home = Path.home()
    dirs: List[Path] = [
        home / ".config" / "opencode",
        home / ".local" / "share" / "opencode",
        home / ".opencode",
    ]
    if sys.platform == "darwin":
        dirs.append(home / "Library" / "Application Support" / "opencode")
    return dirs


@dataclass
class DiscoveredProvider:
    """A provider configured in OpenCode, surfaced without key material.

    ``has_key`` reports whether a usable credential is present; the credential
    itself is never carried here (it stays in :func:`resolve_provider_key`).
    """

    provider: str
    source: str  # absolute path of the auth/config file it came from
    has_key: bool = False
    models: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "has_key": self.has_key,
            "models": list(self.models),
        }


def _extract_key(value: Any) -> Optional[str]:
    """Pull a credential string out of an auth.json provider value, tolerantly."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for field in _KEY_FIELDS:
            v = value.get(field)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _read_json(path: Path) -> Optional[Any]:
    """Read+parse a JSON file read-only; return None on any failure (honest)."""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("opencode config: cannot read %s: %s", path, exc)
        return None


class OpenCodeConfig:
    """Read-only view of OpenCode's local provider config + auth.

    Probes default + configured directories. Schema-tolerant and honest: a missing
    or unreadable file simply contributes nothing.
    """

    def __init__(self, extra_dirs: Optional[List[str]] = None, use_defaults: bool = True):
        self._dirs: List[Path] = list(default_config_dirs()) if use_defaults else []
        for d in extra_dirs or []:
            self._dirs.append(Path(d).expanduser())

    def _auth_files(self) -> List[Path]:
        return [d / "auth.json" for d in self._dirs]

    def _config_files(self) -> List[Path]:
        return [d / "opencode.json" for d in self._dirs]

    # ------------------------------------------------------------------
    # Auth (credentials) — read tolerantly, key material stays internal
    # ------------------------------------------------------------------
    def _auth_map(self) -> Dict[str, Tuple[str, str]]:
        """provider -> (key, source-path). First file to define a provider wins."""
        out: Dict[str, Tuple[str, str]] = {}
        for path in self._auth_files():
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            # Some versions nest under a "providers" key.
            providers = data.get("providers") if isinstance(data.get("providers"), dict) else data
            for provider, value in providers.items():
                if provider in out:
                    continue
                key = _extract_key(value)
                if key:
                    out[provider] = (key, str(path.resolve()))
        return out

    def _config_models(self) -> Dict[str, Tuple[str, ...]]:
        """provider -> model names declared in opencode.json (display only)."""
        out: Dict[str, Tuple[str, ...]] = {}
        for path in self._config_files():
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            provider_block = data.get("provider")
            if not isinstance(provider_block, dict):
                continue
            for provider, cfg in provider_block.items():
                models: List[str] = []
                if isinstance(cfg, dict):
                    raw_models = cfg.get("models")
                    if isinstance(raw_models, dict):
                        models = [str(m) for m in raw_models.keys()]
                    elif isinstance(raw_models, list):
                        models = [str(m) for m in raw_models]
                out.setdefault(provider, tuple(models))
        return out

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def discovered_providers(self) -> List[DiscoveredProvider]:
        """Providers configured in OpenCode, key material stripped.

        Union of providers that have a usable auth credential and providers that
        appear (model list only) in opencode.json.
        """
        auth = self._auth_map()
        models = self._config_models()
        names = set(auth) | set(models)
        out: List[DiscoveredProvider] = []
        for provider in sorted(names):
            key_source = auth.get(provider)
            out.append(
                DiscoveredProvider(
                    provider=provider,
                    source=key_source[1] if key_source else "opencode.json",
                    has_key=key_source is not None,
                    models=models.get(provider, ()),
                )
            )
        return out

    def resolve_provider_base_url(self, provider: str) -> Optional[str]:
        """Return OpenCode's configured base URL for ``provider`` if available."""
        for path in self._config_files():
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            provider_block = data.get("provider")
            if not isinstance(provider_block, dict):
                continue
            cfg = provider_block.get(provider)
            if isinstance(cfg, dict):
                opts = cfg.get("options")
                if isinstance(opts, dict):
                    url = opts.get("baseURL") or opts.get("baseUrl")
                    if isinstance(url, str) and url.strip():
                        return url.strip()
                url = cfg.get("baseURL") or cfg.get("baseUrl")
                if isinstance(url, str) and url.strip():
                    return url.strip()
        return None

    def resolve_provider_key(self, provider: str) -> Optional[str]:
        """Return OpenCode's stored key for ``provider`` (in-process use only).

        This is the single seam through which key material leaves this module, and
        only into the model router — never into an event payload or a log line.
        """
        entry = self._auth_map().get(provider)
        return entry[0] if entry else None
