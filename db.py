"""
Database abstraction layer.
Uses asyncpg (PostgreSQL) when DATABASE_URL is set, otherwise raises on startup.
"""
from __future__ import annotations

import os
import re
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


def _pg(sql: str) -> str:
    """Convert SQLite-style ? placeholders to PostgreSQL $1, $2, ..."""
    counter = 0

    def _replace(_m: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return re.sub(r"\?", _replace, sql)


async def init_pool() -> None:
    global _pool
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL env var not set. "
            "Create a PostgreSQL database and set DATABASE_URL."
        )
    # Render's postgres:// URLs need postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    _pool = await asyncpg.create_pool(url, min_size=1, max_size=10)


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def execute(sql: str, *args: Any) -> str:
    return await get_pool().execute(_pg(sql), *args)


async def executemany(sql: str, args_list: list[tuple]) -> None:
    async with get_pool().acquire() as conn:
        await conn.executemany(_pg(sql), args_list)


async def fetch(sql: str, *args: Any) -> list[dict]:
    rows = await get_pool().fetch(_pg(sql), *args)
    return [dict(r) for r in rows]


async def fetchrow(sql: str, *args: Any) -> dict | None:
    row = await get_pool().fetchrow(_pg(sql), *args)
    return dict(row) if row else None


async def fetchval(sql: str, *args: Any) -> Any:
    return await get_pool().fetchval(_pg(sql), *args)
