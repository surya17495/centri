"""CENTRI hands — capability router over the Hand ABC.

The coordinator hands off by *capability name* (e.g. ``coding.start_task``); the
router picks the configured hand that advertises it. Both the OpenCode subprocess
hand and the (stub) ACP hand satisfy the same :class:`~centri.hands.base.Hand`
contract, so the coordinator never cares which one runs.
"""

import logging
from typing import Any, Dict, List, Optional

from centri.hands.base import ApprovalGate, EventSink, Hand
from centri.schemas import HandCapability, HandoffRequest, HandoffResult

logger = logging.getLogger(__name__)


class Hands:
    """Capability router backed by configured, real hand modules.

    Preference order is ``settings.hand_priority`` (default: ACP first, then the
    OpenCode subprocess). For a capability, ``select`` returns the first hand in
    priority order that advertises it *and* is healthy, falling back to the first
    that merely advertises it. This is how ACP is preferred when its agent is
    reachable and the subprocess hand is the honest fallback when it is not.
    """

    def __init__(self, settings: Any, db: Any):
        self._db = db
        self._enabled = list(settings.enabled_hands)
        # Priority order among enabled hands; unknown names ignored.
        priority = list(getattr(settings, "hand_priority", ["acp", "opencode"]))
        self._priority = [h for h in priority if h in self._enabled]
        self._priority += [h for h in self._enabled if h not in self._priority]
        self._hands: Dict[str, Hand] = {}
        self._init_hands(settings)

    def _init_hands(self, settings: Any) -> None:
        for hand_name in self._enabled:
            if hand_name == "opencode":
                try:
                    from centri.hands.opencode import OpenCodeHand

                    self._hands["opencode"] = OpenCodeHand(db=self._db)
                    logger.info("OpenCode subprocess hand registered")
                except Exception as exc:
                    logger.warning("OpenCode hand init failed: %s", exc)
            elif hand_name == "acp":
                from centri.hands.acp import AcpHand

                command = getattr(settings, "acp_command", None) or getattr(
                    settings, "acp_opencode_command", None
                )
                self._hands["acp"] = AcpHand(command=command)
                logger.info("ACP hand registered (command=%s)", command)
            else:
                logger.info("Unknown hand '%s' skipped", hand_name)

    def _ordered_hands(self) -> List[tuple[str, Hand]]:
        return [(name, self._hands[name]) for name in self._priority if name in self._hands]

    async def list_capabilities(self) -> List[HandCapability]:
        caps: List[HandCapability] = []
        for name, hand in self._ordered_hands():
            try:
                for cap in await hand.capabilities():
                    cap.detail = f"[{name}] {cap.detail}" if cap.detail else f"[{name}]"
                    caps.append(cap)
            except Exception as exc:
                logger.warning("Hand capabilities query failed: %s", exc)
                caps.append(
                    HandCapability(name="hand.error", risk="low", configured=True, healthy=False, detail=f"Hand error: {exc}")
                )
        return caps

    async def select(self, capability: str) -> Optional[Hand]:
        """Pick the best hand for a capability: prefer a healthy one, in priority order."""
        fallback: Optional[Hand] = None
        for name, hand in self._ordered_hands():
            try:
                advertises = any(cap.name == capability for cap in await hand.capabilities())
                if not advertises:
                    continue
                if fallback is None:
                    fallback = hand
                health = await hand.health()
                if health.healthy:
                    logger.debug("Hand '%s' selected for '%s' (healthy)", name, capability)
                    return hand
            except Exception:
                continue
        if fallback is not None:
            logger.debug("No healthy hand for '%s'; using first advertiser as fallback", capability)
        return fallback

    async def execute(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> HandoffResult:
        hand = await self.select(request.to_capability)
        if hand is None:
            return HandoffResult(status="unavailable", summary=f"No hand configured for {request.to_capability}")
        try:
            return await hand.execute(request, event_sink=event_sink, approval_gate=approval_gate)
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
