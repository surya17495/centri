"""CENTRI event bus — fan-out for live runtime events to connected clients."""

import asyncio
import logging
from typing import Any, Dict, List

from centri.redaction import redact_event

logger = logging.getLogger(__name__)


class EventBus:
    """Simple in-memory fan-out bus for runtime events.

    Each connected WebSocket client receives a JSON copy of every published event.
    """

    def __init__(self):
        self._queues: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._queues.append(q)
        logger.debug("EventBus subscribe: %d clients", len(self._queues))
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._queues:
                self._queues.remove(q)
        logger.debug("EventBus unsubscribe: %d clients", len(self._queues))

    async def publish(self, event: Dict[str, Any]) -> None:
        # Scrub secrets before any client (WebSocket fan-out) sees the event.
        event = redact_event(event)
        dead: List[asyncio.Queue] = []
        async with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            async with self._lock:
                for q in dead:
                    if q in self._queues:
                        self._queues.remove(q)
