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
