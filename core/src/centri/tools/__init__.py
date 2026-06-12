"""CENTRI tools — first-class tool contract (Decision 11).

Tools are a first-class contract parallel to a *hand*: every invocation is an
event on the spine with receipts, side-effectful tools round-trip the existing
approval gate before execution, and the output is foldable into the memory graph
by consolidation. ``ToolRegistry.invoke`` is the ONLY execution path so the event
trail, redaction, approval gating, and the consolidation fact hint are uniform
across every provider.
"""

from centri.tools.base import (
    ToolProvider,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    is_read_only_slug,
)

__all__ = [
    "ToolProvider",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "is_read_only_slug",
]
