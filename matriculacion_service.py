"""
Main orchestrator for the automatic enrollment (matriculación) workflow.

Flow:
  1. Download and parse Excel from OneDrive
  2. Validate all records — abort if errors (unless dry_run)
  3. For each student:
       a. Check if user exists in Canvas + Azure AD
       b. If NEW: create in Canvas, Azure AD, add to default Teams team
       c. For each subject: ensure Canvas course + Teams team exist, then enroll
       d. Send appropriate email (welcome or enrollment confirmation)
  4. Save execution to audit DB
  5. Send summary to Teams channel
  6. Email admin if there were errors
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

import canvas_service
import graph_service
import email_service
import audit_service
import teams_notify_service
import course_matcher
from onedrive_service import get_alumnos_from_onedrive, AlumnoData
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ──────────────────────────────────────────────
# In-memory progress store  (ejecucion_id → dict)
# ──────────────────────────────────────────────
_PROGRESS: dict[str, dict] = {}
BATCH_SIZE = 5  # students processed in parallel


def get_progress(ejecucion_id: str) -> dict | None:
    return _PROGRESS.get(ejecucion_id)


def _init_progress_placeholder(ejecucion_id: str) -> None:
    """Mark ejecucion as queued before background task starts."""
    _PROGRESS[ejecucion_id] = {
        "ejecucion_id": ejecucion_id,
        "total": 0,
        "procesados": 0,
        "creados": 0,
        "existentes": 0,
        "inscripciones": 0,
        "errores": 0,
        "estado": "preparando",
        "porcentaje": 0,
        "resultados": [],
        "alumnos_con_error": [],
    }


def _init_progress(ejecucion_id: str, total: int) -> None:
    _PROGRESS[ejecucion_id] = {
        "ejecucion_id": ejecucion_id,
        "total": total,
        "procesados": 0,
        "creados": 0,
        "existentes": 0,
        "inscripciones": 0,
        "errores": 0,
        "estado": "en_proceso",
        "porcentaje": 0,
        "resultados": [],
        "alumnos_con_error": [],  # cedulas with errors for retry
    }


def _update_progress(ejecucion_id: str, result: dict) -> None:
    p = _PROGRESS.get(ejecucion_id)
    if not p:
        return
    p["procesados"] += 1
    if result.get("es_nuevo") and not result.get("errores"):
        p["creados"] += 1
    elif not result.get("es_nuevo"):
        p["existentes"] += 1
    p["inscripciones"] += len([c for c in result.get("cursos", []) if not c.get("errores")])
    p["errores"] += len(result.get("errores", []))
    if result.get("errores"):
        p["alumnos_con_error"].append(result["cedula"])
    p["porcentaje"] = round(p["procesados"] / p["total"] * 100) if p["total"] else 100
    p["resultados"].append(result)


# ──────────────────────────────────────────────
# Credential helpers
# ──────────────────────────────────────────────

def _remove_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _name_parts(nombre: str) -> tuple[str, str]:
    """Return (first_name, first_surname) from a full name string."""
    parts = nombre.strip().split()
    first = parts[0] if parts else "X"
    last = parts[-1] if len(parts) > 1 else parts[0]
    return first, last


def generate_email(nombre: str) -> str:
    """luciano.florentin@usil.edu.py"""
    first, last = _name_parts(nombre)
    first_clean = re.sub(r"[^\w]", "", _remove_accents(first.lower()))
    last_clean = re.sub(r"[^\w]", "", _remove_accents(last.lower()))
    return f"{first_clean}.{last_clean}@usil.edu.py"


def generate_password(cedula: str, nombre: str) -> str:
    """cedula-Xa  (X = 1st letter of nombre UPPER, a = 1st letter of apellido lower)"""
    parts = nombre.strip().split()
    x = parts[0][0].upper() if parts else "A"
    a = _remove_accents(parts[1][0].lower()) if len(parts) > 1 else "x"
    return f"{cedula}-{x}{a}"


def generate_mail_nickname(nombre: str) -> str:
    """The local part before '@' for Azure AD mailNickname."""
    first, last = _name_parts(nombre)
    first_clean = re.sub(r"[^\w]", "", _remove_accents(first.lower()))
    last_clean = re.sub(r"[^\w]", "", _remove_accents(last.lower()))
    return f"{first_clean}.{last_clean}"


def generate_sis_id(materia: str, semestre: str) -> str:
    """USIL-2025-2-matematica-i"""
    norm = _remove_accents(materia.lower())
    norm = re.sub(r"[^\w\s]", "", norm)
    norm = re.sub(r"\s+", "-", norm.strip())
    norm = re.sub(r"-+", "-", norm)
    sem = re.sub(r"\s+", "-", semestre.strip())
    return f"USIL-{sem}-{norm}"


# ──────────────────────────────────────────────
# Per-materia processing
# ──────────────────────────────────────────────

async def _ensure_canvas_course(materia: str, semestre: str,
                                canvas_courses_cache: list[dict] | None = None) -> dict:
    """
    Find or create a Canvas course for `materia`.
    Uses fuzzy matching against existing courses before creating a new one.
    `canvas_courses_cache` should be pre-fetched once per run to avoid repeated API calls.
    """
    # 1. Try exact SIS lookup first
    sis_id = generate_sis_id(materia, semestre)
    course = await canvas_service.get_course_by_sis_id(sis_id)
    if course:
        return course

    # 2. Try fuzzy match against existing Canvas courses
    if canvas_courses_cache is not None:
        match = await course_matcher.resolve_course_name(materia, canvas_courses_cache, semestre)
        if match and match["canvas_id"]:
            logger.info("Fuzzy match: '%s' → '%s' (score=%.0f, source=%s)",
                        materia, match["canvas_name"], match["score"], match["source"])
            # Return the matched course; don't create a new one
            for c in canvas_courses_cache:
                if c.get("id") == match["canvas_id"]:
                    return c

    # 3. No match — create new course with auto-period
    term_id: int | None = None
    try:
        term = await canvas_service.get_or_create_term(semestre)
        term_id = term.get("id")
    except Exception as exc:
        logger.warning("No se pudo crear el período '%s': %s", semestre, exc)
    course = await canvas_service.create_course(materia, sis_id, semestre, term_id=term_id)
    logger.info("Canvas course created: %s (term_id=%s)", sis_id, term_id)
    return course


async def _ensure_teams_team(materia: str, semestre: str) -> dict | None:
    team_name = f"{semestre} - {materia}"
    try:
        team = await graph_service.find_team_by_display_name(team_name)
        if not team:
            team = await graph_service.create_team(team_name, f"Equipo académico: {materia} ({semestre})")
            logger.info("Teams team created: %s", team_name)
        return team
    except Exception as exc:
        logger.warning("No se pudo asegurar el Teams team '%s': %s", team_name, exc)
        return None


async def _process_materia(
    materia: str,
    semestre: str,
    canvas_user: dict | None,
    azure_user: dict | None,
    ejecucion_id: str,
    cedula: str,
    nombre: str,
    dry_run: bool,
    canvas_courses_cache: list[dict] | None = None,
) -> dict:
    result = {"materia": materia, "sis_id": generate_sis_id(materia, semestre), "errores": []}

    try:
        if dry_run:
            sis_id = generate_sis_id(materia, semestre)
            course_exists = await canvas_service.get_course_by_sis_id(sis_id)
            team_name = f"{semestre} - {materia}"
            team_exists = await graph_service.find_team_by_display_name(team_name)
            result.update({
                "dry_run": True,
                "course_existente": course_exists is not None,
                "team_existente": team_exists is not None,
                "accion_canvas": "inscribir en existente" if course_exists else "crear curso y inscribir",
                "accion_teams": "agregar a existente" if team_exists else "crear equipo y agregar",
            })
            return result

        # Real mode
        course = await _ensure_canvas_course(materia, semestre, canvas_courses_cache)
        result["course_id"] = course.get("id")

        team = await _ensure_teams_team(materia, semestre)
        result["team_id"] = team.get("id") if team else None

        # Canvas enrollment
        if canvas_user:
            try:
                await canvas_service.enroll_user(str(course["id"]), str(canvas_user["id"]))
                await audit_service.log_action(ejecucion_id, cedula, nombre, "inscrito", "Canvas", materia, semestre)
            except Exception as exc:
                msg = f"Canvas enroll error: {exc}"
                result["errores"].append(msg)
                await audit_service.log_action(ejecucion_id, cedula, nombre, "error", "Canvas", materia, semestre, msg)

        # Teams enrollment
        if azure_user and team:
            try:
                await graph_service.add_member_to_team(team["id"], azure_user["id"])
                await audit_service.log_action(ejecucion_id, cedula, nombre, "inscrito", "Teams", materia, semestre)
            except Exception as exc:
                msg = f"Teams add error: {exc}"
                result["errores"].append(msg)
                await audit_service.log_action(ejecucion_id, cedula, nombre, "error", "Teams", materia, semestre, msg)

    except Exception as exc:
        msg = str(exc)
        result["errores"].append(msg)
        await audit_service.log_action(ejecucion_id, cedula, nombre, "error", "Canvas/Teams", materia, semestre, msg)

    return result


# ──────────────────────────────────────────────
# Per-student processing
# ──────────────────────────────────────────────

async def _process_alumno(
    alumno: AlumnoData,
    semestre: str,
    ejecucion_id: str,
    dry_run: bool,
    shared_canvas_cache: list[dict] | None = None,
) -> dict:
    # Use semester detected in the student's own sheet if available
    semestre = alumno.semestre or semestre
    email = generate_email(alumno.nombre)
    password = generate_password(alumno.cedula, alumno.nombre)

    result: dict[str, Any] = {
        "cedula": alumno.cedula,
        "nombre": alumno.nombre,
        "email": email,
        "es_nuevo": False,
        "cursos": [],
        "errores": [],
    }

    try:
        # Check existing users in parallel
        canvas_user, azure_user = await asyncio.gather(
            canvas_service.find_user_by_sis_id(alumno.cedula),
            graph_service.get_user_by_upn(email),
            return_exceptions=False,
        )

        is_new = canvas_user is None and azure_user is None
        result["es_nuevo"] = is_new

        if not dry_run and is_new:
            # Create Canvas user
            try:
                canvas_user = await canvas_service.create_user(alumno.nombre, email, alumno.cedula)
                await audit_service.log_action(ejecucion_id, alumno.cedula, alumno.nombre,
                                               "creado", "Canvas", "", semestre)
            except Exception as exc:
                msg = f"Canvas user create: {exc}"
                result["errores"].append(msg)
                await audit_service.log_action(ejecucion_id, alumno.cedula, alumno.nombre,
                                               "error", "Canvas", "", semestre, msg)

            # Create Azure AD user
            try:
                mail_nick = generate_mail_nickname(alumno.nombre)
                azure_user = await graph_service.create_user(alumno.nombre, mail_nick, email, password)
                await audit_service.log_action(ejecucion_id, alumno.cedula, alumno.nombre,
                                               "creado", "AzureAD", "", semestre)
            except Exception as exc:
                msg = f"Azure user create: {exc}"
                result["errores"].append(msg)
                await audit_service.log_action(ejecucion_id, alumno.cedula, alumno.nombre,
                                               "error", "AzureAD", "", semestre, msg)

            # Add to default Teams team
            if azure_user and settings.teams_default_team_id:
                try:
                    await graph_service.add_member_to_team(
                        settings.teams_default_team_id, azure_user["id"]
                    )
                    await audit_service.log_action(ejecucion_id, alumno.cedula, alumno.nombre,
                                                   "creado", "Teams", "Equipo principal", semestre)
                except Exception as exc:
                    result["errores"].append(f"Teams default team: {exc}")

        # Use shared cache if provided (batched run), else fetch once for this student
        canvas_courses_cache: list[dict] = shared_canvas_cache or []
        if not canvas_courses_cache:
            try:
                canvas_courses_cache = await canvas_service.get_courses(per_page=200)
            except Exception as exc:
                logger.warning("No se pudo cargar cursos Canvas para fuzzy matching: %s", exc)

        # Process each subject
        materia_tasks = [
            _process_materia(
                materia, semestre,
                canvas_user if not dry_run else None,
                azure_user if not dry_run else None,
                ejecucion_id, alumno.cedula, alumno.nombre, dry_run,
                canvas_courses_cache=canvas_courses_cache,
            )
            for materia in alumno.materias
        ]
        course_results = await asyncio.gather(*materia_tasks, return_exceptions=True)

        for cr in course_results:
            if isinstance(cr, Exception):
                result["errores"].append(str(cr))
            else:
                result["cursos"].append(cr)
                if cr.get("errores"):
                    result["errores"].extend(cr["errores"])

        if not dry_run:
            enrolled_names = [c["materia"] for c in result["cursos"] if not c.get("errores")]
            if is_new and canvas_user:
                await email_service.send_welcome_email(
                    to=email,
                    nombre=alumno.nombre,
                    password=password,
                    canvas_url=settings.canvas_base_url,
                    teams_url=settings.teams_base_url,
                    cursos=enrolled_names,
                    semestre=semestre,
                )
            elif not is_new and enrolled_names:
                await email_service.send_enrollment_confirmation(
                    to=email,
                    nombre=alumno.nombre,
                    cursos=enrolled_names,
                    semestre=semestre,
                )

    except Exception as exc:
        msg = f"Error general procesando alumno {alumno.cedula}: {exc}"
        result["errores"].append(msg)
        logger.exception(msg)

    return result


# ──────────────────────────────────────────────
# Parallel batch helpers
# ──────────────────────────────────────────────

async def _run_batched(
    alumnos: list[AlumnoData],
    semestre: str,
    ejecucion_id: str,
    dry_run: bool,
) -> list[dict]:
    """Process students in parallel batches of BATCH_SIZE.
    Fetches Canvas courses once and shares across all batches."""
    _init_progress(ejecucion_id, len(alumnos))

    # Fetch shared Canvas courses cache once
    shared_cache: list[dict] = []
    if not dry_run:
        try:
            shared_cache = await canvas_service.get_courses(per_page=200)
        except Exception as exc:
            logger.warning("No se pudo cargar cursos Canvas para batch: %s", exc)

    resultados: list[dict] = []
    for i in range(0, len(alumnos), BATCH_SIZE):
        batch = alumnos[i:i + BATCH_SIZE]
        batch_results = await asyncio.gather(
            *[_process_alumno(a, semestre, ejecucion_id, dry_run, shared_cache) for a in batch],
            return_exceptions=True,
        )
        for alumno, res in zip(batch, batch_results):
            if isinstance(res, Exception):
                res = {
                    "cedula": alumno.cedula,
                    "nombre": alumno.nombre,
                    "es_nuevo": False,
                    "cursos": [],
                    "errores": [str(res)],
                }
            _update_progress(ejecucion_id, res)
            resultados.append(res)
        logger.info("Batch %d/%d completado", min(i + BATCH_SIZE, len(alumnos)), len(alumnos))

    p = _PROGRESS.get(ejecucion_id, {})
    p["estado"] = "completado"
    p["porcentaje"] = 100
    return resultados


def _build_summary(
    ejecucion_id: str,
    start: datetime,
    tipo: str,
    semestre: str,
    alumnos: list,
    resultados: list[dict],
    elapsed: float,
    dry_run: bool,
) -> dict:
    creados = sum(1 for r in resultados if r.get("es_nuevo") and not r.get("errores"))
    existentes = sum(1 for r in resultados if not r.get("es_nuevo"))
    inscripciones = sum(len([c for c in r.get("cursos", []) if not c.get("errores")]) for r in resultados)
    total_errores = sum(len(r.get("errores", [])) for r in resultados)
    all_error_details = [e for r in resultados for e in r.get("errores", [])]

    estado = "exitoso" if total_errores == 0 else "con_errores"
    if not resultados:
        estado = "sin_datos"

    return {
        "ejecucion_id": ejecucion_id,
        "timestamp": start.isoformat(),
        "tipo": tipo,
        "semestre": semestre,
        "estado": estado,
        "total": len(alumnos),
        "creados": creados,
        "existentes": existentes,
        "inscripciones": inscripciones,
        "errores": total_errores,
        "duracion_seg": round(elapsed, 2),
        "resultados": resultados,
        "errores_validacion": [],
        "detalles_errores": all_error_details[:20],
        "dry_run": dry_run,
    }


async def retry_failed(ejecucion_id: str, semestre: str, dry_run: bool = False) -> dict:
    """Re-process only the students that had errors in a previous run."""
    p = _PROGRESS.get(ejecucion_id)
    if not p:
        return {"error": "Ejecución no encontrada en memoria"}
    failed_cedulas = set(p.get("alumnos_con_error", []))
    failed_alumnos = [
        AlumnoData(**{k: v for k, v in r.items() if k in ("cedula", "nombre", "materias", "semestre", "email")})
        for r in p.get("resultados", [])
        if r.get("cedula") in failed_cedulas
    ]
    if not failed_alumnos:
        return {"message": "No hay alumnos con error para reintentar", "total": 0}

    new_ej_id = f"{ejecucion_id}-retry"
    start = datetime.now(timezone.utc)
    resultados = await _run_batched(failed_alumnos, semestre, new_ej_id, dry_run)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    return _build_summary(new_ej_id, start, "retry", semestre, failed_alumnos, resultados, elapsed, dry_run)


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

async def run_matriculacion_from_bytes(
    excel_bytes: bytes,
    dry_run: bool = False,
    semestre: str | None = None,
    ejecucion_id: str | None = None,
) -> dict:
    """Same as run_matriculacion but reads Excel from bytes (file upload) instead of OneDrive."""
    import openpyxl
    from onedrive_service import parse_sheet, validate_alumnos

    await audit_service.init_db()
    semestre = semestre or settings.semestre_actual or "SEM-ACTUAL"
    ejecucion_id = ejecucion_id or str(uuid.uuid4())
    tipo = "dry_run" if dry_run else "manual"
    start = datetime.now(timezone.utc)

    logger.info("Matriculación desde archivo (dry_run=%s, semestre=%s)", dry_run, semestre)

    try:
        import io as _io
        wb = openpyxl.load_workbook(_io.BytesIO(excel_bytes), read_only=False, data_only=True)
        alumnos = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            alumno = parse_sheet(ws, sheet_name)
            if alumno:
                alumnos.append(alumno)
        validation_errors = validate_alumnos(alumnos)
    except Exception as exc:
        msg = f"Error procesando Excel: {exc}"
        summary = {
            "ejecucion_id": ejecucion_id,
            "timestamp": start.isoformat(),
            "tipo": tipo,
            "semestre": semestre,
            "estado": "fallido",
            "error_global": msg,
            "total": 0, "creados": 0, "existentes": 0, "inscripciones": 0, "errores": 1,
            "duracion_seg": 0,
            "resultados": [],
            "errores_validacion": [],
        }
        await audit_service.save_ejecucion(summary)
        return summary

    if validation_errors:
        errs = [{"sheet_name": e.sheet_name, "cedula": e.cedula, "nombre": e.nombre, "error": e.error}
                for e in validation_errors]
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        return {
            "ejecucion_id": ejecucion_id,
            "timestamp": start.isoformat(),
            "tipo": tipo,
            "semestre": semestre,
            "estado": "error_validacion",
            "total": 0, "creados": 0, "existentes": 0, "inscripciones": 0,
            "errores": len(errs),
            "duracion_seg": elapsed,
            "resultados": [],
            "errores_validacion": errs,
        }

    resultados = await _run_batched(alumnos, semestre, ejecucion_id, dry_run)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    summary = _build_summary(ejecucion_id, start, tipo, semestre, alumnos, resultados, elapsed, dry_run)

    if not dry_run:
        await audit_service.save_ejecucion(summary)
        all_error_details = [e for r in resultados for e in r["errores"]]
        if summary["errores"] > 0 and settings.admin_email:
            await email_service.send_admin_error_report(
                to=settings.admin_email,
                semestre=semestre,
                errores=[{"error": e} for e in all_error_details],
                tipo="Errores en proceso de matriculación",
            )
        await teams_notify_service.send_matriculacion_summary(summary)

    logger.info(
        "Matriculación (archivo) finalizada: total=%d creados=%d errores=%d (%.1fs)",
        len(alumnos), summary["creados"], summary["errores"], elapsed,
    )
    return summary


async def run_matriculacion(
    dry_run: bool = False,
    semestre: str | None = None,
    ejecucion_id: str | None = None,
) -> dict:
    """
    Execute the full enrollment workflow.
    Returns a summary dict with totals and per-student results.
    """
    await audit_service.init_db()

    semestre = semestre or settings.semestre_actual or "SEM-ACTUAL"
    ejecucion_id = ejecucion_id or str(uuid.uuid4())
    tipo = "dry_run" if dry_run else "manual"
    start = datetime.now(timezone.utc)

    logger.info("Iniciando matriculación (dry_run=%s, semestre=%s)", dry_run, semestre)

    # 1. Download + parse Excel
    try:
        alumnos, validation_errors = await get_alumnos_from_onedrive()
    except Exception as exc:
        msg = f"Error descargando Excel desde OneDrive: {exc}"
        logger.exception(msg)
        summary = {
            "ejecucion_id": ejecucion_id,
            "timestamp": start.isoformat(),
            "tipo": tipo,
            "semestre": semestre,
            "estado": "fallido",
            "error_global": msg,
            "total": 0, "creados": 0, "existentes": 0, "inscripciones": 0, "errores": 1,
            "duracion_seg": 0,
            "resultados": [],
            "errores_validacion": [],
        }
        await audit_service.save_ejecucion(summary)
        if not dry_run:
            await teams_notify_service.send_matriculacion_summary(summary)
        return summary

    # 2. Validate (abort if errors, unless dry_run)
    if validation_errors:
        errs = [{"sheet_name": e.sheet_name, "cedula": e.cedula, "nombre": e.nombre, "error": e.error}
                for e in validation_errors]
        logger.warning("Validación fallida: %d error(es)", len(errs))
        if not dry_run:
            await teams_notify_service.send_validation_error_alert(errs)
            if settings.admin_email:
                await email_service.send_admin_error_report(
                    to=settings.admin_email,
                    semestre=semestre,
                    errores=errs,
                    tipo="Error de validación en Excel",
                )
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        return {
            "ejecucion_id": ejecucion_id,
            "timestamp": start.isoformat(),
            "tipo": tipo,
            "semestre": semestre,
            "estado": "error_validacion",
            "total": 0, "creados": 0, "existentes": 0, "inscripciones": 0,
            "errores": len(errs),
            "duracion_seg": elapsed,
            "resultados": [],
            "errores_validacion": errs,
        }

    # 3. Process students in parallel batches
    resultados = await _run_batched(alumnos, semestre, ejecucion_id, dry_run)

    # 4. Build summary
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    summary = _build_summary(ejecucion_id, start, tipo, semestre, alumnos, resultados, elapsed, dry_run)

    # 5. Persist
    if not dry_run:
        await audit_service.save_ejecucion(summary)
        all_error_details = [e for r in resultados for e in r["errores"]]
        if summary["errores"] > 0 and settings.admin_email:
            await email_service.send_admin_error_report(
                to=settings.admin_email,
                semestre=semestre,
                errores=[{"error": e} for e in all_error_details],
                tipo="Errores en proceso de matriculación",
            )
        await teams_notify_service.send_matriculacion_summary(summary)

    logger.info(
        "Matriculación finalizada: total=%d creados=%d existentes=%d inscripciones=%d errores=%d (%.1fs)",
        len(alumnos), summary["creados"], summary["existentes"],
        summary["inscripciones"], summary["errores"], elapsed,
    )
    return summary
