#!/usr/bin/env python3
"""Bring up an embedded Postgres+pgvector for a real Letta server, with no Docker.

``pgserver`` ships a self-contained Postgres (and the pgvector ``vector`` extension)
and starts it on a *unix socket only* (``-h ""``). Letta needs a TCP ``pg_uri``
(asyncpg async runtime + pg8000 sync/alembic), so we stop the socket-only postmaster
and restart it with explicit ``-c listen_addresses=127.0.0.1 -c port=<port>``, then
create the ``letta`` database and enable ``CREATE EXTENSION vector``.

After the database is up we also materialise Letta's ORM schema (the wheel ships
no alembic migrations) and apply one Postgres fixup so ``messages.sequence_id``
behaves like the alembic-created identity. See ``schema()`` for the why.

Commands:

  python bench_pg_bootstrap.py up   <pgdata> [port]   # init + start TCP + db + pgvector + schema
  python bench_pg_bootstrap.py down <pgdata>           # stop the postmaster
  python bench_pg_bootstrap.py schema [port]           # (re)create schema only

The postmaster keeps running after ``up`` returns (pg_ctl start is detached), so the
shell script can start Letta against it and tear it down later with ``down``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _bindir() -> Path:
    import pgserver

    return Path(pgserver.__file__).parent / "pginstall" / "bin"


def _pg_ctl() -> str:
    return str(_bindir() / "pg_ctl")


def _psql() -> str:
    return str(_bindir() / "psql")


def _wait_tcp(port: int, timeout: float = 30.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.5)
        finally:
            s.close()
    return False


def _psql_tcp(port: int, sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
    return subprocess.run(
        [_psql(), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", db, "-tAc", sql],
        capture_output=True,
        text=True,
    )


def up(pgdata: Path, port: int) -> str:
    import pgserver

    pgdata.mkdir(parents=True, exist_ok=True)
    # Initialise pgdata + start the bundled socket-only postmaster.
    pgserver.get_server(pgdata, cleanup_mode=None)
    # Stop it and re-launch on TCP. pg_ctl start is non-blocking-detached, so the
    # postmaster outlives this process.
    subprocess.run([_pg_ctl(), "-D", str(pgdata), "stop", "-m", "fast", "-w", "-t", "30"],
                   capture_output=True, text=True)
    r = subprocess.run(
        [_pg_ctl(), "-D", str(pgdata), "-l", str(pgdata / "tcp.log"),
         "-o", f"-c listen_addresses=127.0.0.1 -c port={port}", "start", "-w", "-t", "30"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"pg_ctl start failed: {r.stderr or r.stdout}")
    if not _wait_tcp(port):
        log = (pgdata / "tcp.log").read_text()[-1000:] if (pgdata / "tcp.log").exists() else ""
        raise SystemExit(f"Postgres did not open TCP {port}. log tail:\n{log}")

    # Idempotent: CREATE DATABASE errors if it exists; ignore that case.
    exists = _psql_tcp(port, "SELECT 1 FROM pg_database WHERE datname='letta';").stdout.strip()
    if exists != "1":
        c = _psql_tcp(port, "CREATE DATABASE letta;")
        if c.returncode != 0 and "already exists" not in (c.stderr or ""):
            raise SystemExit(f"CREATE DATABASE letta failed: {c.stderr}")
    v = _psql_tcp(port, "CREATE EXTENSION IF NOT EXISTS vector;", db="letta")
    if v.returncode != 0:
        raise SystemExit(f"CREATE EXTENSION vector failed: {v.stderr}")
    ver = _psql_tcp(port, "SELECT extversion FROM pg_extension WHERE extname='vector';", db="letta")
    uri = f"postgresql://postgres@127.0.0.1:{port}/letta"
    print(f"pgvector={ver.stdout.strip()} uri={uri}")
    return uri


def verify_async(port: int) -> None:
    """Prove the *exact* connection Letta's request-time runtime makes.

    Letta boots/migrates over pg8000 (sync) but serves every request over an
    asyncpg engine built at import time from ``convert_to_async_uri(letta_pg_uri)``
    — which rewrites ``?sslmode=disable`` to ``?ssl=disable`` and targets
    ``127.0.0.1:<port>``. In a live run this async path failed with
    ``ConnectionRefusedError [Errno 111]`` while the sync boot path worked, so the
    sync ``_wait_tcp`` check is not sufficient evidence that Letta will be able to
    talk to the DB. Reproduce asyncpg's connect here, against the ``letta`` DB with
    ``ssl=False`` (matching ``ssl=disable``), and fail loudly *before* booting
    Letta if it cannot — turning a confusing request-time 500 into an explicit
    bring-up error. asyncpg does not honour ``HTTPS_PROXY``/``ALL_PROXY``, so this
    is a pure loopback TCP check.
    """
    import asyncio

    import asyncpg

    async def _check() -> int:
        conn = await asyncpg.connect(
            host="127.0.0.1", port=port, user="postgres", database="letta",
            ssl=False, timeout=10,
        )
        try:
            return await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    try:
        v = asyncio.run(_check())
    except Exception as e:  # noqa: BLE001 — surface the precise asyncpg failure
        raise SystemExit(
            f"asyncpg verification FAILED on 127.0.0.1:{port}/letta "
            f"({type(e).__name__}: {e}). This is the same async path Letta's "
            f"request runtime uses; booting Letta now would 500 on every request. "
            f"Check that the postmaster is actually listening on 127.0.0.1:{port} "
            f"(a stale/competing postgres or an aborted prior run can leave it down)."
        )
    if v != 1:
        raise SystemExit(f"asyncpg verification returned {v!r}, expected 1")
    print(f"asyncpg OK: request-time path reaches 127.0.0.1:{port}/letta (SELECT 1=1)")


def schema(port: int) -> None:
    """Create Letta's ORM schema in the ``letta`` DB, with the Postgres fixups
    that the (absent-from-the-wheel) alembic migrations would otherwise apply.

    The Letta wheel ships no ``alembic.ini``/versions, so the canonical way to
    materialise its schema offline is ``Base.metadata.create_all()`` over the
    registered ORM models. That reproduces tables/indexes/FKs faithfully, with
    one gap: ``messages.sequence_id`` is declared ``server_default=FetchedValue()``
    — i.e. "the database fills it" — which on real Letta is an alembic-created
    identity/sequence. ``create_all`` emits no such default, so inserts fail with
    ``null value in column "sequence_id" ... violates not-null``. We add a
    Postgres ``GENERATED BY DEFAULT AS IDENTITY`` to close exactly that gap (the
    SQLite path is handled by an ORM event listener and is irrelevant here).
    """
    # pg8000 sync driver; force Letta to treat this as a Postgres engine by
    # giving it a pg_uri before any letta import reads settings.
    pg_uri = f"postgresql+pg8000://postgres@127.0.0.1:{port}/letta"
    os.environ["LETTA_PG_URI"] = pg_uri

    import letta.orm  # noqa: F401 — registers all ORM models on Base.metadata
    from letta.orm.base import Base
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(pg_uri)
    Base.metadata.create_all(engine)

    # Apply the identity fixup idempotently: only if it isn't already an identity.
    with engine.begin() as conn:
        is_identity = conn.execute(
            text(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_name='messages' AND column_name='sequence_id'"
            )
        ).scalar()
        if is_identity == "NO":
            conn.execute(
                text("ALTER TABLE messages ALTER COLUMN sequence_id "
                     "ADD GENERATED BY DEFAULT AS IDENTITY")
            )
    n_tables = len(inspect(engine).get_table_names())
    engine.dispose()
    print(f"schema ready: {n_tables} tables, messages.sequence_id is IDENTITY")


def down(pgdata: Path) -> None:
    subprocess.run([_pg_ctl(), "-D", str(pgdata), "stop", "-m", "fast", "-w", "-t", "30"],
                   capture_output=True, text=True)
    print("postgres stopped")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, pgdata = sys.argv[1], Path(sys.argv[2]).expanduser().resolve()
    if cmd == "up":
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 5432
        up(pgdata, port)
        schema(port)
        verify_async(port)
    elif cmd == "down":
        down(pgdata)
    elif cmd == "schema":
        # pgdata arg is ignored here; schema targets the running TCP server.
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 5432
        schema(port)
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
