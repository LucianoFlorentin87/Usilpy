"""
Carga masiva desde Excel/CSV.

Columnas esperadas — Usuarios:
  nombre, email, sis_id, rol_canvas, upn_azure, grupo_azure, equipo_teams

Columnas esperadas — Inscripciones:
  email_usuario, curso_canvas_id, rol_canvas, grupo_azure, equipo_teams, canal_teams
"""

import io
import asyncio
import logging
import secrets
import string
from typing import BinaryIO

logger = logging.getLogger(__name__)

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import canvas_service
import graph_service
import db
from config import get_settings

settings = get_settings()


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
# Cursos
# ---------------------------------------------------------------------------
# Columnas esperadas:
#   nombre, sis_id, semestre, crear_en_canvas (si/no), nombre_equipo_teams (opcional)

async def _process_course_row(row: dict) -> dict:
    result = {
        "fila": row.get("_fila", ""),
        "nombre": row.get("nombre", ""),
        "sis_id": row.get("sis_id", ""),
        "canvas": {"status": "omitido", "detalle": ""},
        "teams": {"status": "omitido", "detalle": ""},
    }

    nombre = row.get("nombre", "").strip()
    sis_id = row.get("sis_id", "").strip()
    semestre = row.get("semestre", settings.semestre_actual).strip()

    if not nombre:
        result["canvas"] = {"status": "error", "detalle": "Falta columna 'nombre'"}
        return result

    crear_canvas = row.get("crear_en_canvas", "si").strip().lower() not in ("no", "false", "0")

    # Canvas — auto-create enrollment term if semestre is given
    term_id: int | None = None
    if crear_canvas and semestre:
        try:
            term = await canvas_service.get_or_create_term(semestre)
            term_id = term.get("id")
        except Exception:
            pass  # term creation failure is non-fatal; course will be created without term

    if crear_canvas:
        try:
            existing = await canvas_service.get_course_by_sis_id(sis_id) if sis_id else None
            if existing:
                result["canvas"] = {"status": "existente", "detalle": f"id={existing.get('id')}"}
            else:
                course = await canvas_service.create_course(nombre, sis_id or nombre, semestre, term_id=term_id)
                result["canvas"] = {"status": "ok", "detalle": f"id={course.get('id')}"}
        except Exception as exc:
            result["canvas"] = {"status": "error", "detalle": str(exc)[:120]}

    # Teams
    team_name = row.get("nombre_equipo_teams", "").strip() or f"{semestre} - {nombre}"
    if row.get("nombre_equipo_teams", "").strip() or row.get("crear_en_teams", "").strip().lower() in ("si", "true", "1"):
        try:
            existing_team = await graph_service.find_team_by_display_name(team_name)
            if existing_team:
                result["teams"] = {"status": "existente", "detalle": team_name}
            else:
                team = await graph_service.create_team(team_name, f"Equipo académico: {nombre} ({semestre})")
                result["teams"] = {"status": "ok", "detalle": f"id={team.get('id', '')[:8]}…"}
        except Exception as exc:
            result["teams"] = {"status": "error", "detalle": str(exc)[:120]}

    return result


async def process_courses_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    for i, r in enumerate(rows, start=2):
        r["_fila"] = i

    results = []
    for r in rows:
        try:
            res = await _process_course_row(r)
        except Exception as exc:
            res = {"error_global": str(exc)}
        results.append(res)

    ok = sum(1 for r in results if r.get("canvas", {}).get("status") == "ok")
    errors = sum(1 for r in results if r.get("canvas", {}).get("status") == "error"
                 or r.get("teams", {}).get("status") == "error")

    return {"tipo": "cursos", "total": len(results), "success": ok, "errors": errors, "rows": results}


# ---------------------------------------------------------------------------
# Usuarios Canvas (solo Canvas)
# ---------------------------------------------------------------------------

async def process_canvas_users_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    results = []
    for i, row in enumerate(rows, start=2):
        r = {"fila": i, "email": row.get("email", ""), "nombre": row.get("nombre", ""),
             "canvas": {"status": "omitido", "detalle": ""}}
        if row.get("email"):
            try:
                user = await canvas_service.create_user(
                    name=row.get("nombre", row["email"]),
                    email=row["email"],
                    sis_id=row.get("sis_id", ""),
                )
                r["canvas"] = {"status": "ok", "detalle": f"id={user.get('id')}"}
            except Exception as exc:
                msg = str(exc)
                r["canvas"] = {"status": "existente" if "unique_id" in msg.lower() or "already" in msg.lower() else "error", "detalle": msg[:120]}
        results.append(r)

    ok = sum(1 for r in results if r.get("canvas", {}).get("status") == "ok")
    errors = sum(1 for r in results if r.get("canvas", {}).get("status") == "error")
    return {"tipo": "canvas_usuarios", "total": len(results), "success": ok, "errors": errors, "rows": results}


# ---------------------------------------------------------------------------
# Usuarios Azure AD (solo Azure)
# ---------------------------------------------------------------------------

async def process_azure_users_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    results = []
    for i, row in enumerate(rows, start=2):
        r = {"fila": i, "upn": row.get("upn", row.get("email", "")), "nombre": row.get("nombre", ""),
             "azure": {"status": "omitido", "detalle": ""}, "teams": {"status": "omitido", "detalle": ""}}
        upn = row.get("upn", row.get("email", "")).strip()
        if upn:
            try:
                nickname = upn.split("@")[0]
                _alphabet = string.ascii_letters + string.digits + "!@#$"
                _default_pw = "".join(secrets.choice(_alphabet) for _ in range(16))
                password = row.get("password") or _default_pw
                az_user = await graph_service.create_user(
                    display_name=row.get("nombre", nickname),
                    mail_nickname=nickname,
                    upn=upn,
                    password=password,
                )
                r["azure"] = {"status": "ok", "detalle": f"id={az_user.get('id', '')[:8]}…"}
                if row.get("grupo_id") and az_user.get("id"):
                    await graph_service.add_member_to_group(row["grupo_id"], az_user["id"])
                    r["teams"] = {"status": "ok", "detalle": "agregado al grupo"}
            except Exception as exc:
                msg = str(exc)
                r["azure"] = {"status": "existente" if "already exists" in msg.lower() else "error", "detalle": msg[:120]}
        results.append(r)

    ok = sum(1 for r in results if r.get("azure", {}).get("status") == "ok")
    errors = sum(1 for r in results if r.get("azure", {}).get("status") == "error")
    return {"tipo": "azure_usuarios", "total": len(results), "success": ok, "errors": errors, "rows": results}


# ---------------------------------------------------------------------------
# Inscripciones Canvas (solo Canvas)
# ---------------------------------------------------------------------------

async def process_canvas_enrollments_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    results = []
    for i, row in enumerate(rows, start=2):
        r = {"fila": i, "email": row.get("email_usuario", row.get("email", "")),
             "curso": row.get("curso_id", row.get("curso_canvas_id", "")),
             "canvas": {"status": "omitido", "detalle": ""}}
        email = r["email"].strip()
        curso_id = r["curso"].strip()
        if email and curso_id:
            try:
                enr = await canvas_service.enroll_user(
                    course_id=curso_id,
                    user_id=email,
                    role=row.get("rol", row.get("rol_canvas", "StudentEnrollment")),
                )
                r["canvas"] = {"status": "ok", "detalle": f"enrollment id={enr.get('id')}"}
            except Exception as exc:
                r["canvas"] = {"status": "error", "detalle": str(exc)[:120]}
        results.append(r)

    ok = sum(1 for r in results if r.get("canvas", {}).get("status") == "ok")
    errors = sum(1 for r in results if r.get("canvas", {}).get("status") == "error")
    return {"tipo": "canvas_inscripciones", "total": len(results), "success": ok, "errors": errors, "rows": results}


# ---------------------------------------------------------------------------
# Teams (crear equipos masivamente)
# ---------------------------------------------------------------------------

async def process_teams_sheet(file_bytes: bytes, filename: str) -> dict:
    df = _normalize_cols(_read_sheet(file_bytes, filename))
    rows = df.to_dict(orient="records")
    results = []
    for i, row in enumerate(rows, start=2):
        nombre = row.get("nombre", "").strip()
        desc = row.get("descripcion", row.get("description", "")).strip()
        r = {"fila": i, "nombre": nombre, "teams": {"status": "omitido", "detalle": ""}}
        if nombre:
            try:
                existing = await graph_service.find_team_by_display_name(nombre)
                if existing:
                    r["teams"] = {"status": "existente", "detalle": f"id={existing.get('id','')[:8]}..."}
                else:
                    team = await graph_service.create_team(nombre, desc)
                    r["teams"] = {"status": "ok", "detalle": f"id={team.get('id','')[:8]}..."}
            except Exception as exc:
                r["teams"] = {"status": "error", "detalle": str(exc)[:120]}
        else:
            r["teams"] = {"status": "error", "detalle": "Columna 'nombre' vacia"}
        results.append(r)

    ok = sum(1 for r in results if r["teams"]["status"] == "ok")
    errors = sum(1 for r in results if r["teams"]["status"] == "error")
    return {"tipo": "teams", "total": len(results), "success": ok, "errors": errors, "rows": results}


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
            _az_alphabet = string.ascii_letters + string.digits + "!@#$"
            temp_pw = "".join(secrets.choice(_az_alphabet) for _ in range(16))
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


# ---------------------------------------------------------------------------
# process_cursos_ids
# ---------------------------------------------------------------------------

def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [
        c.strip().lower()
         .replace(" ", "_")
         .replace("á","a").replace("é","e").replace("í","i")
         .replace("ó","o").replace("ú","u")
        for c in df.columns
    ]
    return df


def _build_plantilla(headers: list[str], rows: list[list], sheet_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for ri, row in enumerate(rows, 2):
        for ci, val in enumerate(row, 1):
            ws.cell(row=ri, column=ci, value=val)
    for ci in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 32
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_plantilla_cursos() -> bytes:
    return _build_plantilla(
        headers=["materia", "periodo", "programa"],
        rows=[
            ["Matemática I", "2025-2", "Ingeniería en Sistemas"],
            ["Administración", "2025-2", "Administración de Empresas"],
            ["Comunicación", "2025-2", ""],
        ],
        sheet_name="Crear cursos",
    )


def build_plantilla_canvas() -> bytes:
    return _build_plantilla(
        headers=["SIS User ID", "Course ID", "Rol"],
        rows=[
            ["3406399", "1573", "StudentEnrollment"],
            ["5405805", "1573", "StudentEnrollment"],
            ["glezcano@usil.edu.py", "1539", "StudentEnrollment"],
        ],
        sheet_name="Inscripciones Canvas",
    )


def build_plantilla_teams() -> bytes:
    return _build_plantilla(
        headers=["Correo", "Group ID"],
        rows=[
            ["glezcano@usil.edu.py", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
            ["lflorentin@usil.edu.py", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
        ],
        sheet_name="Inscripciones Teams",
    )


async def process_cursos_ids(file_bytes: bytes, filename: str) -> bytes:
    """Reads Excel with columns materia, semestre (optional), programa (optional).
    Creates Canvas course + Teams team for each row.
    Returns bytes of Excel with IDs and statuses."""
    df = pd.read_excel(io.BytesIO(file_bytes))
    df = _normalize_cols(df)

    if "materia" not in df.columns:
        raise ValueError("El Excel debe tener una columna 'materia'")

    rows_out = []

    for _, row in df.iterrows():
        materia = str(row.get("materia", "")).strip()
        periodo = (
            str(row.get("periodo", row.get("semestre", ""))).strip()
            if ("periodo" in df.columns or "semestre" in df.columns)
            else ""
        )
        semestre = periodo if periodo and periodo != "nan" else settings.semestre_actual

        canvas_course_id = ""
        canvas_status = "error"
        teams_team_id = ""
        teams_status = "error"

        # Canvas
        try:
            term_obj = await canvas_service.get_or_create_term(semestre)
            term_id = term_obj.get("id") if isinstance(term_obj, dict) else None
            sis_id = f"USIL-{semestre}-{materia[:60]}"
            existing = await canvas_service.get_course_by_sis_id(sis_id)
            if existing:
                canvas_course_id = str(existing.get("id", ""))
                canvas_status = "existente"
            else:
                created = await canvas_service.create_course(
                    name=f"{semestre} - {materia}",
                    sis_id=sis_id,
                    term_id=term_id,
                )
                canvas_course_id = str(created.get("id", ""))
                canvas_status = "creado"
        except Exception as exc:
            logger.error("Canvas error creando curso '%s': %s", materia, exc)
            canvas_status = f"error: {str(exc)[:120]}"

        # Teams
        try:
            team_name = f"{semestre} - {materia}"
            existing_team = await graph_service.find_team_by_display_name(team_name)
            if existing_team:
                teams_team_id = existing_team.get("id", "")
                teams_status = "existente"
            else:
                new_team = await graph_service.create_team(team_name)
                teams_team_id = new_team.get("id", "")
                teams_status = "creado"
        except Exception as exc:
            logger.error("Teams error creando equipo '%s': %s", materia, exc)
            teams_status = f"error: {str(exc)[:120]}"

        rows_out.append({
            "Materia": materia,
            "Periodo": semestre,
            "ID Canvas": canvas_course_id,
            "ID Ms": teams_team_id,
            "Estado Canvas": canvas_status,
            "Estado Teams": teams_status,
        })

        # Persist to DB if at least one platform succeeded
        if canvas_course_id or teams_team_id:
            try:
                await db.execute(
                    """INSERT INTO cursos (materia, periodo, canvas_id, teams_id, canvas_status, teams_status)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT (materia, periodo) DO UPDATE SET
                         canvas_id = EXCLUDED.canvas_id,
                         teams_id = EXCLUDED.teams_id,
                         canvas_status = EXCLUDED.canvas_status,
                         teams_status = EXCLUDED.teams_status""",
                    materia, semestre, canvas_course_id, teams_team_id, canvas_status, teams_status,
                )
            except Exception as db_exc:
                logger.warning("No se pudo guardar curso en BD: %s", db_exc)

    # Build output Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Cursos"

    headers = ["Materia", "Periodo", "ID Canvas", "ID Ms", "Estado Canvas", "Estado Teams"]
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    row_colors = {
        "creado":    "C6EFCE",
        "existente": "FFEB9C",
        "error":     "FFC7CE",
    }

    for row_idx, r in enumerate(rows_out, 2):
        ws.cell(row=row_idx, column=1, value=r["Materia"])
        ws.cell(row=row_idx, column=2, value=r["Periodo"])
        ws.cell(row=row_idx, column=3, value=r["ID Canvas"])
        ws.cell(row=row_idx, column=4, value=r["ID Ms"])
        canvas_st = r["Estado Canvas"]
        teams_st = r["Estado Teams"]
        ws.cell(row=row_idx, column=5, value=canvas_st)
        ws.cell(row=row_idx, column=6, value=teams_st)

        for col_idx in range(1, 7):
            cell = ws.cell(row=row_idx, column=col_idx)
            if col_idx == 5:
                color = row_colors.get(canvas_st, "FFFFFF")
            elif col_idx == 6:
                color = row_colors.get(teams_st, "FFFFFF")
            else:
                if canvas_st == "error" or teams_st == "error":
                    color = row_colors["error"]
                elif canvas_st == "creado" or teams_st == "creado":
                    color = row_colors["creado"]
                else:
                    color = row_colors["existente"]
            cell.fill = PatternFill("solid", fgColor=color)

    for col_idx in range(1, 7):
        ws.column_dimensions[get_column_letter(col_idx)].width = 35

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# process_matriculacion_planilla
# ---------------------------------------------------------------------------

async def process_matriculacion_planilla(file_bytes: bytes, filename: str) -> tuple[dict, bytes]:
    """Reads planilla Excel with columns Materia, ID Canvas, ID Ms, Alumno, SIS, Correo.
    Creates users, enrolls them in Canvas and Teams, sends emails.
    Returns (summary_dict, excel_bytes)."""
    import email_service

    df = pd.read_excel(io.BytesIO(file_bytes))
    df = _normalize_cols(df)

    # Expected normalized columns: materia, id_canvas, id_ms, alumno, sis, correo
    required = {"materia", "id_canvas", "alumno", "sis", "correo"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Faltan columnas: {missing}")

    semestre = settings.semestre_actual

    # user cache keyed by cedula to avoid duplicate creation
    canvas_user_cache: dict[str, dict] = {}
    azure_user_cache: dict[str, dict] = {}
    password_cache: dict[str, str] = {}

    rows_out = []
    total = 0
    nuevos = 0
    existentes_count = 0
    inscripciones_canvas = 0
    inscripciones_teams = 0
    errores = 0

    # Track emails to send: {correo: {"alumno": ..., "cedula": ..., "is_new": ..., "password": ..., "materias": [...]}}
    email_map: dict[str, dict] = {}

    for _, row in df.iterrows():
        total += 1
        materia = str(row.get("materia", "")).strip()
        canvas_id = str(row.get("id_canvas", "")).strip()
        teams_id = str(row.get("id_ms", "")).strip() if "id_ms" in df.columns else ""
        alumno = str(row.get("alumno", "")).strip()
        cedula = str(row.get("sis", "")).strip()
        correo = str(row.get("correo", "")).strip()

        canvas_estado = "ok"
        teams_estado = "ok"
        email_enviado = "no"
        is_new = False

        try:
            # --- Canvas user ---
            if cedula in canvas_user_cache:
                canvas_user = canvas_user_cache[cedula]
            else:
                canvas_user = await canvas_service.find_user_by_sis_id(cedula)
                if canvas_user:
                    canvas_user_cache[cedula] = canvas_user
                else:
                    parts = alumno.split()
                    first_initial = parts[0][0].upper() if parts else "X"
                    last_initial = parts[-1][0].lower() if len(parts) > 1 else "x"
                    password = f"{cedula}-{first_initial}{last_initial}"
                    password_cache[cedula] = password
                    try:
                        canvas_user = await canvas_service.create_user(alumno, correo, sis_id=cedula)
                        canvas_user_cache[cedula] = canvas_user
                        is_new = True
                    except Exception:
                        canvas_user = None
                        canvas_estado = "error"
                        errores += 1

            # --- Azure user ---
            if cedula in azure_user_cache:
                azure_user = azure_user_cache[cedula]
            else:
                azure_user = await graph_service.get_user_by_upn(correo)
                if azure_user:
                    azure_user_cache[cedula] = azure_user
                else:
                    parts = alumno.split()
                    first_initial = parts[0][0].upper() if parts else "X"
                    last_initial = parts[-1][0].lower() if len(parts) > 1 else "x"
                    password = password_cache.get(cedula, f"{cedula}-{first_initial}{last_initial}")
                    password_cache[cedula] = password
                    try:
                        azure_user = await graph_service.create_user(
                            alumno,
                            correo.split("@")[0],
                            correo,
                            password,
                        )
                        azure_user_cache[cedula] = azure_user
                        is_new = True
                    except Exception:
                        azure_user = None

            # --- Enroll in Canvas ---
            if canvas_user and canvas_id:
                try:
                    await canvas_service.enroll_user(canvas_id, canvas_user["id"], "StudentEnrollment")
                    inscripciones_canvas += 1
                except Exception:
                    canvas_estado = "error"
                    errores += 1
            elif not canvas_user:
                canvas_estado = "error"

            # --- Add to Teams ---
            if azure_user and teams_id:
                try:
                    await graph_service.add_member_to_team(teams_id, azure_user["id"])
                    inscripciones_teams += 1
                except Exception:
                    teams_estado = "error"

            # --- Track for email ---
            if correo not in email_map:
                email_map[correo] = {
                    "alumno": alumno,
                    "cedula": cedula,
                    "is_new": is_new,
                    "password": password_cache.get(cedula, ""),
                    "materias": [],
                }
            else:
                if is_new:
                    email_map[correo]["is_new"] = True
                    if not email_map[correo]["password"]:
                        email_map[correo]["password"] = password_cache.get(cedula, "")
            email_map[correo]["materias"].append(materia)

            if is_new:
                nuevos += 1
            else:
                existentes_count += 1

        except Exception:
            canvas_estado = "error"
            errores += 1

        rows_out.append({
            "Alumno": alumno,
            "Cedula": cedula,
            "Materia": materia,
            "ID Canvas": canvas_id,
            "ID Ms": teams_id,
            "Canvas Estado": canvas_estado,
            "Teams Estado": teams_estado,
            "Email Enviado": email_enviado,
        })

    # --- Send emails ---
    emails_enviados = 0
    # Build correo->cedula mapping for marking rows
    correo_to_cedula: dict[str, str] = {info["correo"] if "correo" in info else correo: info.get("cedula","") for correo, info in email_map.items()}
    for correo, info in email_map.items():
        try:
            if info["is_new"]:
                from config import get_settings as _gs
                _s = _gs()
                await email_service.send_welcome_email(
                    correo,
                    info["alumno"],
                    info["password"],
                    canvas_url=_s.canvas_base_url,
                    teams_url=_s.teams_base_url,
                    cursos=info["materias"],
                    semestre=semestre,
                )
            else:
                await email_service.send_enrollment_confirmation(
                    correo,
                    info["alumno"],
                    cursos=info["materias"],
                    semestre=semestre,
                )
            emails_enviados += 1
            cedula = info.get("cedula", "")
            for r in rows_out:
                if r["Cedula"] == cedula:
                    r["Email Enviado"] = "si"
        except Exception:
            pass

    summary = {
        "total": total,
        "nuevos": nuevos,
        "existentes": existentes_count,
        "inscripciones_canvas": inscripciones_canvas,
        "inscripciones_teams": inscripciones_teams,
        "errores": errores,
        "emails_enviados": emails_enviados,
        "rows": rows_out,
    }

    # Build Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Matriculación"

    headers = ["Alumno", "Cedula", "Materia", "ID Canvas", "ID Ms", "Canvas Estado", "Teams Estado", "Email Enviado"]
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    status_colors = {
        "ok":    "C6EFCE",
        "error": "FFC7CE",
        "si":    "C6EFCE",
        "no":    "FFEB9C",
    }

    for row_idx, r in enumerate(rows_out, 2):
        ws.cell(row=row_idx, column=1, value=r["Alumno"])
        ws.cell(row=row_idx, column=2, value=r["Cedula"])
        ws.cell(row=row_idx, column=3, value=r["Materia"])
        ws.cell(row=row_idx, column=4, value=r["ID Canvas"])
        ws.cell(row=row_idx, column=5, value=r["ID Ms"])
        canvas_st = r["Canvas Estado"]
        teams_st = r["Teams Estado"]
        email_st = r["Email Enviado"]
        ws.cell(row=row_idx, column=6, value=canvas_st)
        ws.cell(row=row_idx, column=7, value=teams_st)
        ws.cell(row=row_idx, column=8, value=email_st)

        ws.cell(row=row_idx, column=6).fill = PatternFill("solid", fgColor=status_colors.get(canvas_st, "FFFFFF"))
        ws.cell(row=row_idx, column=7).fill = PatternFill("solid", fgColor=status_colors.get(teams_st, "FFFFFF"))
        ws.cell(row=row_idx, column=8).fill = PatternFill("solid", fgColor=status_colors.get(email_st, "FFFFFF"))

    for col_idx in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 25

    buf = io.BytesIO()
    wb.save(buf)
    excel_bytes = buf.getvalue()

    return summary, excel_bytes


# ---------------------------------------------------------------------------
# Inscripciones Canvas — formato nativo: SIS User ID | Course ID | Rol
# ---------------------------------------------------------------------------

async def process_canvas_enrollment_file(file_bytes: bytes, filename: str) -> bytes:
    """Lee Excel con columnas 'SIS User ID', 'Course ID', 'Rol'.
    Inscribe cada fila en Canvas y devuelve el mismo Excel con columnas
    'Resultado' y 'FechaHoraEjecucion' agregadas."""
    from datetime import datetime, timezone

    df = _read_sheet(file_bytes, filename)
    # Normalizar sólo para buscar, preservar nombres originales para el output
    col_map = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    norm = _normalize_cols(df.copy())

    sis_col   = next((c for c in norm.columns if "sis" in c and "user" in c), None) or next((c for c in norm.columns if "sis" in c), None)
    course_col= next((c for c in norm.columns if "course" in c), None)
    rol_col   = next((c for c in norm.columns if "rol" in c), None)

    if not sis_col or not course_col:
        raise ValueError("El Excel debe tener columnas 'SIS User ID' y 'Course ID'")

    resultados = []
    fechas = []

    for _, row in norm.iterrows():
        sis_id  = str(row.get(sis_col, "")).strip()
        course_id = str(row.get(course_col, "")).strip()
        rol = str(row.get(rol_col, "StudentEnrollment")).strip() if rol_col else "StudentEnrollment"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if not sis_id or not course_id:
            resultados.append("Datos incompletos")
            fechas.append(ts)
            continue

        try:
            # Canvas acepta SIS login ID como sis_login_id:VALOR o email
            user_ref = f"sis_login_id:{sis_id}" if not "@" in sis_id else sis_id
            await canvas_service.enroll_user(course_id, user_ref, rol)
            resultados.append("Inscripción exitosa")
        except Exception as exc:
            msg = str(exc).lower()
            if "already" in msg or "inscrito" in msg or "exists" in msg:
                resultados.append("Usuario ya inscrito")
            elif "not found" in msg or "no encontrado" in msg or "404" in msg:
                if "course" in msg:
                    resultados.append("Curso no encontrado")
                else:
                    resultados.append("Usuario no encontrado")
            else:
                resultados.append(f"Error: {str(exc)[:60]}")
        fechas.append(ts)

    df["Resultado"] = resultados
    df["FechaHoraEjecucion"] = fechas

    wb = Workbook()
    ws = wb.active
    ws.title = "Inscripciones Canvas"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    status_colors = {
        "Inscripción exitosa": "C6EFCE",
        "Usuario ya inscrito": "FFEB9C",
        "Curso no encontrado": "FFC7CE",
        "Usuario no encontrado": "FFC7CE",
    }

    for ci, col in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    resultado_col_idx = list(df.columns).index("Resultado") + 1

    for ri, (_, row) in enumerate(df.iterrows(), 2):
        for ci, val in enumerate(row, 1):
            ws.cell(row=ri, column=ci, value=val)
        resultado = row["Resultado"]
        color = status_colors.get(resultado, "FFC7CE" if "Error" in resultado else "FFFFFF")
        ws.cell(row=ri, column=resultado_col_idx).fill = PatternFill("solid", fgColor=color)

    for ci in range(1, len(df.columns) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 28

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Inscripciones Teams — formato nativo: Correo | Group ID
# ---------------------------------------------------------------------------

async def process_teams_enrollment_file(file_bytes: bytes, filename: str) -> bytes:
    """Lee Excel con columnas 'Correo' y 'Group ID'.
    Agrega cada usuario al equipo y devuelve el Excel con
    'Resultado' y 'FechaHoraEjecucion' agregadas."""
    from datetime import datetime, timezone

    df = _read_sheet(file_bytes, filename)
    norm = _normalize_cols(df.copy())

    correo_col = next((c for c in norm.columns if "correo" in c or "email" in c or "mail" in c), None)
    group_col  = next((c for c in norm.columns if "group" in c or "id" in c), None)

    if not correo_col or not group_col:
        raise ValueError("El Excel debe tener columnas 'Correo' y 'Group ID'")

    resultados = []
    fechas = []

    for _, row in norm.iterrows():
        correo   = str(row.get(correo_col, "")).strip()
        group_id = str(row.get(group_col, "")).strip()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if not correo or not group_id:
            resultados.append("Datos incompletos")
            fechas.append(ts)
            continue

        try:
            az_user = await graph_service.get_user_by_upn(correo)
            if not az_user:
                resultados.append("Usuario no encontrado")
                fechas.append(ts)
                continue
            await graph_service.add_member_to_team(group_id, az_user["id"])
            resultados.append("Agregado correctamente")
        except Exception as exc:
            msg = str(exc).lower()
            if "already" in msg or "member" in msg or "exists" in msg or "409" in msg:
                resultados.append("Ya es miembro")
            elif "not found" in msg or "404" in msg:
                resultados.append("Grupo no encontrado")
            else:
                resultados.append(f"Error: {str(exc)[:60]}")
        fechas.append(ts)

    df["Resultado"] = resultados
    df["FechaHoraEjecucion"] = fechas

    wb = Workbook()
    ws = wb.active
    ws.title = "Inscripciones Teams"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    status_colors = {
        "Agregado correctamente": "C6EFCE",
        "Ya es miembro":          "FFEB9C",
        "Grupo no encontrado":    "FFC7CE",
        "Usuario no encontrado":  "FFC7CE",
    }

    for ci, col in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    resultado_col_idx = list(df.columns).index("Resultado") + 1

    for ri, (_, row) in enumerate(df.iterrows(), 2):
        for ci, val in enumerate(row, 1):
            ws.cell(row=ri, column=ci, value=val)
        resultado = row["Resultado"]
        color = status_colors.get(resultado, "FFC7CE" if "Error" in resultado else "FFFFFF")
        ws.cell(row=ri, column=resultado_col_idx).fill = PatternFill("solid", fgColor=color)

    for ci in range(1, len(df.columns) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 28

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_plantilla_matricular() -> bytes:
    return _build_plantilla(
        headers=["cedula", "nombre", "email", "materia", "periodo", "rol"],
        rows=[
            ["3406399", "Juan Pérez", "jperez@usil.edu.py", "Matemática I", "2026-2", "StudentEnrollment"],
            ["5405805", "Ana García", "agarcia@usil.edu.py", "Administración", "2026-2", "StudentEnrollment"],
        ],
        sheet_name="Matriculacion",
    )


async def process_matricular_sheet(file_bytes: bytes, filename: str) -> bytes:
    """
    Excel con columnas: cedula, nombre, email, materia, periodo, rol (opcional).
    Para cada fila:
      1. Busca canvas_id y teams_id en tabla cursos
      2. Crea usuario en Canvas si no existe
      3. Matricula en Canvas
      4. Agrega al grupo de Teams
      5. Envía email de bienvenida/matriculación
    Retorna Excel con columna Estado por plataforma.
    """
    from datetime import datetime, timezone

    df = _read_sheet(file_bytes, filename)
    df = _normalize_cols(df)

    required = ["cedula", "materia"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"El Excel debe tener columnas: {', '.join(required)}")

    rows_out = []

    for _, row in df.iterrows():
        cedula  = str(row.get("cedula", "")).strip()
        nombre  = str(row.get("nombre", "")).strip()
        email   = str(row.get("email", "")).strip()
        import unicodedata as _ud
        materia = _ud.normalize("NFC", str(row.get("materia", "")).strip())
        periodo = str(row.get("periodo", row.get("semestre", settings.semestre_actual))).strip()
        if not periodo or periodo == "nan":
            periodo = settings.semestre_actual
        rol = str(row.get("rol", "StudentEnrollment")).strip()
        if not rol or rol == "nan":
            rol = "StudentEnrollment"

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        canvas_status = "pendiente"
        teams_status  = "pendiente"
        email_status  = "pendiente"

        if not cedula or not materia:
            rows_out.append({
                "Cédula": cedula, "Nombre": nombre, "Email": email,
                "Materia": materia, "Periodo": periodo,
                "Canvas": "error: cédula o materia faltante",
                "Teams": "-", "Email": "-", "Fecha": ts,
            })
            continue

        # 1. Buscar IDs en BD (LOWER + TRIM para tolerar diferencias de codificación/mayúsculas)
        curso_db = await db.fetchrow(
            "SELECT canvas_id, teams_id FROM cursos WHERE LOWER(TRIM(materia)) = LOWER(?) AND LOWER(TRIM(periodo)) = LOWER(?)",
            materia, periodo,
        )
        canvas_id = curso_db.get("canvas_id", "") if curso_db else ""
        teams_id  = curso_db.get("teams_id", "")  if curso_db else ""

        if not canvas_id and not teams_id:
            rows_out.append({
                "Cédula": cedula, "Nombre": nombre, "Email": email,
                "Materia": materia, "Periodo": periodo,
                "Canvas": f"error: curso '{materia} / {periodo}' no encontrado en BD. Creá el curso primero.",
                "Teams": "-", "Email Status": "-", "Fecha": ts,
            })
            continue

        # 2. Canvas: crear usuario si no existe y matricular
        if canvas_id:
            try:
                sis_id = cedula
                canvas_user = await canvas_service.find_user_by_sis_id(sis_id)
                if not canvas_user and email:
                    canvas_user = await canvas_service.create_user(nombre or cedula, email, sis_id)
                if canvas_user:
                    user_ref = str(canvas_user.get("id", ""))
                    await canvas_service.enroll_user(canvas_id, user_ref, rol)
                    canvas_status = "matriculado"
                else:
                    canvas_status = "error: usuario no creado (falta email)"
            except Exception as exc:
                err = str(exc).lower()
                if "already" in err or "exists" in err:
                    canvas_status = "ya inscrito"
                else:
                    canvas_status = f"error: {str(exc)[:80]}"
        else:
            canvas_status = "sin canvas_id"

        # 3. Teams: agregar al grupo
        if teams_id:
            try:
                azure_user = await graph_service.get_user_by_upn(email) if email else None
                if azure_user:
                    uid = azure_user.get("id", "")
                    added = await graph_service.add_member_to_group(teams_id, uid)
                    teams_status = "agregado" if added else "error: no se pudo agregar"
                else:
                    teams_status = "error: usuario Azure no encontrado/creado"
            except Exception as exc:
                teams_status = f"error: {str(exc)[:80]}"
        else:
            teams_status = "sin teams_id"

        # 4. Email
        email_status = "-"
        if email and canvas_status in ("matriculado", "ya inscrito"):
            try:
                await graph_service.send_welcome_email(
                    to_email=email,
                    nombre=nombre or cedula,
                    canvas_url=settings.canvas_base_url or "#",
                    username=email,
                )
                email_status = "enviado"
            except Exception as exc:
                email_status = f"error: {str(exc)[:60]}"

        rows_out.append({
            "Cédula": cedula, "Nombre": nombre, "Email": email,
            "Materia": materia, "Periodo": periodo,
            "Canvas": canvas_status, "Teams": teams_status,
            "Email Status": email_status, "Fecha": ts,
        })

    # Build output Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Matriculacion"
    headers = ["Cédula", "Nombre", "Email", "Materia", "Periodo", "Canvas", "Teams", "Email Status", "Fecha"]
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = header_fill
        cell.font = header_font

    ok_fill    = PatternFill("solid", fgColor="C6EFCE")
    warn_fill  = PatternFill("solid", fgColor="FFEB9C")
    error_fill = PatternFill("solid", fgColor="FFC7CE")

    for ri, r in enumerate(rows_out, 2):
        for ci, h in enumerate(headers, 1):
            ws.cell(row=ri, column=ci, value=r.get(h, ""))
        canvas_val = r.get("Canvas", "")
        fill = ok_fill if canvas_val in ("matriculado", "ya inscrito") else (error_fill if "error" in canvas_val else warn_fill)
        for ci in range(1, len(headers) + 1):
            ws.cell(row=ri, column=ci).fill = fill

    for ci in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
