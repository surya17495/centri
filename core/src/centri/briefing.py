"""BriefingBuilder — turn a ContextPacket into a hands-ready prompt.

Hot-path: <1ms. Called on every utterance.
"""

import logging
from typing import List

from centri.schemas import ContextPacket

logger = logging.getLogger(__name__)


class BriefingBuilder:
    """Builds a concise briefing string from a ContextPacket."""

    def __init__(self, max_chars: int = 3500):
        self._max_chars = max_chars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build(self, packet: ContextPacket, user_text: str) -> str:
        """Build briefing for hands (OpenCode, etc.)."""
        sections: List[str] = []

        # 0. Identity snippet
        identity = self._identity(packet)
        if identity:
            sections.append(identity)

        # 1. Working directory / desktop context
        ctx = self._desktop(packet)
        if ctx:
            sections.append(ctx)

        # 2. Repo state
        repo = self._repo(packet)
        if repo:
            sections.append(repo)

        # 3. Active session
        sess = self._session(packet)
        if sess:
            sections.append(sess)

        # 4. Recent events (condensed)
        events = self._recent(packet)
        if events:
            sections.append(events)

        # 5. Letta archival recall / fallback context
        recall = self._recall(packet)
        if recall:
            sections.append(recall)

        # 6. Constraints
        constraints = self._constraints(packet)
        if constraints:
            sections.append(constraints)

        # 7. User's current utterance
        sections.append(f"User request: {user_text}")

        briefing = "\n\n".join(sections)
        if len(briefing) > self._max_chars:
            # Hard truncate with ellipsis note
            briefing = briefing[: self._max_chars - 50]
            briefing += "\n[...context truncated]"
        return briefing

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _identity(self, packet: ContextPacket) -> str:
        letta = packet.letta_identity or ""
        if isinstance(letta, str) and len(letta) > 10:
            return f"Agent identity: {letta[:500]}"
        return ""

    def _desktop(self, packet: ContextPacket) -> str:
        dc = packet.desktop_context
        if not dc:
            return ""
        parts: List[str] = []
        if dc.working_directory:
            parts.append(f"cwd: {dc.working_directory}")
        if dc.surface:
            parts.append(f"surface: {dc.surface}")
        if dc.title:
            parts.append(f"title: {dc.title}")
        if dc.file_path:
            parts.append(f"file: {dc.file_path}")
        if dc.selected_text:
            snippet = dc.selected_text[:200].replace("\n", " ")
            parts.append(f"selected: {snippet}")
        return "Desktop context:\n" + "\n".join(parts) if parts else ""

    def _repo(self, packet: ContextPacket) -> str:
        rs = packet.repo_state
        if not rs:
            return ""
        parts = [f"repo: {rs.name}"]
        if rs.branch:
            parts.append(f"branch: {rs.branch}")
        if rs.dirty:
            parts.append("workspace: dirty")
        if rs.ahead:
            parts.append(f"ahead: {rs.ahead}")
        if rs.behind:
            parts.append(f"behind: {rs.behind}")
        return "Repo state:\n" + "\n".join(parts)

    def _session(self, packet: ContextPacket) -> str:
        ss = packet.session_state
        if not ss:
            return ""
        parts = [f"hand: {ss.hand}", f"status: {ss.status}"]
        if ss.session_uid:
            parts.append(f"uid: {ss.session_uid}")
        if ss.summary:
            parts.append(f"summary: {ss.summary[:200]}")
        return "Active session:\n" + "\n".join(parts)

    def _recent(self, packet: ContextPacket) -> str:
        evs = packet.recent_events
        if not evs:
            return ""
        lines: List[str] = []
        for ev in evs[:8]:
            t = ev.get("type", "event")
            ts = ev.get("ts", "")[-14:-6] if ev.get("ts") else ""
            payload = ev.get("payload", {})
            text = payload.get("text", payload.get("description", ""))[:120]
            lines.append(f"  [{ts}] {t}: {text}")
        return "Recent events:\n" + "\n".join(lines)

    def _recall(self, packet: ContextPacket) -> str:
        if not packet.relevant_recall:
            return ""
        lines: List[str] = []
        for item in packet.relevant_recall[:5]:
            if not item:
                continue
            if item.startswith("Standing self (continuity):"):
                lines.append(self._standing_self_recall(item))
            else:
                lines.append(f"  - {item[:240]}")
        return "Relevant memory:\n" + "\n".join(lines) if lines else ""

    def _standing_self_recall(self, item: str) -> str:
        """Preserve the standing-self preamble in handoff prompts.

        Most recall entries are short bullets, but the curated memory item starts
        with the ambient standing self followed by cued ledger memory. Truncating
        that to 240 characters makes spawned hands feel like a fresh session. For
        this one explicit continuity block, keep the standing-self portion as a
        named preamble and then include the beginning of the cued memory.
        """
        text = item[:1200]
        return "\n".join(f"  {line}" if line else "" for line in text.splitlines())

    def _constraints(self, packet: ContextPacket) -> str:
        cs = packet.constraints
        if not cs:
            return ""
        return "Constraints:\n" + "\n".join(f"  - {c}" for c in cs)
