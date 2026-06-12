"""models.dev model catalog seam (ROADMAP 3b.5, Decision 5).

models.dev publishes a machine-readable catalog of LLM providers/models at
``https://models.dev/api.json``. Decision 5 makes it the **catalog for UI
display only** — the shell uses it to render provider/model pickers; it is *not*
on the call path (LiteLLM remains the Python transport for actual completions).
So this seam is deliberately soft:

  - **No hard dependency for core operation.** If the fetch fails (offline, no
    network in the sandbox, models.dev down), the catalog reports
    ``available: False`` with a reason — honest-unavailable — and the rest of
    CENTRI runs unaffected.
  - **On-disk cache + TTL.** The fetched JSON is cached under the CENTRI state
    dir; within the TTL we serve the cache without a network call, so the shell
    stays fast and works offline once warmed.

The fetch uses urllib (stdlib) so the seam adds no dependency.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # a day; catalog changes slowly


def _default_cache_path() -> Path:
    return Path.home() / ".centri" / "models_dev_catalog.json"


class ModelsCatalog:
    """Fetch + cache the models.dev catalog; honest-unavailable offline."""

    def __init__(
        self,
        url: str = MODELS_DEV_URL,
        cache_path: Optional[Path] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        timeout: float = 5.0,
    ):
        self._url = url
        self._cache_path = cache_path or _default_cache_path()
        self._ttl = ttl_seconds
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------
    def _read_cache(self) -> Optional[Dict[str, Any]]:
        try:
            if not self._cache_path.is_file():
                return None
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "fetched_at" in raw and "catalog" in raw:
                return raw
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("models catalog: cache read failed: %s", exc)
        return None

    def _write_cache(self, catalog: Any) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"fetched_at": time.time(), "catalog": catalog}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("models catalog: cache write failed: %s", exc)

    def _cache_fresh(self, entry: Dict[str, Any]) -> bool:
        return (time.time() - float(entry.get("fetched_at", 0))) < self._ttl

    # ------------------------------------------------------------------
    # Network (stdlib; no added dependency)
    # ------------------------------------------------------------------
    def _fetch(self) -> Any:
        req = urllib.request.Request(self._url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https URL
            return json.loads(resp.read().decode("utf-8"))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def get(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Return the catalog for the shell.

        Shape: ``{available, source, fetched_at?, catalog?, reason?}``. Serves a
        fresh cache without a network call; refetches when stale/missing; on a
        failed fetch falls back to a stale cache if present, else honest-unavailable.
        """
        cached = self._read_cache()
        if cached and not force_refresh and self._cache_fresh(cached):
            return {
                "available": True,
                "source": "cache",
                "fetched_at": cached.get("fetched_at"),
                "catalog": cached.get("catalog"),
            }
        try:
            catalog = self._fetch()
            self._write_cache(catalog)
            return {
                "available": True,
                "source": "network",
                "fetched_at": time.time(),
                "catalog": catalog,
            }
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            # Offline / unreachable: serve a stale cache if we have one, else honest.
            if cached:
                return {
                    "available": True,
                    "source": "stale-cache",
                    "fetched_at": cached.get("fetched_at"),
                    "catalog": cached.get("catalog"),
                    "reason": f"refresh failed, serving stale cache: {exc}",
                }
            return {
                "available": False,
                "source": "none",
                "reason": f"models.dev unreachable and no cache: {exc}",
            }
