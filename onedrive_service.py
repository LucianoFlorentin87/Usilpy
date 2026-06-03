"""
Download and parse the enrollment Excel from OneDrive via Microsoft Graph (app-only auth).

Sheet format (two supported):
  Format A (label-value):
    A1:"Cédula"   B1: 3406399
    A2:"Nombre"   B2: Luciano Florentín
    A3: blank
    A4:"Materias" (optional header)
    A5+: one subject per row in column A (or B)

  Format B (plain):
    A1: 3406399
    A2: Luciano Florentín
    A3+: one subject per row
"""
from __future__ import annotations

import io
import re
import unicodedata
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
import openpyxl

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _get_graph_token() -> str:
    import msal
    app = msal.ConfidentialClientApplication(
        settings.azure_client_id,
        authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
        client_credential=settings.azure_client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Azure token error: {result.get('error_description')}")
    return result["access_token"]


def _graph_headers() -> dict:
    return {"Authorization": f"Bearer {_get_graph_token()}"}


async def download_excel_bytes(file_id: str, drive_id: str = "") -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        if drive_id:
            url = f"{GRAPH_BASE}/drives/{drive_id}/items/{file_id}/content"
        else:
            # Use the default drive of the app's service principal scope
            url = f"{GRAPH_BASE}/drive/items/{file_id}/content"
        resp = await client.get(url, headers=_graph_headers(), follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _clean(val) -> str:
    return "" if val is None else str(val).strip()


def _is_cedula_value(val: str) -> bool:
    return bool(val and re.match(r"^\d{4,12}$", re.sub(r"[\s.\-]", "", val)))


def _normalize_cedula(val: str) -> str:
    return re.sub(r"[^\d]", "", val)


_CEDULA_LABELS = {"cédula", "cedula", "ci", "id", "dni", "documento", "nro"}
_NOMBRE_LABELS = {"nombre", "alumno", "estudiante", "name", "apellido", "nombres"}
_MATERIA_LABELS = {"materia", "materias", "asignatura", "asignaturas", "curso", "cursos", "subject"}


def _label_match(cell: str, labels: set) -> bool:
    c = cell.lower().strip()
    return any(lbl in c for lbl in labels)


@dataclass
class AlumnoData:
    cedula: str
    nombre: str
    materias: list[str] = field(default_factory=list)
    sheet_name: str = ""


def parse_sheet(ws, sheet_name: str) -> Optional[AlumnoData]:
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        cleaned = [_clean(c) for c in row]
        rows.append(cleaned)

    # strip leading/trailing blank rows
    while rows and all(c == "" for c in rows[0]):
        rows.pop(0)
    while rows and all(c == "" for c in rows[-1]):
        rows.pop()

    if len(rows) < 2:
        return None

    cedula = nombre = ""
    materias: list[str] = []

    first_a = rows[0][0] if rows[0] else ""
    second_a = rows[1][0] if len(rows) > 1 else ""

    format_a = _label_match(first_a, _CEDULA_LABELS) and _label_match(second_a, _NOMBRE_LABELS)

    if format_a:
        cedula = _normalize_cedula(rows[0][1] if len(rows[0]) > 1 else "")
        nombre = rows[1][1] if len(rows) > 1 and len(rows[1]) > 1 else ""
        # Materias start after row index 1, skip any "Materias" header row
        start = 2
        for i in range(2, len(rows)):
            cell = rows[i][0]
            if _label_match(cell, _MATERIA_LABELS):
                start = i + 1
                break
            if cell:
                start = i
                break
        for row in rows[start:]:
            mat = row[0] or (row[1] if len(row) > 1 else "")
            if mat and not _label_match(mat, _MATERIA_LABELS):
                materias.append(mat)
    else:
        # Format B: first cedula-looking value, then name, then subjects
        idx = 0
        for i, row in enumerate(rows):
            val = row[0] or (row[1] if len(row) > 1 else "")
            if _is_cedula_value(val):
                cedula = _normalize_cedula(val)
                idx = i + 1
                break

        for i in range(idx, len(rows)):
            val = rows[i][0] or (rows[i][1] if len(rows[i]) > 1 else "")
            if val and not _is_cedula_value(val) and not _label_match(val, _MATERIA_LABELS):
                nombre = val
                idx = i + 1
                break

        for row in rows[idx:]:
            mat = row[0] or (row[1] if len(row) > 1 else "")
            if mat and not _label_match(mat, _MATERIA_LABELS):
                materias.append(mat)

    if not cedula or not nombre:
        logger.warning("Hoja '%s': no se pudo extraer cédula o nombre", sheet_name)
        return None

    return AlumnoData(
        cedula=cedula,
        nombre=nombre,
        materias=[m for m in materias if m],
        sheet_name=sheet_name,
    )


@dataclass
class ValidationError:
    sheet_name: str
    cedula: str
    nombre: str
    error: str


def validate_alumnos(alumnos: list[AlumnoData]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    seen_cedulas: dict[str, str] = {}

    for a in alumnos:
        if not a.cedula:
            errors.append(ValidationError(a.sheet_name, "", a.nombre, "Cédula vacía"))
        elif not re.match(r"^\d+$", a.cedula):
            errors.append(ValidationError(a.sheet_name, a.cedula, a.nombre, f"Cédula no numérica: '{a.cedula}'"))

        parts = a.nombre.strip().split()
        if len(parts) < 2:
            errors.append(ValidationError(a.sheet_name, a.cedula, a.nombre,
                                          "Nombre incompleto (se requiere al menos nombre y apellido)"))

        if not a.materias:
            errors.append(ValidationError(a.sheet_name, a.cedula, a.nombre, "Sin materias asignadas"))

        if a.cedula and a.cedula in seen_cedulas:
            errors.append(ValidationError(
                a.sheet_name, a.cedula, a.nombre,
                f"Cédula duplicada (también en hoja '{seen_cedulas[a.cedula]}')"
            ))
        elif a.cedula:
            seen_cedulas[a.cedula] = a.sheet_name

    return errors


async def get_alumnos_from_onedrive(
    file_id: str | None = None,
    drive_id: str | None = None,
) -> tuple[list[AlumnoData], list[ValidationError]]:
    """
    Download and parse the enrollment Excel from OneDrive.
    Returns (alumnos, validation_errors).
    Raises if download fails.
    """
    fid = file_id or settings.onedrive_file_id
    did = drive_id or settings.onedrive_drive_id

    if not fid:
        raise ValueError("ONEDRIVE_FILE_ID no configurado en .env")

    raw = await download_excel_bytes(fid, did)
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=False, data_only=True)

    alumnos: list[AlumnoData] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        alumno = parse_sheet(ws, sheet_name)
        if alumno:
            alumnos.append(alumno)
        else:
            logger.info("Hoja '%s' omitida (sin datos válidos)", sheet_name)

    errors = validate_alumnos(alumnos)
    return alumnos, errors
