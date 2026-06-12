import sys
import tempfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.config import get_settings, update_settings
from centri.runtime import Runtime


@pytest.fixture
def clean_settings():
    # Reset settings before/after test
    from centri import config
    old_settings = config._settings
    config._settings = None
    yield
    config._settings = old_settings


async def test_settings_database_override(clean_settings):
    # Initialize runtime config
    settings = get_settings()
    # Should start with the default model
    assert settings.model_reasoning in ("deepseek-ai/DeepSeek-V3", "pioneer/claude-opus-4-8")

    # Create temporary database
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")

    # Set setting override in database
    await db.set_setting_override("model_reasoning", "pioneer/claude-opus-4-8")
    
    # Verify we can fetch it
    val = await db.get_setting_override("model_reasoning")
    assert val == "pioneer/claude-opus-4-8"

    # Verify get_all_setting_overrides
    overrides = await db.get_all_setting_overrides()
    assert overrides == {"model_reasoning": "pioneer/claude-opus-4-8"}

    # Mock settings update
    update_settings(overrides)
    assert get_settings().model_reasoning == "pioneer/claude-opus-4-8"

    await db.close()


async def test_settings_api_endpoints(clean_settings):
    from fastapi.testclient import TestClient
    from centri.app import app
    
    # Reset settings singleton to default
    from centri import config
    config._settings = None

    with TestClient(app) as client:
        # GET overrides
        r = client.get("/settings/overrides")
        assert r.status_code == 200
        assert "overrides" in r.json()

        # POST overrides
        r = client.post("/settings/overrides", json={"settings": {"model_reasoning": "pioneer/claude-opus-4-8"}})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["overrides"]["model_reasoning"] == "pioneer/claude-opus-4-8"

        # Verify change is reflected in memory settings
        assert get_settings().model_reasoning == "pioneer/claude-opus-4-8"

        # POST invalid setting key should fail
        r = client.post("/settings/overrides", json={"settings": {"invalid_key_xxx": "val"}})
        assert r.status_code == 400
