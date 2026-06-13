"""HAL JSONL ingestion adapters for Hermes and migrated mempalace memory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from centri.ingest.base import MessageAdapter, coerce_ts, flatten_content

logger = logging.getLogger(__name__)


def _hal_events_dir() -> Path:
    return Path.home() / ".hermes" / "hal" / "events"


def _jsonl_files(path: Path) -> Iterable[Path]:
    if path.is_file() and path.suffix == ".jsonl":
        yield path
    elif path.is_dir():
        yield from sorted(path.glob("*.jsonl"))


def _payload_text(obj: Dict[str, Any]) -> str:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    assert isinstance(payload, dict)
    parts: List[str] = []
    user_preview = payload.get("user_preview")
    assistant_preview = payload.get("assistant_preview")
    if user_preview:
        parts.append(f"User: {flatten_content(user_preview)}")
    if assistant_preview:
        parts.append(f"Assistant: {flatten_content(assistant_preview)}")
    if parts:
        return "\n".join(parts)
    for key in ("content", "text", "message", "summary", "fact", "decision"):
        if payload.get(key):
            return flatten_content(payload.get(key))
    if obj.get("summary") or obj.get("message"):
        return flatten_content(obj.get("summary") or obj.get("message"))
    return json.dumps(payload or obj, ensure_ascii=False, sort_keys=True)[:2000]


class _HalJsonlIngestor(MessageAdapter):
    """Shared reader for HAL event JSONL files."""

    def default_locations(self) -> List[Path]:
        return [_hal_events_dir()]

    def _include_file(self, path: Path) -> bool:
        return True

    def _include_event(self, obj: Dict[str, Any], path: Path) -> bool:
        return True

    def _role(self, obj: Dict[str, Any]) -> str:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if isinstance(payload, dict):
            if payload.get("assistant_preview"):
                return "assistant"
            if payload.get("user_preview"):
                return "user"
            if payload.get("role"):
                return str(payload.get("role"))
        return "model"

    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for fp in _jsonl_files(path):
            if not self._include_file(fp):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                logger.debug("HAL ingest: cannot read %s: %s", fp, exc)
                continue
            for lineno, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(obj, dict) or not self._include_event(obj, fp):
                    continue
                rid = obj.get("event_id") or obj.get("id") or lineno
                ts = obj.get("timestamp") or obj.get("timestamp_ms") or obj.get("ts")
                out.append(
                    {
                        "id": f"{fp.name}:{rid}",
                        "session_id": str(obj.get("session_id") or fp.stem),
                        "role": self._role(obj),
                        "content": _payload_text(obj),
                        "ts": coerce_ts(ts),
                    }
                )
        return out


class HermesIngestor(_HalJsonlIngestor):
    """Import Hermes/HAL memory and turn-sync JSONL events."""

    agent = "hermes"
    tool = "hermes"
    event_type = "ingest.hermes.message"
    event_source = "ingest.hermes"
    source_prefix = "hermes"
    fact_tags = ("ingest", "hermes", "hal", "transcript")

    def _include_file(self, path: Path) -> bool:
        name = path.name
        if name.startswith(("opencode_", "tool_result_")) or "mempalace" in name:
            return False
        return name.startswith(("hal_", "hermes_")) or name in {"hal_memory_hal.jsonl"}

    def _include_event(self, obj: Dict[str, Any], path: Path) -> bool:
        event_type = str(obj.get("event_type") or obj.get("type") or "")
        source = str(obj.get("source") or "")
        if "mempalace" in event_type or source == "mempalace":
            return False
        return source.startswith(("hermes", "hal_")) or event_type.startswith("hal.")

    def fact_topic(self, row: Dict[str, Any]) -> str:
        return f"hermes-session:{row.get('session_id') or row.get('id')}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        content = (row.get("content") or "").strip()
        return f"Hermes/HAL memory {row.get('session_id')}: {content[:400]}"


class MempalaceIngestor(_HalJsonlIngestor):
    """Import memories migrated from the old mempalace store into HAL JSONL."""

    agent = "mempalace"
    tool = "mempalace"
    event_type = "ingest.mempalace.message"
    event_source = "ingest.mempalace"
    source_prefix = "mempalace"
    fact_tags = ("ingest", "mempalace", "memory")

    def _include_file(self, path: Path) -> bool:
        return "mempalace" in path.name

    def _include_event(self, obj: Dict[str, Any], path: Path) -> bool:
        event_type = str(obj.get("event_type") or obj.get("type") or "")
        source = str(obj.get("source") or "")
        return source == "mempalace" or "mempalace" in event_type or "mempalace" in path.name

    def fact_topic(self, row: Dict[str, Any]) -> str:
        return f"mempalace:{row.get('session_id') or row.get('id')}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        content = (row.get("content") or "").strip()
        return f"Mempalace memory {row.get('session_id')}: {content[:400]}"
