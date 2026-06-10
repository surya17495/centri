"""CENTRI configuration — one Settings dataclass, env-driven."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """CENTRI configuration — all env placeholders, user fills .env (BYOK)."""

    # Server
    core_host: str = "127.0.0.1"
    core_port: int = 8760
    core_token: str = "change-me"

    # Database
    db_path: Path = field(default_factory=lambda: Path.home() / ".centri" / "state.db")

    # LiteLLM / model gateway (BYOK: point at your own proxy or provider)
    litellm_base_url: str = ""
    litellm_api_key: str = ""

    # Model role names (model IDs managed by LiteLLM or direct provider)
    model_intent: str = "meta-llama/Llama-3.3-70B-Instruct"
    model_fast_reply: str = "meta-llama/Llama-3.3-70B-Instruct"
    model_reasoning: str = "deepseek-ai/DeepSeek-V3"
    model_narration: str = "meta-llama/Llama-3.3-70B-Instruct"
    model_summarization: str = "meta-llama/Llama-3.3-70B-Instruct"
    model_vision: str = ""
    model_embeddings: str = ""

    # Nebius (OpenAI-compatible backend, used through LiteLLM)
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.studio.nebius.ai/v1"

    # OpenAI (direct provider path when not using LiteLLM proxy)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # Letta (optional semantic memory backend; CENTRI runs without it)
    letta_url: str = ""
    letta_api_key: str = ""
    letta_agent_id: str = "centri-main"

    # OpenCode hand (CLI-based, no sidecar needed)
    opencode_cli: str = "opencode"

    # Hands
    enabled_hands: List[str] = field(default_factory=lambda: ["opencode"])

    # Autonomy
    autonomy_level: str = "autonomous_local"
    auto_commit: bool = True
    auto_push: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        db_path_str = os.getenv("CENTRI_DB_PATH", "~/.centri/state.db")
        db_path = Path(db_path_str).expanduser()

        hands_raw = os.getenv("CENTRI_ENABLED_HANDS", "opencode")
        enabled_hands = [h.strip() for h in hands_raw.split(",") if h.strip()]

        return cls(
            core_host=os.getenv("CENTRI_CORE_HOST", "127.0.0.1"),
            core_port=int(os.getenv("CENTRI_CORE_PORT", "8760")),
            core_token=os.getenv("CENTRI_CORE_TOKEN", "change-me"),
            db_path=db_path,
            litellm_base_url=os.getenv("LITELLM_BASE_URL", ""),
            litellm_api_key=os.getenv("LITELLM_API_KEY", ""),
            model_intent=os.getenv("MODEL_INTENT", "meta-llama/Llama-3.3-70B-Instruct"),
            model_fast_reply=os.getenv("MODEL_FAST_REPLY", "meta-llama/Llama-3.3-70B-Instruct"),
            model_reasoning=os.getenv("MODEL_REASONING", "deepseek-ai/DeepSeek-V3"),
            model_narration=os.getenv("MODEL_NARRATION", "meta-llama/Llama-3.3-70B-Instruct"),
            model_summarization=os.getenv("MODEL_SUMMARIZATION", "meta-llama/Llama-3.3-70B-Instruct"),
            model_vision=os.getenv("MODEL_VISION", ""),
            model_embeddings=os.getenv("MODEL_EMBEDDINGS", ""),
            nebius_api_key=os.getenv("NEBIUS_API_KEY", ""),
            nebius_base_url=os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            letta_url=os.getenv("CENTRI_LETTA_URL", ""),
            letta_api_key=os.getenv("CENTRI_LETTA_API_KEY", ""),
            letta_agent_id=os.getenv("CENTRI_LETTA_AGENT_ID", "centri-main"),
            opencode_cli=os.getenv("OPENCODE_CLI", "opencode"),
            enabled_hands=enabled_hands,
            autonomy_level=os.getenv("CENTRI_AUTONOMY_LEVEL", "autonomous_local"),
            auto_commit=os.getenv("CENTRI_AUTO_COMMIT", "true").lower() == "true",
            auto_push=os.getenv("CENTRI_AUTO_PUSH", "false").lower() == "true",
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
