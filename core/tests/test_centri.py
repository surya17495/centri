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

    async def test_role_models_entries_are_typed_objects(self):
        # The shell renders role_models values as objects ({configured, model, ...});
        # a bare string here would mean the API contract drifted from the UI types.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            payload = client.get("/status").json()
            for role, info in payload["role_models"].items():
                assert isinstance(info, dict), f"role_models[{role}] must be an object"
                assert "configured" in info

    async def test_cors_allows_shell_origins(self):
        # The shell (vite dev server / Tauri webview) calls the REST API
        # cross-origin; without CORS headers every browser fetch is blocked.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.get("/status", headers={"Origin": "http://localhost:1420"})
            assert r.headers.get("access-control-allow-origin") == "http://localhost:1420"
            pre = client.options(
                "/utterance",
                headers={
                    "Origin": "http://127.0.0.1:1420",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert pre.status_code == 200
            assert pre.headers.get("access-control-allow-origin") == "http://127.0.0.1:1420"

    async def test_events_endpoint_matches_live_envelope(self):
        # The shell hydrates history from /events expecting the same shape as
        # live WebSocket frames: parsed `payload` dict + top-level mirrors,
        # never a raw payload_json string.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            client.post("/utterance", json={"text": "hello centri", "user_id": "t"})
            events = client.get("/events?limit=20").json()["events"]
            assert events, "utterance should have produced events"
            for ev in events:
                assert "payload_json" not in ev
                assert isinstance(ev.get("payload"), dict)
                assert ev.get("id"), "every persisted event must carry a stable id"
            utterances = [e for e in events if e["type"] == "user.utterance"]
            assert utterances and utterances[0].get("text") == "hello centri"

    async def test_approval_resolved_event_carries_decision(self, monkeypatch):
        # The shell resolves approval cards from payload.decision / status;
        # an approval.resolved frame without them renders 'Rejected' even
        # when the user clicked Approve.
        import centri.config as config_module
        from centri.config import Settings
        monkeypatch.setattr(config_module, "_settings", Settings(autonomy_level="supervised"))
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.post("/utterance", json={"text": "please refactor the auth module", "user_id": "t"})
            data = r.json()
            assert data["response_type"] == "approval_requested"
            approval_id = data["data"]["approval_id"]
            with client.websocket_connect("/events/stream") as ws:
                res = client.post(f"/approvals/{approval_id}/approve")
                assert res.json()["status"] == "approved"
                for _ in range(50):
                    ev = ws.receive_json()
                    if ev.get("type") == "approval.resolved" and ev.get("approval_id") == approval_id:
                        assert ev.get("status") == "approved"
                        assert ev.get("payload", {}).get("decision") == "approved"
                        assert ev.get("id", "").startswith("evt-")
                        break
                else:
                    raise AssertionError("approval.resolved event not observed")


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
