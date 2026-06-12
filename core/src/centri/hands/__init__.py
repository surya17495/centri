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

    async def _advertisers(self, capability: str) -> List[tuple[str, Hand]]:
        """All hands advertising a capability, in priority order, healthy first.

        Within priority order, a healthy hand sorts ahead of an unhealthy one, so
        the preferred *reachable* hand is tried first and the rest remain as
        ordered fallbacks for mid-task degradation.
        """
        healthy: List[tuple[str, Hand]] = []
        unhealthy: List[tuple[str, Hand]] = []
        for name, hand in self._ordered_hands():
            try:
                if not any(cap.name == capability for cap in await hand.capabilities()):
                    continue
                if (await hand.health()).healthy:
                    healthy.append((name, hand))
                else:
                    unhealthy.append((name, hand))
            except Exception:
                continue
        return healthy + unhealthy

    async def select(self, capability: str) -> Optional[Hand]:
        """Pick the best hand for a capability: prefer a healthy one, in priority order."""
        ordered = await self._advertisers(capability)
        return ordered[0][1] if ordered else None

    # Hand statuses that are real successes; anything else triggers a degrade to
    # the next advertiser (the fallback chain).
    _SUCCESS_STATUSES = frozenset({"completed", "ok", "steered", "cancelled"})

    async def execute(
        self,
        request: HandoffRequest,
        event_sink: Optional[EventSink] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> HandoffResult:
        """Run a handoff, degrading down the priority chain on failure.

        The preferred reachable hand (e.g. ACP) runs first. If it fails — whether
        it returns a non-success status or raises (e.g. its process was killed
        mid-task) — and a lower-priority advertiser exists (e.g. the OpenCode
        subprocess), CENTRI degrades to it, emitting an honest ``hand.degraded``
        event on the spine. When no fallback remains, the last honest failure is
        returned as-is; CENTRI never fakes a success and never leaves the caller
        without a real terminal status.
        """
        ordered = await self._advertisers(request.to_capability)
        if not ordered:
            return HandoffResult(status="unavailable", summary=f"No hand configured for {request.to_capability}")

        last: HandoffResult = HandoffResult(status="unavailable", summary="no hand ran")
        for idx, (name, hand) in enumerate(ordered):
            try:
                result = await hand.execute(request, event_sink=event_sink, approval_gate=approval_gate)
            except Exception as exc:
                logger.error("Hand '%s' execution raised: %s", name, exc, exc_info=True)
                result = HandoffResult(status="error", summary=f"{name} hand error: {exc}")

            if result.status in self._SUCCESS_STATUSES:
                return result

            last = result
            next_name = ordered[idx + 1][0] if idx + 1 < len(ordered) else None
            if next_name is not None and event_sink is not None:
                # Honest event trail: the spine records the degrade, who failed,
                # why, and who we are trying next — no silent swallow.
                try:
                    await event_sink({
                        "type": "hand.degraded",
                        "source": "hands",
                        "summary": f"{name} hand {result.status}: degrading to {next_name}",
                        "failed_hand": name,
                        "failed_status": result.status,
                        "reason": result.summary[:240],
                        "fallback_hand": next_name,
                    })
                except Exception:
                    logger.debug("hand.degraded event_sink error", exc_info=True)
        return last

    async def cancel(self, capability: str, task_id: str) -> bool:
        hand = await self.select(capability)
        if hand is None:
            return False
        return await hand.cancel(task_id)

    async def health(self) -> List[HandCapability]:
        return await self.list_capabilities()
