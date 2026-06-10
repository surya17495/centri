"""CENTRI hand contract.

A *hand* is an external agent CENTRI delegates work to. The coordinator must not
care whether the hand is a local subprocess (OpenCode CLI) or a JSON-RPC peer
spoken to over stdio (ACP — Agent Client Protocol, agentclientprotocol.com). Both
satisfy the same ``Hand`` ABC below.

Honest-failure principle: ``health()`` reports a hand as *healthy* or
*unavailable with a reason*. There is no placeholder "connected" state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from centri.schemas import HandCapability, HandoffRequest, HandoffResult

# A sink a hand calls to stream an event (task.progress / hand.progress /
# artifact.created / approval.requested) live, before the handoff returns. The
# jobs layer supplies a sink that records the event to the ledger and fans it out
# on the bus. Hands that run to completion (subprocess) may ignore it and instead
# return events in ``HandoffResult.events_to_record``.
EventSink = Callable[[Dict[str, Any]], Awaitable[None]]

# A gate a hand awaits when the agent requests permission for a destructive
# action. Given the approval payload (tool, action, preview), it returns the
# resolved outcome string ("allow" / "deny"). The jobs layer supplies one that
# creates an ``approval.requested`` record and blocks on its resolution.
ApprovalGate = Callable[[Dict[str, Any]], Awaitable[str]]


@dataclass
class HandHealth:
    """Result of a ``health()`` probe.

    ``healthy=True`` means the hand is configured and reachable. Otherwise
    ``reason`` explains why it is unavailable.
    """

    healthy: bool
    reason: str = ""


class Hand(ABC):
    """Contract every CENTRI hand implements.

    ``execute`` returns a ``HandoffResult`` whose ``events_to_record`` carry the
    streaming progress (task.progress / hand.progress / artifact.created). The
    jobs layer persists those events to the spine, so a hand streams progress by
    appending to that list — the same shape works for a subprocess that runs to
    completion and for an ACP peer that emits incremental updates.
    """

    @abstractmethod
    async def capabilities(self) -> List[HandCapability]:
        """Capabilities this hand advertises, each with live health."""

    @abstractmethod
    async def health(self) -> HandHealth:
        """Probe whether the hand is configured and reachable."""

    @abstractmethod
    async def execute(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> HandoffResult:
        """Run a handoff request to completion.

        ``event_sink``, when provided, is awaited for each progress/artifact
        event as it happens, so the UI sees live updates rather than a single
        completion. ``approval_gate``, when provided, is awaited when the agent
        requests permission for a destructive action; it returns the resolved
        outcome. Hands may ignore either and still return final events in
        ``HandoffResult.events_to_record``.
        """

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        """Cancel an in-flight task. Returns True if a cancel was issued."""
