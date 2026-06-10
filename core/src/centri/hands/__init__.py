"""CENTRI hands — capability router over the Hand ABC.

The coordinator hands off by *capability name* (e.g. ``coding.start_task``); the
router picks the configured hand that advertises it. Both the OpenCode subprocess
hand and the (stub) ACP hand satisfy the same :class:`~centri.hands.base.Hand`
contract, so the coordinator never cares which one runs.
"""

import logging
from typing import Any, Dict, List, Optional

from centri.hands.base import Hand
from centri.schemas import HandCapability, HandoffRequest, HandoffResult

logger = logging.getLogger(__name__)


class Hands:
    """Capability router backed by configured, real hand modules."""

    def __init__(self, settings: Any, db: Any):
        self._db = db
        self._enabled = set(settings.enabled_hands)
        self._hands: Dict[str, Hand] = {}
        self._init_hands(settings)

    def _init_hands(self, settings: Any) -> None:
        for hand_name in self._enabled:
            if hand_name == "opencode":
                try:
                    from centri.hands.opencode import OpenCodeHand

                    self._hands["opencode"] = OpenCodeHand(db=self._db)
                    logger.info("OpenCode hand registered")
                except Exception as exc:
                    logger.warning("OpenCode hand init failed: %s", exc)
            elif hand_name == "acp":
                from centri.hands.acp import AcpHand

                self._hands["acp"] = AcpHand(command=getattr(settings, "acp_command", None))
                logger.info("ACP hand registered (honest-unavailable until Phase 1)")
            else:
                logger.info("Unknown hand '%s' skipped", hand_name)

    async def list_capabilities(self) -> List[HandCapability]:
        caps: List[HandCapability] = []
        for hand in self._hands.values():
            try:
                caps.extend(await hand.capabilities())
            except Exception as exc:
                logger.warning("Hand capabilities query failed: %s", exc)
                caps.append(
                    HandCapability(name="hand.error", risk="low", configured=True, healthy=False, detail=f"Hand error: {exc}")
                )
        return caps

    async def select(self, capability: str) -> Optional[Hand]:
        for name, hand in self._hands.items():
            try:
                for cap in await hand.capabilities():
                    if cap.name == capability:
                        logger.debug("Hand '%s' selected for '%s'", name, capability)
                        return hand
            except Exception:
                continue
        return None

    async def execute(self, request: HandoffRequest) -> HandoffResult:
        hand = await self.select(request.to_capability)
        if hand is None:
            return HandoffResult(status="unavailable", summary=f"No hand configured for {request.to_capability}")
        try:
            return await hand.execute(request)
        except Exception as exc:
            logger.error("Hand execution failed: %s", exc, exc_info=True)
            return HandoffResult(status="error", summary=str(exc))

    async def cancel(self, capability: str, task_id: str) -> bool:
        hand = await self.select(capability)
        if hand is None:
            return False
        return await hand.cancel(task_id)

    async def health(self) -> List[HandCapability]:
        return await self.list_capabilities()
