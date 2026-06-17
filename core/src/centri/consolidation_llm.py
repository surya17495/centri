"""Minimal OpenAI-compatible chat client for the LLM consolidation tier.

Deliberately thin and provider-agnostic (base_url + api_key + model), exactly the
shape the build spec calls for so it is Nebius-Token-Factory-ready but bound to no
provider. Uses ``httpx`` (already a core dependency) directly rather than the
``openai`` SDK so there is no new dependency and the request/response shape is
explicit and testable.

Honest-unavailable contract: :func:`resolve_consolidation_client` returns ``None``
when no base URL / model is configured, and the LLM tier then does nothing (the
deterministic tier is unaffected). The key is read from settings/env at runtime
and never hardcoded — the orchestrator injects it via the proxy.

The completion returns both the text content and the API's ``usage`` block (token
counts) so the consolidator can stamp token discipline into its receipts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    """A chat-completion result: text + token usage + the model id used."""

    content: Optional[str]
    usage: Dict[str, int]
    model: str


class OpenAIChatClient:
    """Synchronous OpenAI-compatible ``/chat/completions`` client over httpx.

    Synchronous on purpose (mirrors the repo's :class:`LettaHTTPClient` pattern);
    the async consolidator calls it through ``asyncio.to_thread`` so it never
    blocks the loop. Temperature defaults to 0 — the model was verified to emit
    clean JSON op arrays at temperature 0.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        *,
        timeout: float = 60.0,
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages: List[Dict[str, str]]) -> ChatResult:
        """Run one chat completion. Raises on transport/HTTP error.

        Callers wrap this so a runtime failure degrades to honest-unavailable
        rather than breaking the consolidation tick.
        """
        import httpx

        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return _result_from_response(data, self._model)


def _result_from_response(data: Dict[str, Any], model: str) -> ChatResult:
    content: Optional[str] = None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
    }
    return ChatResult(content=content, usage=usage, model=data.get("model") or model)


def resolve_consolidation_client(settings: Any = None) -> Optional[OpenAIChatClient]:
    """Build the consolidation chat client from settings, or ``None`` if unset.

    Honest-unavailable: requires both a base URL and a model id. The key may be
    empty (some local gateways need none); the orchestrator injects the real key
    at runtime. Returns ``None`` when unconfigured so the LLM tier reports
    unavailable and does nothing.
    """
    if settings is None:
        return None
    base_url = getattr(settings, "consolidation_base_url", "") or ""
    model = getattr(settings, "consolidation_model", "") or ""
    api_key = getattr(settings, "consolidation_api_key", "") or ""
    if not base_url or not model:
        return None
    max_tokens = getattr(settings, "consolidation_max_tokens", 8192) or 8192
    return OpenAIChatClient(base_url=base_url, api_key=api_key, model=model, max_tokens=max_tokens)
