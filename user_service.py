"""
Gestión de usuarios del sistema con roles y permisos.
Tabla 'system_users' en SQLite (mismo audit.db).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from audit_service import DB_PATH

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS system_users (
    id           TEXT PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    email        TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'viewer',
    full_name    TEXT DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_login   TEXT
)
"""

VALID_ROLES = {"admin", "academico", "viewer"}


async def init_users_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_USERS)
        await db.commit()


async def seed_admin(username: str, password_hash: str) -> None:
    """Insert initial admin if no users exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM system_users")
        count = (await cursor.fetchone())[0]
        if count == 0:
            await db.execute(
                """INSERT INTO system_users (id, username, email, password_hash, role, full_name, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    username,
                    f"{username}@sistema.local",
                    password_hash,
                    "admin",
                    "Administrador",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()


async def get_user_by_username(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM system_users WHERE username = ? AND active = 1", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM system_users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, username, email, role, full_name, active, created_at, last_login FROM system_users ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def create_user(username: str, email: str, password_hash: str, role: str, full_name: str = "") -> dict:
    if role not in VALID_ROLES:
        raise ValueError(f"Rol inválido '{role}'. Válidos: {', '.join(VALID_ROLES)}")
    user_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO system_users (id, username, email, password_hash, role, full_name, active, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (user_id, username, email, password_hash, role, full_name, ts),
        )
        await db.commit()
    return {"id": user_id, "username": username, "email": email, "role": role, "full_name": full_name, "active": 1}


async def update_user(user_id: str, fields: dict) -> dict | None:
    allowed = {"email", "role", "full_name", "active", "password_hash"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_user_by_id(user_id)
    if "role" in updates and updates["role"] not in VALID_ROLES:
        raise ValueError(f"Rol inválido '{updates['role']}'")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE system_users SET {set_clause} WHERE id = ?", values)
        await db.commit()
    return await get_user_by_id(user_id)


async def update_last_login(user_id: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE system_users SET last_login = ? WHERE id = ?", (ts, user_id))
        await db.commit()


async def delete_user(user_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM system_users WHERE id = ?", (user_id,))
        await db.commit()
