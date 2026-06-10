"""CENTRI Pydantic schemas — contracts only, no business logic."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CoordinatorResponse:
    response_type: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusResponse:
    repo: Optional[str] = None
    branch: Optional[str] = None
    dirty: bool = False
    active_thread: Optional[str] = None
    active_task: Optional[str] = None
    running_tasks: List[str] = field(default_factory=list)
    pending_approvals: int = 0
    blockers: List[str] = field(default_factory=list)
    message: str = ""


@dataclass
class DesktopContext:
    surface: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    file_path: Optional[str] = None
    selected_text: Optional[str] = None
    working_directory: Optional[str] = None
    listening: bool = False
    speaking: bool = False
    voice_activity_status: Optional[str] = None


@dataclass
class RepoState:
    id: Optional[str] = None
    root: str = ""
    name: str = ""
    branch: Optional[str] = None
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    last_seen: Optional[str] = None


@dataclass
class SessionState:
    id: Optional[str] = None
    session_uid: Optional[str] = None
    hand: str = ""
    status: str = ""
    repo_id: Optional[str] = None
    summary: str = ""
    last_seen: Optional[str] = None


@dataclass
class ContextPacket:
    active_thread: Optional[Dict[str, Any]] = None
    current_task: Optional[Dict[str, Any]] = None
    desktop_context: Optional[DesktopContext] = None
    repo_state: Optional[RepoState] = None
    session_state: Optional[SessionState] = None
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    letta_identity: Optional[str] = None
    relevant_recall: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)


@dataclass
class HandoffRequest:
    id: str = ""
    from_agent: str = "coordinator"
    to_capability: str = ""
    user_intent: str = ""
    context: Optional[ContextPacket] = None
    risk: str = "medium"
    approval_required: bool = False


@dataclass
class HandoffResult:
    status: str = ""
    summary: str = ""
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    session_uid: Optional[str] = None
    next_step: str = ""
    approval_request: Optional[Dict[str, Any]] = None
    events_to_record: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ApprovalRequest:
    id: str = ""
    task_id: Optional[str] = None
    thread_id: Optional[str] = None
    label: str = ""
    detail: str = ""
    risk: str = "medium"
    artifact: Optional[Dict[str, Any]] = None
    requested_action: str = ""


@dataclass
class Artifact:
    type: str = ""
    title: str = ""
    summary: str = ""
    paths: List[str] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    transcript_excerpt: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HandCapability:
    name: str = ""
    risk: str = "low"
    configured: bool = False
    healthy: bool = False
    detail: str = ""


@dataclass
class HealthSnapshot:
    db: str = "unknown"
    memory: str = "unknown"
    hands: List[HandCapability] = field(default_factory=list)
    jobs: str = "unknown"
    scheduler: str = "unknown"
    uptime_seconds: float = 0.0
