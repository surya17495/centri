"""CENTRI ACP hand — skeleton for an Agent Client Protocol peer.

ACP (agentclientprotocol.com) speaks JSON-RPC over stdio: CENTRI launches the
agent as a subprocess and exchanges newline-delimited JSON-RPC messages on its
stdin/stdout. This class establishes the contract slot; the wire protocol lands
in Phase 1. Until then it is honest-unavailable — it never claims success.
"""

from __future__ import annotations

from typing import List, Optional

from centri.hands.base import Hand, HandHealth
from centri.schemas import HandCapability, HandoffRequest, HandoffResult

_UNAVAILABLE = "ACP wire protocol not implemented until Phase 1"


class AcpHand(Hand):
    """Stub ACP client hand. Reports unavailable with a reason."""

    def __init__(self, command: Optional[str] = None):
        # The command that would launch the ACP agent subprocess (Phase 1).
        self._command = command

    async def capabilities(self) -> List[HandCapability]:
        return [
            HandCapability(
                name="coding.start_task",
                risk="medium",
                configured=bool(self._command),
                healthy=False,
                detail=_UNAVAILABLE,
            )
        ]

    async def health(self) -> HandHealth:
        return HandHealth(healthy=False, reason=_UNAVAILABLE)

    async def execute(self, request: HandoffRequest) -> HandoffResult:
        return HandoffResult(status="unavailable", summary=_UNAVAILABLE)

    async def cancel(self, task_id: str) -> bool:
        return False
