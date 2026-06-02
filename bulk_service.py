"""
Carga masiva desde Excel/CSV.

Columnas esperadas — Usuarios:
  nombre, email, sis_id, rol_canvas, upn_azure, grupo_azure, equipo_teams

Columnas esperadas — Inscripciones:
  email_usuario, curso_canvas_id, rol_canvas, grupo_azure, equipo_teams, canal_teams
"""

import io
import asyncio
from typing import BinaryIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import canvas_service
import graph_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_sheet(file_bytes: bytes, filename: str) -> pd.DataFrame:
    buf = io.BytesIO(file_bytes)
    if filename.lower().endswith(".csv"):
        return pd.read_csv(buf, dtype=str).fillna("")
    return pd.read_excel(buf, dtype=str).fillna("")


def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------

async def _process_user_row(row: dict) -> dict:
    result = {
        "fila": row.get("_fila", ""),
        "email": row.get("email", ""),
        "nombre": row.get("nombre", ""),
        "canvas": {"status": "omitido", "detalle": ""},
        "azure": {"status": "omitido", "detalle": ""},
        "teams": {"status": "omitido", "detalle": ""},
    }

    # Canvas
    if row.get("email"):
        try:
            user = await canvas_service.create_user(
                name=row.get("nombre", row["email"]),
                email=row["email"],
                sis_id=row.get("sis_id", ""),
            )
            result["canvas"] = {"status": "ok", "detalle": f"id={user.get('id')}"}
        except Exception as exc:
            msg = str(exc)
            if "unique_id" in msg.lower() or "already" in msg.lower():
                result["canvas"] = {"status": "existente", "detalle": "ya existe en Canvas"}
            else:
                result["canvas"] = {"status": "error", "detalle": msg[:120]}

    # Azure AD
    if row.get("upn_azure"):
        try:
            nickname = row["upn_azure"].split("@")[0]
            temp_pw = "Temporal@2024!"
            az_user = await graph_service.create_user(
                display_name=row.get("nombre", nickname),
                mail_nickname=nickname,
                upn=row["upn_azure"],
                password=temp_pw,
            )
            result["azure"] = {"status": "ok", "detalle": f"id={az_user.get('id', '')[:8]}…"}

            # Agregar a grupo si corresponde
            if row.get("grupo_azure") and az_user.get("id"):
                added = await graph_service.add_member_to_group(
                    row["grupo_azure"], az_user["id"]
                )
                if added:
                    result["teams"] = {"status": "ok", "detalle": "agregado al grupo/equipo"}
                else:
                    result["teams"] = {"status": "advertencia", "detalle": "no se pudo agregar al grupo"}
        except Exception as exc:
            msg = str(exc)
            if "already exists" in msg.lower() or "objectconflict" in msg.lower():
                result["azure"] = {"status": "existente", "detalle": "ya existe en Azure AD"}
            else:
                result["azure"] = {"status": "error", "detalle": msg[:120]}

    return result


async def process_users_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    for i, r in enumerate(rows, start=2):
        r["_fila"] = i

    tasks = [_process_user_row(r) for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = []
    for r in results:
        if isinstance(r, Exception):
            processed.append({"error_global": str(r)})
        else:
            processed.append(r)

    ok = sum(1 for r in processed if r.get("canvas", {}).get("status") == "ok")
    errors = sum(
        1 for r in processed
        if r.get("canvas", {}).get("status") == "error"
        or r.get("azure", {}).get("status") == "error"
    )

    return {
        "tipo": "usuarios",
        "total": len(processed),
        "success": ok,
        "errors": errors,
        "rows": processed,
    }


# ---------------------------------------------------------------------------
# Inscripciones
# ---------------------------------------------------------------------------

async def _process_enrollment_row(row: dict) -> dict:
    result = {
        "fila": row.get("_fila", ""),
        "email": row.get("email_usuario", ""),
        "curso": row.get("curso_canvas_id", ""),
        "canvas": {"status": "omitido", "detalle": ""},
        "azure": {"status": "omitido", "detalle": ""},
        "teams": {"status": "omitido", "detalle": ""},
    }

    # Canvas enrollment
    if row.get("email_usuario") and row.get("curso_canvas_id"):
        try:
            enrollment = await canvas_service.enroll_user(
                course_id=row["curso_canvas_id"],
                user_id=row["email_usuario"],
                role=row.get("rol_canvas", "StudentEnrollment"),
            )
            result["canvas"] = {
                "status": "ok",
                "detalle": f"enrollment id={enrollment.get('id')}",
            }
        except Exception as exc:
            result["canvas"] = {"status": "error", "detalle": str(exc)[:120]}

    # Azure: agregar a grupo del curso
    if row.get("grupo_azure") and row.get("email_usuario"):
        try:
            users = await graph_service.get_users()
            az_user = next(
                (u for u in users if u.get("mail", "").lower() == row["email_usuario"].lower()),
                None,
            )
            if az_user:
                added = await graph_service.add_member_to_group(
                    row["grupo_azure"], az_user["id"]
                )
                result["azure"] = {
                    "status": "ok" if added else "advertencia",
                    "detalle": "agregado al grupo" if added else "no se pudo agregar",
                }
                result["teams"] = result["azure"].copy()
            else:
                result["azure"] = {"status": "advertencia", "detalle": "usuario no encontrado en Azure"}
        except Exception as exc:
            result["azure"] = {"status": "error", "detalle": str(exc)[:120]}

    return result


async def process_enrollments_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    for i, r in enumerate(rows, start=2):
        r["_fila"] = i

    tasks = [_process_enrollment_row(r) for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = []
    for r in results:
        if isinstance(r, Exception):
            processed.append({"error_global": str(r)})
        else:
            processed.append(r)

    ok = sum(1 for r in processed if r.get("canvas", {}).get("status") == "ok")
    errors = sum(1 for r in processed if r.get("canvas", {}).get("status") == "error")

    return {
        "tipo": "inscripciones",
        "total": len(processed),
        "success": ok,
        "errors": errors,
        "rows": processed,
    }


# ---------------------------------------------------------------------------
# Generador de reporte Excel
# ---------------------------------------------------------------------------

def build_report_excel(report: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Reporte"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)

    status_colors = {
        "ok": "C6EFCE",
        "existente": "FFEB9C",
        "advertencia": "FFEB9C",
        "error": "FFC7CE",
        "omitido": "EFEFEF",
    }

    if report.get("tipo") == "usuarios":
        headers = ["Fila", "Email", "Nombre", "Canvas Estado", "Canvas Detalle",
                   "Azure Estado", "Azure Detalle", "Teams Estado", "Teams Detalle"]
    else:
        headers = ["Fila", "Email", "Curso ID", "Canvas Estado", "Canvas Detalle",
                   "Azure Estado", "Azure Detalle", "Teams Estado", "Teams Detalle"]

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row_idx, row in enumerate(report.get("rows", []), start=2):
        canvas_st = row.get("canvas", {}).get("status", "")
        azure_st = row.get("azure", {}).get("status", "")
        teams_st = row.get("teams", {}).get("status", "")

        values = [
            row.get("fila", row_idx),
            row.get("email", ""),
            row.get("nombre", row.get("curso", "")),
            canvas_st,
            row.get("canvas", {}).get("detalle", ""),
            azure_st,
            row.get("azure", {}).get("detalle", ""),
            teams_st,
            row.get("teams", {}).get("detalle", ""),
        ]

        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            if col == 4:
                color = status_colors.get(canvas_st, "FFFFFF")
                cell.fill = PatternFill("solid", fgColor=color)
            elif col == 6:
                color = status_colors.get(azure_st, "FFFFFF")
                cell.fill = PatternFill("solid", fgColor=color)
            elif col == 8:
                color = status_colors.get(teams_st, "FFFFFF")
                cell.fill = PatternFill("solid", fgColor=color)

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
