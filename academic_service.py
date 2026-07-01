"""
Servicio académico GND: importación de historial de notas, mallas curriculares
y validación de correlativas.
"""
from __future__ import annotations

import io
import logging
import unicodedata
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import db

logger = logging.getLogger(__name__)

PROGRAMAS = {
    "ADM": ("GND", "Administración de Empresas"),
    "NEG": ("GND", "Negocios Internacionales"),
    "MKT": ("GND", "Marketing y Gestión Comercial"),
}

MALLA_SHEETS = {
    "ADM": "malla ADM",
    "NEG": "malla NEG",
    "MKT": "malla MKT",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s: Any) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", str(s).strip())
    return s


def _parse_nota(raw: Any) -> tuple[str, float | None, bool | None]:
    """Devuelve (nota_texto, nota_num, aprobado)."""
    txt = _norm(raw).lower()
    if txt in ("", "nan", "none"):
        return ("", None, None)
    # intentar numérico
    try:
        n = float(txt)
        aprobado = n >= 3
        return (str(raw).strip(), n, aprobado)
    except ValueError:
        pass
    if txt in ("cursando", "curso"):
        return ("cursando", None, None)
    if txt in ("ausente", "a", "n/h"):
        return ("ausente", None, False)
    if txt == "pendiente":
        return ("pendiente", None, None)
    if txt == "aprobado":
        return ("aprobado", None, True)
    if txt == "desaprobado":
        return ("desaprobado", None, False)
    return (str(raw).strip(), None, None)


def _norm_periodo(p: Any) -> str:
    """Normaliza 2023.1 → 2023-1, 2023.2 → 2023-2, etc."""
    s = _norm(p)
    return s.replace(".", "-")


# ---------------------------------------------------------------------------
# Importar historial desde Excel
# ---------------------------------------------------------------------------

async def importar_historial_gnd(file_bytes: bytes, filename: str) -> dict:
    """
    Importa historial de notas del Excel GND.
    Retorna resumen: {insertados, actualizados, errores, detalle}.
    """
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)

    insertados = 0
    actualizados = 0
    errores = []

    for sheet_key, (programa, carrera) in PROGRAMAS.items():
        if sheet_key not in xl.sheet_names:
            continue

        df = xl.parse(sheet_key, dtype=str).fillna("")

        # Normalizar columnas
        df.columns = [_norm(c).lower().replace(" ", "_") for c in df.columns]

        # Mapear columnas con nombres variables entre hojas
        col_map = {
            "nombre": next((c for c in df.columns if "nombre" in c and "apellido" in c), None),
            "cedula": next((c for c in df.columns if c == "cedula"), None),
            "codigo": next((c for c in df.columns if "codigo" in c and "asig" in c), None),
            "materia": next((c for c in df.columns if c == "cursos"), None),
            "ciclo": next((c for c in df.columns if c == "ciclo"), None),
            "nota": next((c for c in df.columns if c in ("nta", "nota")), None),
            "periodo": next((c for c in df.columns if c == "periodo"), None),
            "docente": next((c for c in df.columns if c == "docente"), None),
        }

        rows_to_upsert = []
        cedula_col = col_map["cedula"]
        materia_col = col_map["materia"]
        for _, row in df.iterrows():
            # Use direct column access to avoid pandas Series ambiguity
            cedula = _norm(row[cedula_col] if cedula_col and cedula_col in row.index else "")
            materia = _norm(row[materia_col] if materia_col and materia_col in row.index else "")
            if not cedula or not materia:
                continue

            nombre = _norm(row.get(col_map["nombre"] or "", ""))
            codigo = _norm(row.get(col_map["codigo"] or "", ""))
            ciclo_raw = row.get(col_map["ciclo"] or "", "")
            try:
                ciclo = int(float(ciclo_raw)) if ciclo_raw and ciclo_raw != "nan" else None
            except (ValueError, TypeError):
                ciclo = None

            nota_raw = row.get(col_map["nota"] or "", "")
            nota_txt, nota_num, aprobado = _parse_nota(nota_raw)
            periodo = _norm_periodo(row.get(col_map["periodo"] or "", ""))
            docente = _norm(row.get(col_map["docente"] or "", ""))

            rows_to_upsert.append((
                cedula, nombre, carrera, programa,
                codigo, materia, ciclo,
                nota_txt, nota_num, aprobado,
                periodo, docente,
            ))

        if not rows_to_upsert:
            continue

        try:
            # Batch upsert — one round-trip per sheet
            pool = db.get_pool()
            async with pool.acquire() as conn:
                result = await conn.executemany(
                    """INSERT INTO historial_academico
                           (cedula, nombre, carrera, programa, codigo_materia, materia,
                            ciclo, nota, nota_num, aprobado, periodo, docente)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                       ON CONFLICT (cedula, codigo_materia, periodo)
                       DO UPDATE SET
                           nombre=EXCLUDED.nombre, carrera=EXCLUDED.carrera,
                           nota=EXCLUDED.nota, nota_num=EXCLUDED.nota_num,
                           aprobado=EXCLUDED.aprobado, docente=EXCLUDED.docente,
                           ciclo=EXCLUDED.ciclo""",
                    rows_to_upsert,
                )
            insertados += len(rows_to_upsert)
        except Exception as exc:
            msg = f"{sheet_key}: {exc}"
            logger.error(msg)
            errores.append(msg)

    return {
        "insertados": insertados,
        "actualizados": actualizados,
        "errores": errores,
    }


# ---------------------------------------------------------------------------
# Importar mallas curriculares (correlativas)
# ---------------------------------------------------------------------------

async def importar_mallas_gnd(file_bytes: bytes, filename: str) -> dict:
    """
    Importa mallas curriculares (correlativas) del Excel GND.
    """
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)

    insertados = 0
    errores = []

    for sheet_key, sheet_name in MALLA_SHEETS.items():
        if sheet_name not in xl.sheet_names:
            continue

        _, carrera = PROGRAMAS[sheet_key]
        df = xl.parse(sheet_name, header=None, dtype=str).fillna("")

        # Encontrar fila de encabezado (contiene "Semestre")
        header_row = None
        for i, row in df.iterrows():
            vals = [_norm(v).lower() for v in row.values]
            if "semestre" in vals:
                header_row = i
                break

        if header_row is None:
            errores.append(f"{sheet_name}: no se encontró fila de encabezado")
            continue

        # Reasignar encabezados
        df.columns = [_norm(v).lower().replace(" ", "_") for v in df.iloc[header_row].values]
        df = df.iloc[header_row + 1:].reset_index(drop=True)

        # Columnas clave
        sem_col = next((c for c in df.columns if "semestre" in c), None)
        cod_col = next((c for c in df.columns if "código" in c or "codigo" in c), None)
        mat_col = next((c for c in df.columns if "asignatura" in c), None)
        pre_col = next((c for c in df.columns if "requisito" in c), None)

        if not mat_col or not pre_col:
            errores.append(f"{sheet_name}: columnas faltantes")
            continue

        rows = []
        for _, row in df.iterrows():
            materia = _norm(row.get(mat_col, ""))
            prereq = _norm(row.get(pre_col, ""))
            if not materia or materia.lower() in ("nan", "optativas"):
                continue
            semestre_raw = row.get(sem_col, "") if sem_col else ""
            try:
                semestre = int(float(semestre_raw)) if semestre_raw and semestre_raw not in ("nan", "") else None
            except (ValueError, TypeError):
                semestre = None
            codigo = _norm(row.get(cod_col, "")) if cod_col else ""
            prereq_final = None if prereq.lower() in ("ninguno", "nan", "") else prereq
            rows.append(("GND", carrera, semestre, codigo, materia, prereq_final))

        try:
            pool = db.get_pool()
            async with pool.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO correlativas (programa, carrera, semestre, codigo_materia, materia, prerequisito)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT DO NOTHING""",
                    rows,
                )
            insertados += len(rows)
        except Exception as exc:
            errores.append(f"{sheet_name}: {exc}")

    return {"insertados": insertados, "errores": errores}


# ---------------------------------------------------------------------------
# Validar correlativas
# ---------------------------------------------------------------------------

async def validar_correlativas(cedula: str, materia: str, carrera: str, programa: str = "GND") -> dict:
    """
    Verifica si el alumno (cedula) puede inscribirse a la materia dada.
    Retorna: {puede: bool, motivo: str, prerequisitos: [...]}
    """
    # Buscar correlativas de la materia
    prereqs = await db.fetch(
        """SELECT prerequisito FROM correlativas
           WHERE LOWER(TRIM(materia)) = LOWER(TRIM(?))
             AND carrera = ? AND programa = ?
             AND prerequisito IS NOT NULL""",
        materia, carrera, programa,
    )

    if not prereqs:
        return {"puede": True, "motivo": "Sin correlativas", "prerequisitos": []}

    faltantes = []
    for row in prereqs:
        prereq = row["prerequisito"]
        # Puede ser "Matemática I-II" → múltiples
        partes = [p.strip() for p in prereq.replace(" y ", ",").split(",")]
        for parte in partes:
            if not parte:
                continue
            # Verificar aprobación en historial
            aprobado = await db.fetchval(
                """SELECT COUNT(*) FROM historial_academico
                   WHERE cedula = ?
                     AND LOWER(TRIM(materia)) = LOWER(TRIM(?))
                     AND aprobado = true""",
                cedula, parte,
            )
            if not aprobado:
                faltantes.append(parte)

    if faltantes:
        return {
            "puede": False,
            "motivo": f"Debe aprobar primero: {', '.join(faltantes)}",
            "prerequisitos": faltantes,
        }

    return {"puede": True, "motivo": "Correlativas cumplidas", "prerequisitos": []}


# ---------------------------------------------------------------------------
# Historial de un alumno
# ---------------------------------------------------------------------------

async def historial_alumno(cedula: str) -> list[dict]:
    return await db.fetch(
        """SELECT materia, codigo_materia, ciclo, nota, nota_num, aprobado, periodo, carrera
           FROM historial_academico
           WHERE cedula = ?
           ORDER BY ciclo, periodo""",
        cedula,
    )


async def buscar_alumno(q: str) -> list[dict]:
    """Busca alumnos por cédula o nombre, un resultado por alumno."""
    like = f"%{q}%"
    return await db.fetch(
        """SELECT cedula, MAX(nombre) as nombre, MAX(carrera) as carrera, MAX(programa) as programa
           FROM historial_academico
           WHERE cedula ILIKE ? OR nombre ILIKE ?
           GROUP BY cedula
           ORDER BY MAX(nombre)
           LIMIT 20""",
        like, like,
    )
