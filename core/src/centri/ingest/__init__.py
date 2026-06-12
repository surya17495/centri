"""CENTRI ingestion adapters — tail external session stores into the spine.

Work done *outside* CENTRI (a developer running OpenCode / Claude Code / Cursor
directly) is invisible to the memory graph unless its session store is folded
into the event ledger. An ingestion adapter is a read-only tail of one such
store: it reads new rows since a per-source high-water mark, normalizes each into
an ``ingest.<tool>.message`` event, and appends it to the spine (redaction
applied on write, importance ``low``). Consolidation then digests those events
exactly like native ones — no special-casing downstream.

3b.3 shipped a single OpenCode adapter; 3b.4 generalizes the machinery into an
*adapter registry* (:class:`IngestRegistry`) with per-agent adapters that share
the same HWM/idempotency/redaction core, adds Claude Code + Cursor adapters, and
adds discovery (probe well-known paths) + bootstrap (one-time full import on a
fresh install). Because ingestion is high-water-mark based, **bootstrap is the
first tick**: the one-time import and the continuous ambient tail are the same
code path.

Design invariants:

  - **Idempotent.** Re-running an ingest over the same store produces no
    duplicate events. Event ids are deterministic (``ingest:<source>:<row-key>``)
    and the high-water mark is persisted per source.
  - **Incremental.** Each pass reads only rows past the stored high-water mark.
  - **Source = the external store, not the tool.** The high-water mark is keyed
    by a caller-supplied ``source`` label so several stores (or agents) tail
    independently.
  - **Read-only + schema-tolerant.** Stores are opened read-only; column/key/JSON
    shapes vary across agent versions, so readers degrade honestly (skip with a
    logged reason) when expected tables are missing.
"""

from centri.ingest.base import DiscoveredSource, IngestSpec, MessageAdapter
from centri.ingest.claude_code import ClaudeCodeIngestor
from centri.ingest.cursor import CursorIngestor
from centri.ingest.generic import GenericAdapterConfig, GenericIngestor
from centri.ingest.opencode import OpenCodeIngestor, ingest_opencode_db
from centri.ingest.registry import IngestConfig, IngestRegistry

__all__ = [
    "MessageAdapter",
    "DiscoveredSource",
    "IngestSpec",
    "OpenCodeIngestor",
    "ingest_opencode_db",
    "ClaudeCodeIngestor",
    "CursorIngestor",
    "GenericIngestor",
    "GenericAdapterConfig",
    "IngestRegistry",
    "IngestConfig",
]
