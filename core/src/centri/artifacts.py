"""CENTRI artifacts — user-facing proof of work."""

import logging
from typing import Any, Dict, List


logger = logging.getLogger(__name__)


class Artifacts:
    """Turn hand results into user-visible artifacts."""

    def __init__(self, db: Any):
        self._db = db

    async def collect_for_task(self, task_id: str) -> List[Dict[str, Any]]:
        """Gather artifacts for a completed task."""
        task = await self._db.get_task(task_id)
        if not task:
            return []
        artifacts: List[Dict[str, Any]] = []
        # Task result
        if task.get("result"):
            artifacts.append({
                "type": "summary",
                "title": f"Task result: {task_id[:8]}",
                "summary": task["result"],
            })
        # Session info
        if task.get("session_uid"):
            artifacts.append({
                "type": "session",
                "title": "OpenCode session",
                "session_uid": task["session_uid"],
            })
        # Approvals involved
        approvals = await self._db.pending_approvals(task_id=task_id)
        for a in approvals:
            artifacts.append({
                "type": "approval",
                "title": f"Approval: {a['label']}",
                "status": a["status"],
                "risk": a["risk"],
            })
        return artifacts

    async def summarize_for_user(self, task_id: str) -> str:
        """One-sentence spoken summary of what happened."""
        task = await self._db.get_task(task_id)
        if not task:
            return "I couldn't find that task."
        status = task.get("status", "unknown")
        desc = task.get("description", "")
        if status == "completed":
            return f"Done with: {desc}."
        if status == "failed":
            return f"The task failed: {desc}."
        if status == "cancelled":
            return f"Cancelled: {desc}."
        return f"Task status: {status}. {desc}."
