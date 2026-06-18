"""
Gestión de usuarios del sistema — PostgreSQL via db.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import db

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS system_users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    full_name     TEXT DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT
)
"""

VALID_ROLES = {"admin", "academico", "viewer"}


async def init_users_table() -> None:
    await db.execute(_CREATE_USERS)


async def seed_admin(username: str, password_hash: str) -> None:
    count = await db.fetchval("SELECT COUNT(*) FROM system_users")
    if not count:
        await db.execute(
            """INSERT INTO system_users (id, username, email, password_hash, role, full_name, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            str(uuid.uuid4()),
            username,
            f"{username}@sistema.local",
            password_hash,
            "admin",
            "Administrador",
            datetime.now(timezone.utc).isoformat(),
        )


async def get_user_by_username(username: str) -> dict | None:
    return await db.fetchrow(
        "SELECT * FROM system_users WHERE username = ? AND active = 1", username
    )


async def get_user_by_id(user_id: str) -> dict | None:
    return await db.fetchrow("SELECT * FROM system_users WHERE id = ?", user_id)


async def list_users() -> list[dict]:
    return await db.fetch(
        "SELECT id, username, email, role, full_name, active, created_at, last_login FROM system_users ORDER BY created_at DESC"
    )


async def create_user(username: str, email: str, password_hash: str, role: str, full_name: str = "") -> dict:
    if role not in VALID_ROLES:
        raise ValueError(f"Rol inválido '{role}'. Válidos: {', '.join(VALID_ROLES)}")
    user_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO system_users (id, username, email, password_hash, role, full_name, active, created_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        user_id, username, email, password_hash, role, full_name, ts,
    )
    return {"id": user_id, "username": username, "email": email, "role": role, "full_name": full_name, "active": 1}


async def update_user(user_id: str, fields: dict) -> dict | None:
    allowed = {"email", "role", "full_name", "active", "password_hash"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_user_by_id(user_id)
    if "role" in updates and updates["role"] not in VALID_ROLES:
        raise ValueError(f"Rol inválido '{updates['role']}'")
    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items(), 1):
        set_parts.append(f"{k} = ${i}")
        values.append(v)
    values.append(user_id)
    await db.get_pool().execute(
        f"UPDATE system_users SET {', '.join(set_parts)} WHERE id = ${len(values)}",
        *values,
    )
    return await get_user_by_id(user_id)


async def update_last_login(user_id: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE system_users SET last_login = ? WHERE id = ?", ts, user_id)


async def delete_user(user_id: str) -> None:
    await db.execute("DELETE FROM system_users WHERE id = ?", user_id)
