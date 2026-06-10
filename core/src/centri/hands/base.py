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
from typing import List

from centri.schemas import HandCapability, HandoffRequest, HandoffResult


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
    async def execute(self, request: HandoffRequest) -> HandoffResult:
        """Run a handoff request to completion, streaming progress as events."""

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        """Cancel an in-flight task. Returns True if a cancel was issued."""
