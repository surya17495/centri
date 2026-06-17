"""CENTRI CLI entry point.

``centri`` with no arguments launches the HTTP core (uvicorn). Subcommands are
admin/maintenance one-shots that boot the minimal pieces they need, run, and
exit — they never start the server. Follows the spine-first contract: the
``memory rebuild`` command re-derives the typed graph from the lossless event
ledger and records its own receipt-bearing spine event.
"""

import argparse
import asyncio
import logging
import sys

import uvicorn

from centri.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def serve() -> None:
    settings = get_settings()
    logger.info("Starting CENTRI on %s:%d", settings.core_host, settings.core_port)
    uvicorn.run(
        "centri.app:app",
        host=settings.core_host,
        port=settings.core_port,
        reload=False,
        log_level="info",
    )


async def _memory_rebuild(embed: bool) -> int:
    """Re-derive the typed graph from the spine, optionally embedding every node.

    Reads the full event ledger and replays it oldest-first through the
    deterministic consolidator (proving re-derivability). When ``--embed`` is set
    the configured embedding provider is wired into the consolidator so each
    re-derived decision/fact gets its write-time vector — the one-shot backfill
    for existing nodes called for in the build spec. Honest-unavailable: with no
    embedding model configured the rebuild still succeeds, it just writes no
    vectors and reports ``embedding:unavailable``. Emits a ``memory.rebuild``
    spine event recording the policy version, provider stamp, and node counts.
    """
    from datetime import datetime, timezone

    from centri.consolidation import Consolidator
    from centri.curation import (
        NullEmbeddingProvider,
        RankWeights,
        active_policy_version,
        resolve_embedding_provider,
    )
    from centri.db import Database
    from centri.memory_graph import MemoryGraph

    settings = get_settings()
    db = Database()
    graph = MemoryGraph(db)
    await graph.ensure_tables()

    provider = resolve_embedding_provider(settings) if embed else NullEmbeddingProvider()
    stamp = getattr(provider, "stamp", "embedding:unavailable")
    available = bool(getattr(provider, "available", False))
    if embed and not available:
        logger.warning(
            "embeddings requested but no provider configured (%s); rebuilding without vectors",
            stamp,
        )

    consolidator = Consolidator(db, graph, embedding_provider=provider)
    written = await consolidator.rebuild_from_events()
    counts = await graph.counts()
    # Policy version the read path will run under once embeddings are weighted on.
    policy_version = active_policy_version(RankWeights.from_settings(settings))

    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "embed": embed,
        "embedding_available": available,
        "embedding_stamp": stamp,
        "policy_version": policy_version,
        "written": written,
        "counts": counts,
    }
    try:
        await db.append_event(
            event_id=f"memory-rebuild-{ts}",
            type="memory.rebuild",
            source="cli",
            ts=ts,
            payload=payload,
        )
    except Exception:
        logger.warning("memory.rebuild ledger write failed", exc_info=True)

    await db.close()
    logger.info(
        "memory rebuild complete: wrote %d objects, embedding=%s (%s), counts=%s",
        written,
        "on" if available else "off",
        stamp,
        counts,
    )
    return 0


async def _memory_reconcile() -> int:
    """Close legacy-provenance open loops (HAL/Hermes/mempalace).

    Marks any still-open or dormant loop whose tags include ``hermes``,
    ``hal``, or ``mempalace`` as ``done``. The rows stay in the graph — only
    the state changes — so audit history is preserved while the prospective
    surface stops pushing legacy intents.
    """
    from centri.db import Database
    from centri.memory_graph import MemoryGraph

    db = Database()
    graph = MemoryGraph(db)
    await graph.ensure_tables()
    closed = await graph.reconcile_legacy_loops()
    await db.close()
    logger.info("memory reconcile: closed %d legacy open loop(s)", closed)
    return 0


def run(argv: list[str] | None = None) -> None:
    """CLI dispatch. No subcommand => launch the server (backward compatible)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        serve()
        return

    parser = argparse.ArgumentParser(prog="centri", description="CENTRI core")
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Run the HTTP core (default).")
    serve_p.set_defaults(func=lambda _args: serve())

    memory_p = sub.add_parser("memory", help="Memory maintenance commands.")
    memory_sub = memory_p.add_subparsers(dest="memory_command")
    rebuild_p = memory_sub.add_parser(
        "rebuild", help="Re-derive the typed graph from the event spine."
    )
    rebuild_p.add_argument(
        "--embed",
        action="store_true",
        help="Compute write-time vectors using the configured embedding provider.",
    )
    rebuild_p.set_defaults(
        func=lambda args: sys.exit(asyncio.run(_memory_rebuild(args.embed)))
    )

    reconcile_p = memory_sub.add_parser(
        "reconcile",
        help="Close legacy HAL/Hermes/mempalace open loops (non-destructive).",
    )
    reconcile_p.set_defaults(
        func=lambda args: sys.exit(asyncio.run(_memory_reconcile()))
    )

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        if args.command == "memory":
            memory_p.print_help()
        else:
            parser.print_help()
        return
    func(args)


if __name__ == "__main__":
    run()
