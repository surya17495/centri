"""CENTRI redaction tests — prove secrets are scrubbed before persistence.

Events are the source of truth and are written to an append-only ledger, so a
secret that reaches a payload would live forever. These tests prove the write
path (db.append_event) and the fan-out path (event_bus.publish) both scrub.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.event_bus import EventBus
from centri.redaction import REDACTED, redact_jsonable, redact_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestRedactPrimitives:
    def test_sensitive_key_values_redacted(self):
        out = redact_jsonable({"api_key": "sk-secretvalue123456", "name": "ok"})
        assert out["api_key"] == REDACTED
        assert out["name"] == "ok"

    def test_nested_sensitive_keys(self):
        out = redact_jsonable({"outer": {"openai_api_key": "abc123", "fine": 1}})
        assert out["outer"]["openai_api_key"] == REDACTED
        assert out["outer"]["fine"] == 1

    def test_known_token_in_freetext(self):
        out = redact_text("here is ghp_0123456789abcdefABCD my github token")
        assert "ghp_0123456789" not in out
        assert "***" in out

    def test_bearer_header(self):
        out = redact_text("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in out

    def test_assignment_pattern(self):
        out = redact_text("OPENAI_API_KEY=sk-livesecret123456")
        assert "sk-livesecret123456" not in out

    def test_private_key_block(self):
        text = "-----BEGIN PRIVATE KEY-----\nMIIBVgIBADAN\n-----END PRIVATE KEY-----"
        out = redact_text(text)
        assert "MIIBVgIBADAN" not in out

    def test_empty_sensitive_value_preserved(self):
        out = redact_jsonable({"token": ""})
        assert out["token"] == ""


class TestRedactionWiredIntoLedger:
    async def test_append_event_scrubs_payload(self):
        tmpdir = tempfile.mkdtemp()
        db = Database(Path(tmpdir) / "state.db")
        try:
            await db.append_event(
                "e1",
                "test.secret",
                "source",
                _now(),
                payload={"api_key": "sk-shouldnotpersist123", "note": "AWS_SECRET=topsecretvalue99"},
            )
            rows = await db.recent_events(limit=1)
            payload = json.loads(rows[0]["payload_json"])
            assert payload["api_key"] == REDACTED
            assert "topsecretvalue99" not in payload["note"]
        finally:
            await db.close()


class TestRedactionWiredIntoEventBus:
    async def test_publish_scrubs_before_fanout(self):
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish({"type": "user.utterance", "payload": {"password": "hunter2"}})
        event = await q.get()
        assert event["payload"]["password"] == REDACTED
