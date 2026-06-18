"""
Parseo de planillas académicas de inscripción.

Formato esperado: un alumno por pestaña.
  C4  → Programa (CPEL / GND / GA)
  C5  → Período  (ej. 2026-1)
  C9  → Nombre completo (todo mayúsculas, "Apellido, Nombre")
  C12 → Cédula
  G5:G20 → Nombres de cursos inscriptos (celdas vacías = fin)
"""
from __future__ import annotations

import io
import re
from typing import Any

import openpyxl


# Programas válidos conocidos
PROGRAMAS_VALIDOS = {"CPEL", "GND", "GA"}


def _cell_val(ws, row: int, col: int) -> str:
    v = ws.cell(row=row, column=col).value
    if v is None:
        return ""
    return str(v).strip()


def _normalizar_nombre(raw: str) -> str:
    """'FLORES RODAS, CAMILA ARACELI' → 'Camila Araceli Flores Rodas'"""
    raw = raw.strip()
    if not raw:
        return ""
    if "," in raw:
        apellido, nombre = raw.split(",", 1)
        partes = nombre.strip().split() + apellido.strip().split()
    else:
        partes = raw.split()
    return " ".join(p.capitalize() for p in partes if p)


def _normalizar_cedula(raw: str) -> str:
    digits = re.sub(r"[^\d]", "", raw)
    return digits


def _parsear_hoja(ws, semestre_override: str | None = None) -> dict:
    nombre_raw = _cell_val(ws, 9, 3)   # C9
    cedula_raw = _cell_val(ws, 12, 3)  # C12
    programa   = _cell_val(ws, 4, 3)   # C4
    periodo    = _cell_val(ws, 5, 3)   # C5

    nombre = _normalizar_nombre(nombre_raw)
    cedula = _normalizar_cedula(cedula_raw)
    if semestre_override:
        periodo = semestre_override

    # Cursos: columna G (col 7), filas 5 a 20
    cursos = []
    for row in range(5, 21):
        val = _cell_val(ws, row, 7)
        if not val:
            continue
        cursos.append({"nombre_original": val, "match_ok": True})

    issues = []

    if not nombre:
        issues.append({"tipo": "error", "mensaje": "Nombre no encontrado en C9"})
    if not cedula:
        issues.append({"tipo": "error", "mensaje": "Cédula no encontrada en C12"})
    elif not cedula.isdigit():
        issues.append({"tipo": "error", "mensaje": f"Cédula inválida: '{cedula_raw}'"})
    if not programa:
        issues.append({"tipo": "warn", "mensaje": "Programa no encontrado en C4"})
    elif programa.upper() not in PROGRAMAS_VALIDOS:
        issues.append({"tipo": "warn", "mensaje": f"Programa desconocido: '{programa}' (esperado: CPEL, GND, GA)"})
    if not periodo:
        issues.append({"tipo": "warn", "mensaje": "Período no encontrado en C5"})
    if not cursos:
        issues.append({"tipo": "warn", "mensaje": "No se encontraron cursos en columna G"})

    return {
        "pestaña":    ws.title,
        "nombre":     nombre,
        "nombre_raw": nombre_raw,
        "cedula":     cedula,
        "programa":   programa.upper() if programa else "",
        "periodo":    periodo,
        "cursos":     cursos,
        "issues":     issues,
    }


def parsear_planilla(file_bytes: bytes, semestre: str | None = None) -> dict:
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    alumnos = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        alumno = _parsear_hoja(ws, semestre_override=semestre)
        alumnos.append(alumno)

    total = len(alumnos)
    listos = sum(1 for a in alumnos if not any(i["tipo"] == "error" for i in a["issues"]))
    con_error = total - listos

    return {
        "total": total,
        "listos": listos,
        "con_error": con_error,
        "alumnos": alumnos,
    }


def exportar_excel(alumnos: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Parseo"

    headers = ["Pestaña", "Nombre", "Cédula", "Programa", "Período", "Cursos", "Estado", "Issues"]
    fill_h = PatternFill("solid", fgColor="1F4E79")
    font_h = Font(color="FFFFFF", bold=True)
    for i, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.fill = fill_h
        cell.font = font_h
        cell.alignment = Alignment(horizontal="center")

    for row_i, a in enumerate(alumnos, 2):
        issues = a.get("issues", [])
        estado = "ERROR" if any(x["tipo"] == "error" for x in issues) else ("AVISO" if issues else "OK")
        cursos_str = " | ".join(c["nombre_original"] for c in a.get("cursos", []))
        issues_str = " | ".join(x["mensaje"] for x in issues)
        ws.append([
            a.get("pestaña", ""),
            a.get("nombre", ""),
            a.get("cedula", ""),
            a.get("programa", ""),
            a.get("periodo", ""),
            cursos_str,
            estado,
            issues_str,
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
