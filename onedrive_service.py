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


_CEDULA_LABELS = {"cédula", "cedula", "ci", "c.i", "id", "dni", "documento", "nro"}
_NOMBRE_LABELS = {"nombre", "alumno", "estudiante", "name", "apellido", "nombres", "nombre y apellido"}
_MATERIA_LABELS = {"materia", "materias", "asignatura", "asignaturas", "curso", "cursos", "subject"}
_SEMESTRE_RE = re.compile(r"\b(20\d{2}[-/]\d{1,2})\b")


def _label_match(cell: str, labels: set) -> bool:
    c = cell.lower().strip().rstrip(":")
    return any(lbl in c for lbl in labels)


@dataclass
class AlumnoData:
    cedula: str
    nombre: str
    materias: list[str] = field(default_factory=list)
    sheet_name: str = ""
    semestre: str = ""


def parse_sheet(ws, sheet_name: str) -> Optional[AlumnoData]:
    """
    Keyword-search parser: scans every cell looking for label keywords
    (Cédula, Nombre, Curso…) regardless of their position in the sheet.
    Supports the one-student-per-sheet layout used by the academic team.
    """
    # Build a (row, col) -> value map for all non-empty cells
    cell_map: dict[tuple[int, int], str] = {}
    for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
        for c_idx, val in enumerate(row):
            v = _clean(val)
            if v:
                cell_map[(r_idx, c_idx)] = v

    if not cell_map:
        return None

    def _neighbors(r: int, c: int) -> list[str]:
        """Return non-empty values from the cells immediately below and to the right."""
        candidates = []
        for dr, dc in [(1, 0), (2, 0), (0, 1), (0, 2), (3, 0)]:
            v = cell_map.get((r + dr, c + dc), "")
            if v:
                candidates.append(v)
        return candidates

    cedula = nombre = semestre = ""
    curso_col: int | None = None
    curso_header_row: int | None = None

    for (r, c), val in sorted(cell_map.items()):
        low = val.lower().strip().rstrip(":")

        # Detect semester pattern directly in cell (e.g. "2026-1")
        if not semestre:
            m = _SEMESTRE_RE.search(val)
            if m:
                semestre = m.group(1)

        # Cédula label
        if not cedula and _label_match(val, _CEDULA_LABELS):
            for neighbor in _neighbors(r, c):
                if _is_cedula_value(neighbor):
                    cedula = _normalize_cedula(neighbor)
                    break
            continue

        # Nombre label
        if not nombre and _label_match(val, _NOMBRE_LABELS):
            for neighbor in _neighbors(r, c):
                # A name has letters and is not a cedula and is not another label
                if neighbor and not _is_cedula_value(neighbor) and not _label_match(neighbor, _CEDULA_LABELS):
                    nombre = neighbor
                    break
            continue

        # "Curso" column header — remember the column to read courses below it
        if _label_match(val, _MATERIA_LABELS) and curso_col is None:
            curso_col = c
            curso_header_row = r
            continue

        # Fallback: bare cedula value with no label (e.g. just a number in a cell)
        if not cedula and _is_cedula_value(val):
            cedula = _normalize_cedula(val)
            continue

    # Extract courses from the detected column
    materias: list[str] = []
    if curso_col is not None and curso_header_row is not None:
        max_row = max(r for r, _ in cell_map)
        for row_i in range(curso_header_row + 1, max_row + 2):
            v = cell_map.get((row_i, curso_col), "")
            if v and not _label_match(v, _MATERIA_LABELS):
                materias.append(v)

    # If no dedicated Curso column found, fall back to scanning all values
    # that look like subject names (not labels, not cedula, not nombre)
    if not materias:
        all_vals = sorted(cell_map.items())
        for (r, c), val in all_vals:
            low = val.lower().strip().rstrip(":")
            if (val != nombre and val != cedula and val != semestre
                    and not _label_match(val, _CEDULA_LABELS)
                    and not _label_match(val, _NOMBRE_LABELS)
                    and not _is_cedula_value(val)
                    and not _SEMESTRE_RE.search(val)
                    and len(val) > 3
                    and not any(kw in low for kw in ("programa", "obs", "nota", "área", "area", "facultad", "carrera"))):
                materias.append(val)

    if not cedula and not nombre:
        logger.debug("Hoja '%s': omitida (sin cédula ni nombre)", sheet_name)
        return None
    if not cedula:
        logger.warning("Hoja '%s': sin cédula", sheet_name)
        return None
    if not nombre:
        logger.warning("Hoja '%s': sin nombre", sheet_name)
        return None

    return AlumnoData(
        cedula=cedula,
        nombre=nombre,
        materias=[m for m in materias if m],
        sheet_name=sheet_name,
        semestre=semestre,
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
        if len(parts) < 1 or not a.nombre.strip():
            errors.append(ValidationError(a.sheet_name, a.cedula, a.nombre,
                                          "Nombre vacío"))

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
