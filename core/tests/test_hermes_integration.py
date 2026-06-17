import json
import pytest
from fastapi.testclient import TestClient
from centri.app import app
from centri.config import get_settings

@pytest.mark.asyncio
async def test_import_accepts_hermes_event_types():
    # Load settings and auth token
    import time
    token = get_settings().auth_token
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ts = str(int(time.time() * 1000))

    with TestClient(app) as client:
        # Import hermes.user.message
        user_env = {
            "type": "hermes.user.message",
            "source": "hermes_turn_sync",
            "session_id": "test-session",
            "thread_id": "test-session",
            "payload": {
                "event_uid": f"test-uid-1-{ts}",
                "role": "user",
                "text": "test user message text",
                "thread_id": "test-session"
            }
        }
        # Import hermes.assistant.message
        asst_env = {
            "type": "hermes.assistant.message",
            "source": "hermes_turn_sync",
            "session_id": "test-session",
            "thread_id": "test-session",
            "payload": {
                "event_uid": f"test-uid-2-{ts}",
                "role": "assistant",
                "text": "test assistant message text",
                "thread_id": "test-session"
            }
        }
        # Import hermes.tool.result
        tool_env = {
            "type": "hermes.tool.result",
            "source": "hermes_turn_sync",
            "session_id": "test-session",
            "thread_id": "test-session",
            "payload": {
                "event_uid": f"test-uid-3-{ts}",
                "role": "tool",
                "text": "test tool result output text",
                "thread_id": "test-session"
            }
        }
        # Import hermes.memory.write
        write_env = {
            "type": "hermes.memory.write",
            "source": "hermes_memory_write",
            "session_id": "test-session",
            "thread_id": "test-session",
            "payload": {
                "event_uid": f"test-uid-4-{ts}",
                "role": "system",
                "text": "test memory write text",
                "action": "upsert",
                "target": "mem_facts"
            }
        }
        
        batch = {"events": [user_env, asst_env, tool_env, write_env]}
        r = client.post("/events/import", json=batch, headers=headers).json()
        print("IMPORT RESPONSE:", r)
        assert r["accepted"] == 4
        assert r["duplicates"] == 0
        
        # Verify they land on the ledger/spine
        events_resp = client.get("/events", params={"thread_id": "test-session"}, headers=headers).json()
        events = events_resp.get("events", [])
        
        types = [e.get("type") for e in events]
        assert "hermes.user.message" in types
        assert "hermes.assistant.message" in types
        assert "hermes.tool.result" in types
        assert "hermes.memory.write" in types
