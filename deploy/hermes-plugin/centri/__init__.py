"""Centri memory plugin — MemoryProvider backed by Centri's cognitive event-spine API.

Centri owns the event spine, typed memory graph, deterministic curation,
and cue-driven context assembly.  This plugin is the Hermes-facing thin
adapter that translates MemoryProvider calls into Centri HTTP requests.

Deployable copy — install at ~/.hermes/plugins/centri/ (symlink or copy).
See docs/HERMES-INTEGRATION.md.
"""
from __future__ import annotations

import sys, os
# The MemoryProvider base class lives in the Hermes agent package. Add its repo
# to sys.path if it isn't already importable. Override the path by exporting
# HERMES_AGENT_REPO if your layout differs.
_HERMES_REPO = os.environ.get("HERMES_AGENT_REPO", os.path.expanduser("~/.hermes/hermes-agent"))
if _HERMES_REPO not in sys.path:
    sys.path.insert(0, _HERMES_REPO)

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas (exposed to the agent)
# ---------------------------------------------------------------------------

RETAIN_SCHEMA = {
    "name": "centri_retain",
    "description": (
        "Store information in Centri's cognitive memory system via its event "
        "spine.  Writes are idempotent on (source, event_uid)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to store."},
            "context": {"type": "string", "description": "Optional source/context label."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for the event payload.",
            },
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "centri_recall",
    "description": (
        "Recall relevant long-term memory from Centri's deterministic curation "
        "system.  Returns a scored, budgeted brief assembled from the event "
        "spine and typed memory graph."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Maximum number of results."},
        },
        "required": ["query"],
    },
}

REFLECT_SCHEMA = {
    "name": "centri_reflect",
    "description": (
        "Assemble Centri context for a query — ambient standing context plus "
        "cued retrieval from the memory graph, within a token budget."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The question to reflect on."},
            "budget_tokens": {"type": "integer", "description": "Approximate context budget."},
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    """Load Centri provider config from config.yaml."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        plugin_cfg = cfg_get(config, "plugins", "centri", default={}) or {}
        memory_cfg = cfg_get(config, "memory", "centri", default={}) or {}
        merged: dict[str, Any] = {}
        if isinstance(memory_cfg, dict):
            merged.update(memory_cfg)
        if isinstance(plugin_cfg, dict):
            merged.update(plugin_cfg)
        return merged
    except Exception:
        return {}


def _api_base(config: dict) -> str:
    return str(config.get("api_base") or os.environ.get("CENTRI_API_BASE") or "http://127.0.0.1:8760")


def _api_token(config: dict) -> str:
    return str(config.get("auth_token") or os.environ.get("CENTRI_AUTH_TOKEN") or "")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests/httpx dependency)
# ---------------------------------------------------------------------------

def _api_call(
    method: str,
    url: str,
    *,
    token: str = "",
    json_body: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Make an HTTP request and return parsed JSON."""
    data = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return {"status": resp.status, "data": json.loads(body) if body else {}}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        logger.warning("Centri API %s %s → %s: %s", method, url, exc.code, body[:500])
        return {"status": exc.status, "error": body, "data": {}}
    except Exception as exc:
        logger.warning("Centri API %s %s failed: %s", method, url, exc)
        return {"status": 0, "error": str(exc), "data": {}}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class CentriMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider backed by Centri's cognitive event-spine API."""

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or _load_config()
        self._session_id = ""
        self._hermes_home = ""
        self._platform = ""

    @property
    def name(self) -> str:
        return "centri"

    def is_available(self) -> bool:
        base = _api_base(self._config)
        resp = _api_call("GET", f"{base}/health", timeout=5)
        return resp.get("status") == 200

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "centri")
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._platform = str(kwargs.get("platform") or "")
        logger.info(
            "Centri memory provider initialized (session=%s, platform=%s, base=%s)",
            self._session_id, self._platform, _api_base(self._config),
        )

    def system_prompt_block(self) -> str:
        return (
            "# Centri Memory\n"
            "Centri is the active Hermes memory provider. Use `centri_recall` for "
            "recall, `centri_retain` to store durable facts, and `centri_reflect` "
            "to assemble broader context.  Centri uses a deterministic event-spine "
            "and typed memory graph — no flat-file fallback."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._config = _load_config()
        if not query:
            return ""
        base = _api_base(self._config)
        token = _api_token(self._config)
        resp = _api_call(
            "POST",
            f"{base}/memory/recall",
            token=token,
            json_body={"cue": query, "thread_id": session_id or self._session_id, "format": "markdown+items"},
            timeout=15,
        )
        if resp.get("status") != 200:
            return ""
        data = resp.get("data", {})
        # The recall endpoint returns a brief; extract markdown if present
        brief = data.get("brief") or data.get("markdown") or data.get("content") or ""
        if isinstance(brief, dict):
            brief = json.dumps(brief, ensure_ascii=False)
        return f"## Centri Context\n{brief}" if brief else ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._config = _load_config()
        base = _api_base(self._config)
        token = _api_token(self._config)
        sid = session_id or self._session_id

        import uuid

        # Helper to cap message at 8000 chars
        def _cap(text: str) -> str:
            if not text:
                return ""
            return text[:8000]

        events = []

        # 1. User message
        events.append({
            "type": "hermes.user.message",
            "event_type": "hermes.user.message",
            "source": "hermes_turn_sync",
            "session_id": sid,
            "thread_id": sid,
            "payload": {
                "event_uid": uuid.uuid4().hex,
                "role": "user",
                "text": _cap(user_content),
                "thread_id": sid
            }
        })

        # 2. Assistant message
        events.append({
            "type": "hermes.assistant.message",
            "event_type": "hermes.assistant.message",
            "source": "hermes_turn_sync",
            "session_id": sid,
            "thread_id": sid,
            "payload": {
                "event_uid": uuid.uuid4().hex,
                "role": "assistant",
                "text": _cap(assistant_content),
                "thread_id": sid
            }
        })

        # 3. Additional tool calls and tool results from messages list
        if messages:
            for m in messages:
                role = str(m.get("role") or "").lower()
                m_type = str(m.get("type") or m.get("part_type") or "").lower()

                # Skip reasoning/step-start/step-finish messages
                if role in ("reasoning", "step-start", "step-finish") or m_type in ("reasoning", "step-start", "step-finish"):
                    continue

                content = m.get("content") or ""
                if not content and "tool_calls" in m:
                    content = json.dumps(m["tool_calls"], ensure_ascii=False)
                elif isinstance(content, (dict, list)):
                    content = json.dumps(content, ensure_ascii=False)
                else:
                    content = str(content)

                # Skip messages shorter than 40 chars
                if len(content) < 40:
                    continue

                # Identify if this is a tool message or tool call/result
                is_tool = False
                if role in ("tool", "function") or "tool_calls" in m or "tool_call_id" in m:
                    is_tool = True
                else:
                    # check if content looks like tool output
                    strip_content = content.strip()
                    if strip_content.startswith("{") or strip_content.startswith("["):
                        try:
                            parsed = json.loads(strip_content)
                            if isinstance(parsed, dict) and any(k in parsed for k in ("output", "result", "stdout", "status", "exit_code")):
                                is_tool = True
                        except Exception:
                            pass
                    lower_content = content.lower()
                    if "tool output" in lower_content or "tool response" in lower_content or "exit code" in lower_content:
                        is_tool = True

                if is_tool:
                    events.append({
                        "type": "hermes.tool.result",
                        "event_type": "hermes.tool.result",
                        "source": "hermes_turn_sync",
                        "session_id": sid,
                        "thread_id": sid,
                        "payload": {
                            "event_uid": uuid.uuid4().hex,
                            "role": "tool",
                            "text": _cap(content),
                            "thread_id": sid
                        }
                    })

        # 4. Batch all events into a single /events/import call
        resp = None
        try:
            resp = _api_call(
                "POST",
                f"{base}/events/import",
                token=token,
                json_body={"events": events},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Centri events import failed: %s", exc)

        # 5. Fall back to /utterance only if /events/import failed
        if resp is None or resp.get("status") != 200:
            _api_call(
                "POST",
                f"{base}/utterance",
                token=token,
                json_body={
                    "text": f"[hermes-turn session={sid}] user: {user_content[:2000]}\nassistant: {assistant_content[:2000]}",
                    "user_id": "hermes",
                    "source": "hermes_turn_sync",
                    "thread_id": sid,
                },
                timeout=10,
            )


    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RETAIN_SCHEMA, RECALL_SCHEMA, REFLECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "centri_retain":
                return self._handle_retain(args)
            if tool_name == "centri_recall":
                return self._handle_recall(args)
            if tool_name == "centri_reflect":
                return self._handle_reflect(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as exc:
            logger.warning("Centri memory tool %s failed: %s", tool_name, exc, exc_info=True)
            return tool_error(str(exc))

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        if not new_session_id:
            return
        old = self._session_id
        self._session_id = str(new_session_id)
        logger.info("Centri session switched: %s → %s", old, self._session_id)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not content:
            return
        self._config = _load_config()
        base = _api_base(self._config)
        token = _api_token(self._config)
        sid = self._session_id

        import uuid

        event = {
            "type": "hermes.memory.write",
            "event_type": "hermes.memory.write",
            "source": "hermes_memory_write",
            "session_id": sid,
            "thread_id": sid,
            "payload": {
                "event_uid": uuid.uuid4().hex,
                "role": "system",
                "text": content[:8000],
                "action": action,
                "target": target,
            },
        }

        resp = None
        try:
            resp = _api_call(
                "POST",
                f"{base}/events/import",
                token=token,
                json_body={"events": [event]},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Centri memory write events import failed: %s", exc)

        if resp is None or resp.get("status") != 200:
            _api_call(
                "POST",
                f"{base}/utterance",
                token=token,
                json_body={
                    "text": f"[memory-write action={action} target={target}] {content[:3000]}",
                    "user_id": "hermes",
                    "source": "hermes_memory_write",
                    "thread_id": sid,
                },
                timeout=10,
            )


    def shutdown(self) -> None:
        logger.info("Centri memory provider shut down")

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    def _handle_retain(self, args: Dict[str, Any]) -> str:
        # Always reload config so hot-updates (auth_token, api_base) are picked up.
        self._config = _load_config()
        content = str(args.get("content") or "")
        if not content:
            return tool_error("Missing required parameter: content")
        context = args.get("context") or ""
        tags = _normalize_tags(args.get("tags"))
        base = _api_base(self._config)
        token = _api_token(self._config)

        # Build the event payload for Centri's event import
        event = {
            "event_type": "hermes.retain",
            "source": "hermes_centri_provider",
            "session_id": self._session_id,
            "payload": {
                "content": content,
                "context": context,
                "tags": tags,
            },
        }
        resp = _api_call(
            "POST",
            f"{base}/events/import",
            token=token,
            json_body={"events": [event]},
            timeout=15,
        )
        if resp.get("status") == 200:
            data = resp.get("data", {})
            return json.dumps({
                "stored": True,
                "event_ids": data.get("event_ids", []),
                "backend": "centri",
            })
        # Fallback: try utterance endpoint
        resp2 = _api_call(
            "POST",
            f"{base}/utterance",
            token=token,
            json_body={
                "text": content,
                "user_id": "hermes",
                "source": "centri_retain",
                "thread_id": self._session_id,
            },
            timeout=15,
        )
        return json.dumps({
            "stored": resp2.get("status") == 200,
            "backend": "centri",
            "fallback": "utterance",
            "status": resp2.get("status"),
        })

    def _handle_recall(self, args: Dict[str, Any]) -> str:
        # Always reload config so hot-updates (auth_token, api_base) are picked up.
        self._config = _load_config()
        query = str(args.get("query") or "")
        if not query:
            return tool_error("Missing required parameter: query")
        limit = int(args.get("limit") or 10)
        base = _api_base(self._config)
        token = _api_token(self._config)

        resp = _api_call(
            "POST",
            f"{base}/memory/recall",
            token=token,
            json_body={
                "cue": query,
                "thread_id": self._session_id,
                "budget_tokens": 4000,
                "format": "markdown+items",
            },
            timeout=15,
        )
        if resp.get("status") != 200:
            return json.dumps({
                "results": [],
                "count": 0,
                "backend": "centri",
                "message": f"Centri API returned status {resp.get('status')}",
            })

        data = resp.get("data", {})
        # Normalize the response
        brief = data.get("brief") or data.get("content") or ""
        items = data.get("items") or []
        if isinstance(brief, str) and brief:
            items = [{"content": brief}]
        results = items[:limit]
        return json.dumps({
            "results": results,
            "count": len(results),
            "backend": "centri",
            "message": "" if results else "No memories found.",
        }, ensure_ascii=False)

    def _handle_reflect(self, args: Dict[str, Any]) -> str:
        # Always reload config so hot-updates (auth_token, api_base) are picked up.
        self._config = _load_config()
        query = str(args.get("query") or "")
        if not query:
            return tool_error("Missing required parameter: query")
        budget = int(args.get("budget_tokens") or 4000)
        base = _api_base(self._config)
        token = _api_token(self._config)

        # Use the briefing endpoint for full context assembly
        resp = _api_call(
            "GET",
            f"{base}/briefing",
            token=token,
            timeout=15,
        )
        if resp.get("status") != 200:
            # Fallback to recall with cue
            resp = _api_call(
                "POST",
                f"{base}/memory/recall",
                token=token,
                json_body={
                    "cue": query,
                    "thread_id": self._session_id,
                    "budget_tokens": budget,
                    "format": "markdown+items",
                },
                timeout=15,
            )

        data = resp.get("data", {})
        brief = data.get("brief") or data.get("content") or data.get("markdown") or ""
        if isinstance(brief, dict):
            brief = json.dumps(brief, ensure_ascii=False)
        return json.dumps({
            "briefing": brief[:budget * 4] if brief else "",  # rough char estimate
            "backend": "centri",
            "budget_tokens": budget,
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    tags: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def register(ctx) -> None:
    """Register Centri as a Hermes MemoryProvider."""
    ctx.register_memory_provider(CentriMemoryProvider())
    logger.info("Centri memory provider registered")
