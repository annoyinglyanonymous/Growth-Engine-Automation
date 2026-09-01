"""Async connection pool, opened and closed by the FastAPI lifespan."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import settings

# Windows defaults to ProactorEventLoop, which has no add_reader(), so psycopg
# refuses to run async on it: "Psycopg cannot use the 'ProactorEventLoop' to
# run in async mode". The pool hides that as a PoolTimeout after 30s, which
# looks like a network problem and is not one.
#
# Set before the pool is constructed, and before uvicorn creates its loop --
# importing this module is what installs the policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pool = AsyncConnectionPool(
    conninfo=settings.database_url,
    min_size=settings.pool_min_size,
    max_size=settings.pool_max_size,
    kwargs={"row_factory": dict_row},
    # Validate a connection before handing it to a request.
    #
    # Observed: after the server sat idle, the first /context returned
    # "psycopg.OperationalError: consuming input failed: server closed the
    # connection unexpectedly" from require_brand -- the pool's very first
    # query. Supabase's pooler drops idle server connections, and without a
    # check the pool cheerfully lends out the dead socket. The failure looks
    # like a bug in whatever query happened to run first, which is how twenty
    # minutes went into reading a refactor that was fine.
    #
    # Costs one round trip per checkout. Worth it: the alternative is that
    # every caller has to retry, and a generator half way through writing an
    # asset pack is the worst place to discover that.
    check=AsyncConnectionPool.check_connection,
    # Retire connections before the pooler does it for us.
    max_idle=settings.pool_max_idle,
    # Opened explicitly in the lifespan rather than at import, so importing
    # this module never blocks on the network.
    open=False,
)


@asynccontextmanager
async def cursor() -> AsyncIterator:
    async with pool.connection() as conn, conn.cursor() as cur:
        yield cur


async def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    async with cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    async with cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()
