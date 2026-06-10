"""CENTRI redaction — scrub secrets from event payloads before persistence.

Events are the source of truth and are written to an append-only ledger, so any
secret that lands in a payload would be persisted forever. This module redacts
API keys, tokens, passwords, bearer headers, and private keys *before* the event
bus or DB ever writes them.

Adapted from the Hermes HAL event substrate (secrets-regex scrubbing). The
file-I/O / FTS / graph machinery from that module is intentionally dropped —
CENTRI only needs the scrubbing core wired into its own write path.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose *values* are always secrets regardless of content.
_SENSITIVE_KEY_RE = re.compile(
    r"^(?:"
    r"api[_-]?key|key|token|secret|password|passwd|authorization|credential|private[_-]?key|"
    r".*[_-](?:api[_-]?key|token|secret|password|passwd|credential|private[_-]?key)"
    r")$",
    re.IGNORECASE,
)

# Inline secret patterns found inside free text.
_BEARER_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)[A-Z0-9_]*\s*=\s*)(['\"]?)[^\s'\"]+\2"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9-]{10,})"
    r"(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----\s*[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)


def redact_text(text: str | None) -> str | None:
    """Redact inline secrets in a single string."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _ASSIGNMENT_RE.sub(r"\1\2***\2", text)
    text = _KNOWN_TOKEN_RE.sub("***", text)
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    return text


def redact_jsonable(value: Any) -> Any:
    """Recursively redact a JSON-able value.

    - Strings are scrubbed for inline secrets.
    - Dict values under a sensitive key name are fully redacted.
    - Lists/tuples/dicts are walked recursively.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [redact_jsonable(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            skey = str(key)
            if _SENSITIVE_KEY_RE.search(skey):
                out[skey] = item if item in (None, "") else REDACTED
            else:
                out[skey] = redact_jsonable(item)
        return out
    return value


def redact_event(event: Any) -> Any:
    """Redact a full event envelope in place-safe (returns a new structure).

    Applies to both top-level string fields and the nested ``payload`` so that
    secrets cannot leak through flattened/compat fields either.
    """
    return redact_jsonable(event)
