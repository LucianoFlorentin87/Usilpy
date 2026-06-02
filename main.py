import io
import os

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
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

app = FastAPI(title="Gestión Académica Universitaria", version="1.0.0")

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
    """Login local con usuario/contraseña. Devuelve JWT."""
    user = auth_service.authenticate_local(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    token = auth_service.create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "user": user}


@app.get("/api/auth/azure/login")
async def azure_login():
    """Redirige al flujo de login de Azure AD."""
    if not auth_service.settings.azure_client_id:
        raise HTTPException(status_code=503, detail="Azure AD no configurado")
    url = auth_service.build_azure_login_url()
    return RedirectResponse(url)


@app.get("/api/auth/azure/callback")
async def azure_callback(code: str = "", state: str = "", error: str = ""):
    """Recibe el código OAuth2 de Azure, emite JWT interno y redirige al frontend."""
    if error:
        return RedirectResponse(f"/?auth_error={error}")
    try:
        user = await auth_service.exchange_azure_code(code, state)
    except Exception as exc:
        return RedirectResponse(f"/?auth_error={str(exc)[:60]}")
    token = auth_service.create_access_token(user)
    # Redirige al frontend con el token en el fragment (nunca llega al servidor)
    return RedirectResponse(f"/#token={token}")


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    """Devuelve el perfil del usuario autenticado."""
    return current_user


@app.post("/api/auth/logout")
async def logout():
    """El cliente debe descartar el token; aquí solo confirmamos."""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Canvas  (protegido)
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
# Azure AD / Microsoft Graph  (protegido)
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
# Bulk / Carga Masiva  (protegido)
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
async def bulk_template(_: dict = Depends(get_current_user)):
    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(status_code=404, detail="Plantilla no encontrada en el servidor")
    return FileResponse(
        path=TEMPLATE_PATH,
        filename="plantilla_carga_masiva.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


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
