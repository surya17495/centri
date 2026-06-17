"""Test isolation: prevent .env credential leakage into test runs.

These tests monkeypatch ``config._settings`` with fake credentials, but
``ModelRouter._provider_credentials()`` reads ``os.getenv()`` first (line 149).
When a developer has a real .env file (for running the live service),
``Settings.from_env()`` calls ``load_dotenv()`` which injects real API keys
into ``os.environ`` — shadowing the test-supplied values.

Fix: pre-set every credential env var to an empty string. Because
``load_dotenv()`` defaults to ``override=False`` (uses ``setdefault``), it
won't clobber the pre-set empties. Then reset the cached settings singleton so
the next ``get_settings()`` picks up the sanitized environment.

For users cloning the repo without a .env file, this is a no-op — the vars
are already empty.
"""
import pytest

# Every env var that _provider_credentials() or Settings.from_env() reads,
# which could leak real credentials from a developer's .env file.
_CREDENTIAL_VARS = [
    "CENTRI_AUTH_TOKEN",
    "LITELLM_API_KEY",
    "NEBIUS_API_KEY",
    "OPENAI_API_KEY",
    "CENTRI_LITELLM_API_KEY",
    "CENTRI_NEBIUS_API_KEY",
    "CENTRI_OPENAI_API_KEY",
    "NEBIUS_BASE_URL",
    "OPENAI_BASE_URL",
    "CENTRI_NEBIUS_BASE_URL",
    "CENTRI_OPENAI_BASE_URL",
    "NEBIUS_API_BASE",
    "OPENAI_API_BASE",
    "CENTRI_LETTA_API_KEY",
    "CENTRI_COMPOSIO_API_KEY",
    "COMPOSIO_API_KEY",
    "CENTRI_CONSOLIDATION_API_KEY",
    # Embedding config — not secrets, but must be cleared so tests that assert
    # "embeddings unavailable by default" pass even when the developer has
    # fastembed installed and CENTRI_EMBEDDING_LOCAL_MODEL in .env.
    "CENTRI_EMBEDDING_LOCAL_MODEL",
    "CENTRI_EMBEDDING_MODEL",
    "CENTRI_EMBEDDING_ENABLED",
]


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch: pytest.MonkeyPatch):
    """Prevent real .env credentials from leaking into tests.

    Sets all credential env vars to empty string so ``load_dotenv()`` (which
    uses ``setdefault`` and won't override existing keys) leaves them blank.
    Then resets the cached ``_settings`` singleton so the next
    ``get_settings()`` call rebuilds from the sanitized environment.
    """
    for var in _CREDENTIAL_VARS:
        monkeypatch.setenv(var, "")

    # Reset cached settings so get_settings() re-reads from the now-clean env.
    try:
        from centri import config
        monkeypatch.setattr(config, "_settings", None)
    except ImportError:
        # centri isn't importable yet (e.g. path not set up); tests that need
        # it set up their own sys.path.
        pass
