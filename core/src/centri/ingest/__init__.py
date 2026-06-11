"""CENTRI ingestion adapters — tail external session stores into the spine.

Work done *outside* CENTRI (a developer running OpenCode/Cursor directly) is
invisible to the memory graph unless its session store is folded into the event
ledger. An ingestion adapter is a read-only tail of one such store: it reads new
rows since a per-source high-water mark, normalizes each into an
``ingest.<tool>.message`` event, and appends it to the spine (redaction applied
on write, importance ``low``). Consolidation then digests those events exactly
like native ones — no special-casing downstream.

Design invariants (ROADMAP 3b.3):

  - **Idempotent.** Re-running an ingest over the same store produces no
    duplicate events. Event ids are deterministic (``ingest:<source>:<row-key>``)
    and the high-water mark is persisted per source.
  - **Incremental.** Each pass reads only rows past the stored high-water mark.
  - **Source = the external store, not the tool.** The high-water mark is keyed
    by a caller-supplied ``source`` label so several ``opencode.db`` files (or
    other tools) can be tailed independently.
"""

from centri.ingest.opencode import OpenCodeIngestor, ingest_opencode_db

__all__ = ["OpenCodeIngestor", "ingest_opencode_db"]
