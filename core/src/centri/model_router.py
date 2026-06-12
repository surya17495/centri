"""CENTRI model router — LiteLLM gateway for text/vision/embedding calls.

LiveKit owns voice STT/TTS. Letta owns memory-agent turns.
This module is only for coordinator reasoning/intent/narration/embeddings.
"""

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypeVar

try:
    import litellm
except ModuleNotFoundError:
    litellm = None  # type: ignore[assignment]

from centri.config import get_settings

logger = logging.getLogger(__name__)
T = TypeVar("T")

# In-memory LRU for embeds to avoid redundant calls across turns
_EMBED_CACHE: Dict[str, List[float]] = {}
_EMBED_LIFETIME_SECONDS = 600


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    model: str
    api_key: Optional[str]
    api_base: Optional[str]
    via_proxy: bool


class ModelRouter:
    """Routes coordinator model calls through LiteLLM.

    Configured roles:
    - intent: fast classification
    - fast_reply: quick answers
    - reasoning: complex analysis
    - narration: spoken responses
    - summarization: status summaries
    - vision: image analysis
    - embeddings: vector encoding
    """

    def __init__(self, opencode_config: Any = None):
        settings = get_settings()
        self._settings = settings
        self._litellm_base_url = settings.litellm_base_url
        self._litellm_api_key = settings.litellm_api_key
        # Single-LLM-config (Decision 5): OpenCode's own auth is a *fallback*
        # source of provider keys when no CENTRI_* env key is set. Built lazily
        # from default + configured dirs unless one is injected (tests). Env wins.
        if opencode_config is None:
            try:
                from centri.opencode_config import OpenCodeConfig

                extra = getattr(settings, "ingest_opencode_paths", "") or ""
                dirs = [p.strip() for p in extra.split(",") if p.strip()]
                opencode_config = OpenCodeConfig(extra_dirs=dirs or None)
            except Exception:  # noqa: BLE001 — reuse is best-effort, never fatal
                opencode_config = None
        self._opencode_config = opencode_config
        if self._litellm_base_url:
            os.environ.setdefault("LITELLM_BASE_URL", self._litellm_base_url)
        if self._litellm_api_key:
            os.environ.setdefault("LITELLM_API_KEY", self._litellm_api_key)

    # ------------------------------------------------------------------
    # Model name resolution
    # ------------------------------------------------------------------
    def _role_model(self, role: str) -> str:
        mapping = {
            "intent": self._settings.model_intent,
            "fast_reply": self._settings.model_fast_reply,
            "reasoning": self._settings.model_reasoning,
            "narration": self._settings.model_narration,
            "summarization": self._settings.model_summarization,
            "vision": self._settings.model_vision,
            "embeddings": self._settings.model_embeddings,
        }
        model = mapping.get(role, self._settings.model_fast_reply)
        # Fallback to Nebius models if env has them
        if not model:
            if role in ("intent", "fast_reply", "summarization"):
                model = "meta-llama/Llama-3.3-70B-Instruct"
            elif role == "reasoning":
                model = "deepseek-ai/DeepSeek-V3"
            elif role == "embeddings":
                model = "BAAI/bge-en-icl"
            elif role == "vision":
                model = "Qwen/Qwen2-VL-72B-Instruct"
            elif role == "narration":
                model = "meta-llama/Llama-3.3-70B-Instruct"
        if not model:
            model = "meta-llama/Llama-3.3-70B-Instruct"
        return model

    def _resolve_model(self, role: str) -> Optional[ResolvedModel]:
        model = self._role_model(role)
        if not model:
            return None

        if self._litellm_base_url:
            return ResolvedModel(
                role=role,
                model=model,
                api_key=self._litellm_api_key or None,
                api_base=self._litellm_base_url,
                via_proxy=True,
            )

        model_id = self._normalize_direct_model(model)
        provider = model_id.split("/", 1)[0] if "/" in model_id else ""
        api_key, api_base = self._provider_credentials(provider)
        if provider and not api_key:
            logger.warning("ModelRouter role=%s provider=%s has no configured API key", role, provider)
            return None
        return ResolvedModel(
            role=role,
            model=model_id,
            api_key=api_key,
            api_base=api_base,
            via_proxy=False,
        )

    def _normalize_direct_model(self, model: str) -> str:
        """Normalize bare model IDs for direct Nebius/OpenAI use."""
        if self._has_supported_provider_prefix(model):
            return model
        if self._looks_like_openai_model(model):
            return f"openai/{model}"
        if self._settings.nebius_api_key:
            return f"nebius/{model}"
        if self._settings.openai_api_key:
            return f"openai/{model}"
        return model

    def _provider_credentials(self, provider: str) -> tuple[Optional[str], Optional[str]]:
        # CENTRI_* env keys always win (Decision 5: env > OpenCode auth).
        if provider == "openai":
            api_key = self._settings.openai_api_key or self._opencode_key("openai")
            return api_key or None, self._settings.openai_base_url or None
        if provider == "nebius":
            api_key = self._settings.nebius_api_key or self._opencode_key("nebius")
            return api_key or None, self._settings.nebius_base_url or None
        # Other providers: no env path here, so OpenCode auth is the only source.
        return self._opencode_key(provider) or None, None

    def _opencode_key(self, provider: str) -> Optional[str]:
        """OpenCode-auth fallback for a provider key; honest None when absent."""
        cfg = self._opencode_config
        if cfg is None:
            return None
        try:
            return cfg.resolve_provider_key(provider)
        except Exception:  # noqa: BLE001 — fallback must never break resolution
            return None

    def _has_supported_provider_prefix(self, model: str) -> bool:
        return model.startswith(("openai/", "nebius/"))

    def _looks_like_openai_model(self, model: str) -> bool:
        prefixes = ("gpt-", "o1", "o3", "o4", "text-embedding-", "omni-")
        return model.startswith(prefixes)

    def role_models(self) -> Dict[str, Dict[str, Any]]:
        roles = ("intent", "fast_reply", "reasoning", "narration", "summarization", "vision", "embeddings")
        result: Dict[str, Dict[str, Any]] = {}
        for role in roles:
            resolved = self._resolve_model(role)
            if resolved is None:
                result[role] = {"configured": False}
                continue
            result[role] = {
                "configured": True,
                "model": resolved.model,
                "via_proxy": resolved.via_proxy,
                "api_base": resolved.api_base,
            }
        return result

    # ------------------------------------------------------------------
    # Low-level completion
    # ------------------------------------------------------------------
    def _call(
        self, role: str, messages: List[Dict[str, str]], **kwargs: Any
    ) -> Optional[str]:
        if litellm is None:
            logger.warning("litellm not installed; model call skipped for role=%s", role)
            return None
        resolved = self._resolve_model(role)
        if resolved is None:
            logger.warning("Model not configured for role: %s", role)
            return None
        try:
            response = litellm.completion(
                model=resolved.model,
                messages=messages,
                api_key=resolved.api_key,
                api_base=resolved.api_base,
                max_tokens=kwargs.get("max_tokens", 4096),
                temperature=kwargs.get("temperature", 0.5),
            )
            content = response.choices[0].message.content  # type: ignore[union-attr]
            return content.strip() if content else None
        except Exception as exc:
            logger.warning("ModelRouter %s call failed: %s", role, exc)
            return None

    # ------------------------------------------------------------------
    # Intent classification (fast, deterministic first, then LLM)
    # ------------------------------------------------------------------
    def classify_intent(self, text: str, context: Optional[str] = None) -> str:
        lowered = text.strip().lower()
        # Fast deterministic patterns
        if any(k in lowered for k in ("status", "what's going on", "what is happening")):
            return "status"
        if any(k in lowered for k in ("tell it to", "steer", "send message to")):
            return "steering"
        if any(k in lowered for k in ("fix", "implement", "create", "add", "write", "build", "test", "run tests", "refactor", "solve")):
            return "coding_task"
        if any(k in lowered for k in ("approve", "reject", "cancel that", "yes do it", "no don't")):
            return "approval_response"
        if any(k in lowered for k in ("stop", "halt", "pause")):
            return "stop"
        # LLM fallback
        prompt = (
            f"Classify the user's intent from this utterance.\n\n"
            f"Utterance: \"{text}\"\n"
            f"Context: {context or 'none'}\n\n"
            "Intent is one of: status, steering, coding_task, approval_response, general_question, stop, unknown.\n"
            "Return only the intent word."
        )
        result = self._call("intent", [{"role": "user", "content": prompt}], max_tokens=20, temperature=0.0)
        if result:
            return result.lower().strip().split()[0]
        return "unknown"

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------
    def summarize_status(self, context: str) -> str:
        prompt = (
            "Summarize the current operational status for the user. Speak naturally, like a human assistant.\n\n"
            f"Status context:\n{context}\n\n"
            "Summary (1-3 sentences):"
        )
        result = self._call("summarization", [{"role": "user", "content": prompt}], max_tokens=300, temperature=0.6)
        return result or "I don't have a status update right now."

    # ------------------------------------------------------------------
    # Narration
    # ------------------------------------------------------------------
    def narrate(self, event_or_result: str, voice: bool = True) -> str:
        style = "Speak like a calm, attentive human assistant." if voice else ""
        prompt = (
            f"{style}\n\n"
            f"Turn this operational event into a brief, natural sentence to speak to the user:\n\n{event_or_result}\n\n"
            "Response (be concise):"
        )
        result = self._call("narration", [{"role": "user", "content": prompt}], max_tokens=200, temperature=0.7)
        return result or "OK."

    def narrate_fast(self, text: str) -> str:
        """Very cheap narration — template-based where possible.

        Use this for common confirmation messages to avoid LLM token cost.
        """
        lowered = text.lower()
        if "started" in lowered:
            return "Started working on that."
        if "completed" in lowered:
            return "All done."
        if "failed" in lowered:
            return "Something went wrong."
        if "approval" in lowered:
            return "I need your approval for this."
        return self.narrate(text, voice=True)

    # ------------------------------------------------------------------
    # Reasoning / structured output
    # ------------------------------------------------------------------
    def reason(self, prompt: str, output_schema: Optional[Dict[str, Any]] = None) -> Any:
        messages = [{"role": "user", "content": prompt}]
        if output_schema:
            # Future: use structured completions / json mode
            result = self._call("reasoning", messages, max_tokens=2000, temperature=0.3)
            return result
        return self._call("reasoning", messages, max_tokens=2000, temperature=0.3)

    # ------------------------------------------------------------------
    # Embeddings with LRU caching
    # ------------------------------------------------------------------
    def embed(self, texts: List[str], model: Optional[str] = None) -> Optional[List[List[float]]]:
        if litellm is None:
            logger.warning("litellm not installed; embed skipped")
            return None
        resolved = self._resolve_model("embeddings")
        # An explicit model wins over the role default so a provider configured
        # purely via CENTRI_EMBEDDING_MODEL (no MODEL_EMBEDDINGS role) still
        # resolves: reuse the role's transport (proxy/key/base) but swap the model.
        if model:
            if resolved is None:
                resolved = self._resolve_model("fast_reply")
            if resolved is not None:
                resolved = ResolvedModel(
                    role="embeddings",
                    model=self._normalize_direct_model(model) if not resolved.via_proxy else model,
                    api_key=resolved.api_key,
                    api_base=resolved.api_base,
                    via_proxy=resolved.via_proxy,
                )
        if resolved is None:
            logger.warning("Embedding model not configured")
            return None

        # Cache hit / miss split
        results_by_text: Dict[str, Optional[List[float]]] = {}
        texts_to_fetch: List[str] = []
        now = time.time()

        _expire_old_embed_cache(now)

        for text in texts:
            key = hashlib.sha256(text.encode()).hexdigest()
            cached = _EMBED_CACHE.get(key)
            if cached:
                results_by_text[text] = cached
            else:
                texts_to_fetch.append(text)

        if texts_to_fetch:
            try:
                response = litellm.embedding(
                    model=resolved.model,
                    input=texts_to_fetch,
                    api_key=resolved.api_key,
                    api_base=resolved.api_base,
                )
                fetched: List[List[float]] = [item["embedding"] for item in response["data"]]  # type: ignore[index]
                for t, emb in zip(texts_to_fetch, fetched):
                    key = hashlib.sha256(t.encode()).hexdigest()
                    _EMBED_CACHE[key] = emb
                for t, emb in zip(texts_to_fetch, fetched):
                    results_by_text[t] = emb
            except Exception as exc:
                logger.warning("Embedding call failed: %s", exc)
                # Fill missing with None
                for t in texts_to_fetch:
                    if t not in results_by_text:
                        results_by_text[t] = None

        return [results_by_text[t] for t in texts]


def _expire_old_embed_cache(now: float) -> None:
    """Remove entries older than _EMBED_LIFETIME_SECONDS."""
    stale = [k for k, v in _EMBED_CACHE.items() if now - getattr(v, "__ts", now) > _EMBED_LIFETIME_SECONDS]
    for k in stale:
        del _EMBED_CACHE[k]
