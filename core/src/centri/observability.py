"""CENTRI observability — transcripts, traces, health snapshots."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from centri.schemas import HealthSnapshot

logger = logging.getLogger(__name__)


class Observability:
    """Diagnostics, health, and audit."""

    def __init__(self):
        self._start = datetime.now(timezone.utc)

    async def health_snapshot(
        self,
        db: Any,
        memory: Any,
        hands: Any,
        jobs: Any,
        scheduler: Any,
    ) -> HealthSnapshot:
        now = datetime.now(timezone.utc)
        uptime = (now - self._start).total_seconds()
        db_status = "ok"
        try:
            await db.recent_events(limit=1)
        except Exception as exc:
            db_status = f"error:{exc}"
        memory_status = await memory.health() if memory else "not_configured"
        hand_caps = await hands.health() if hands else []
        jobs_status = "ok"
        scheduler_status = "ok"
        return HealthSnapshot(
            db=db_status,
            memory=memory_status,
            hands=hand_caps,
            jobs=jobs_status,
            scheduler=scheduler_status,
            uptime_seconds=uptime,
        )

    async def tool_trace(self, tool_call: Dict[str, Any]) -> None:
        logger.info("tool-trace: %s", tool_call)

    async def transcript_capture(self, channel: str, text: str, speaker: str) -> None:
        logger.info("transcript[%s] %s: %s", channel, speaker, text)

    async def model_call_log(self, role: str, model: str, duration_ms: float, tokens: int, cost: float) -> None:
        logger.info("model-call role=%s model=%s duration=%sms tokens=%d cost=%.6f", role, model, duration_ms, tokens, cost)

    async def job_failure_packet(self, task_id: str, error: str, context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "job_failure",
            "task_id": task_id,
            "error": error,
            "context": context,
            "ts": _now(),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
