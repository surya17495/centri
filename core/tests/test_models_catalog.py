"""Phase 3b.5 — models.dev catalog seam (single LLM config, Decision 5).

models.dev is the UI-display model catalog only (LiteLLM stays the transport).
The seam caches on disk with a TTL and is honest-unavailable offline, never a
hard dependency. Network is faked here (no real models.dev call in the sandbox).
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.models_catalog import ModelsCatalog


class _Catalog(ModelsCatalog):
    """Test double with a scripted _fetch (count calls, can fail on demand)."""

    def __init__(self, *args, payload=None, fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._payload = payload if payload is not None else {"providers": {"openai": {}}}
        self._fail = fail
        self.fetch_calls = 0

    def _fetch(self):
        self.fetch_calls += 1
        if self._fail:
            raise OSError("offline")
        return self._payload


class TestModelsCatalog:
    def test_fetch_and_cache_written(self, tmp_path):
        cache = tmp_path / "cat.json"
        cat = _Catalog(cache_path=cache, payload={"providers": {"x": 1}})
        res = cat.get()
        assert res["available"] is True
        assert res["source"] == "network"
        assert res["catalog"] == {"providers": {"x": 1}}
        assert cache.is_file()

    def test_fresh_cache_avoids_refetch(self, tmp_path):
        cache = tmp_path / "cat.json"
        cat = _Catalog(cache_path=cache, ttl_seconds=10_000)
        cat.get()
        assert cat.fetch_calls == 1
        again = cat.get()
        assert again["source"] == "cache"
        assert cat.fetch_calls == 1  # served from cache, no second fetch

    def test_stale_cache_triggers_refetch(self, tmp_path):
        cache = tmp_path / "cat.json"
        cat = _Catalog(cache_path=cache, ttl_seconds=0)  # everything is stale
        cat.get()
        cat.get()
        assert cat.fetch_calls == 2

    def test_offline_no_cache_is_honest_unavailable(self, tmp_path):
        cache = tmp_path / "cat.json"
        cat = _Catalog(cache_path=cache, fail=True)
        res = cat.get()
        assert res["available"] is False
        assert "unreachable" in res["reason"]

    def test_offline_with_stale_cache_serves_stale(self, tmp_path):
        cache = tmp_path / "cat.json"
        # Warm the cache once online.
        warm = _Catalog(cache_path=cache, ttl_seconds=0, payload={"v": 1})
        warm.get()
        # Now go offline; stale cache should still be served.
        offline = _Catalog(cache_path=cache, ttl_seconds=0, fail=True)
        res = offline.get()
        assert res["available"] is True
        assert res["source"] == "stale-cache"
        assert res["catalog"] == {"v": 1}
