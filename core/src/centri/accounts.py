"""CENTRI accounts — external provider connection state.

Honest-unavailable by default: a provider is reported only when it is actually
configured. No placeholder "connected" status.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class Accounts:
    """Track account connections and credentials."""

    def __init__(self, settings: Any):
        self._config = settings
        self._accounts: Dict[str, Dict[str, Any]] = {}

    async def list_accounts(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        if getattr(self._config, "letta_url", ""):
            result.append({"provider": "letta", "status": "configured"})
        return result

    async def connect(self, provider: str) -> Dict[str, Any]:
        return {"provider": provider, "status": "not_implemented", "url": None}

    async def refresh(self, provider: str) -> bool:
        return False

    async def scopes(self, provider: str) -> List[str]:
        return []

    async def health(self, provider: str) -> str:
        return "ok"
