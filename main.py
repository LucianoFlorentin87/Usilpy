import io
import logging
import os
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel

import canvas_service
import graph_service
import bulk_service
import auth_service
import audit_service
import matriculacion_service
import user_service
import webhook_service
import course_matcher
import parseo_service
from scheduler import lifespan, get_next_run

app = FastAPI(title="Gestión Académica Universitaria", version="2.0.0", lifespan=lifespan)

# ── Security headers ───────────────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ── CORS ───────────────────────────────────────────────────────────────────────
_settings_cors = auth_service.settings  # reuse already-imported settings reference later
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] or [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Simple in-process rate limiter for login ──────────────────────────────────
_login_attempts: dict = defaultdict(list)
_import_jobs: dict = {}  # job_id -> {status, result}
_LOGIN_MAX = 10       # attempts
_LOGIN_WINDOW = 300   # seconds (5 min)

app.mount("/static", StaticFiles(directory="static"), name="static")

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        payload = auth_service.decode_token(credentials.credentials)
        return payload
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def require_role(*roles: str):
    async def _dep(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Permisos insuficientes")
        return current_user
    return _dep


_require_admin = require_role("admin")
_require_admin_or_academico = require_role("admin", "academico")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/api/admin/retry-team/{group_id}")
async def retry_team_provisioning(group_id: str, _: dict = Depends(get_current_user)):
    """Reintenta convertir un grupo M365 en equipo Teams."""
    import asyncio as _aio
    import graph_service as _gs
    hdrs = _gs._headers()
    import httpx as _httpx
    GRAPH_BASE = _gs.GRAPH_BASE
    team_payload = {
        "memberSettings": {"allowCreateUpdateChannels": True},
        "messagingSettings": {"allowUserEditMessages": True, "allowUserDeleteMessages": True},
    }
    async with _httpx.AsyncClient(timeout=60) as client:
        # Check group exists
        r = await client.get(f"{GRAPH_BASE}/groups/{group_id}?$select=id,displayName", headers=hdrs)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Grupo no encontrado en Azure AD")
        group_name = r.json().get("displayName", group_id)
        # Retry PUT /team up to 5 times
        for attempt in range(5):
            tr = await client.put(f"{GRAPH_BASE}/groups/{group_id}/team", headers=hdrs, json=team_payload)
            if tr.status_code in (200, 201):
                return {"status": "ok", "group": group_name, "team_id": tr.json().get("id", group_id)}
            if tr.status_code in (404, 409):
                await _aio.sleep(5)
                continue
            raise HTTPException(status_code=tr.status_code, detail=tr.text[:300])
    return {"status": "timeout", "group": group_name, "detail": "El equipo puede aparecer en Teams en unos minutos"}


@app.get("/api/canvas-ping")
async def canvas_ping():
    """Diagnóstico público de Canvas — sin auth."""
    import canvas_service as _cs
    from config import get_settings
    s = get_settings()
    result = {"canvas_base_url": s.canvas_base_url or "(vacío)", "canvas_api_token": "configurado" if s.canvas_api_token else "(vacío)"}
    try:
        acct_id = await _cs._account_id()
        result["account_id"] = acct_id
        terms = await _cs.get_terms()
        result["canvas_conexion"] = f"OK — {len(terms)} periodos"
    except Exception as exc:
        result["canvas_conexion"] = f"ERROR: {exc}"
    return result


@app.get("/api/diagnostico")
async def diagnostico(_: dict = Depends(get_current_user)):
    """Diagnóstico de variables de entorno y conectividad real con Canvas y Azure."""
    from config import get_settings
    import canvas_service as _cs
    import graph_service as _gs
    s = get_settings()

    # Test Canvas connectivity
    canvas_test = "no probado"
    try:
        terms = await _cs.get_terms()
        canvas_test = f"✓ OK — {len(terms)} períodos encontrados"
    except Exception as exc:
        canvas_test = f"✗ ERROR: {exc}"

    # Test Azure/Teams connectivity
    azure_test = "no probado"
    try:
        token = _gs._get_token()
        azure_test = "✓ OK — token obtenido"
    except Exception as exc:
        azure_test = f"✗ ERROR: {exc}"

    return {
        "canvas_base_url":    s.canvas_base_url or "(vacío)",
        "canvas_api_token":   "✓ configurado" if s.canvas_api_token else "(vacío)",
        "azure_tenant_id":    "✓ configurado" if s.azure_tenant_id else "(vacío)",
        "azure_client_id":    "✓ configurado" if s.azure_client_id else "(vacío)",
        "azure_client_secret":"✓ configurado" if s.azure_client_secret else "(vacío)",
        "admin_username":     s.admin_username or "(vacío)",
        "semestre_actual":    s.semestre_actual,
        "canvas_conexion":    canvas_test,
        "azure_conexion":     azure_test,
    }


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request):
    # Rate limiting: max _LOGIN_MAX attempts per IP per _LOGIN_WINDOW seconds
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intente en 5 minutos.")
    _login_attempts[ip].append(now)

    # Try DB users first (async)
    db_user = await user_service.get_user_by_username(body.username)
    if db_user:
        if not auth_service.verify_password(body.password, db_user["password_hash"]):
            raise HTTPException(status_code=401, detail="Credenciales incorrectas")
        await user_service.update_last_login(db_user["id"])
        payload = {
            "sub": db_user["id"],
            "username": db_user["username"],
            "name": db_user["full_name"] or db_user["username"],
            "email": db_user["email"],
            "role": db_user["role"],
            "provider": "local",
        }
    else:
        # Fallback: env-based admin (initial setup before any DB user)
        settings = auth_service.settings
        if (body.username == settings.admin_username
                and settings.admin_password_hash
                and auth_service.verify_password(body.password, settings.admin_password_hash)):
            payload = {"sub": body.username, "username": body.username,
                       "name": body.username, "role": "admin", "provider": "local"}
        else:
            raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = auth_service.create_access_token(payload)
    return {"access_token": token, "token_type": "bearer", "user": payload}


@app.post("/api/auth/refresh")
async def refresh_token(current_user: dict = Depends(get_current_user)):
    """Renueva el token JWT sin necesidad de volver a loguearse."""
    payload = {k: v for k, v in current_user.items() if k != "exp"}
    token = auth_service.create_access_token(payload)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/auth/azure/login")
async def azure_login():
    if not auth_service.settings.azure_client_id:
        raise HTTPException(status_code=503, detail="Azure AD no configurado")
    url = auth_service.build_azure_login_url()
    return RedirectResponse(url)


@app.get("/api/auth/azure/callback")
async def azure_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        # Never reflect external error strings — use a fixed safe message
        return RedirectResponse("/?auth_error=azure_login_failed")
    try:
        user = await auth_service.exchange_azure_code(code, state)
    except Exception:
        return RedirectResponse("/?auth_error=azure_login_failed")
    token = auth_service.create_access_token(user)
    # Return token in JSON body via a short-lived server-set cookie to avoid URL exposure
    response = RedirectResponse("/#azure_ok")
    response.set_cookie(
        "azure_token", token,
        httponly=False,  # JS reads it once then clears it; kept non-httponly intentionally for SPA auth flow
        secure=True,
        samesite="strict",
        max_age=60,      # 1-minute window to consume; cleared by JS after read
        path="/",
    )
    return response


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return current_user


@app.post("/api/auth/logout")
async def logout():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

@app.get("/api/canvas/terms")
async def list_terms(_: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_terms()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/canvas/terms")
async def create_term(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_or_create_term(payload["name"])
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.get("/api/canvas/courses")
async def list_courses(_: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_courses()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)
        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.get("/api/canvas/all-courses")
async def list_all_courses(_: dict = Depends(_require_admin)):
    """Todos los cursos de Canvas con id, nombre y sis_course_id (paginado)."""
    try:
        return await canvas_service.get_all_courses()
    except Exception as exc:
        logger.error("Canvas all-courses error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/canvas/asistencia/{course_id}")
async def reporte_asistencia(course_id: int, umbral: float = 70, _: dict = Depends(_require_admin)):
    """Reporte de asistencia (Roll Call) de un curso con % y habilitación a examen."""
    try:
        rep = await canvas_service.get_roll_call_report(course_id)
    except Exception as exc:
        logger.error("Canvas asistencia error curso %s: %s", course_id, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    if not rep["disponible"]:
        raise HTTPException(status_code=404, detail="Este curso no tiene registros de Roll Call Attendance en Canvas.")
    alumnos = []
    for a in rep["alumnos"]:
        pct = a["porcentaje"]
        alumnos.append({**a, "habilitado": (pct is not None and pct >= umbral)})
    total = len(alumnos)
    habilitados = sum(1 for a in alumnos if a["habilitado"])
    sin_registro = sum(1 for a in alumnos if a["porcentaje"] is None)
    return {
        "course_id": course_id,
        "umbral": umbral,
        "total": total,
        "habilitados": habilitados,
        "no_habilitados": total - habilitados,
        "sin_registro": sin_registro,
        "alumnos": alumnos,
    }


@app.get("/api/canvas/asistencia/{course_id}/excel")
async def reporte_asistencia_excel(course_id: int, umbral: float = 70, curso: str = "", _: dict = Depends(_require_admin)):
    """Descarga el reporte de asistencia como .xlsx con formato."""
    rep = await reporte_asistencia(course_id, umbral, _)
    try:
        detalle = await canvas_service.get_roll_call_detail(course_id)
    except Exception as exc:
        logger.warning("Roll Call detalle no disponible curso %s: %s", course_id, exc)
        detalle = {"disponible": False, "registros": []}

    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    # Índices del detalle: por alumno y fecha
    por_alumno: dict = {}
    fechas_set = set()
    for reg in detalle["registros"]:
        por_alumno.setdefault(reg["student_id"], {})[reg["fecha"]] = reg["estado"]
        fechas_set.add(reg["fecha"])
    fechas = sorted(fechas_set)

    wb = Workbook()
    ws = wb.active
    ws.title = "Asistencia"

    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF")
    ok_fill   = PatternFill("solid", fgColor="DCFCE7")
    ok_font   = Font(bold=True, color="15803D")
    bad_fill  = PatternFill("solid", fgColor="FEE2E2")
    bad_font  = Font(bold=True, color="B91C1C")
    na_font   = Font(color="9CA3AF")
    thin = Border(*[Side(style="thin", color="D1D5DB")] * 4)
    center = Alignment(horizontal="center")

    # Título y resumen
    ws["A1"] = "Reporte de asistencia (Roll Call)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Curso: {curso or course_id}"
    ws["A3"] = f"Mínimo para habilitar examen: {umbral:g}%"
    ws["A4"] = (f"Alumnos: {rep['total']}  ·  Habilitados: {rep['habilitados']}  ·  "
                f"No habilitados: {rep['no_habilitados']}  ·  Sin registro: {rep['sin_registro']}")

    headers = ["#", "Alumno", "Cédula / Login", "% Asistencia", "Habilitado"]
    if fechas:
        headers += ["Presentes", "Ausentes", "Tardanzas"]
    ws.append([])
    ws.append(headers)
    hrow = ws.max_row
    for col in range(1, len(headers) + 1):
        c = ws.cell(row=hrow, column=col)
        c.fill = header_fill
        c.font = header_font
        c.alignment = center
        c.border = thin

    for i, a in enumerate(rep["alumnos"], 1):
        pct = a["porcentaje"]
        fila = [
            i, a["nombre"], a["sis_user_id"] or a["login_id"] or "—",
            pct if pct is not None else "Sin registro",
            "SÍ" if a["habilitado"] else ("—" if pct is None else "NO"),
        ]
        if fechas:
            estados = por_alumno.get(a["user_id"], {})
            fila += [
                sum(1 for e in estados.values() if e == "present"),
                sum(1 for e in estados.values() if e == "absent"),
                sum(1 for e in estados.values() if e == "late"),
            ]
        ws.append(fila)
        r = ws.max_row
        for col in range(1, len(headers) + 1):
            ws.cell(row=r, column=col).border = thin
        ws.cell(row=r, column=1).alignment = center
        ws.cell(row=r, column=4).alignment = center
        ws.cell(row=r, column=5).alignment = center
        estado = ws.cell(row=r, column=5)
        pct_cell = ws.cell(row=r, column=4)
        if pct is None:
            estado.font = na_font
            pct_cell.font = na_font
        elif a["habilitado"]:
            estado.fill = ok_fill
            estado.font = ok_font
            pct_cell.font = Font(color="15803D")
        else:
            estado.fill = bad_fill
            estado.font = bad_font
            pct_cell.font = Font(color="B91C1C")

    widths = {"A": 6, "B": 42, "C": 18, "D": 15, "E": 14, "F": 11, "G": 11, "H": 11}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = f"A{hrow + 1}"

    # ── Hoja 2: planilla de asistencia por fecha (formato institucional) ──
    if fechas:
        from openpyxl.utils import get_column_letter
        from datetime import date as _date

        nombre_hoja = "".join(ch for ch in (curso or "DETALLE").upper() if ch not in "[]:*?/\\")[:31] or "DETALLE"
        ws2 = wb.create_sheet(nombre_hoja)

        n_fechas = len(fechas)
        ultima_col = 1 + n_fechas + 2  # ESTUDIANTE + fechas + TOTAL + %

        # Fila 1: título del curso (merge en todo el ancho)
        ws2.cell(row=1, column=1, value=(curso or "").upper() or nombre_hoja)
        ws2.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ultima_col)
        t = ws2.cell(row=1, column=1)
        t.font = Font(bold=True, size=13)
        t.alignment = Alignment(horizontal="center")

        # Fila 3: cabecera
        ws2.cell(row=3, column=1, value="ESTUDIANTE")
        for j, f in enumerate(fechas, start=2):
            y, m, d = int(f[:4]), int(f[5:7]), int(f[8:10])
            c = ws2.cell(row=3, column=j, value=_date(y, m, d))
            c.number_format = "DD/MM"
        ws2.cell(row=3, column=1 + n_fechas + 1, value="TOTAL ASISTENCIA")
        ws2.cell(row=3, column=1 + n_fechas + 2, value="% ASISTENCIA")
        for col in range(1, ultima_col + 1):
            c = ws2.cell(row=3, column=col)
            c.fill = header_fill
            c.font = header_font
            c.alignment = center
            c.border = thin

        # Filas de alumnos: 1 = presente/tardanza, 0 = ausente, vacío = sin registro
        alumnos_orden = sorted(rep["alumnos"], key=lambda a: a["nombre"].upper())
        for i, a in enumerate(alumnos_orden, start=4):
            estados = por_alumno.get(a["user_id"], {})
            ws2.cell(row=i, column=1, value=a["nombre"].upper()).border = thin
            for j, f in enumerate(fechas, start=2):
                est = estados.get(f)
                val = None if est is None else (0 if est == "absent" else 1)
                c = ws2.cell(row=i, column=j, value=val)
                c.alignment = center
                c.border = thin
                if val == 0:
                    c.font = Font(color="B91C1C")
            col_ini = get_column_letter(2)
            col_fin = get_column_letter(1 + n_fechas)
            ct = ws2.cell(row=i, column=1 + n_fechas + 1, value=f"=SUM({col_ini}{i}:{col_fin}{i})")
            ct.alignment = center
            ct.border = thin
            ct.font = Font(bold=True)
            cp = ws2.cell(row=i, column=1 + n_fechas + 2,
                          value=f"={get_column_letter(1 + n_fechas + 1)}{i}/{n_fechas}")
            cp.number_format = "0%"
            cp.alignment = center
            cp.border = thin
            cp.font = Font(bold=True)

        ws2.column_dimensions["A"].width = 40
        for j in range(2, 1 + n_fechas + 1):
            ws2.column_dimensions[get_column_letter(j)].width = 6.5
        ws2.column_dimensions[get_column_letter(1 + n_fechas + 1)].width = 18
        ws2.column_dimensions[get_column_letter(1 + n_fechas + 2)].width = 14
        ws2.freeze_panes = "B4"
    else:
        ws_nota = wb.create_sheet("Detalle por fecha")
        ws_nota["A1"] = "El detalle día-por-día no está disponible para este curso (Roll Call no accesible o sin registros)."

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (curso or str(course_id)))[:60].strip() or str(course_id)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="asistencia_{safe_name}.xlsx"'},
    )


@app.get("/api/canvas/users")
async def list_canvas_users(_: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_users()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/canvas/users")
async def create_canvas_user(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.create_user(
            name=payload["nombre"],
            email=payload["email"],
            sis_id=payload.get("sis_id", ""),
        )
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/canvas/courses/{course_id}/enrollments")
async def enroll(course_id: str, payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.enroll_user(
            course_id=course_id,
            user_id=payload["user_id"],
            role=payload.get("role", "StudentEnrollment"),
        )
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


# ---------------------------------------------------------------------------
# Azure AD / Microsoft Graph
# ---------------------------------------------------------------------------

@app.get("/api/azure/users")
async def list_azure_users(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_users()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/azure/users")
async def create_azure_user(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await graph_service.create_user(
            display_name=payload["display_name"],
            mail_nickname=payload["mail_nickname"],
            upn=payload["upn"],
            password=payload["password"],
        )
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.get("/api/azure/groups")
async def list_groups(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_groups()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/azure/groups")
async def create_group(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await graph_service.create_group(
            display_name=payload["display_name"],
            description=payload.get("description", ""),
        )
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------

@app.get("/api/teams")
async def list_teams(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_teams()
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/teams")
async def create_team_endpoint(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await graph_service.create_team(
            display_name=payload["display_name"],
            description=payload.get("description", ""),
        )
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


@app.post("/api/teams/members")
async def add_team_member_endpoint(payload: dict, _: dict = Depends(get_current_user)):
    try:
        ok = await graph_service.add_member_to_team(payload["team_id"], payload["user_id"])
        if not ok:
            raise HTTPException(status_code=400, detail="No se pudo agregar el miembro")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Upstream service error: %s", exc)

        raise HTTPException(status_code=502, detail="Error de comunicación con servicio externo")


# ---------------------------------------------------------------------------
# Bulk / Carga Masiva
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
TEMPLATE_PATH = "plantilla_carga_masiva.xlsx"


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def _validate_file(file: UploadFile) -> None:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado '{ext}'. Use .xlsx, .xls o .csv",
        )


async def _read_validated(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande (máx 20 MB)")
    return data


@app.post("/api/bulk/usuarios")
async def bulk_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/inscripciones")
async def bulk_inscripciones(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_enrollments_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.get("/api/bulk/template")
async def bulk_template(_: dict = Depends(get_current_user)):
    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(status_code=404, detail="Plantilla no encontrada en el servidor")
    return FileResponse(
        path=TEMPLATE_PATH,
        filename="plantilla_carga_masiva.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/bulk/template/{tipo}")
async def bulk_template_tipo(tipo: str, _: dict = Depends(get_current_user)):
    """Genera y devuelve una plantilla Excel vacía según el tipo solicitado."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    templates = {
        "cursos": ["nombre", "sis_id", "semestre", "crear_en_canvas", "nombre_equipo_teams", "crear_en_teams"],
        "canvas-usuarios": ["nombre", "email", "sis_id"],
        "azure-usuarios": ["nombre", "upn", "password", "grupo_id"],
        "teams": ["nombre", "descripcion"],
        "canvas-inscripciones": ["email_usuario", "curso_id", "rol"],
        "usuarios": ["nombre", "email", "sis_id", "rol_canvas", "upn_azure", "grupo_azure", "equipo_teams"],
        "inscripciones": ["email_usuario", "curso_canvas_id", "rol_canvas", "grupo_azure", "equipo_teams"],
    }
    cols = templates.get(tipo)
    if not cols:
        raise HTTPException(status_code=404, detail=f"Tipo '{tipo}' no reconocido")

    wb = Workbook()
    ws = wb.active
    ws.title = tipo
    fill = PatternFill("solid", fgColor="1F4E79")
    font = Font(color="FFFFFF", bold=True)
    for i, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i, value=col)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[chr(64 + i)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="plantilla_{tipo}.xlsx"'},
    )


@app.post("/api/bulk/cursos")
async def bulk_cursos(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_courses_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/canvas/usuarios")
async def bulk_canvas_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_canvas_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/azure/usuarios")
async def bulk_azure_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_azure_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/canvas/inscripciones")
async def bulk_canvas_inscripciones(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_canvas_enrollments_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/teams")
async def bulk_teams(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await _read_validated(file)
    try:
        report = await bulk_service.process_teams_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/reporte-excel")
async def bulk_reporte_excel(report: dict, _: dict = Depends(get_current_user)):
    try:
        excel_bytes = bulk_service.build_report_excel(report)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="reporte_carga_masiva.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Matriculación Automática
# ---------------------------------------------------------------------------

@app.post("/api/matriculacion")
async def run_matriculacion(
    background_tasks: BackgroundTasks,
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Start enrollment (OneDrive). Returns ejecucion_id immediately; poll /progress/{id}."""
    import uuid
    ejecucion_id = str(uuid.uuid4())
    matriculacion_service._init_progress_placeholder(ejecucion_id)
    background_tasks.add_task(
        matriculacion_service.run_matriculacion, dry_run=False, semestre=semestre, ejecucion_id=ejecucion_id
    )
    return JSONResponse(content={"ejecucion_id": ejecucion_id, "estado": "iniciado"})


@app.post("/api/matriculacion/dry-run")
async def dry_run_matriculacion(
    background_tasks: BackgroundTasks,
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Simulate enrollment (OneDrive). Returns ejecucion_id immediately."""
    import uuid
    ejecucion_id = str(uuid.uuid4())
    matriculacion_service._init_progress_placeholder(ejecucion_id)
    background_tasks.add_task(
        matriculacion_service.run_matriculacion, dry_run=True, semestre=semestre, ejecucion_id=ejecucion_id
    )
    return JSONResponse(content={"ejecucion_id": ejecucion_id, "estado": "iniciado"})


@app.post("/api/matriculacion/upload")
async def matriculacion_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Start enrollment from uploaded Excel. Returns ejecucion_id immediately."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")
    import uuid
    file_bytes = await _read_validated(file)
    ejecucion_id = str(uuid.uuid4())
    matriculacion_service._init_progress_placeholder(ejecucion_id)
    background_tasks.add_task(
        matriculacion_service.run_matriculacion_from_bytes,
        excel_bytes=file_bytes, dry_run=False, semestre=semestre, ejecucion_id=ejecucion_id
    )
    return JSONResponse(content={"ejecucion_id": ejecucion_id, "estado": "iniciado"})


@app.post("/api/matriculacion/upload/dry-run")
async def matriculacion_upload_dry_run(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Simulate enrollment from uploaded Excel. Returns ejecucion_id immediately."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")
    import uuid
    file_bytes = await _read_validated(file)
    ejecucion_id = str(uuid.uuid4())
    matriculacion_service._init_progress_placeholder(ejecucion_id)
    background_tasks.add_task(
        matriculacion_service.run_matriculacion_from_bytes,
        excel_bytes=file_bytes, dry_run=True, semestre=semestre, ejecucion_id=ejecucion_id
    )
    return JSONResponse(content={"ejecucion_id": ejecucion_id, "estado": "iniciado"})


@app.get("/api/matriculacion/progress/{ejecucion_id}")
async def get_matriculacion_progress(
    ejecucion_id: str,
    _: dict = Depends(get_current_user),
):
    """Poll real-time progress of a running or completed enrollment batch."""
    p = matriculacion_service.get_progress(ejecucion_id)
    if not p:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    return JSONResponse(content=p)


@app.post("/api/matriculacion/retry/{ejecucion_id}")
async def retry_matriculacion(
    ejecucion_id: str,
    semestre: str | None = None,
    _: dict = Depends(require_role("admin", "academico")),
):
    """Re-process only the students that had errors in a previous run."""
    try:
        result = await matriculacion_service.retry_failed(
            ejecucion_id=ejecucion_id,
            semestre=semestre or settings.semestre_actual or "SEM-ACTUAL",
        )
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/matriculacion/historial")
async def get_historial(
    semestre: str | None = Query(None),
    cedula: str | None = Query(None),
    limit: int = Query(500, le=2000),
    _: dict = Depends(get_current_user),
):
    try:
        rows = await audit_service.get_historial(semestre=semestre, cedula=cedula, limit=limit)
        return rows
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/matriculacion/audit/export")
async def export_audit(
    semestre: str | None = Query(None),
    cedula: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    try:
        excel_bytes = await audit_service.export_to_excel(semestre=semestre, cedula=cedula)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    fname = f"auditoria_{semestre or 'completa'}.xlsx"
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# User Management (admin only)
# ---------------------------------------------------------------------------

@app.get("/api/users")
async def list_system_users(_: dict = Depends(_require_admin)):
    try:
        return await user_service.list_users()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/users")
async def create_system_user(payload: dict, _: dict = Depends(_require_admin)):
    if not payload.get("username") or not payload.get("email") or not payload.get("password"):
        raise HTTPException(status_code=400, detail="username, email y password son requeridos")
    password_hash = auth_service.hash_password(payload["password"])
    try:
        return await user_service.create_user(
            username=payload["username"],
            email=payload["email"],
            password_hash=password_hash,
            role=payload.get("role", "viewer"),
            full_name=payload.get("full_name", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.patch("/api/users/{user_id}")
async def update_system_user(user_id: str, payload: dict, _: dict = Depends(_require_admin)):
    if "password" in payload:
        payload["password_hash"] = auth_service.hash_password(payload.pop("password"))
    try:
        updated = await user_service.update_user(user_id, payload)
        if not updated:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        updated.pop("password_hash", None)
        return updated
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/users/{user_id}")
async def delete_system_user(user_id: str, current: dict = Depends(_require_admin)):
    if current.get("sub") == user_id:
        raise HTTPException(status_code=400, detail="No podés eliminar tu propio usuario")
    try:
        await user_service.delete_user(user_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Webhook — integración con sistema académico externo
# ---------------------------------------------------------------------------

def _check_webhook_key(request: Request):
    settings = auth_service.settings
    if not settings.webhook_api_key:
        raise HTTPException(status_code=503, detail="Webhook no configurado (falta WEBHOOK_API_KEY en .env)")
    key = request.headers.get("X-API-Key", "")
    if key != settings.webhook_api_key:
        raise HTTPException(status_code=401, detail="API key inválida")


@app.post("/api/webhook/inscripciones")
async def webhook_single(payload: dict, request: Request):
    """Recibe una inscripción desde el sistema académico externo."""
    _check_webhook_key(request)
    try:
        return await webhook_service.receive_enrollment(payload, source="webhook")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/webhook/inscripciones/bulk")
async def webhook_bulk(payload: dict, request: Request):
    """Recibe múltiples inscripciones de una vez."""
    _check_webhook_key(request)
    rows = payload.get("inscripciones", [])
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="Se esperaba {\"inscripciones\": [...]}")
    try:
        return await webhook_service.receive_bulk(rows, source="webhook")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/inscripciones/pendientes")
async def list_pending_enrollments(
    semestre: str | None = Query(None),
    estado: str = Query("pendiente"),
    _: dict = Depends(_require_admin_or_academico),
):
    try:
        return await webhook_service.list_pending(semestre=semestre, estado=estado)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/inscripciones/pendientes/stats")
async def pending_stats(_: dict = Depends(_require_admin_or_academico)):
    try:
        return await webhook_service.get_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/inscripciones/pendientes/upload")
async def upload_pending(
    file: UploadFile = File(...),
    semestre: str | None = None,
    current: dict = Depends(_require_admin_or_academico),
):
    """Portal académico: carga un Excel con inscripciones a la cola pendiente."""
    _validate_file(file)
    file_bytes = await _read_validated(file)
    import io
    import pandas as pd
    buf = io.BytesIO(file_bytes)
    df = pd.read_excel(buf, dtype=str).fillna("")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rows = df.to_dict(orient="records")
    if semestre:
        for r in rows:
            if not r.get("semestre"):
                r["semestre"] = semestre
    source = f"portal:{current.get('username', 'unknown')}"
    try:
        return await webhook_service.receive_bulk(rows, source=source)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/inscripciones/pendientes/procesar")
async def process_pending(payload: dict, _: dict = Depends(_require_admin)):
    """Admin procesa inscripciones pendientes pasando sus IDs."""
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="Enviá la lista de ids a procesar")
    rows = await webhook_service.list_pending(estado="pendiente")
    to_process = [r for r in rows if r["id"] in set(ids)]
    if not to_process:
        raise HTTPException(status_code=404, detail="No hay pendientes con esos IDs")

    results = []
    for row in to_process:
        try:
            email = row.get("email", "")
            curso_id = row.get("curso_id", "")
            if email and curso_id:
                enr = await canvas_service.enroll_user(
                    course_id=curso_id,
                    user_id=email,
                    role=row.get("rol", "StudentEnrollment"),
                )
                await webhook_service.mark_processed([row["id"]], f"enrollment id={enr.get('id')}")
                results.append({"id": row["id"], "status": "ok"})
            else:
                await webhook_service.mark_error([row["id"]], "Falta email o curso_id")
                results.append({"id": row["id"], "status": "error", "detalle": "Falta email o curso_id"})
        except Exception as exc:
            await webhook_service.mark_error([row["id"]], str(exc)[:200])
            results.append({"id": row["id"], "status": "error", "detalle": str(exc)[:120]})

    ok = sum(1 for r in results if r["status"] == "ok")
    errors = sum(1 for r in results if r["status"] == "error")
    return {"total": len(results), "ok": ok, "errors": errors, "rows": results}


@app.delete("/api/inscripciones/pendientes")
async def delete_pending_enrollments(payload: dict, _: dict = Depends(_require_admin)):
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="Enviá la lista de ids")
    await webhook_service.delete_pending(ids)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Course Aliases (fuzzy match management)
# ---------------------------------------------------------------------------

@app.get("/api/course-aliases")
async def get_course_aliases(estado: str | None = Query(None), _: dict = Depends(_require_admin)):
    try:
        return await course_matcher.list_aliases(estado=estado)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/course-aliases/unresolved")
async def get_unresolved_courses(_: dict = Depends(_require_admin)):
    try:
        return await course_matcher.list_unresolved()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/course-aliases/{alias_id}/resolve")
async def resolve_course_alias(alias_id: int, payload: dict, current: dict = Depends(_require_admin)):
    try:
        await course_matcher.resolve_alias(
            alias_id=alias_id,
            canvas_sis_id=payload["canvas_sis_id"],
            canvas_name=payload.get("canvas_name", ""),
            resolved_by=current.get("username", "admin"),
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/course-aliases/manual")
async def add_manual_alias(payload: dict, current: dict = Depends(_require_admin)):
    """Manually register: variant → canvas_sis_id."""
    try:
        await course_matcher.save_alias(
            variant=payload["variant"],
            canvas_sis_id=payload["canvas_sis_id"],
            canvas_name=payload.get("canvas_name", ""),
            score=100.0,
            estado="confirmado",
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
async def get_dashboard(_: dict = Depends(get_current_user)):
    try:
        kpis = await audit_service.get_dashboard_kpis()
        kpis["proxima_ejecucion"] = get_next_run()
        return kpis
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Búsqueda de alumnos (Canvas + Azure AD)
# ---------------------------------------------------------------------------

@app.get("/api/alumnos/buscar")
async def buscar_alumno(q: str = Query(..., min_length=2), _: dict = Depends(get_current_user)):
    """Busca un alumno por cédula o nombre en Canvas y Azure AD en paralelo."""
    import asyncio

    errores = []

    async def _canvas():
        try:
            if not auth_service.settings.canvas_base_url:
                errores.append("Canvas: CANVAS_BASE_URL no configurado en el servidor")
                return []
            if q.isdigit():
                u = await canvas_service.find_user_by_sis_id(q)
                if u:
                    return [{"fuente": "canvas", "id": u.get("id"), "nombre": u.get("name"),
                             "email": u.get("email"), "sis_id": q, "canvas_id": u.get("id")}]
            results = await canvas_service.search_users(q)
            return [{"fuente": "canvas", "id": u.get("id"), "nombre": u.get("name"),
                     "email": u.get("email"), "sis_id": u.get("sis_user_id"), "canvas_id": u.get("id")}
                    for u in results]
        except Exception as e:
            errores.append(f"Canvas: {str(e)[:120]}")
            return []

    async def _azure():
        try:
            if not auth_service.settings.azure_client_id:
                errores.append("Azure AD: AZURE_CLIENT_ID no configurado en el servidor")
                return []
            results = await graph_service.search_users(q)
            return [{"fuente": "azure", "id": u.get("id"), "nombre": u.get("displayName"),
                     "email": u.get("mail") or u.get("userPrincipalName"),
                     "upn": u.get("userPrincipalName"), "activo": u.get("accountEnabled", True)}
                    for u in results]
        except Exception as e:
            errores.append(f"Azure AD: {str(e)[:120]}")
            return []

    canvas_res, azure_res = await asyncio.gather(_canvas(), _azure())

    merged: dict[str, dict] = {}
    for u in canvas_res:
        key = (u.get("email") or u.get("sis_id") or "").lower()
        merged[key] = {**u, "en_canvas": True, "en_azure": False}
    for u in azure_res:
        key = (u.get("email") or "").lower()
        if key in merged:
            merged[key].update({"en_azure": True, "upn": u.get("upn"), "azure_id": u.get("id"), "activo": u.get("activo")})
        else:
            merged[key] = {**u, "en_canvas": False, "en_azure": True}

    # Si Canvas y Azure no encontraron nada, buscar en BD local (historial_academico)
    if not merged:
        try:
            import academic_service as _ac
            local = await _ac.buscar_alumno(q)
            for u in local:
                key = (u.get("cedula") or "").lower()
                merged[key] = {
                    "fuente": "local",
                    "nombre": u.get("nombre"),
                    "email": u.get("email", ""),
                    "sis_id": u.get("cedula"),
                    "cedula": u.get("cedula"),
                    "carrera": u.get("carrera"),
                    "programa": u.get("programa"),
                    "en_canvas": False,
                    "en_azure": False,
                }
        except Exception as e:
            errores.append(f"BD local: {str(e)[:120]}")

    return {"resultados": list(merged.values()), "errores": errores}


@app.get("/api/canvas/courses/activos")
async def list_courses_activos(programa: str | None = None, _: dict = Depends(get_current_user)):
    """Cursos activos de Canvas, opcionalmente filtrados por programa."""
    if not get_settings().canvas_base_url:
        raise HTTPException(status_code=503, detail="Canvas no configurado")
    try:
        courses = await canvas_service.get_courses(per_page=200)
        result = []
        for c in courses:
            if c.get("workflow_state") == "deleted":
                continue
            name = c.get("name", "")
            sis_id = c.get("sis_course_id", "") or ""
            code = c.get("course_code", "") or ""
            # Filtrar por programa si se especificó
            if programa:
                searchable = (name + sis_id + code).upper()
                if programa.upper() not in searchable:
                    continue
            result.append({"id": c.get("id"), "name": name, "sis_id": sis_id, "code": code})
        return sorted(result, key=lambda x: x["name"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error Canvas: {str(exc)}")


# ---------------------------------------------------------------------------
# Parseo de planilla académica
# ---------------------------------------------------------------------------

@app.post("/api/parseo/planilla")
async def parsear_planilla(
    file: UploadFile = File(...),
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")
    file_bytes = await _read_validated(file)
    try:
        result = parseo_service.parsear_planilla(file_bytes, semestre=semestre)
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al parsear: {exc}")


@app.post("/api/parseo/enviar-cola")
async def enviar_parseo_cola(payload: dict, current: dict = Depends(get_current_user)):
    alumnos = payload.get("alumnos", [])
    if not alumnos:
        raise HTTPException(status_code=400, detail="No hay alumnos para enviar")
    source = f"parseo:{current.get('username', 'unknown')}"
    enviados = 0
    for a in alumnos:
        for curso in a.get("cursos", []):
            row = {
                "cedula":   a.get("cedula", ""),
                "nombre":   a.get("nombre", ""),
                "programa": a.get("programa", ""),
                "semestre": a.get("periodo", ""),
                "curso_nombre": curso.get("nombre_original", curso.get("nombre", "")),
                "dia":       curso.get("dia", ""),
                "hora_inicio": curso.get("hora_inicio", ""),
                "hora_fin":  curso.get("hora_fin", ""),
                "programa":  a.get("programa", ""),
                "carrera":   a.get("carrera", ""),
                "source":   source,
            }
            try:
                await webhook_service.receive_enrollment(row, source=source)
                enviados += 1
            except Exception:
                pass
    return {"enviados": enviados}


@app.post("/api/parseo/exportar")
async def exportar_parseo(payload: dict, _: dict = Depends(get_current_user)):
    alumnos = payload.get("alumnos", [])
    try:
        excel_bytes = parseo_service.exportar_excel(alumnos)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="parseo_planilla.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Portal académico — mis inscripciones y formulario de inscripción
# ---------------------------------------------------------------------------

@app.get("/api/portal/mis-inscripciones")
async def mis_inscripciones(
    semestre: str | None = None,
    estado: str | None = None,
    current: dict = Depends(_require_admin_or_academico),
):
    """Devuelve los registros de pending_enrollments generados por el usuario actual."""
    import db as _db

    username = current.get("username", "")
    if current.get("role") == "admin":
        source_filter = "%parseo:%"
    else:
        source_filter = f"parseo:{username}"

    conditions = ["source LIKE ?"]
    params: list = [source_filter]
    if semestre:
        conditions.append("semestre = ?")
        params.append(semestre)
    if estado:
        conditions.append("estado = ?")
        params.append(estado)

    where = "WHERE " + " AND ".join(conditions)
    rows = await _db.fetch(
        f"SELECT * FROM pending_enrollments {where} ORDER BY received_at DESC LIMIT 2000",
        *params,
    )

    # Agrupa por alumno (cedula + semestre)
    agrupado: dict = {}
    for r in rows:
        key = (r.get("cedula", ""), r.get("semestre", ""))
        if key not in agrupado:
            agrupado[key] = {
                "cedula":      r.get("cedula", ""),
                "nombre":      r.get("nombre", ""),
                "email":       r.get("email", ""),
                "semestre":    r.get("semestre", ""),
                "programa":    r.get("programa", ""),
                "carrera":     r.get("carrera", ""),
                "source":      r.get("source", ""),
                "received_at": r.get("received_at", ""),
                "ids":         [],
                "cursos":      [],
                "estados":     [],
            }
        agrupado[key]["ids"].append(r.get("id", ""))
        agrupado[key]["cursos"].append({
            "nombre": r.get("curso_nombre") or r.get("detalle") or "",
            "dia": r.get("dia", ""),
            "hora_inicio": r.get("hora_inicio", ""),
            "hora_fin": r.get("hora_fin", ""),
        })
        agrupado[key]["estados"].append(r.get("estado", ""))

    alumnos = list(agrupado.values())
    for a in alumnos:
        estados = set(a["estados"])
        if "error" in estados:
            a["estado_general"] = "error"
        elif "pendiente" in estados:
            a["estado_general"] = "pendiente"
        else:
            a["estado_general"] = "procesado"

    return {"alumnos": alumnos, "total": len(alumnos)}


@app.delete("/api/portal/mis-inscripciones")
async def portal_delete_inscripciones(payload: dict, current: dict = Depends(_require_admin_or_academico)):
    """Elimina inscripciones propias (solo las generadas por el usuario actual)."""
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="Enviá la lista de ids")
    source = f"parseo:{current.get('username', '')}"
    import db as _db
    placeholders = ",".join("?" * len(ids))
    await _db.execute(
        f"DELETE FROM pending_enrollments WHERE id IN ({placeholders}) AND source = ?",
        *ids, source,
    )
    return {"ok": True}


@app.get("/api/portal/formulario/{cedula}")
async def formulario_inscripcion(
    cedula: str,
    semestre: str | None = None,
    _: dict = Depends(_require_admin_or_academico),
):
    """Genera HTML imprimible con el formulario de inscripción de un alumno."""
    import db as _db

    conditions = ["cedula = ?"]
    params: list = [cedula]
    if semestre:
        conditions.append("semestre = ?")
        params.append(semestre)
    where = "WHERE " + " AND ".join(conditions)
    rows = await _db.fetch(
        f"SELECT * FROM pending_enrollments {where} ORDER BY received_at DESC", *params
    )

    if not rows:
        raise HTTPException(status_code=404, detail="No se encontraron inscripciones para esta cédula")

    first = rows[0]
    nombre   = first.get("nombre", "")
    email    = first.get("email", "")
    sem      = first.get("semestre", semestre or "")
    source   = first.get("source", "")
    academico = source.replace("parseo:", "") if "parseo:" in source else source
    fecha    = (first.get("received_at") or "")[:10]

    from html import escape as _he
    cursos_html = "".join(
        f"<tr><td>{i+1}</td><td>{_he(r.get('curso_nombre') or '')}</td><td>{_he(r.get('rol','StudentEnrollment').replace('Enrollment',''))}</td><td>{_he(r.get('estado',''))}</td></tr>"
        for i, r in enumerate(rows)
    )

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Formulario de Inscripción — {_he(nombre)}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; }}
  h1 {{ color: #1a3c6b; border-bottom: 2px solid #1a3c6b; padding-bottom: 8px; }}
  .logo {{ font-size: 22px; font-weight: bold; color: #1a3c6b; }}
  .subtitulo {{ font-size: 13px; color: #555; margin-bottom: 20px; }}
  table.info {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
  table.info td {{ padding: 6px 10px; border: 1px solid #ccc; }}
  table.info td:first-child {{ font-weight: bold; width: 180px; background: #f0f4fa; }}
  table.cursos {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  table.cursos th {{ background: #1a3c6b; color: #fff; padding: 8px; text-align: left; }}
  table.cursos td {{ padding: 7px 10px; border: 1px solid #ddd; }}
  table.cursos tr:nth-child(even) {{ background: #f7f9fc; }}
  .firma {{ margin-top: 60px; display: flex; gap: 80px; }}
  .firma div {{ border-top: 1px solid #333; padding-top: 6px; text-align: center; min-width: 200px; }}
  @media print {{ .no-print {{ display: none; }} }}
  .btn-print {{ margin: 20px 0; padding: 10px 24px; background: #1a3c6b; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 15px; }}
</style>
</head>
<body>
<div class="logo">USIL Paraguay</div>
<div class="subtitulo">Universidad San Ignacio de Loyola</div>
<h1>Formulario de Inscripción</h1>
<button class="btn-print no-print" onclick="window.print()">🖨️ Imprimir</button>
<table class="info">
  <tr><td>Nombre completo</td><td>{_he(nombre)}</td></tr>
  <tr><td>Cédula de identidad</td><td>{_he(cedula)}</td></tr>
  <tr><td>Correo electrónico</td><td>{_he(email) if email else '—'}</td></tr>
  <tr><td>Período / Semestre</td><td>{_he(sem)}</td></tr>
  <tr><td>Fecha de inscripción</td><td>{_he(fecha)}</td></tr>
  <tr><td>Registrado por</td><td>{_he(academico)}</td></tr>
</table>
<h2 style="color:#1a3c6b;font-size:16px;">Cursos inscriptos</h2>
<table class="cursos">
  <thead><tr><th>#</th><th>Curso</th><th>Rol</th><th>Estado</th></tr></thead>
  <tbody>{cursos_html}</tbody>
</table>
<div class="firma">
  <div>Firma del alumno</div>
  <div>Sello / Firma académica</div>
</div>
</body>
</html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Gestión masiva endpoints
# ---------------------------------------------------------------------------

@app.get("/api/gestion/plantilla-cursos")
async def plantilla_cursos(_user=Depends(_require_admin)):
    from fastapi.responses import Response
    import bulk_service as _bulk
    excel_bytes = _bulk.build_plantilla_cursos()
    return Response(content=excel_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=plantilla_crear_cursos.xlsx"})


@app.get("/api/gestion/plantilla-canvas")
async def plantilla_canvas(_user=Depends(_require_admin)):
    from fastapi.responses import Response
    import bulk_service as _bulk
    excel_bytes = _bulk.build_plantilla_canvas()
    return Response(content=excel_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=plantilla_inscripciones_canvas.xlsx"})


@app.get("/api/gestion/plantilla-teams")
async def plantilla_teams(_user=Depends(_require_admin)):
    from fastapi.responses import Response
    import bulk_service as _bulk
    excel_bytes = _bulk.build_plantilla_teams()
    return Response(content=excel_bytes,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=plantilla_inscripciones_teams.xlsx"})


@app.post("/api/gestion/crear-cursos")
async def gestion_crear_cursos(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Upload Excel with materias → creates Canvas courses + Teams teams → returns Excel with IDs"""
    import bulk_service as _bulk
    data = await _read_validated(file)
    excel_bytes = await _bulk.process_cursos_ids(data, file.filename)
    from fastapi.responses import Response
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=cursos_ids.xlsx"}
    )


@app.post("/api/gestion/matricular")
async def gestion_matricular(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Upload planilla Excel → create users + enroll in Canvas + Teams + send emails"""
    import bulk_service as _bulk
    data = await _read_validated(file)
    summary, excel_bytes = await _bulk.process_matriculacion_planilla(data, file.filename)
    import base64
    summary["excel_b64"] = base64.b64encode(excel_bytes).decode()
    return summary


# ── Académico: historial y correlativas ───────────────────────────────────────

@app.post("/api/academico/importar-historial")
async def importar_historial(file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks(), _user=Depends(_require_admin)):
    """Importa historial de notas GND desde Excel en background."""
    import academic_service as _ac
    import uuid as _uuid
    import asyncio as _asyncio
    # Read raw bytes fast — no pandas here
    raw = await file.read()
    fname = file.filename
    job_id = str(_uuid.uuid4())[:8]
    _import_jobs[job_id] = {"status": "running", "result": None}

    async def _run():
        try:
            result = await _asyncio.get_event_loop().run_in_executor(
                None, lambda: _ac._importar_historial_sync(raw, fname)
            )
            # DB insert is async — run after sync parsing
            result2 = await _ac.importar_historial_gnd_rows(result["rows"], result["carrera_map"])
            _import_jobs[job_id] = {"status": "done", "result": result2}
        except Exception as e:
            _import_jobs[job_id] = {"status": "error", "result": {"errores": [str(e)]}}

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@app.post("/api/academico/importar-mallas")
async def importar_mallas(file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks(), _user=Depends(_require_admin)):
    """Importa mallas curriculares (correlativas) GND desde Excel en background."""
    import academic_service as _ac
    import uuid as _uuid
    import asyncio as _asyncio
    raw = await file.read()
    fname = file.filename
    job_id = str(_uuid.uuid4())[:8]
    _import_jobs[job_id] = {"status": "running", "result": None}

    async def _run():
        try:
            rows = await _asyncio.get_event_loop().run_in_executor(
                None, lambda: _ac._parsear_mallas_sync(raw, fname)
            )
            result = await _ac.importar_mallas_rows(rows)
            _import_jobs[job_id] = {"status": "done", "result": result}
        except Exception as e:
            _import_jobs[job_id] = {"status": "error", "result": {"errores": [str(e)]}}

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/academico/import-status/{job_id}")
async def import_status(job_id: str, _user=Depends(_require_admin)):
    job = _import_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job


@app.get("/api/academico/alumno/{cedula}")
async def historial_alumno(cedula: str, _user=Depends(get_current_user)):
    """Retorna historial académico completo de un alumno."""
    import academic_service as _ac
    return await _ac.historial_alumno(cedula)


@app.get("/api/academico/materias")
async def get_materias(programa: str, carrera: str, _user=Depends(get_current_user)):
    """Lista de materias únicas de una carrera/programa desde historial_academico."""
    import academic_service as _ac
    return await _ac.get_materias_por_carrera(programa, carrera)


@app.get("/api/academico/alumno/{cedula}/inscripcion")
async def estado_inscripcion(cedula: str, _user=Depends(get_current_user)):
    """Estado de inscripción: qué materias puede/no puede inscribir según correlativas."""
    import academic_service as _ac
    return await _ac.estado_inscripcion(cedula)


@app.get("/api/academico/buscar")
async def buscar_alumno(q: str, _user=Depends(get_current_user)):
    """Busca alumnos por cédula o nombre."""
    import academic_service as _ac
    return await _ac.buscar_alumno(q)


@app.get("/api/admin/tablas-academicas")
async def tablas_academicas(_user=Depends(_require_admin)):
    """Lista las tablas públicas de BD con cantidad de filas (diagnóstico)."""
    import db
    rows = await db.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
    )
    result = {}
    for r in rows:
        tname = r["table_name"]
        try:
            cnt = await db.fetchval(f'SELECT COUNT(*) FROM "{tname}"')
        except Exception:
            cnt = -1
        result[tname] = cnt
    return result


@app.post("/api/admin/limpiar-datos-academicos")
async def limpiar_datos_academicos(_user=Depends(_require_admin)):
    """Elimina TODOS los registros de historial/historico y correlativas."""
    import db
    # Intentar los nombres posibles de la tabla historial
    HISTORIAL_CANDIDATES = [
        "historial_academico",
        "histórico_académico",
        "historico_academico",
        "historial_academico_gnd",
    ]

    n_h = 0
    deleted_tables = []
    errors = []
    for tname in HISTORIAL_CANDIDATES:
        try:
            cnt = await db.fetchval(f'SELECT COUNT(*) FROM "{tname}"')
            await db.execute(f'DELETE FROM "{tname}"')
            n_h += cnt
            deleted_tables.append(tname)
        except Exception as e:
            err = str(e)
            if "does not exist" not in err and "relation" not in err.lower():
                errors.append(f"{tname}: {err}")

    n_c = 0
    try:
        n_c = await db.fetchval("SELECT COUNT(*) FROM correlativas")
        await db.execute("DELETE FROM correlativas")
    except Exception:
        pass

    return {
        "eliminados_historial": n_h,
        "eliminados_correlativas": n_c,
        "tablas_limpiadas": deleted_tables,
        "errores": errors,
    }


@app.get("/api/admin/mallas/comparar")
async def comparar_mallas(_user=Depends(_require_admin)):
    """Compara nombres de materias entre malla (correlativas) e historial_academico."""
    import academic_service as _ac
    return await _ac.comparar_mallas_historial()


@app.post("/api/admin/mallas/reparar")
async def reparar_mallas(_user=Depends(_require_admin)):
    """Actualiza nombres de materias en historial_academico para que coincidan con la malla."""
    import academic_service as _ac
    return await _ac.reparar_nombres_historial()


@app.get("/api/academico/validar-correlativas")
async def validar_correlativas(
    cedula: str,
    materia: str,
    carrera: str,
    programa: str = "GND",
    _user=Depends(get_current_user),
):
    """Verifica si el alumno puede inscribirse a la materia (correlativas)."""
    import academic_service as _ac
    return await _ac.validar_correlativas(cedula, materia, carrera, programa)


@app.post("/api/gestion/matricular-alumnos")
async def gestion_matricular_alumnos(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Excel: cedula, nombre, email, materia, periodo → matricula en Canvas + Teams + email"""
    import bulk_service as _bulk
    from fastapi.responses import Response
    data = await _read_validated(file)
    excel_bytes = await _bulk.process_matricular_sheet(data, file.filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=matriculacion_resultado.xlsx"}
    )


@app.get("/api/gestion/plantilla-matricular")
async def plantilla_matricular(_user=Depends(_require_admin)):
    import bulk_service as _bulk
    from fastapi.responses import Response
    return Response(
        content=_bulk.build_plantilla_matricular(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=plantilla_matricular.xlsx"}
    )


@app.post("/api/gestion/inscribir-canvas")
async def gestion_inscribir_canvas(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Upload Excel SIS User ID|Course ID|Rol → inscribe en Canvas → returns Excel con Resultado"""
    import bulk_service as _bulk
    from fastapi.responses import Response
    data = await _read_validated(file)
    excel_bytes = await _bulk.process_canvas_enrollment_file(data, file.filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=resultado_canvas.xlsx"}
    )


@app.post("/api/gestion/inscribir-teams")
async def gestion_inscribir_teams(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Upload Excel Correo|Group ID → agrega a equipos Teams → returns Excel con Resultado"""
    import bulk_service as _bulk
    from fastapi.responses import Response
    data = await _read_validated(file)
    excel_bytes = await _bulk.process_teams_enrollment_file(data, file.filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=resultado_teams.xlsx"}
    )


@app.post("/api/sync/canvas")
async def sync_canvas(background_tasks: BackgroundTasks, _user=Depends(_require_admin)):
    """Trigger manual Canvas sync en background — retorna job_id para polling."""
    import sync_service
    import uuid as _uuid
    job_id = str(_uuid.uuid4())[:8]
    _import_jobs[job_id] = {"status": "running", "result": None}

    async def _run():
        try:
            await sync_service.init_db()
            result = await sync_service.run_sync("manual")
            _import_jobs[job_id] = {"status": "done", "result": result}
        except Exception as exc:
            _import_jobs[job_id] = {"status": "done", "result": {"error": str(exc)}}

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/sync/canvas/status/{job_id}")
async def sync_canvas_status(job_id: str, _user=Depends(_require_admin)):
    job = _import_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job


@app.get("/api/sync/estado")
async def sync_estado(_user=Depends(_require_admin)):
    """Last sync log + summary counts."""
    import db as _db
    import sync_service
    await sync_service.init_db()
    last = await _db.fetchrow(
        "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
    )
    totals = await _db.fetchrow("""
        SELECT
            (SELECT COUNT(*) FROM sync_cursos)          AS total_cursos,
            (SELECT COUNT(*) FROM sync_alumnos)         AS total_alumnos,
            (SELECT COUNT(*) FROM sync_matriculaciones) AS total_matriculaciones,
            (SELECT COUNT(*) FROM sync_calificaciones)  AS total_calificaciones,
            (SELECT COUNT(*) FROM sync_asistencias)     AS total_asistencias
    """)
    return {"ultima_sync": dict(last) if last else None, "totales": dict(totals) if totals else {}}


@app.get("/api/sync/alumnos")
async def sync_get_alumnos(q: str = "", limit: int = 50, _user=Depends(_require_admin)):
    import db as _db
    import sync_service
    await sync_service.init_db()
    if q:
        rows = await _db.fetch(
            "SELECT * FROM sync_alumnos WHERE nombre ILIKE ? OR email ILIKE ? OR sis_user_id ILIKE ? LIMIT ?",
            f"%{q}%", f"%{q}%", f"%{q}%", limit
        )
    else:
        rows = await _db.fetch("SELECT * FROM sync_alumnos ORDER BY nombre LIMIT ?", limit)
    return rows


@app.get("/api/sync/alumno/{canvas_user_id}")
async def sync_get_alumno(canvas_user_id: int, _user=Depends(_require_admin)):
    import db as _db
    import sync_service
    await sync_service.init_db()
    alumno = await _db.fetchrow("SELECT * FROM sync_alumnos WHERE canvas_user_id = ?", canvas_user_id)
    if not alumno:
        raise HTTPException(status_code=404, detail="Alumno no encontrado")
    cursos = await _db.fetch("""
        SELECT c.canvas_course_id, c.nombre, c.semestre, m.estado,
               cal.nota_actual, cal.nota_final, cal.letra_actual,
               (SELECT COUNT(*) FROM sync_asistencias a
                WHERE a.canvas_course_id = c.canvas_course_id AND a.canvas_user_id = ? AND a.estado = 'present') AS presentes,
               (SELECT COUNT(*) FROM sync_asistencias a
                WHERE a.canvas_course_id = c.canvas_course_id AND a.canvas_user_id = ?) AS total_clases
        FROM sync_matriculaciones m
        JOIN sync_cursos c ON c.canvas_course_id = m.canvas_course_id
        LEFT JOIN sync_calificaciones cal ON cal.canvas_course_id = m.canvas_course_id AND cal.canvas_user_id = m.canvas_user_id
        WHERE m.canvas_user_id = ?
        ORDER BY c.semestre DESC, c.nombre
    """, canvas_user_id, canvas_user_id, canvas_user_id)
    return {"alumno": dict(alumno), "cursos": cursos}


@app.get("/api/sync/calificaciones/{canvas_course_id}")
async def sync_get_calificaciones(canvas_course_id: int, _user=Depends(_require_admin)):
    import db as _db
    import sync_service
    await sync_service.init_db()
    rows = await _db.fetch("""
        SELECT a.nombre, a.email, a.sis_user_id, c.nota_actual, c.nota_final, c.letra_actual, c.letra_final, c.ultima_sync
        FROM sync_calificaciones c
        JOIN sync_alumnos a ON a.canvas_user_id = c.canvas_user_id
        WHERE c.canvas_course_id = ?
        ORDER BY a.nombre
    """, canvas_course_id)
    return rows


@app.get("/api/sync/asistencias/{canvas_course_id}")
async def sync_get_asistencias(canvas_course_id: int, _user=Depends(_require_admin)):
    import db as _db
    import sync_service
    await sync_service.init_db()
    rows = await _db.fetch("""
        SELECT a.nombre, a.email, a.sis_user_id, s.fecha_clase, s.estado
        FROM sync_asistencias s
        JOIN sync_alumnos a ON a.canvas_user_id = s.canvas_user_id
        WHERE s.canvas_course_id = ?
        ORDER BY s.fecha_clase DESC, a.nombre
    """, canvas_course_id)
    return rows


@app.get("/api/gestion/cron")
async def gestion_get_cron(_user=Depends(_require_admin)):
    from scheduler import get_next_run, scheduler
    job = scheduler.get_job("matriculacion_diaria")
    trigger_info = ""
    if job and job.trigger:
        trigger_info = str(job.trigger)
    return {
        "cron_hora": settings.cron_hora,
        "next_run": get_next_run(),
        "trigger": trigger_info,
    }


@app.patch("/api/gestion/cron")
async def gestion_update_cron(body: dict, _user=Depends(_require_admin)):
    """Update cron schedule. Body: {"cron_hora": "HH:MM"}"""
    from scheduler import scheduler
    from apscheduler.triggers.cron import CronTrigger
    cron_hora = body.get("cron_hora", "07:00")
    try:
        hora, minuto = cron_hora.split(":")
        hora_int, minuto_int = int(hora), int(minuto)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Formato inválido. Use HH:MM")
    trigger = CronTrigger(hour=hora_int, minute=minuto_int, timezone="UTC")
    scheduler.reschedule_job("matriculacion_diaria", trigger=trigger)
    settings.cron_hora = cron_hora
    from scheduler import get_next_run
    return {"ok": True, "cron_hora": cron_hora, "next_run": get_next_run()}
