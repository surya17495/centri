"""CENTRI context assembly — builds ContextPacket for coordinator and hands."""

from datetime import datetime, timezone
from typing import Any, Optional

from centri.schemas import ContextPacket, RepoState, SessionState


class ContextAssembler:
    """Assembles the ContextPacket from runtime state, memory, and live inputs."""

    def __init__(self, db: Any, memory: Any, desktop: Optional[Any] = None):
        self._db = db
        self._memory = memory
        self._desktop = desktop

    async def build(
        self,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> ContextPacket:
        """Build a ContextPacket for the current turn."""
        now = datetime.now(timezone.utc).isoformat()

        # Active thread
        active_thread = None
        if thread_id:
            active_thread = await self._db.get_thread(thread_id)

        # Current task
        current_task = None
        if task_id:
            current_task = await self._db.get_task(task_id)
        elif active_thread:
            tasks = await self._db.list_tasks(thread_id=active_thread.get("id"), status="running")
            if tasks:
                current_task = tasks[0]

        # Desktop context (if available)
        desktop_context = None
        if self._desktop:
            desktop_context = await self._desktop.current_context()

        # Repo state
        repo_state = None
        repo = await self._db.active_repo()
        if repo:
            repo_state = RepoState(
                id=repo.get("id"),
                root=repo.get("root", ""),
                name=repo.get("name", ""),
                branch=repo.get("branch"),
                dirty=bool(repo.get("dirty", 0)),
                ahead=int(repo.get("ahead", 0)),
                behind=int(repo.get("behind", 0)),
                last_seen=repo.get("last_seen"),
            )

        # Session state
        session_state = None
        latest = await self._db.latest_session("opencode")
        if latest:
            session_state = SessionState(
                id=latest.get("id"),
                session_uid=latest.get("session_uid"),
                hand="opencode",
                status=latest.get("status", "unknown"),
                repo_id=latest.get("repo_id"),
                summary=latest.get("summary", ""),
                last_seen=latest.get("last_seen"),
            )

        # Recent events
        recent_events = await self._db.recent_events(limit=20, thread_id=thread_id)

        # Identity + recall
        letta_identity = await self._memory.identity()
        relevant_recall: list[str] = []
        if active_thread:
            recall = await self._memory.recall(query=active_thread.get("goal", ""), limit=3)
            relevant_recall = recall if recall else []

        # Constraints from permissions
        constraints: list[str] = []
        constraints.append("minimal impact")
        constraints.append("no brittle mocks")
        constraints.append("preserve existing tests")
        if repo_state and repo_state.dirty:
            constraints.append("workspace is dirty: commit or stash before risky changes")

        return ContextPacket(
            active_thread=active_thread,
            current_task=current_task,
            desktop_context=desktop_context,
            repo_state=repo_state,
            session_state=session_state,
            recent_events=recent_events,
            letta_identity=str(letta_identity) if letta_identity else None,
            relevant_recall=relevant_recall,
            constraints=constraints,
        )
