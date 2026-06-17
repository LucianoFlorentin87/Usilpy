"""
Webhook para recibir datos de inscripciones desde el sistema académico externo.
Los datos quedan en cola (pending_enrollments) hasta que el admin los procesa.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite

from audit_service import DB_PATH

_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS pending_enrollments (
    id           TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    source       TEXT DEFAULT 'webhook',
    semestre     TEXT,
    cedula       TEXT NOT NULL,
    nombre       TEXT,
    email        TEXT,
    curso_id     TEXT,
    curso_nombre TEXT,
    rol          TEXT DEFAULT 'StudentEnrollment',
    estado       TEXT DEFAULT 'pendiente',
    processed_at TEXT,
    detalle      TEXT,
    dia          TEXT DEFAULT '',
    hora_inicio  TEXT DEFAULT '',
    hora_fin     TEXT DEFAULT '',
    programa     TEXT DEFAULT '',
    carrera      TEXT DEFAULT ''
)
"""


async def init_pending_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_PENDING)
        new_cols = [
            "dia TEXT DEFAULT ''", "hora_inicio TEXT DEFAULT ''", "hora_fin TEXT DEFAULT ''",
            "programa TEXT DEFAULT ''", "carrera TEXT DEFAULT ''",
        ]
        for col in new_cols:
            try:
                await db.execute(f"ALTER TABLE pending_enrollments ADD COLUMN {col}")
            except Exception:
                pass
        await db.commit()


async def receive_enrollment(data: dict, source: str = "webhook") -> dict:
    """Save an enrollment record to the pending queue."""
    row_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO pending_enrollments
               (id, received_at, source, semestre, cedula, nombre, email, curso_id, curso_nombre, rol, dia, hora_inicio, hora_fin, programa, carrera)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row_id, ts, source,
                data.get("semestre", ""),
                data.get("cedula", data.get("sis_id", "")),
                data.get("nombre", ""),
                data.get("email", ""),
                data.get("curso_id", ""),
                data.get("curso_nombre", ""),
                data.get("rol", "StudentEnrollment"),
                data.get("dia", ""),
                data.get("hora_inicio", ""),
                data.get("hora_fin", ""),
                data.get("programa", ""),
                data.get("carrera", ""),
            ),
        )
        await db.commit()
    return {"id": row_id, "estado": "pendiente"}


async def receive_bulk(rows: list[dict], source: str = "webhook") -> dict:
    """Save multiple enrollment records at once."""
    ts = datetime.now(timezone.utc).isoformat()
    accepted = 0
    errors = []
    async with aiosqlite.connect(DB_PATH) as db:
        for i, data in enumerate(rows):
            cedula = data.get("cedula", data.get("sis_id", "")).strip()
            if not cedula:
                errors.append({"fila": i + 1, "error": "Falta cédula"})
                continue
            await db.execute(
                """INSERT INTO pending_enrollments
                   (id, received_at, source, semestre, cedula, nombre, email, curso_id, curso_nombre, rol, dia, hora_inicio, hora_fin, programa, carrera)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), ts, source,
                    data.get("semestre", ""),
                    cedula,
                    data.get("nombre", ""),
                    data.get("email", ""),
                    data.get("curso_id", ""),
                    data.get("curso_nombre", ""),
                    data.get("rol", "StudentEnrollment"),
                    data.get("dia", ""),
                    data.get("hora_inicio", ""),
                    data.get("hora_fin", ""),
                    data.get("programa", ""),
                    data.get("carrera", ""),
                ),
            )
            accepted += 1
        await db.commit()
    return {"accepted": accepted, "errors": errors}


async def list_pending(semestre: str | None = None, estado: str = "pendiente", limit: int = 500) -> list[dict]:
    conditions = ["estado = ?"]
    params: list = [estado]
    if semestre:
        conditions.append("semestre = ?")
        params.append(semestre)
    where = "WHERE " + " AND ".join(conditions)
    query = f"SELECT * FROM pending_enrollments {where} ORDER BY received_at DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT estado, COUNT(*) as cnt FROM pending_enrollments GROUP BY estado"
        )
        rows = await cur.fetchall()
        return {r["estado"]: r["cnt"] for r in rows}


async def mark_processed(ids: list[str], detalle: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for row_id in ids:
            await db.execute(
                "UPDATE pending_enrollments SET estado='procesado', processed_at=?, detalle=? WHERE id=?",
                (ts, detalle, row_id),
            )
        await db.commit()


async def mark_error(ids: list[str], detalle: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for row_id in ids:
            await db.execute(
                "UPDATE pending_enrollments SET estado='error', processed_at=?, detalle=? WHERE id=?",
                (ts, detalle, row_id),
            )
        await db.commit()


async def delete_pending(ids: list[str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for row_id in ids:
            await db.execute("DELETE FROM pending_enrollments WHERE id=?", (row_id,))
        await db.commit()
