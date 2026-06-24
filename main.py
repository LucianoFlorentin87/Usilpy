import io
import os

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    except JWTError:
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


@app.get("/api/diagnostico")
async def diagnostico(_: dict = Depends(get_current_user)):
    """Diagnóstico de variables de entorno (sin exponer valores sensibles)."""
    from config import get_settings
    s = get_settings()
    return {
        "canvas_base_url":    s.canvas_base_url or "(vacío)",
        "canvas_api_token":   "✓ configurado" if s.canvas_api_token else "(vacío)",
        "azure_tenant_id":    "✓ configurado" if s.azure_tenant_id else "(vacío)",
        "azure_client_id":    "✓ configurado" if s.azure_client_id else "(vacío)",
        "azure_client_secret":"✓ configurado" if s.azure_client_secret else "(vacío)",
        "admin_username":     s.admin_username or "(vacío)",
        "semestre_actual":    s.semestre_actual,
    }


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest):
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


@app.get("/api/auth/azure/login")
async def azure_login():
    if not auth_service.settings.azure_client_id:
        raise HTTPException(status_code=503, detail="Azure AD no configurado")
    url = auth_service.build_azure_login_url()
    return RedirectResponse(url)


@app.get("/api/auth/azure/callback")
async def azure_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/?auth_error={error}")
    try:
        user = await auth_service.exchange_azure_code(code, state)
    except Exception as exc:
        return RedirectResponse(f"/?auth_error={str(exc)[:60]}")
    token = auth_service.create_access_token(user)
    return RedirectResponse(f"/#token={token}")


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
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/canvas/terms")
async def create_term(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_or_create_term(payload["name"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/canvas/courses")
async def list_courses(_: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_courses()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/canvas/users")
async def list_canvas_users(_: dict = Depends(get_current_user)):
    try:
        return await canvas_service.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/canvas/users")
async def create_canvas_user(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.create_user(
            name=payload["nombre"],
            email=payload["email"],
            sis_id=payload.get("sis_id", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/canvas/courses/{course_id}/enrollments")
async def enroll(course_id: str, payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await canvas_service.enroll_user(
            course_id=course_id,
            user_id=payload["user_id"],
            role=payload.get("role", "StudentEnrollment"),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Azure AD / Microsoft Graph
# ---------------------------------------------------------------------------

@app.get("/api/azure/users")
async def list_azure_users(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/azure/groups")
async def list_groups(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_groups()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/azure/groups")
async def create_group(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await graph_service.create_group(
            display_name=payload["display_name"],
            description=payload.get("description", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------

@app.get("/api/teams")
async def list_teams(_: dict = Depends(get_current_user)):
    try:
        return await graph_service.get_teams()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/teams")
async def create_team_endpoint(payload: dict, _: dict = Depends(get_current_user)):
    try:
        return await graph_service.create_team(
            display_name=payload["display_name"],
            description=payload.get("description", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Bulk / Carga Masiva
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
TEMPLATE_PATH = "plantilla_carga_masiva.xlsx"


def _validate_file(file: UploadFile) -> None:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado '{ext}'. Use .xlsx, .xls o .csv",
        )


@app.post("/api/bulk/usuarios")
async def bulk_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/inscripciones")
async def bulk_inscripciones(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_enrollments_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.get("/api/bulk/template")
async def bulk_template():
    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(status_code=404, detail="Plantilla no encontrada en el servidor")
    return FileResponse(
        path=TEMPLATE_PATH,
        filename="plantilla_carga_masiva.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/bulk/template/{tipo}")
async def bulk_template_tipo(tipo: str):
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
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_courses_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/canvas/usuarios")
async def bulk_canvas_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_canvas_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/azure/usuarios")
async def bulk_azure_usuarios(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_azure_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/canvas/inscripciones")
async def bulk_canvas_inscripciones(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_canvas_enrollments_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/teams")
async def bulk_teams(file: UploadFile = File(...), _: dict = Depends(get_current_user)):
    _validate_file(file)
    file_bytes = await file.read()
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
    file_bytes = await file.read()
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
    file_bytes = await file.read()
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
    file_bytes = await file.read()
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
            if not canvas_service.settings.canvas_base_url:
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
            if not canvas_service.settings.azure_client_id:
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
    file_bytes = await file.read()
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

    cursos_html = "".join(
        f"<tr><td>{i+1}</td><td>{r.get('curso_nombre') or ''}</td><td>{r.get('rol','StudentEnrollment').replace('Enrollment','')}</td><td>{r.get('estado','')}</td></tr>"
        for i, r in enumerate(rows)
    )

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Formulario de Inscripción — {nombre}</title>
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
  <tr><td>Nombre completo</td><td>{nombre}</td></tr>
  <tr><td>Cédula de identidad</td><td>{cedula}</td></tr>
  <tr><td>Correo electrónico</td><td>{email or '—'}</td></tr>
  <tr><td>Período / Semestre</td><td>{sem}</td></tr>
  <tr><td>Fecha de inscripción</td><td>{fecha}</td></tr>
  <tr><td>Registrado por</td><td>{academico}</td></tr>
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
    data = await file.read()
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
    data = await file.read()
    summary, excel_bytes = await _bulk.process_matriculacion_planilla(data, file.filename)
    import base64
    summary["excel_b64"] = base64.b64encode(excel_bytes).decode()
    return summary


@app.post("/api/gestion/inscribir-canvas")
async def gestion_inscribir_canvas(file: UploadFile = File(...), _user=Depends(_require_admin)):
    """Upload Excel SIS User ID|Course ID|Rol → inscribe en Canvas → returns Excel con Resultado"""
    import bulk_service as _bulk
    from fastapi.responses import Response
    data = await file.read()
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
    data = await file.read()
    excel_bytes = await _bulk.process_teams_enrollment_file(data, file.filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=resultado_teams.xlsx"}
    )


@app.post("/api/sync/canvas")
async def sync_canvas(_user=Depends(_require_admin)):
    """Trigger manual Canvas sync — cursos, alumnos, matriculas, notas, asistencias."""
    import sync_service
    await sync_service.init_db()
    result = await sync_service.run_sync("manual")
    return result


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
