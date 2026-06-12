"""A scripted fake ACP agent for testing CENTRI's ACP client hand.

Speaks JSON-RPC 2.0 over stdio (newline-delimited). It is deliberately minimal
and deterministic: it answers initialize/session/new, then on session/prompt it
streams a couple of session/update notifications, optionally requests permission,
and returns a stopReason.

Behavior is controlled by env vars so one script covers every test scenario:
  ACP_FAKE_MODE = "stream"     -> stream chunks + tool calls, end_turn
                  "permission" -> request permission mid-turn, then end_turn
                  "cancel"     -> wait for session/cancel, return cancelled
"""

import json
import os
import sys

MODE = os.environ.get("ACP_FAKE_MODE", "stream")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def reply(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def request(req_id, method, params):
    send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})


def read_msg():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return read_msg()
    return json.loads(line)


def stream_updates(session_id):
    if os.environ.get("ACP_FAKE_REALISH"):
        # Update kinds the *real* opencode binary emits that the original fake
        # never did: a thought chunk (reasoning) and an available-commands
        # notification. The hand must trace the thought and ignore the commands
        # update without crashing. See the real-binary probe in test_acp_hand.py.
        notify("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [{"name": "init", "description": "guided setup"}],
            },
        })
        notify("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "messageId": "t1",
                "content": {"type": "text", "text": "The user wants me to work on it."},
            },
        })
    notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m1",
            "content": {"type": "text", "text": "Working on it."},
        },
    })
    notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_1",
            "title": "edit main.py",
            "kind": "edit",
            "status": "pending",
        },
    })
    notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "patched"}}],
        },
    })
    notify("session/update", {
        "sessionId": session_id,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m2",
            "content": {"type": "text", "text": " Done."},
        },
    })
    if os.environ.get("ACP_FAKE_REALISH"):
        notify("session/update", {
            "sessionId": session_id,
            "update": {"sessionUpdate": "usage_update", "used": 8805, "size": 200000},
        })
    if os.environ.get("ACP_FAKE_LONG"):
        # A deliberately long chunk (>240 chars) so tests can prove the
        # transcript event keeps full text while UI summaries stay truncated.
        notify("session/update", {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m_long",
                "content": {"type": "text", "text": " " + ("Detailed transcript sentence with specifics. " * 12).strip()},
            },
        })


def main():
    session_id = "sess-fake-1"
    next_outgoing_id = 1000
    while True:
        msg = read_msg()
        if msg is None:
            return
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": False, "promptCapabilities": {}},
                "agentInfo": {"name": "fake-acp-agent", "version": "0.0.1"},
                "authMethods": [],
            })
        elif method == "session/new":
            reply(msg_id, {"sessionId": session_id})
        elif method == "session/prompt":
            stream_updates(session_id)
            if MODE == "permission":
                # Ask the client for permission and wait for the response.
                request(next_outgoing_id, "session/request_permission", {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "call_danger", "title": "rm -rf build", "kind": "execute"},
                    "options": [
                        {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                    ],
                })
                perm_id = next_outgoing_id
                next_outgoing_id += 1
                # Read messages until we get the permission response.
                outcome = None
                while True:
                    resp = read_msg()
                    if resp is None:
                        return
                    if resp.get("id") == perm_id and "result" in resp:
                        outcome = resp["result"].get("outcome", {})
                        break
                    if resp.get("method") == "session/cancel":
                        reply(msg_id, {"stopReason": "cancelled"})
                        break
                else:
                    outcome = None
                if outcome is not None:
                    notify("session/update", {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": "m3",
                            "content": {"type": "text", "text": f" permission={json.dumps(outcome)}"},
                        },
                    })
                    reply(msg_id, {"stopReason": "end_turn"})
            elif MODE == "cancel":
                # Wait for a cancel notification, then report cancelled.
                while True:
                    resp = read_msg()
                    if resp is None:
                        return
                    if resp.get("method") == "session/cancel":
                        reply(msg_id, {"stopReason": "cancelled"})
                        break
            else:
                reply(msg_id, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            # Standalone cancel outside a prompt; ignore.
            pass
        elif msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
