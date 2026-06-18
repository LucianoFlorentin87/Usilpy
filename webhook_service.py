"""
Webhook para recibir datos de inscripciones — PostgreSQL via db.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import db

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
    await db.execute(_CREATE_PENDING)
    for col_def in (
        "dia TEXT DEFAULT ''", "hora_inicio TEXT DEFAULT ''", "hora_fin TEXT DEFAULT ''",
        "programa TEXT DEFAULT ''", "carrera TEXT DEFAULT ''",
    ):
        col_name = col_def.split()[0]
        try:
            await db.execute(
                f"ALTER TABLE pending_enrollments ADD COLUMN IF NOT EXISTS {col_def}"
            )
        except Exception:
            pass


async def receive_enrollment(data: dict, source: str = "webhook") -> dict:
    row_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO pending_enrollments
           (id, received_at, source, semestre, cedula, nombre, email, curso_id, curso_nombre, rol, dia, hora_inicio, hora_fin, programa, carrera)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    )
    return {"id": row_id, "estado": "pendiente"}


async def receive_bulk(rows: list[dict], source: str = "webhook") -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    accepted = 0
    errors = []
    for i, data in enumerate(rows):
        cedula = data.get("cedula", data.get("sis_id", "")).strip()
        if not cedula:
            errors.append({"fila": i + 1, "error": "Falta cédula"})
            continue
        try:
            await db.execute(
                """INSERT INTO pending_enrollments
                   (id, received_at, source, semestre, cedula, nombre, email, curso_id, curso_nombre, rol, dia, hora_inicio, hora_fin, programa, carrera)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            )
            accepted += 1
        except Exception as exc:
            errors.append({"fila": i + 1, "error": str(exc)})
    return {"accepted": accepted, "errors": errors}


async def list_pending(semestre: str | None = None, estado: str = "pendiente", limit: int = 500) -> list[dict]:
    conditions = ["estado = ?"]
    params: list = [estado]
    if semestre:
        conditions.append("semestre = ?")
        params.append(semestre)
    params.append(limit)
    where = "WHERE " + " AND ".join(conditions)
    return await db.fetch(
        f"SELECT * FROM pending_enrollments {where} ORDER BY received_at DESC LIMIT ?",
        *params,
    )


async def get_stats() -> dict:
    rows = await db.fetch(
        "SELECT estado, COUNT(*) as cnt FROM pending_enrollments GROUP BY estado"
    )
    return {r["estado"]: r["cnt"] for r in rows}


async def mark_processed(ids: list[str], detalle: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    for row_id in ids:
        await db.execute(
            "UPDATE pending_enrollments SET estado='procesado', processed_at=?, detalle=? WHERE id=?",
            ts, detalle, row_id,
        )


async def mark_error(ids: list[str], detalle: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    for row_id in ids:
        await db.execute(
            "UPDATE pending_enrollments SET estado='error', processed_at=?, detalle=? WHERE id=?",
            ts, detalle, row_id,
        )


async def delete_pending(ids: list[str]) -> None:
    for row_id in ids:
        await db.execute("DELETE FROM pending_enrollments WHERE id=?", row_id)
