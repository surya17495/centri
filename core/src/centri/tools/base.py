"""Tool contract core (Decision 11).

A *tool* is an external capability CENTRI can invoke (web search, browser
automation, an API call). It is a first-class contract parallel to a hand:

  - Every invocation is an event on the spine with receipts (``tool.requested``
    then ``tool.completed`` / ``tool.failed`` / ``tool.denied``), written via
    ``db.append_event`` so the redaction seam scrubs secrets before persistence.
  - Side-effectful tools round-trip the EXISTING approval gate (the same machinery
    hands use for permission requests) before execution; a deny/timeout yields a
    ``tool.denied`` event and an honest failure, never execution. Read-only tools
    (e.g. search) skip the gate.
  - The completion event carries a deterministic ``fact`` hint (mirroring the
    ``hand.transcript`` pattern) so the consolidation worker folds tool results
    into the memory graph with a receipt — without an LLM and without
    confabulating.

Honest-unavailable everywhere: a provider with no credentials reports
``available() == False`` with a reason and is listed as unavailable; the registry
never fakes success.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Slug fragments that mark a tool as read-only (no approval gate). Conservative:
# anything NOT clearly a read is treated as side-effectful.
_READ_ONLY_MARKERS = ("SEARCH", "GET", "LIST", "FETCH", "READ", "FIND", "LOOKUP", "QUERY")

# UI summaries stay short; the full output is preserved on the completion event
# (mirrors the hand.transcript full-text-plus-240-char-summary pattern).
_SUMMARY_LEN = 240
_FACT_STATEMENT_LEN = 400

# An approval gate the registry awaits before executing a side-effectful tool.
# Given the approval payload, it returns the resolved outcome string
# ("allow" / "deny"). The jobs/coordinator layer supplies one that creates an
# ``approval.requested`` record and blocks on its resolution — the same callable
# shape hands receive.
ApprovalGate = Callable[[Dict[str, Any]], Awaitable[str]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_read_only_slug(slug: str) -> bool:
    """Classify a tool slug as read-only (skips the approval gate).

    Conservative default: a slug is read-only only when it contains a read
    marker (SEARCH/GET/LIST/FETCH/...). Everything else is side-effectful and
    must round-trip the approval gate before execution.
    """
    up = (slug or "").upper()
    return any(marker in up for marker in _READ_ONLY_MARKERS)


@dataclass
class ToolSpec:
    """A tool a provider exposes."""

    name: str
    provider: str
    description: str = ""
    side_effectful: bool = True
    input_schema: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "description": self.description,
            "side_effectful": self.side_effectful,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolResult:
    """The outcome of a tool invocation.

    ``status`` is one of ``completed`` / ``failed`` / ``unavailable``. ``output``
    is the provider's parsed result (str or dict), ``error`` an honest failure
    reason, and ``raw`` the provider's raw payload (kept for receipts; redaction
    runs before it ever touches the ledger).
    """

    status: str
    output: Any = None
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
        }


class ToolProvider(ABC):
    """Contract every tool provider implements.

    ``available()`` is honest: it returns False with a reason when the provider
    is not configured (e.g. no API key) rather than pretending to work.
    """

    name: str = "provider"

    @abstractmethod
    def available(self) -> bool:
        """Whether the provider is configured and usable."""

    @abstractmethod
    def unavailable_reason(self) -> str:
        """Why the provider is unavailable (empty when available)."""

    @abstractmethod
    def list_tools(self) -> List[ToolSpec]:
        """Tools this provider exposes (may be non-empty even when unavailable)."""

    @abstractmethod
    async def execute(
        self, name: str, arguments: Dict[str, Any], **kwargs: Any
    ) -> ToolResult:
        """Run a single tool. Must never raise for an honest failure — return a
        ``ToolResult`` with ``status="failed"`` / ``"unavailable"`` instead."""


class ToolRegistry:
    """Registers providers and is the ONLY execution path for tools.

    ``invoke`` emits the full event trail, gates side-effectful tools through the
    approval machinery, executes via the provider, and attaches the consolidation
    fact hint — so every tool call is auditable and re-derivable from the spine.
    """

    def __init__(self, db: Any, event_bus: Any = None, tenant_id: str = "local"):
        self._db = db
        self._event_bus = event_bus
        self._tenant_id = tenant_id
        self._providers: Dict[str, ToolProvider] = {}

    def register(self, provider: ToolProvider) -> None:
        self._providers[provider.name] = provider

    def providers(self) -> List[ToolProvider]:
        return list(self._providers.values())

    def list_tools(self) -> List[Dict[str, Any]]:
        """Every tool across providers, each with provider availability + reason.

        Honest-unavailable: a provider with no credentials still lists its tools,
        flagged ``available=False`` with a reason, so a caller sees what *would*
        be possible once configured.
        """
        out: List[Dict[str, Any]] = []
        for provider in self._providers.values():
            avail = provider.available()
            reason = "" if avail else provider.unavailable_reason()
            for spec in provider.list_tools():
                entry = spec.as_dict()
                entry["available"] = avail
                entry["reason"] = reason
                out.append(entry)
        return out

    def _find(self, name: str) -> Optional[tuple[ToolProvider, ToolSpec]]:
        for provider in self._providers.values():
            for spec in provider.list_tools():
                if spec.name == name:
                    return provider, spec
        return None

    async def invoke(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        approval_gate: Optional[ApprovalGate] = None,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Invoke a tool through the full contract. Returns the result + event ids.

        Steps: (1) emit ``tool.requested``; (2) for side-effectful tools, await the
        approval gate — deny/timeout => ``tool.denied`` + honest failure, no
        execution; (3) execute via the provider; (4) emit ``tool.completed`` /
        ``tool.failed`` with a short summary + full output and a deterministic
        ``fact`` hint for consolidation.
        """
        arguments = arguments or {}
        event_ids: List[str] = []

        match = self._find(name)
        if match is None:
            ev = await self._emit(
                "tool.failed",
                {
                    "tool": name,
                    "status": "unavailable",
                    "error": f"no tool named '{name}' is registered",
                    "summary": f"tool '{name}' not found",
                },
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
            )
            event_ids.append(ev)
            return {
                "result": ToolResult(status="unavailable", error=f"unknown tool '{name}'").as_dict(),
                "event_ids": event_ids,
            }

        provider, spec = match

        requested = await self._emit(
            "tool.requested",
            {
                "tool": name,
                "provider": provider.name,
                "side_effectful": spec.side_effectful,
                "arguments": arguments,
                "summary": f"tool requested: {name}",
            },
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
        )
        event_ids.append(requested)

        # Honest-unavailable: a provider with no credentials never executes.
        if not provider.available():
            reason = provider.unavailable_reason()
            ev = await self._emit(
                "tool.failed",
                {
                    "tool": name,
                    "provider": provider.name,
                    "status": "unavailable",
                    "error": reason,
                    "summary": f"tool '{name}' unavailable: {reason}"[:_SUMMARY_LEN],
                },
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
            )
            event_ids.append(ev)
            return {
                "result": ToolResult(status="unavailable", error=reason).as_dict(),
                "event_ids": event_ids,
            }

        # Side-effectful tools round-trip the approval gate; read-only skip it.
        if spec.side_effectful:
            outcome = "deny"
            if approval_gate is not None:
                gate_payload = {
                    "tool": name,
                    "title": f"Tool: {name}",
                    "action": name,
                    "preview": _short_args(arguments),
                    "risk": "high",
                }
                try:
                    outcome = await approval_gate(gate_payload)
                except Exception:
                    logger.debug("Tool approval gate raised; denying", exc_info=True)
                    outcome = "deny"
            if outcome != "allow":
                ev = await self._emit(
                    "tool.denied",
                    {
                        "tool": name,
                        "provider": provider.name,
                        "status": "denied",
                        "error": "approval denied" if approval_gate else "no approval gate; side-effectful tool denied",
                        "summary": f"tool '{name}' denied",
                    },
                    thread_id=thread_id,
                    task_id=task_id,
                    repo_id=repo_id,
                )
                event_ids.append(ev)
                return {
                    "result": ToolResult(
                        status="failed",
                        error="approval denied" if approval_gate else "side-effectful tool requires an approval gate",
                    ).as_dict(),
                    "event_ids": event_ids,
                }

        # Execute.
        try:
            result = await provider.execute(name, arguments)
        except Exception as exc:  # provider must not raise, but never trust it
            logger.error("Tool '%s' execution raised: %s", name, exc, exc_info=True)
            result = ToolResult(status="failed", error=str(exc))

        terminal_type = "tool.completed" if result.status == "completed" else "tool.failed"
        payload: Dict[str, Any] = {
            "tool": name,
            "provider": provider.name,
            "status": result.status,
            "summary": _summarize(name, result)[:_SUMMARY_LEN],
            "output": result.output,
        }
        if result.error:
            payload["error"] = result.error
        if result.raw is not None:
            payload["raw"] = result.raw
        # Deterministic fact hint so consolidation folds the result with a receipt.
        if result.status == "completed":
            payload["fact"] = {
                "topic": f"tool:{provider.name}:{name}",
                "statement": _fact_statement(name, arguments, result),
                "tags": ["tool", provider.name],
            }
        ev = await self._emit(
            terminal_type,
            payload,
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
        )
        event_ids.append(ev)
        return {"result": result.as_dict(), "event_ids": event_ids}

    async def _emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        thread_id: Optional[str],
        task_id: Optional[str],
        repo_id: Optional[str],
    ) -> str:
        ts = _now()
        event_id = f"tool-{event_type.split('.')[-1]}-{ts}-{abs(hash((event_type, ts, payload.get('tool')))) % 100000}"
        # append_event runs redaction before persistence; secrets in arguments or
        # output are scrubbed from the ledger.
        await self._db.append_event(
            event_id=event_id,
            type=event_type,
            source="tool",
            ts=ts,
            thread_id=thread_id,
            task_id=task_id,
            repo_id=repo_id,
            payload=payload,
            tenant_id=self._tenant_id,
        )
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {
                        "id": event_id,
                        "type": event_type,
                        "ts": ts,
                        "source": "tool",
                        "thread_id": thread_id,
                        "task_id": task_id,
                        "payload": payload,
                        "summary": payload.get("summary", ""),
                    }
                )
            except Exception:
                logger.debug("Tool event publish failed", exc_info=True)
        return event_id


def _short_args(arguments: Dict[str, Any]) -> str:
    try:
        items = ", ".join(f"{k}={str(v)[:60]}" for k, v in arguments.items())
    except Exception:
        items = str(arguments)
    return items[:_SUMMARY_LEN]


def _summarize(name: str, result: ToolResult) -> str:
    if result.status != "completed":
        return f"{name} {result.status}: {result.error or ''}".strip()
    out = result.output
    text = out if isinstance(out, str) else _stringify(out)
    return f"{name}: {text}"


def _stringify(value: Any) -> str:
    try:
        import json

        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _fact_statement(name: str, arguments: Dict[str, Any], result: ToolResult) -> str:
    query = arguments.get("query") or arguments.get("q") or _short_args(arguments)
    out = result.output
    text = out if isinstance(out, str) else _stringify(out)
    return f"Tool {name}({str(query)[:120]}) -> {text[:_FACT_STATEMENT_LEN]}"
