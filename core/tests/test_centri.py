"""CENTRI test suite — validates core components without legacy dependencies."""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def tmp_db():
    """Yield a temporary Database, then close it."""
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    yield db
    await db.close()


class TestDatabase:
    async def test_append_and_read(self, tmp_db: Database):
        await tmp_db.append_event("e1", "test.type", "source", _now(), payload={"x": 1})
        recent = await tmp_db.recent_events(limit=1)
        assert len(recent) == 1
        assert recent[0]["payload_json"] == '{"x": 1}'

    async def test_thread_crud(self, tmp_db: Database):
        await tmp_db.create_thread("t1", "My goal", "do it", created_at=_now(), updated_at=_now())
        t = await tmp_db.get_thread("t1")
        assert t and t["title"] == "My goal"

    async def test_task_no_duplication(self, tmp_db: Database):
        await tmp_db.create_task("tk1", "t1", "desc", created_at=_now(), updated_at=_now())
        tasks = await tmp_db.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["id"] == "tk1"


class TestPermissions:
    def test_risk_classification(self):
        from centri.permissions import Permissions
        perm = Permissions(Settings())
        assert perm.classify_action("coding.status") == "low"
        assert perm.classify_action("coding.start_task") == "medium"
        assert perm.classify_action("coding.execute_unsafe") == "high"
        assert perm.classify_action("sudo.execute") == "blocked"


class TestCoordinatorStatus:
    async def test_status_endpoint(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200 and r.json()["status"] == "ok"

    async def test_status_exposes_role_models(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.get("/status")
            assert r.status_code == 200
            payload = r.json()
            assert "role_models" in payload
            assert "intent" in payload["role_models"]
            assert "reasoning" in payload["role_models"]


class TestHands:
    async def test_opencode_capabilities(self):
        from centri.hands import Hands
        from centri.db import Database
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        hands = Hands(Settings(enabled_hands=["opencode"]), db)
        caps = await hands.list_capabilities()
        names = [c.name for c in caps]
        assert "coding.start_task" in names
        await db.close()
