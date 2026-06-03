"""
SQLite audit log for enrollment operations.
Uses aiosqlite for async access.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

DB_PATH = "audit.db"

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    ejecucion_id TEXT   NOT NULL,
    cedula      TEXT    NOT NULL,
    nombre      TEXT    NOT NULL,
    accion      TEXT    NOT NULL,
    plataforma  TEXT,
    curso       TEXT,
    semestre    TEXT,
    detalle     TEXT
)
"""

_CREATE_EJECUCIONES = """
CREATE TABLE IF NOT EXISTS ejecuciones (
    id           TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    tipo         TEXT,
    semestre     TEXT,
    total        INTEGER DEFAULT 0,
    creados      INTEGER DEFAULT 0,
    existentes   INTEGER DEFAULT 0,
    inscripciones INTEGER DEFAULT 0,
    errores      INTEGER DEFAULT 0,
    duracion_seg REAL,
    estado       TEXT
)
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_AUDIT)
        await db.execute(_CREATE_EJECUCIONES)
        await db.commit()


async def log_action(
    ejecucion_id: str,
    cedula: str,
    nombre: str,
    accion: str,
    plataforma: str = "",
    curso: str = "",
    semestre: str = "",
    detalle: str = "",
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log
               (timestamp, ejecucion_id, cedula, nombre, accion, plataforma, curso, semestre, detalle)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, ejecucion_id, cedula, nombre, accion, plataforma, curso, semestre, detalle),
        )
        await db.commit()


async def save_ejecucion(summary: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO ejecuciones
               (id, timestamp, tipo, semestre, total, creados, existentes, inscripciones, errores, duracion_seg, estado)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                summary.get("ejecucion_id", str(uuid.uuid4())),
                summary.get("timestamp", datetime.now(timezone.utc).isoformat()),
                summary.get("tipo", "manual"),
                summary.get("semestre", ""),
                summary.get("total", 0),
                summary.get("creados", 0),
                summary.get("existentes", 0),
                summary.get("inscripciones", 0),
                summary.get("errores", 0),
                summary.get("duracion_seg", 0.0),
                summary.get("estado", "exitoso"),
            ),
        )
        await db.commit()


async def get_historial(
    semestre: str | None = None,
    cedula: str | None = None,
    limit: int = 500,
) -> list[dict]:
    conditions = []
    params: list[Any] = []
    if semestre:
        conditions.append("semestre = ?")
        params.append(semestre)
    if cedula:
        conditions.append("cedula = ?")
        params.append(cedula)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_ejecuciones(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM ejecuciones ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_dashboard_kpis() -> dict:
    """KPIs for today and summary totals."""
    today = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Today's stats from audit_log
        cur = await db.execute(
            "SELECT accion, COUNT(*) as cnt FROM audit_log WHERE DATE(timestamp) = ? GROUP BY accion",
            (today,),
        )
        rows = await cur.fetchall()
        today_by_action: dict[str, int] = {r["accion"]: r["cnt"] for r in rows}

        # Last 7 days processed per day
        cur2 = await db.execute(
            """SELECT DATE(timestamp) as day, COUNT(DISTINCT cedula) as cnt
               FROM audit_log WHERE DATE(timestamp) >= DATE('now','-6 days')
               GROUP BY day ORDER BY day""",
        )
        daily = [{"day": r["day"], "count": r["cnt"]} for r in await cur2.fetchall()]

        # Last executions
        cur3 = await db.execute(
            "SELECT * FROM ejecuciones ORDER BY timestamp DESC LIMIT 10"
        )
        ejecuciones = [dict(r) for r in await cur3.fetchall()]

        # Total unique students processed ever
        cur4 = await db.execute("SELECT COUNT(DISTINCT cedula) as cnt FROM audit_log")
        total_alumnos = (await cur4.fetchone())["cnt"]

    return {
        "hoy": {
            "procesados": sum(today_by_action.values()),
            "creados": today_by_action.get("creado", 0),
            "inscritos": today_by_action.get("inscrito", 0),
            "errores": today_by_action.get("error", 0),
        },
        "total_alumnos": total_alumnos,
        "ultimos_7_dias": daily,
        "ultimas_ejecuciones": ejecuciones,
    }


async def export_to_excel(semestre: str | None = None, cedula: str | None = None) -> bytes:
    rows = await get_historial(semestre=semestre, cedula=cedula, limit=10000)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Auditoría"

    headers = ["ID", "Timestamp", "Ejecución ID", "Cédula", "Nombre", "Acción",
               "Plataforma", "Curso", "Semestre", "Detalle"]

    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF")

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    action_fills = {
        "creado":   PatternFill("solid", fgColor="D1FAE5"),
        "inscrito": PatternFill("solid", fgColor="DBEAFE"),
        "error":    PatternFill("solid", fgColor="FEE2E2"),
        "omitido":  PatternFill("solid", fgColor="F3F4F6"),
    }

    for r_idx, row in enumerate(rows, 2):
        values = [
            row.get("id"), row.get("timestamp"), row.get("ejecucion_id"),
            row.get("cedula"), row.get("nombre"), row.get("accion"),
            row.get("plataforma"), row.get("curso"), row.get("semestre"),
            row.get("detalle"),
        ]
        accion = row.get("accion", "")
        fill = action_fills.get(accion, PatternFill("solid", fgColor="FFFFFF"))
        for c_idx, val in enumerate(values, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.fill = fill

    # Auto-width
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
