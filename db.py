"""
Database abstraction layer.
Uses asyncpg (PostgreSQL) when DATABASE_URL is set, otherwise raises on startup.
"""
from __future__ import annotations

import logging
import os
import re
import traceback as _tb
from typing import Any
from urllib.parse import unquote

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _pg(sql: str) -> str:
    """Convert SQLite-style ? placeholders to PostgreSQL $1, $2, ..."""
    counter = 0

    def _replace(_m: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return re.sub(r"\?", _replace, sql)


def _parse_db_url(url: str) -> dict:
    """Parse DATABASE_URL with regex to avoid urlparse choking on special chars in password.

    Handles passwords with *, [, ] and other characters that confuse Python's urlparse,
    including the Supabase Connect-button format: postgresql://user:[password]@host/db
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # scheme://user:password@host:port/database  (password may contain brackets/special chars)
    m = re.match(
        r"postgresql://([^:@]+):(.+)@([^:/\[\]]+)(?::(\d+))?/(.+)",
        url,
    )
    if not m:
        raise ValueError(f"Cannot parse DATABASE_URL — unexpected format")
    user, password, host, port, database = m.groups()
    # Strip literal brackets added by Supabase Connect UI: [password] → password
    password = password.strip("[]")
    # Decode any percent-encoding (e.g. %2A → *)
    password = unquote(password)
    return {
        "host": host,
        "port": int(port or 5432),
        "user": user,
        "password": password,
        "database": database.split("?")[0],  # strip query params if any
    }


async def init_pool(retries: int = 4, delay: float = 2.0) -> None:
    """Crea el pool de conexiones. Reintenta con backoff porque el pooler de
    Supabase a veces responde 'Tenant or user not found' de forma transitoria."""
    global _pool
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL env var not set. "
            "Create a PostgreSQL database and set DATABASE_URL."
        )
    # Log sanitized URL for diagnosis (hide password)
    safe = re.sub(r"(:)[^@]+(@)", r"\1***\2", url)
    logger.info("DB init_pool — raw url (sanitized): %s", safe)
    logger.debug("DB init_pool — called from:\n%s", "".join(_tb.format_stack()))
    kwargs = _parse_db_url(url)
    logger.info("DB init_pool — parsed host=%s port=%s user=%s db=%s",
                kwargs["host"], kwargs["port"], kwargs["user"], kwargs["database"])

    import asyncio
    ultimo_error: Exception | None = None
    for intento in range(1, retries + 1):
        try:
            _pool = await asyncpg.create_pool(**kwargs, min_size=1, max_size=10,
                                              ssl="require", statement_cache_size=0)
            if intento > 1:
                logger.info("DB conectada en el intento %d", intento)
            return
        except Exception as exc:
            ultimo_error = exc
            if intento < retries:
                espera = delay * (2 ** (intento - 1))
                logger.warning("DB intento %d/%d falló (%s). Reintentando en %.0fs…",
                               intento, retries, str(exc)[:120], espera)
                await asyncio.sleep(espera)
    raise ultimo_error  # type: ignore[misc]


async def ensure_pool() -> asyncpg.Pool:
    """Devuelve el pool, reconectando si se perdió (p.ej. caída transitoria del
    pooler). Permite que la app se recupere sola sin reiniciar el servicio."""
    global _pool
    if _pool is None:
        logger.info("DB sin pool — intentando reconectar…")
        await init_pool()
    return _pool  # type: ignore[return-value]


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def execute(sql: str, *args: Any) -> str:
    return await (await ensure_pool()).execute(_pg(sql), *args)


async def executemany(sql: str, args_list: list[tuple]) -> None:
    async with (await ensure_pool()).acquire() as conn:
        await conn.executemany(_pg(sql), args_list)


async def fetch(sql: str, *args: Any) -> list[dict]:
    rows = await (await ensure_pool()).fetch(_pg(sql), *args)
    return [dict(r) for r in rows]


async def fetchrow(sql: str, *args: Any) -> dict | None:
    row = await (await ensure_pool()).fetchrow(_pg(sql), *args)
    return dict(row) if row else None


async def fetchval(sql: str, *args: Any) -> Any:
    return await (await ensure_pool()).fetchval(_pg(sql), *args)
