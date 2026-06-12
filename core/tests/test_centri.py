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


class TestThreads:
    """Phase 3b.2: chat threads scope the timeline; memory stays global."""

    async def test_utterance_creates_default_thread_and_tags_events(self):
        # No thread_id supplied -> events land in the catch-all chat thread so
        # /events?thread_id= can still partition them.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            client.post("/utterance", json={"text": "hello", "user_id": "t"})
            events = client.get("/events?thread_id=th-default").json()["events"]
            utterances = [e for e in events if e["type"] == "user.utterance"]
            assert utterances, "default-thread utterance should be filterable"
            assert utterances[0]["thread_id"] == "th-default"

    async def test_explicit_thread_id_is_accepted_and_created_on_first_use(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            client.post(
                "/utterance",
                json={"text": "scoped hi", "user_id": "t", "thread_id": "th-explicit"},
            )
            threads = {t["id"] for t in client.get("/threads").json()["threads"]}
            assert "th-explicit" in threads, "first use must create the thread"
            ev = client.get("/events?thread_id=th-explicit").json()["events"]
            assert any(e.get("text") == "scoped hi" for e in ev)

    async def test_threads_have_disjoint_chat(self):
        # Acceptance: two threads, disjoint chat timelines.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            client.post("/utterance", json={"text": "in thread A", "thread_id": "th-A"})
            client.post("/utterance", json={"text": "in thread B", "thread_id": "th-B"})
            a_texts = [e.get("text") for e in client.get("/events?thread_id=th-A").json()["events"]]
            b_texts = [e.get("text") for e in client.get("/events?thread_id=th-B").json()["events"]]
            assert "in thread A" in a_texts and "in thread A" not in b_texts
            assert "in thread B" in b_texts and "in thread B" not in a_texts

    async def test_create_thread_endpoint(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            created = client.post("/threads", json={"title": "Planning"}).json()["thread"]
            assert created["id"].startswith("th-") and created["title"] == "Planning"
            ids = {t["id"] for t in client.get("/threads").json()["threads"]}
            assert created["id"] in ids


class TestIngest:
    """Phase 3b.3: POST /ingest/opencode tails an external opencode.db once,
    idempotently, into the spine."""

    def _make_db(self, path, rows):
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO message (id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    async def test_ingest_endpoint_idempotent(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        oc = Path(tempfile.mkdtemp()) / "opencode.db"
        # Unique source + id + a future-dated ts so the event is findable at the
        # top of the (shared dev DB) timeline regardless of prior pollution.
        import uuid
        src = f"ep-{uuid.uuid4().hex[:8]}"
        mid = f"m-{uuid.uuid4().hex[:8]}"
        # Timestamp = now so the event lands at the top of the (shared dev DB)
        # timeline without leaving a far-future artifact behind.
        self._make_db(oc, [
            (mid, "s1", "assistant", "ingested via endpoint", _now()),
        ])
        with TestClient(app) as client:
            first = client.post("/ingest/opencode", json={"db_path": str(oc), "source": src}).json()
            assert first["ingested"] == 1 and first["available"] is True
            # Re-run: idempotent, no new events.
            second = client.post("/ingest/opencode", json={"db_path": str(oc), "source": src}).json()
            assert second["ingested"] == 0
            events = client.get("/events?limit=20").json()["events"]
            ingested = [e for e in events if e["type"] == "ingest.opencode.message" and e["id"].endswith(mid)]
            assert len(ingested) == 1
            assert ingested[0]["payload"]["tool"] == "opencode"

    async def test_ingest_endpoint_requires_path(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.post("/ingest/opencode", json={})
            assert r.status_code == 400


class TestBootstrap:
    """Phase 3b.4: GET /ingest/discover reports found coding-agent stores;
    POST /ingest/bootstrap runs a one-time full import (idempotent) and emits
    progress events on the spine. Endpoint behavior is fixture-verified via an
    explicit source so the test never depends on the sandbox's real ~/.claude."""

    def _make_db(self, path, rows):
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO message (id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    async def test_discover_endpoint_shape(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            body = client.get("/ingest/discover").json()
            assert "sources" in body and "agents" in body
            assert "total_messages" in body and "available_count" in body
            # The three registry agents are reported (none disabled by default).
            assert set(body["agents"]) == {"opencode", "claude_code", "cursor"}

    async def test_bootstrap_explicit_source_idempotent(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        import uuid
        oc = Path(tempfile.mkdtemp()) / "opencode.db"
        mid = f"bm-{uuid.uuid4().hex[:8]}"
        self._make_db(oc, [(mid, "s1", "assistant", "bootstrap import", _now())])
        with TestClient(app) as client:
            first = client.post(
                "/ingest/bootstrap",
                json={"sources": [{"agent": "opencode", "path": str(oc),
                                   "source": f"bs-{mid}"}]},
            ).json()
            assert first["imported"] == 1
            # Idempotent: re-running imports nothing new.
            second = client.post(
                "/ingest/bootstrap",
                json={"sources": [{"agent": "opencode", "path": str(oc),
                                   "source": f"bs-{mid}"}]},
            ).json()
            assert second["imported"] == 0
            # Progress events landed on the spine.
            events = client.get("/events?limit=50").json()["events"]
            types = [e["type"] for e in events]
            assert "ingest.bootstrap.completed" in types


class TestSingleLlmConfig:
    """Phase 3b.5: OpenCode provider reuse + models.dev catalog endpoints."""

    async def test_providers_discovered_shape(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            body = client.get("/providers/discovered").json()
            assert body["available"] is True
            assert "providers" in body and "count" in body
            assert isinstance(body["providers"], list)

    async def test_discover_includes_opencode_providers(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            body = client.get("/ingest/discover").json()
            # The single-LLM-config surface is folded into discovery.
            assert "opencode_providers" in body
            assert isinstance(body["opencode_providers"], list)

    async def test_models_catalog_endpoint_is_not_a_hard_dependency(self):
        # Offline in the sandbox: models.dev is unreachable, so the endpoint must
        # answer honest-unavailable (or serve a warmed cache) — never error.
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.get("/models/catalog")
            assert r.status_code == 200
            body = r.json()
            assert "available" in body
            if not body["available"]:
                assert "reason" in body


class TestAuth:
    """Phase 3a: shared-secret bearer auth for VM deployment.

    Empty token (default) keeps local dev open; once CENTRI_AUTH_TOKEN is set
    every route except /health requires `Authorization: Bearer <token>` and
    the WebSocket requires ?token= (browsers cannot set WS headers).
    """

    def _secure(self, monkeypatch, **kw):
        import centri.config as config_module
        monkeypatch.setattr(
            config_module, "_settings", Settings(auth_token="s3cret", **kw)
        )

    async def test_rest_requires_token_when_configured(self, monkeypatch):
        self._secure(monkeypatch)
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            assert client.get("/status").status_code == 401
            assert (
                client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            ok = client.get("/status", headers={"Authorization": "Bearer s3cret"})
            assert ok.status_code == 200 and ok.json()["status"] == "ok"

    async def test_health_stays_public_for_probes(self, monkeypatch):
        self._secure(monkeypatch)
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    async def test_unauthorized_response_carries_cors_headers(self, monkeypatch):
        # If the 401 lacked CORS headers the browser would report an opaque
        # CORS failure instead of an auth failure — undebuggable from the shell.
        self._secure(monkeypatch)
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            r = client.get("/status", headers={"Origin": "http://localhost:1420"})
            assert r.status_code == 401
            assert r.headers.get("access-control-allow-origin") == "http://localhost:1420"

    async def test_websocket_requires_token(self, monkeypatch):
        self._secure(monkeypatch)
        import pytest
        from starlette.websockets import WebSocketDisconnect
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/events/stream") as ws:
                    ws.receive_json()
            # Query-param token (browser path) must be accepted.
            with client.websocket_connect("/events/stream?token=s3cret"):
                pass

    async def test_auth_disabled_by_default(self):
        from fastapi.testclient import TestClient
        from centri.app import app
        with TestClient(app) as client:
            assert client.get("/status").status_code == 200
            with client.websocket_connect("/events/stream"):
                pass


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

    async def test_opencode_transcript_event_keeps_full_output(self):
        """Phase 3b.1: _build_result records an untruncated hand.transcript."""
        from centri.hands.opencode import OpenCodeHand
        hand = OpenCodeHand(db=None)
        long_out = "line of detailed agent output\n" * 200  # ~5800 chars > 3000 excerpt
        result = hand._build_result(
            code=0, out=long_out, err="warn: something", description="build the feature",
            cwd="/tmp/proj", request_id="req-123",
        )
        transcripts = [e for e in result.events_to_record if e["type"] == "hand.transcript"]
        assert len(transcripts) == 1
        t = transcripts[0]
        assert t["text"] == long_out.strip()           # full, beyond the 3000-char artifact excerpt
        assert len(t["text"]) > 3000
        assert t["intent"] == "build the feature"
        assert t["stop_reason"] == "exit:0"
        assert t["stderr"] == "warn: something"
        fact = t["fact"]
        assert fact["topic"] == "delegated-session:req-123"  # no session_uid in plain output
        assert "build the feature" in fact["statement"]
        assert fact["tags"] == ["hand", "transcript", "opencode"]
        # The opencode.run event is still recorded alongside.
        assert any(e["type"] == "opencode.run" for e in result.events_to_record)

    async def test_opencode_transcript_empty_output_has_no_fact_hint(self):
        from centri.hands.opencode import OpenCodeHand
        hand = OpenCodeHand(db=None)
        result = hand._build_result(
            code=1, out="", err="boom", description="x", cwd=None, request_id="req-9",
        )
        t = [e for e in result.events_to_record if e["type"] == "hand.transcript"][0]
        assert t["text"] == ""
        assert "fact" not in t  # consolidation must never confabulate from nothing
