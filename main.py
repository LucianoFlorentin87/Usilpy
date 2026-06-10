import io
import os

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Query
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


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    user = auth_service.authenticate_local(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = auth_service.create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "user": user}


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
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Execute the full enrollment process (real mode) using OneDrive."""
    try:
        result = await matriculacion_service.run_matriculacion(dry_run=False, semestre=semestre)
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/matriculacion/dry-run")
async def dry_run_matriculacion(
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Simulate the enrollment process without making any changes (OneDrive)."""
    try:
        result = await matriculacion_service.run_matriculacion(dry_run=True, semestre=semestre)
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/matriculacion/upload")
async def matriculacion_upload(
    file: UploadFile = File(...),
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Execute enrollment using an uploaded Excel file (no OneDrive required)."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")
    file_bytes = await file.read()
    try:
        result = await matriculacion_service.run_matriculacion_from_bytes(
            excel_bytes=file_bytes, dry_run=False, semestre=semestre
        )
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/matriculacion/upload/dry-run")
async def matriculacion_upload_dry_run(
    file: UploadFile = File(...),
    semestre: str | None = None,
    _: dict = Depends(get_current_user),
):
    """Simulate enrollment using an uploaded Excel file."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")
    file_bytes = await file.read()
    try:
        result = await matriculacion_service.run_matriculacion_from_bytes(
            excel_bytes=file_bytes, dry_run=True, semestre=semestre
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
