"""Real Letta server transport for :class:`LettaMemoryStore`.

When ``CENTRI_LETTA_URL`` points at a running Letta server, the store routes its
archival facts to that server's *archival memory* (passages) instead of the local
SQLite projection. This is the ``letta_http`` mode the bench reports: a real
Letta agent, real pgvector-backed archival passages, real semantic retrieval.

The transport is deliberately thin and synchronous (the ``letta-client`` SDK is
sync); the async ``LettaMemoryStore`` calls it through ``asyncio.to_thread`` so it
never blocks the loop. It is constructed only when a server is configured, so the
import of ``letta_client`` is lazy and the dependency stays optional.

Why passages and not core blocks: Letta's archival memory is the prose-retrieval
store the architecture doc compares against — unbounded notes you retrieve by
similarity, with **no typed supersession**. Storing a renamed-service pair leaves
both notes retrievable, which is exactly the accumulation failure the bench
surfaces against CENTRI's typed graph.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

try:  # pragma: no cover - exercised only when the SDK is installed
    from letta_client import Letta
except ModuleNotFoundError:  # pragma: no cover
    Letta = None  # type: ignore[assignment]


class LettaHTTPClient:
    """Synchronous wrapper over a real Letta server's agent + archival memory."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        agent_name: str = "centri-bench",
        model: str = "moonshotai/Kimi-K2.6",
        model_endpoint: str = "http://127.0.0.1:8901/v1",
        embedding_model: str = "Qwen/Qwen3-Embedding-8B",
        embedding_endpoint: str = "http://127.0.0.1:8901/v1",
        embedding_dim: int = 4096,
    ) -> None:
        if Letta is None:
            raise RuntimeError("letta-client is not installed")
        kwargs = {"base_url": base_url.rstrip("/")}
        if api_key:
            kwargs["token"] = api_key
        self._client = Letta(**kwargs)
        self._agent_name = agent_name
        self._model = model
        self._model_endpoint = model_endpoint
        self._embedding_model = embedding_model
        self._embedding_endpoint = embedding_endpoint
        self._embedding_dim = embedding_dim
        self._agent_id: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------
    def ensure_agent(self) -> str:
        """Create (or recreate) the bench agent and return its id."""
        llm_config = {
            "model": self._model,
            "model_endpoint_type": "openai",
            "model_endpoint": self._model_endpoint,
            "context_window": 32000,
        }
        embedding_config = {
            "embedding_model": self._embedding_model,
            "embedding_endpoint_type": "openai",
            "embedding_endpoint": self._embedding_endpoint,
            "embedding_dim": self._embedding_dim,
            "embedding_chunk_size": 300,
        }
        agent = self._client.agents.create(
            name=f"{self._agent_name}-{_short_uid()}",
            llm_config=llm_config,
            embedding_config=embedding_config,
            memory_blocks=[],
            include_base_tools=False,
        )
        self._agent_id = agent.id
        return agent.id

    def reset(self) -> None:
        """Drop the current agent so a fresh ingest starts clean."""
        if self._agent_id:
            try:
                self._client.agents.delete(self._agent_id)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._agent_id = None

    # -- archival passages --------------------------------------------------
    def insert_passage(self, text: str, tags: Optional[List[str]] = None) -> None:
        assert self._agent_id, "ensure_agent() must run first"
        self._client.agents.passages.create(self._agent_id, text=text, tags=tags or [])

    def search_passages(self, query: str, limit: int = 12) -> List[Tuple[str, Optional[str]]]:
        """Return (text, passage_id) tuples for the query, most relevant first."""
        assert self._agent_id, "ensure_agent() must run first"
        res = self._client.agents.passages.list(self._agent_id, search=query or None, limit=limit)
        rows = res if isinstance(res, list) else getattr(res, "data", res)
        out: List[Tuple[str, Optional[str]]] = []
        for p in rows or []:
            text = getattr(p, "text", None)
            if text is None and isinstance(p, dict):
                text = p.get("text")
            pid = getattr(p, "id", None)
            if text:
                out.append((text, pid))
        return out


def _short_uid() -> str:
    import uuid

    return uuid.uuid4().hex[:8]
