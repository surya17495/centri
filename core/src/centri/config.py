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
    # Shared-secret bearer token. Empty (default) = auth disabled for local dev;
    # set CENTRI_AUTH_TOKEN when exposing the core beyond localhost (Phase 3a).
    auth_token: str = ""
    cors_origins: tuple[str, ...] = (
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "https://tauri.localhost",
    )

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
    # Models a real Letta server uses for its agent (OpenAI-compatible endpoints).
    letta_model: str = "moonshotai/Kimi-K2.6"
    letta_model_endpoint: str = "http://127.0.0.1:8901/v1"
    letta_embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    letta_embedding_endpoint: str = "http://127.0.0.1:8901/v1"
    letta_embedding_dim: int = 4096

    # OpenCode hand (CLI-based, no sidecar needed)
    opencode_cli: str = "opencode"

    # OpenCode ingestion adapter (3b.3): path to an external opencode.db to tail
    # into the spine on each scheduler tick. Empty (default) = no ambient tail;
    # the POST /ingest/opencode endpoint still works for one-shot ingests.
    opencode_ingest_db: str = ""

    # Ingestion adapter registry (3b.4): per-agent path overrides probed *in
    # addition to* the platform defaults (comma-separated), and a comma-separated
    # list of agents to disable (privacy / opt-out). Discovery + bootstrap probe
    # well-known default ~/.claude, Cursor state.vscdb, opencode.db locations, so
    # these are only needed when a store lives somewhere unusual.
    ingest_opencode_paths: str = ""
    ingest_claude_code_paths: str = ""
    ingest_cursor_paths: str = ""
    ingest_disabled_agents: str = ""

    # ACP hand — command that launches an Agent Client Protocol agent over stdio.
    # Every hand is uniformly "an ACP agent identified by a launch command";
    # Cursor / Claude Code / etc. are just different values here, not new code.
    # The canonical default coding hand is OpenCode-over-ACP, so acp_command
    # defaults to "opencode acp" explicitly (ROADMAP "Decisions"). The native
    # OpenCode subprocess hand below is a degraded fallback, not the default.
    acp_command: str = "opencode acp"
    acp_opencode_command: str = "opencode acp"

    # Hands. enabled_hands lists which hands to register; hand_priority is the
    # preference order used by the router (a healthy higher-priority hand wins).
    # "acp" first => OpenCode-over-ACP is the default; "opencode" (native
    # subprocess) is the degraded fallback retained for when no ACP peer is up.
    enabled_hands: List[str] = field(default_factory=lambda: ["acp", "opencode"])
    hand_priority: List[str] = field(default_factory=lambda: ["acp", "opencode"])

    # Autonomy
    autonomy_level: str = "autonomous_local"
    auto_commit: bool = True
    auto_push: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        db_path_str = os.getenv("CENTRI_DB_PATH", "~/.centri/state.db")
        db_path = Path(db_path_str).expanduser()

        hands_raw = os.getenv("CENTRI_ENABLED_HANDS", "acp,opencode")
        enabled_hands = [h.strip() for h in hands_raw.split(",") if h.strip()]
        priority_raw = os.getenv("CENTRI_HAND_PRIORITY", "acp,opencode")
        hand_priority = [h.strip() for h in priority_raw.split(",") if h.strip()]

        cors_raw = os.getenv("CENTRI_CORS_ORIGINS", "")
        cors_origins = (
            tuple(o.strip() for o in cors_raw.split(",") if o.strip())
            if cors_raw
            else cls.cors_origins
        )

        return cls(
            core_host=os.getenv("CENTRI_CORE_HOST", "127.0.0.1"),
            core_port=int(os.getenv("CENTRI_CORE_PORT", "8760")),
            core_token=os.getenv("CENTRI_CORE_TOKEN", "change-me"),
            auth_token=os.getenv("CENTRI_AUTH_TOKEN", ""),
            cors_origins=cors_origins,
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
            letta_model=os.getenv("CENTRI_LETTA_MODEL", "moonshotai/Kimi-K2.6"),
            letta_model_endpoint=os.getenv("CENTRI_LETTA_MODEL_ENDPOINT", "http://127.0.0.1:8901/v1"),
            letta_embedding_model=os.getenv("CENTRI_LETTA_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
            letta_embedding_endpoint=os.getenv("CENTRI_LETTA_EMBEDDING_ENDPOINT", "http://127.0.0.1:8901/v1"),
            letta_embedding_dim=int(os.getenv("CENTRI_LETTA_EMBEDDING_DIM", "4096")),
            opencode_cli=os.getenv("OPENCODE_CLI", "opencode"),
            opencode_ingest_db=os.getenv("CENTRI_OPENCODE_INGEST_DB", ""),
            ingest_opencode_paths=os.getenv("CENTRI_INGEST_OPENCODE_PATHS", ""),
            ingest_claude_code_paths=os.getenv("CENTRI_INGEST_CLAUDE_CODE_PATHS", ""),
            ingest_cursor_paths=os.getenv("CENTRI_INGEST_CURSOR_PATHS", ""),
            ingest_disabled_agents=os.getenv("CENTRI_INGEST_DISABLED_AGENTS", ""),
            acp_command=os.getenv("CENTRI_ACP_COMMAND", "opencode acp"),
            acp_opencode_command=os.getenv("CENTRI_ACP_OPENCODE_COMMAND", "opencode acp"),
            enabled_hands=enabled_hands,
            hand_priority=hand_priority,
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
