"""CENTRI memory orchestration.

Letta owns CENTRI's cognitive memory: core/RAM context and archival recall.
SQLite is only the operational ledger plus offline fallback projection.  Local
task summaries are written for audit/recovery, then promoted to Letta when it is
available; they are not CENTRI's primary semantic memory store.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.config import get_settings

logger = logging.getLogger(__name__)

try:
    import httpx
except ModuleNotFoundError:
    httpx = None  # type: ignore[assignment]


# ------------------------------------------------------------------
# Core block schemas (CENTRI-managed; synced to Letta on boot)
# ------------------------------------------------------------------
CORE_BLOCK_LABELS = {
    "persona",
    "human",
    "user_preferences",
    "project_context",
    "operational_constraints",
}

PROCEDURAL_MEMORY_LIMIT = 200  # recent procedural items per channel


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    """Memory orchestration with Letta integration and local procedural cache."""

    def __init__(self, db: Any, event_bus: Any = None, letta_client: Any = None):
        self._db = db
        self._event_bus = event_bus
        self._settings = get_settings()
        self._base = (self._settings.letta_url or "").rstrip("/")
        self._api_key = self._settings.letta_api_key or ""
        self._agent_id = self._settings.letta_agent_id
        self._client: Optional[Any] = letta_client

    # ------------------------------------------------------------------
    # Letta HTTP client
    # ------------------------------------------------------------------
    def _client_or_none(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        if not self._base or httpx is None:
            return None
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else None,
            timeout=10.0,
            limits=httpx.Limits(max_connections=5),
        )
        return self._client

    # ------------------------------------------------------------------
    # Identity — core blocks from Letta
    # ------------------------------------------------------------------
    async def identity(self) -> Dict[str, Any]:
        """Letta core memory blocks. Returns minimal placeholder if Letta offline."""
        c = self._client_or_none()
        if not c:
            # Offline fallback: last Letta core/RAM snapshot cached in SQLite.
            return await self._local_identity()
        try:
            r = await asyncio.wait_for(
                c.get(f"/v1/agents/{self._agent_id}/memory"),
                timeout=5.0,
            )
            if r.status_code == 200:
                data = r.json()
                identity = {
                    "blocks": data.get("blocks", []),
                    "persona": data.get("persona", ""),
                    "human": data.get("human", ""),
                    "agent_id": self._agent_id,
                }
                # Cache locally
                await self._save_local_identity(identity)
                return identity
        except Exception as exc:
            logger.warning("Letta identity unavailable: %s", exc)
        return await self._local_identity()

    async def _local_identity(self) -> Dict[str, Any]:
        """Fallback identity from last known state stored in SQLite."""
        try:
            row = await self._db.get_identity_cache(self._agent_id)
            if row:
                blocks = []
                try:
                    blocks = json.loads(row.get("blocks_json", "{}")).get("blocks", [])
                except Exception:
                    pass
                return {
                    "blocks": blocks,
                    "persona": row.get("persona", ""),
                    "human": row.get("human", ""),
                    "agent_id": self._agent_id,
                    "degraded": True,
                    "last_updated": row.get("updated_at"),
                }
        except Exception:
            logger.debug("Local identity read failed", exc_info=True)
        return {
            "blocks": [],
            "persona": "",
            "human": "",
            "agent_id": self._agent_id,
            "degraded": True,
        }

    async def _save_local_identity(self, identity: Dict[str, Any]) -> None:
        """Persist Letta identity snapshot for offline use."""
        try:
            await self._db.upsert_identity_cache(
                agent_id=str(identity.get("agent_id", self._agent_id)),
                blocks_json=json.dumps({"blocks": identity.get("blocks", [])}),
                persona=str(identity.get("persona", "")),
                human=str(identity.get("human", "")),
                updated_at=_now(),
            )
        except Exception:
            logger.debug("Local identity save failed", exc_info=True)

    # ------------------------------------------------------------------
    # Recall — Letta first, SQLite fallback only
    # ------------------------------------------------------------------
    async def recall(self, query: str, limit: int = 5) -> List[str]:
        """Recall from Letta archival memory, falling back locally only offline."""
        if self._client_or_none():
            try:
                letta = await asyncio.wait_for(self._letta_recall(query, limit=limit), timeout=5.0)
                if letta:
                    await self._publish_memory_recall(letta, source="letta")
                    return letta[:limit]
            except asyncio.TimeoutError:
                logger.warning("Letta recall timed out; using local fallback")

        fallback = await self._fallback_recall(query, limit=limit)
        if fallback:
            await self._publish_memory_recall(fallback, source="sqlite_fallback")
        return fallback[:limit]

    async def _letta_recall(self, query: str, limit: int = 5) -> List[str]:
        c = self._client_or_none()
        if not c:
            return []
        try:
            r = await asyncio.wait_for(
                c.post(
                    f"/v1/agents/{self._agent_id}/messages",
                    json={"messages": [{"role": "user", "text": f"recall: {query}"}]},
                ),
                timeout=5.0,
            )
            if r.status_code == 200:
                data = r.json()
                # Letta returns a list of messages; content varies.
                # Extract string fragments safely.
                results: List[str] = []
                messages = data if isinstance(data, list) else data.get("messages", [])
                for msg in messages:
                    if isinstance(msg, dict):
                        for key in ("text", "content", "message"):
                            val = msg.get(key)
                            if isinstance(val, str) and val.strip():
                                results.append(val.strip())
                            elif isinstance(val, list):
                                for item in val:
                                    if isinstance(item, str) and item.strip():
                                        results.append(item.strip())
                return results[:limit]
        except Exception as exc:
            logger.warning("Letta recall unavailable: %s", exc)
        return []

    async def _procedural_recall(self, query: str, limit: int = 5) -> List[str]:
        return await self._fallback_recall(query, limit=limit)

    async def _fallback_recall(self, query: str, limit: int = 5) -> List[str]:
        """Offline fallback from SQLite ledger projections, not primary memory."""
        try:
            recent = await self._db.recent_events(limit=PROCEDURAL_MEMORY_LIMIT)
        except Exception:
            recent = []
        q_lower = query.lower()
        results: List[str] = []
        for ev in recent:
            payload = ev.get("payload_json", "")
            if isinstance(payload, dict):
                text = " ".join(
                    str(v) for k, v in payload.items() if isinstance(v, str)
                )
            elif isinstance(payload, str):
                try:
                    d = json.loads(payload)
                    text = " ".join(
                        str(v) for k, v in d.items() if isinstance(v, str)
                    )
                except json.JSONDecodeError:
                    text = payload
            else:
                text = str(payload)
            if any(term in text.lower() for term in q_lower.split()[:4]):
                # Prefer concise summaries if available
                summary_obj = None
                try:
                    summary_obj = json.loads(payload) if isinstance(payload, str) else payload
                except Exception:
                    pass
                if summary_obj and isinstance(summary_obj, dict) and "synthesized_summary" in summary_obj:
                    results.append(f"[fallback] {summary_obj['synthesized_summary']}")
                elif summary_obj and isinstance(summary_obj, dict) and "action" in summary_obj and "outcome" in summary_obj:
                    results.append(f"[fallback] {summary_obj['action']} -> {summary_obj['outcome']}")
                else:
                    results.append(f"[fallback] {text[:200].replace(chr(10), ' ')}")
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Learn — write to Letta and local procedural store
    # ------------------------------------------------------------------
    async def learn(self, event_or_result: Dict[str, Any]) -> bool:
        """Promote meaningful outcomes to Letta; keep SQLite as ledger/fallback."""
        synthesized = await self._synthesize_task(event_or_result)

        c = self._client_or_none()
        if not c:
            return await self._learn_fallback(event_or_result, synthesized)
        try:
            msg = f"[memory_update] {json.dumps(synthesized or event_or_result, default=str)}"
            r = await asyncio.wait_for(
                c.post(
                    f"/v1/agents/{self._agent_id}/messages",
                    json={"messages": [{"role": "user", "text": msg}]},
                ),
                timeout=5.0,
            )
            if r.status_code == 200:
                await self._record_memory_promotion(event_or_result, synthesized)
                return True
        except Exception as exc:
            logger.warning("Letta learn failed: %s", exc)
        return await self._learn_fallback(event_or_result, synthesized)

    async def _synthesize_task(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """On task completion, produce a short synthetic summary for briefing reuse."""
        ev_type = event.get("type", "")
        if ev_type not in ("task.completed", "task.failed", "task.started", "task_started"):
            return None
        task_id = event.get("task_id", "")
        description = event.get("description", "")
        thread_id = event.get("thread_id")
        repo_id = event.get("repo_id")
        status = "completed" if "completed" in ev_type else ("failed" if "failed" in ev_type else "started")

        # Build a minimal synthetic entry
        synthesized = {
            "action": description[:200],
            "status": status,
            "outcome": event.get("summary", event.get("error", "")),
            "synthesized_summary": f"{description[:100]} ({status})",
            "task_id": task_id,
            "thread_id": thread_id,
            "repo_id": repo_id,
            "memory_owner": "letta",
            "local_role": "ledger_projection",
        }
        try:
            ts = _now()
            await self._db.append_event(
                event_id=f"synth-{task_id}-{ts}",
                type="procedural.memory",
                source="memory.synthesis",
                ts=ts,
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
                payload=synthesized,
            )
            await self._db.append_event(
                event_id=f"memory-synth-{task_id}-{ts}",
                type="memory.synthesized",
                source="memory",
                ts=ts,
                thread_id=thread_id,
                task_id=task_id,
                repo_id=repo_id,
                payload=synthesized,
            )
            if self._event_bus is not None:
                await self._event_bus.publish(
                    {
                        "type": "memory.synthesized",
                        "ts": ts,
                        "source": "memory",
                        "thread_id": thread_id,
                        "task_id": task_id,
                        "repo_id": repo_id,
                        "payload": synthesized,
                        "status": synthesized["status"],
                        "summary": synthesized["synthesized_summary"],
                    }
                )
            return synthesized
        except Exception:
            logger.debug("Task synthesis write failed", exc_info=True)
        return synthesized

    async def _learn_procedural(self, event_or_result: Dict[str, Any]) -> bool:
        return await self._learn_fallback(event_or_result, None)

    async def _learn_fallback(
        self,
        event_or_result: Dict[str, Any],
        synthesized: Optional[Dict[str, Any]],
    ) -> bool:
        """Letta-unavailable fallback marker in the SQLite ledger."""
        try:
            ts = _now()
            await self._db.append_event(
                event_id=f"memory-fallback-{ts}",
                type="memory.fallback",
                source="memory",
                ts=ts,
                task_id=event_or_result.get("task_id"),
                thread_id=event_or_result.get("thread_id"),
                repo_id=event_or_result.get("repo_id"),
                payload={"event": event_or_result, "synthesized": synthesized},
            )
            return True
        except Exception:
            return False

    async def _record_memory_promotion(
        self,
        event_or_result: Dict[str, Any],
        synthesized: Optional[Dict[str, Any]],
    ) -> None:
        try:
            ts = _now()
            await self._db.append_event(
                event_id=f"memory-promoted-{ts}",
                type="memory.promoted",
                source="memory",
                ts=ts,
                task_id=event_or_result.get("task_id"),
                thread_id=event_or_result.get("thread_id"),
                repo_id=event_or_result.get("repo_id"),
                payload={"target": "letta", "synthesized": synthesized},
            )
        except Exception:
            logger.debug("Memory promotion ledger write failed", exc_info=True)

    async def _publish_memory_recall(self, recall: List[str], source: str) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                {
                    "type": "memory.recall",
                    "ts": _now(),
                    "source": "memory",
                    "payload": {"recall": recall, "memory_source": source},
                    "recall": recall,
                    "memory_source": source,
                }
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    async def health(self) -> str:
        c = self._client_or_none()
        if not c:
            return "not_configured"
        try:
            r = await asyncio.wait_for(c.get("/v1/health"), timeout=3.0)
            if r.status_code == 200:
                return "healthy"
            return f"unhealthy:{r.status_code}"
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as exc:
            return f"offline:{type(exc).__name__}"
