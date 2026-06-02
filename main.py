import io
import os

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import canvas_service
import graph_service
import bulk_service

app = FastAPI(title="Gestión Académica Universitaria", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

@app.get("/api/canvas/courses")
async def list_courses():
    try:
        return await canvas_service.get_courses()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/canvas/users")
async def list_canvas_users():
    try:
        return await canvas_service.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/canvas/users")
async def create_canvas_user(payload: dict):
    try:
        return await canvas_service.create_user(
            name=payload["nombre"],
            email=payload["email"],
            sis_id=payload.get("sis_id", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/canvas/courses/{course_id}/enrollments")
async def enroll(course_id: str, payload: dict):
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
async def list_azure_users():
    try:
        return await graph_service.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/azure/users")
async def create_azure_user(payload: dict):
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
async def list_groups():
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
async def bulk_usuarios(file: UploadFile = File(...)):
    """Procesa una hoja Excel/CSV con datos de usuarios (Canvas + Azure + Teams)."""
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_users_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.post("/api/bulk/inscripciones")
async def bulk_inscripciones(file: UploadFile = File(...)):
    """Procesa una hoja Excel/CSV con inscripciones a cursos (Canvas + Azure + Teams)."""
    _validate_file(file)
    file_bytes = await file.read()
    try:
        report = await bulk_service.process_enrollments_sheet(file_bytes, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {exc}")
    return JSONResponse(content=report)


@app.get("/api/bulk/template")
async def bulk_template():
    """Descarga la plantilla Excel vacía para carga masiva."""
    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(status_code=404, detail="Plantilla no encontrada en el servidor")
    return FileResponse(
        path=TEMPLATE_PATH,
        filename="plantilla_carga_masiva.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/bulk/reporte-excel")
async def bulk_reporte_excel(report: dict):
    """Genera un Excel coloreado a partir del reporte JSON devuelto por /bulk/usuarios o /bulk/inscripciones."""
    try:
        excel_bytes = bulk_service.build_report_excel(report)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return StreamingResponse(
        io.BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="reporte_carga_masiva.xlsx"'},
    )
