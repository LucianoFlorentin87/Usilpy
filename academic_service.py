"""
Servicio académico: importación de historial de notas, mallas curriculares
y validación de correlativas (programas GND y CPEL).
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

# CPEL -----------------------------------------------------------------------
# Hojas de historial a procesar (en orden de prioridad)
CPEL_HISTORIAL_SHEETS = ["CPEL PRO", "CPEL", "NO TOCAR"]

# Hojas de malla CPEL → carrera canónica
CPEL_MALLA_SHEETS = {
    "malla ADMI": "Administración de Empresas",
    "malla NEGO": "Negocios Internacionales",
    "malla MKT":  "Marketing y Gestión Comercial",
}

# Correcciones de typos conocidos en nombres de materias CPEL
_CPEL_MATERIA_FIXES: dict[str, str] = {
    "administracion estategica":                    "Administración Estratégica",
    "administracion financiera":                    "Administración Financiera",
    "administracion publica":                       "Administración Pública",
    "administracion ii":                            "Administración II",
    "administracion de recursos humanos":           "Administración de Recursos Humanos",
    "administracion  i":                            "Administración I",
    "auditoría de gestion administrativa":          "Auditoría de Gestión Administrativa",
    "formulacion y evaluacion de proyecto":         "Formulación y Evaluación de Proyectos de Inversión",
    "investigacion y analisis de mercado ii":       "Investigación y Análisis de Mercado II",
    "investigacion y analisis de mercado i":        "Investigación y Análisis de Mercado I",
    "metodologia de la investigacion":              "Metodología de la Investigación",
    "logistica":                                    "Logística",
    "negociacion":                                  "Negociación",
    "negociacion en negocios internacionales":      "Negociación en Negocios Internacionales",
    "politica de precio":                           "Política de Precio",
    "politica y estrategia de empresas":            "Política y Estrategia de Empresas",
    "organizacion de sistemas y metodos":           "Organización de Sistemas y Métodos",
    "matematica i":                                 "Matemática I",
    "matematica ii":                                "Matemática II",
    "matematica financiera":                        "Matemática Financiera",
    "ingles i":                                     "Inglés I",
    "ingles ii":                                    "Inglés II",
    "ingles iii":                                   "Inglés III",
    "ingles iv":                                    "Inglés IV",
    "direccion y planeamiento":                     "Dirección y Planeamiento",
    "etica":                                        "Ética",
    "pymes y empresas":                             "Pymes y Empresas Familiares",
    "publicidad y promocion":                       "Publicidad y Promoción",
    "estrategia de distribucion":                   "Estrategia de Distribución",
    "comportamiento organizacional del marketing":  "Comportamiento Organizacional",
    "investigacion y analisis de mercado internacionales": "Investigación y Análisis de Mercados Internacionales",
    "markenting":                                   "Marketing",
    "administración  i":                            "Administración I",
}


def _fix_materia(name: str) -> str:
    """Apply known typo corrections to a materia name."""
    key = unicodedata.normalize("NFKD", name.strip().lower())
    key = "".join(c for c in key if not unicodedata.combining(c))
    return _CPEL_MATERIA_FIXES.get(key, name.strip())


# Normaliza los distintos nombres de carrera que aparecen en las planillas CPEL
def _norm_carrera_cpel(raw: str) -> str:
    s = raw.strip().lower()
    if any(x in s for x in ("adm", "empresa")):
        return "Administración de Empresas"
    if any(x in s for x in ("neg", "global")):
        return "Negocios Internacionales"
    if any(x in s for x in ("mar", "mkt", "gestion")):
        return "Marketing y Gestión Comercial"
    return raw.strip()


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

def _importar_historial_sync(file_bytes: bytes, filename: str) -> dict:
    """Parse Excel synchronously (CPU-bound). Returns rows + carrera_map.
    Auto-detects GND vs CPEL by sheet names."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)
    all_rows: list[tuple] = []
    carrera_map: dict[str, str] = {}

    # Build code→canonical-name map from mallas in this same file
    malla_rows = _parsear_mallas_sync(file_bytes, filename)
    codigo_map = _build_codigo_map(malla_rows)

    # CPEL detection: use sheets that only exist in CPEL files (not GND)
    if any(s in xl.sheet_names for s in ["CPEL PRO", "CPEL", "NO TOCAR", "malla ADMI"]):
        cpel_rows = _parsear_historial_cpel_sync(file_bytes)
        # Also build code→name from CPEL historial sheet (has 92% coverage)
        cpel_codigo_map = _build_cpel_codigo_map_from_historial(file_bytes)
        codigo_map.update(cpel_codigo_map)
        # Normalize materia names via code map
        normalized = []
        for r in cpel_rows:
            codigo = r[4].strip().upper() if r[4] else ""
            materia = codigo_map.get(codigo, r[5]) if codigo else r[5]
            normalized.append(r[:5] + (materia,) + r[6:])
        all_rows.extend(normalized)
        for r in normalized:
            carrera_map[r[2]] = r[2]

    # GND detection: if any GND sheet is present, parse GND
    gnd_found = False
    for sheet_key, (programa, carrera) in PROGRAMAS.items():
        if sheet_key not in xl.sheet_names:
            continue
        gnd_found = True
        carrera_map[sheet_key] = carrera

    if not gnd_found:
        return {"rows": all_rows, "carrera_map": carrera_map}

    for sheet_key, (programa, carrera) in PROGRAMAS.items():
        if sheet_key not in xl.sheet_names:
            continue

        df = xl.parse(sheet_key, dtype=str).fillna("")
        seen: dict[str, int] = {}
        new_cols = []
        for c in df.columns:
            key = _norm(c).lower().replace(" ", "_")
            if key in seen:
                seen[key] += 1
                new_cols.append(f"{key}__{seen[key]}")
            else:
                seen[key] = 0
                new_cols.append(key)
        df.columns = new_cols

        col_map = {
            "nombre":  next((c for c in df.columns if "nombre" in c and "apellido" in c), None),
            "cedula":  next((c for c in df.columns if c == "cedula"), None),
            "codigo":  next((c for c in df.columns if "codigo" in c and "asig" in c), None),
            "materia": next((c for c in df.columns if c == "cursos"), None),
            "ciclo":   next((c for c in df.columns if c == "ciclo"), None),
            "nota":    next((c for c in df.columns if c in ("nta", "nota")), None),
            "periodo": next((c for c in df.columns if c == "periodo"), None),
            "docente": next((c for c in df.columns if c == "docente"), None),
        }

        for _, row in df.iterrows():
            cedula = _norm(row.get(col_map["cedula"] or "", ""))
            materia = _norm(row.get(col_map["materia"] or "", ""))
            if not cedula or not cedula.isdigit() or not materia:
                continue
            nombre = _norm(row.get(col_map["nombre"] or "", ""))
            codigo = _norm(row.get(col_map["codigo"] or "", ""))
            if codigo.lower() in ("nan", "none", ""):
                codigo = ""
            # Normalizar nombre de materia por código si existe en la malla
            if codigo:
                materia = codigo_map.get(codigo.upper(), materia)
            ciclo_raw = row.get(col_map["ciclo"] or "", "")
            try:
                ciclo = int(float(ciclo_raw)) if ciclo_raw and ciclo_raw != "nan" else None
            except (ValueError, TypeError):
                ciclo = None
            nota_txt, nota_num, aprobado = _parse_nota(row.get(col_map["nota"] or "", ""))
            periodo = _norm_periodo(row.get(col_map["periodo"] or "", ""))
            docente = _norm(row.get(col_map["docente"] or "", ""))
            all_rows.append((cedula, nombre, carrera, programa, codigo, materia, ciclo,
                             nota_txt, nota_num, aprobado, periodo, docente))

    return {"rows": all_rows, "carrera_map": carrera_map}


def _build_cpel_codigo_map_from_historial(file_bytes: bytes) -> dict[str, str]:
    """Extrae mapa {codigo_upper → nombre} de la hoja CPEL del historial (92% coverage)."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)
    code_map: dict[str, str] = {}
    if "CPEL" not in xl.sheet_names:
        return code_map
    df = xl.parse("CPEL", dtype=str).fillna("")
    cols = [_norm(c).lower().replace(" ", "_") for c in df.columns]
    df.columns = cols
    cod_col = next((c for c in cols if "codigo" in c and "asig" in c), None)
    mat_col = next((c for c in cols if c == "cursos"), None)
    if not cod_col or not mat_col:
        return code_map
    for _, row in df.iterrows():
        cod = _norm(row.get(cod_col, "")).upper()
        mat = _norm(row.get(mat_col, ""))
        if cod and cod not in ("NAN", "NONE", "") and mat and mat.lower() not in ("nan", ""):
            code_map.setdefault(cod, _fix_materia(mat))
    return code_map


def _parsear_historial_cpel_sync(file_bytes: bytes) -> list[tuple]:
    """Parsea las hojas de historial CPEL. La carrera se lee de la columna Carreras por fila."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)
    all_rows: list[tuple] = []
    seen_keys: set[tuple] = set()  # dedup (cedula, codigo_materia, periodo) entre hojas

    for sheet_name in CPEL_HISTORIAL_SHEETS:
        if sheet_name not in xl.sheet_names:
            continue

        df = xl.parse(sheet_name, dtype=str).fillna("")

        # Normalizar nombres de columna eliminando duplicados
        seen: dict[str, int] = {}
        new_cols = []
        for c in df.columns:
            key = _norm(c).lower().replace(" ", "_")
            if key in seen:
                seen[key] += 1
                new_cols.append(f"{key}__{seen[key]}")
            else:
                seen[key] = 0
                new_cols.append(key)
        df.columns = new_cols

        col_map = {
            "nombre":   next((c for c in df.columns if "nombre" in c and "apellido" in c), None),
            "cedula":   next((c for c in df.columns if c == "cedula"), None),
            "carreras": next((c for c in df.columns if c == "carreras"), None),
            "codigo":   next((c for c in df.columns if "codigo" in c and "asig" in c), None),
            "materia":  next((c for c in df.columns if c == "cursos"), None),
            "ciclo":    next((c for c in df.columns if c == "ciclo"), None),
            "nota":     next((c for c in df.columns if c in ("nta", "nota")), None),
            "periodo":  next((c for c in df.columns if c == "periodo"), None),
            "docente":  next((c for c in df.columns if c == "docente"), None),
        }

        for _, row in df.iterrows():
            cedula = _norm(row.get(col_map["cedula"] or "", ""))
            materia = _norm(row.get(col_map["materia"] or "", ""))
            if not cedula or not cedula.isdigit() or not materia:
                continue

            codigo = _norm(row.get(col_map["codigo"] or "", "")) if col_map["codigo"] else ""
            periodo = _norm_periodo(row.get(col_map["periodo"] or "", "")) if col_map["periodo"] else ""

            dedup_key = (cedula, codigo or materia, periodo)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            carrera_raw = _norm(row.get(col_map["carreras"] or "", "")) if col_map["carreras"] else ""
            carrera = _norm_carrera_cpel(carrera_raw) if carrera_raw else "Desconocida"

            nombre = _norm(row.get(col_map["nombre"] or "", "")) if col_map["nombre"] else ""
            ciclo_raw = row.get(col_map["ciclo"] or "", "") if col_map["ciclo"] else ""
            try:
                ciclo = int(float(ciclo_raw)) if ciclo_raw and ciclo_raw not in ("nan", "") else None
            except (ValueError, TypeError):
                ciclo = None

            nota_raw = row.get(col_map["nota"] or "", "") if col_map["nota"] else ""
            nota_txt, nota_num, aprobado = _parse_nota(nota_raw)
            docente = _norm(row.get(col_map["docente"] or "", "")) if col_map["docente"] else ""

            all_rows.append((cedula, nombre, carrera, "CPEL", codigo, _fix_materia(materia), ciclo,
                             nota_txt, nota_num, aprobado, periodo, docente))

    return all_rows


def _parsear_mallas_cpel_sync(file_bytes: bytes) -> list[tuple]:
    """Parsea las hojas de malla CPEL. Sin columna de prerequisitos — se importan con prereq=None."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)
    all_rows: list[tuple] = []

    for sheet_name, carrera in CPEL_MALLA_SHEETS.items():
        if sheet_name not in xl.sheet_names:
            continue

        df = xl.parse(sheet_name, header=None, dtype=str).fillna("")

        # Encontrar fila de encabezado buscando alguna columna que contenga "asign" o "sem"
        header_row = None
        for i, row in df.iterrows():
            vals = [_norm(v).lower() for v in row.values]
            if any("asign" in v or (v.startswith("sem") and len(v) <= 8) for v in vals if v):
                header_row = i
                break
        if header_row is None:
            continue

        df.columns = [_norm(v).lower().replace(" ", "_") for v in df.iloc[header_row].values]
        df = df.iloc[header_row + 1:].reset_index(drop=True)

        mat_col = next((c for c in df.columns if "asign" in c), None)
        sem_col = next((c for c in df.columns if c.startswith("sem") and len(c) <= 8), None)

        if not mat_col:
            continue

        for _, row in df.iterrows():
            materia = _norm(row.get(mat_col, ""))
            if not materia or materia.lower() in ("nan", ""):
                continue
            semestre_raw = row.get(sem_col, "") if sem_col else ""
            try:
                semestre = int(float(semestre_raw)) if semestre_raw and semestre_raw not in ("nan", "") else None
            except (ValueError, TypeError):
                semestre = None
            all_rows.append(("CPEL", carrera, semestre, "", _fix_materia(materia), None))

    return all_rows


async def importar_historial_gnd_rows(rows: list, carrera_map: dict) -> dict:
    """Insert pre-parsed rows into DB (async)."""
    insertados = 0
    errores = []
    if not rows:
        return {"insertados": 0, "errores": ["No se encontraron filas válidas"]}
    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO historial_academico
                       (cedula, nombre, carrera, programa, codigo_materia, materia,
                        ciclo, nota, nota_num, aprobado, periodo, docente)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                   ON CONFLICT (cedula, codigo_materia, periodo)
                   DO UPDATE SET nombre=EXCLUDED.nombre, carrera=EXCLUDED.carrera,
                       nota=EXCLUDED.nota, nota_num=EXCLUDED.nota_num,
                       aprobado=EXCLUDED.aprobado, docente=EXCLUDED.docente, ciclo=EXCLUDED.ciclo""",
                rows,
            )
        insertados = len(rows)
    except Exception as exc:
        logger.error(exc)
        errores.append(str(exc))
    return {"insertados": insertados, "errores": errores}


async def importar_historial_gnd(file_bytes: bytes, filename: str) -> dict:
    """Legacy: parse + insert (kept for compatibility)."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)

    insertados = 0
    actualizados = 0
    errores = []

    for sheet_key, (programa, carrera) in PROGRAMAS.items():
        if sheet_key not in xl.sheet_names:
            continue

        df = xl.parse(sheet_key, dtype=str).fillna("")

        # Rename duplicate columns by appending index suffix before normalizing
        seen: dict[str, int] = {}
        new_cols = []
        for c in df.columns:
            key = _norm(c).lower().replace(" ", "_")
            if key in seen:
                seen[key] += 1
                new_cols.append(f"{key}__{seen[key]}")
            else:
                seen[key] = 0
                new_cols.append(key)
        df.columns = new_cols

        # Mapear columnas — solo primera ocurrencia de cada campo
        col_map = {
            "nombre":  next((c for c in df.columns if "nombre" in c and "apellido" in c), None),
            "cedula":  next((c for c in df.columns if c == "cedula"), None),   # first = alumno
            "codigo":  next((c for c in df.columns if "codigo" in c and "asig" in c), None),
            "materia": next((c for c in df.columns if c == "cursos"), None),
            "ciclo":   next((c for c in df.columns if c == "ciclo"), None),
            "nota":    next((c for c in df.columns if c in ("nta", "nota")), None),
            "periodo": next((c for c in df.columns if c == "periodo"), None),
            "docente": next((c for c in df.columns if c == "docente"), None),
        }

        rows_to_upsert = []
        for _, row in df.iterrows():
            cedula = _norm(row.get(col_map["cedula"] or "", ""))
            materia = _norm(row.get(col_map["materia"] or "", ""))
            # Skip rows with non-numeric cedula (header noise, teacher cedula, etc.)
            if not cedula or not cedula.isdigit() or not materia:
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

def _build_codigo_map(malla_rows: list) -> dict[str, str]:
    """Construye mapa {codigo_upper → nombre_canónico} desde las filas de malla parseadas."""
    m: dict[str, str] = {}
    for r in malla_rows:
        codigo = r[3].strip().upper()
        materia = r[4].strip()
        if codigo and codigo not in ("NAN", "NONE", ""):
            m[codigo] = materia
    return m


def _parsear_mallas_sync(file_bytes: bytes, filename: str) -> list:
    """Parse mallas Excel synchronously. Returns list of row tuples.
    Auto-detects GND vs CPEL by sheet names."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)
    all_rows: list[tuple] = []

    # CPEL mallas (use malla ADMI as discriminator — only present in CPEL files)
    if "malla ADMI" in xl.sheet_names:
        all_rows.extend(_parsear_mallas_cpel_sync(file_bytes))

    for sheet_key, sheet_name in MALLA_SHEETS.items():
        if sheet_name not in xl.sheet_names:
            continue
        _, carrera = PROGRAMAS[sheet_key]
        df = xl.parse(sheet_name, header=None, dtype=str).fillna("")
        header_row = None
        for i, row in df.iterrows():
            vals = [_norm(v).lower() for v in row.values]
            if "semestre" in vals:
                header_row = i
                break
        if header_row is None:
            continue
        df.columns = [_norm(v).lower().replace(" ", "_") for v in df.iloc[header_row].values]
        df = df.iloc[header_row + 1:].reset_index(drop=True)

        # Skip sub-header row if the first data row has no numeric semestre
        first_sem = _norm(df.iloc[0].iloc[0]) if len(df) > 0 else ""
        if first_sem and not first_sem.replace(".", "").isdigit():
            df = df.iloc[1:].reset_index(drop=True)

        sem_col = next((c for c in df.columns if c == "semestre"), None) or \
                  next((c for c in df.columns if "semestre" in c), None)
        # Prefer the plain "asignatura" col over "código_de_asignatura"
        mat_col = next((c for c in df.columns if c == "asignatura"), None) or \
                  next((c for c in df.columns if "asignatura" in c and "codigo" not in c and "código" not in c), None)
        pre_col = next((c for c in df.columns if "requisito" in c), None)
        # Code col: may be named "código de asignatura" (first col) or appear last
        cod_col = next((c for c in df.columns if ("código" in c or "codigo" in c) and "asig" in c), None)
        if not mat_col or not pre_col:
            continue
        for _, row in df.iterrows():
            materia = _norm(row.get(mat_col, ""))
            prereq = _norm(row.get(pre_col, ""))
            if not materia or materia.lower() in ("nan", "optativas", "total", ""):
                continue
            semestre_raw = row.get(sem_col, "") if sem_col else ""
            try:
                semestre = int(float(semestre_raw)) if semestre_raw and semestre_raw not in ("nan", "") else None
            except (ValueError, TypeError):
                semestre = None
            codigo = _norm(row.get(cod_col, "")) if cod_col else ""
            if codigo.lower() in ("nan", "none", ""):
                codigo = ""
            prereq_final = None if prereq.lower() in ("ninguno", "nan", "") else prereq
            all_rows.append(("GND", carrera, semestre, codigo, materia, prereq_final))

    return all_rows


async def importar_mallas_rows(rows: list) -> dict:
    """Insert pre-parsed malla rows into DB."""
    if not rows:
        return {"insertados": 0, "errores": ["No se encontraron filas válidas"]}
    try:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO correlativas (programa, carrera, semestre, codigo_materia, materia, prerequisito)
                   VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
                rows,
            )
        return {"insertados": len(rows), "errores": []}
    except Exception as exc:
        return {"insertados": 0, "errores": [str(exc)]}


async def importar_mallas_gnd(file_bytes: bytes, filename: str) -> dict:
    """Legacy: parse + insert."""
    buf = io.BytesIO(file_bytes)
    xl = pd.ExcelFile(buf)

    insertados = 0
    errores = []

    for sheet_key, sheet_name in MALLA_SHEETS.items():
        if sheet_name not in xl.sheet_names:
            continue

        _, carrera = PROGRAMAS[sheet_key]
        df = xl.parse(sheet_name, header=None, dtype=str).fillna("")

        header_row = None
        for i, row in df.iterrows():
            vals = [_norm(v).lower() for v in row.values]
            if "semestre" in vals:
                header_row = i
                break

        if header_row is None:
            errores.append(f"{sheet_name}: no se encontró fila de encabezado")
            continue

        df.columns = [_norm(v).lower().replace(" ", "_") for v in df.iloc[header_row].values]
        df = df.iloc[header_row + 1:].reset_index(drop=True)

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


async def get_materias_por_carrera(programa: str, carrera: str) -> list[dict]:
    """Devuelve las materias únicas de una carrera/programa desde historial_academico,
    ordenadas por ciclo (semestre) y nombre. Esta es la fuente de verdad de materias."""
    rows = await db.fetch(
        """SELECT DISTINCT
               COALESCE(NULLIF(codigo_materia,''), materia) AS codigo,
               materia,
               MIN(ciclo) AS semestre
           FROM historial_academico
           WHERE programa = ? AND carrera = ?
             AND materia IS NOT NULL AND materia <> ''
           GROUP BY codigo_materia, materia
           ORDER BY MIN(ciclo) NULLS LAST, materia""",
        programa, carrera,
    )
    return [{"codigo": r["codigo"], "materia": r["materia"], "semestre": r["semestre"]} for r in rows]


async def estado_inscripcion(cedula: str) -> dict:
    """
    Devuelve para cada materia del historial de la carrera del alumno:
    - aprobada: ya la aprobó
    - puede_inscribir: correlativas cumplidas pero no aprobada aún
    - bloqueada: correlativas pendientes (indica cuáles)
    - cursando: nota sin definir (cursando / pendiente)

    Fuente de materias: historial_academico (no correlativas).
    """
    # Historial del alumno
    historial = await db.fetch(
        """SELECT materia, codigo_materia, nota, nota_num, aprobado, ciclo, periodo, carrera, programa
           FROM historial_academico WHERE cedula = ? ORDER BY ciclo, materia""",
        cedula,
    )
    if not historial:
        return {"cedula": cedula, "materias": [], "carrera": None, "programa": None}

    carrera  = historial[0]["carrera"]
    programa = historial[0]["programa"]

    # Set de materias aprobadas (normalizado)
    aprobadas = {_norm(r["materia"]).lower() for r in historial if r["aprobado"] is True}
    # Set de materias cursadas/en curso
    en_curso  = {_norm(r["materia"]).lower() for r in historial if r["aprobado"] is None and r["nota"]}
    # Set de materias reprobadas (cursó y no aprobó; puede recursar)
    reprobadas = {_norm(r["materia"]).lower() for r in historial if r["aprobado"] is False} - aprobadas

    # Malla completa de la carrera — desde historial, no desde correlativas
    malla = await get_materias_por_carrera(programa, carrera)

    # Prereqs por materia — desde correlativas si existen (opcional)
    prereq_map: dict[str, list[str]] = {}
    prereqs_rows = await db.fetch(
        """SELECT materia, prerequisito FROM correlativas
           WHERE carrera = ? AND programa = ? AND prerequisito IS NOT NULL""",
        carrera, programa,
    )
    for r in prereqs_rows:
        key = _norm(r["materia"]).lower()
        prereq_map.setdefault(key, [])
        for p in r["prerequisito"].replace(" y ", ",").split(","):
            p = p.strip()
            if p:
                prereq_map[key].append(p)

    materias = []
    seen = set()
    for m in malla:
        nombre = _norm(m["materia"])
        key    = nombre.lower()
        if key in seen:
            continue
        seen.add(key)
        prereqs = prereq_map.get(key, [])

        if key in aprobadas:
            estado = "aprobada"
            faltantes = []
        elif key in en_curso:
            estado = "cursando"
            faltantes = []
        else:
            faltantes = [p for p in prereqs if _norm(p).lower() not in aprobadas]
            if faltantes:
                estado = "bloqueada"
            elif key in reprobadas:
                estado = "reprobada"  # puede recursar
            else:
                estado = "puede_inscribir"

        materias.append({
            "codigo":    m["codigo"],
            "materia":   nombre,
            "semestre":  m["semestre"],
            "estado":    estado,
            "faltantes": faltantes,
        })

    return {"cedula": cedula, "carrera": carrera, "programa": programa, "materias": materias}


import re as _re
_ROMAN_SUFFIX = _re.compile(r'\b(I{1,3}|IV|VI{0,3}|IX|X{1,3}|\d+)$')


def _materia_suffix(name: str) -> str:
    """Extrae el sufijo numérico/romano final de un nombre de materia, o '' si no tiene."""
    m = _ROMAN_SUFFIX.search(name.strip())
    return m.group(0).upper() if m else ""


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _safe_match(source: str, candidates: set[str]) -> str | None:
    """Devuelve el nombre canónico que corresponde a source, o None si no hay match seguro.

    Reglas (en orden):
    1. Exacto → retorna tal cual
    2. Case-insensitive exacto → retorna canónico
    3. Sin acentos + case-insensitive exacto → retorna canónico
    4. Fuzzy >= 0.85 SOLO si el sufijo numérico/romano es idéntico (evita I↔II, etc.)
    """
    import difflib

    # 1. Exacto
    if source in candidates:
        return source

    # 2. Case-insensitive
    lower_map = {c.lower(): c for c in candidates}
    if source.lower() in lower_map:
        return lower_map[source.lower()]

    # 3. Sin acentos
    stripped_map = {_strip_accents(c): c for c in candidates}
    if _strip_accents(source) in stripped_map:
        return stripped_map[_strip_accents(source)]

    # 4. Fuzzy — solo si el sufijo numérico coincide exactamente
    src_suffix = _materia_suffix(source)
    safe_candidates = {c for c in candidates if _materia_suffix(c) == src_suffix}
    if not safe_candidates:
        return None
    matches = difflib.get_close_matches(source, safe_candidates, n=1, cutoff=0.85)
    return matches[0] if matches else None


async def comparar_mallas_historial() -> dict:
    """Compara nombres de materias entre correlativas (malla) e historial_academico."""
    malla_rows = await db.fetch(
        "SELECT DISTINCT programa, carrera, materia FROM correlativas ORDER BY programa, carrera, materia"
    )
    malla_map: dict[tuple, set] = {}
    for r in malla_rows:
        key = (r["programa"], r["carrera"])
        malla_map.setdefault(key, set())
        malla_map[key].add(r["materia"].strip())

    hist_rows = await db.fetch(
        "SELECT DISTINCT programa, carrera, materia FROM historial_academico ORDER BY programa, carrera, materia"
    )

    mismatches = []
    for r in hist_rows:
        key = (r["programa"], r["carrera"])
        materia_hist = r["materia"].strip()
        canónicos = malla_map.get(key, set())
        if not canónicos or materia_hist in canónicos:
            continue
        sugerencia = _safe_match(materia_hist, canónicos)
        if sugerencia and sugerencia != materia_hist:
            mismatches.append({
                "programa": r["programa"],
                "carrera": r["carrera"],
                "en_historial": materia_hist,
                "sugerencia": sugerencia,
            })
        elif not sugerencia:
            mismatches.append({
                "programa": r["programa"],
                "carrera": r["carrera"],
                "en_historial": materia_hist,
                "sugerencia": None,
            })

    return {"total": len(mismatches), "mismatches": mismatches}


async def reparar_nombres_historial() -> dict:
    """Actualiza en historial_academico los nombres que tienen match seguro con la malla."""
    malla_rows = await db.fetch(
        "SELECT DISTINCT programa, carrera, materia FROM correlativas"
    )
    malla_map: dict[tuple, set] = {}
    for r in malla_rows:
        key = (r["programa"], r["carrera"])
        malla_map.setdefault(key, set())
        malla_map[key].add(r["materia"].strip())

    hist_rows = await db.fetch(
        "SELECT DISTINCT programa, carrera, materia FROM historial_academico"
    )

    updates = []
    for r in hist_rows:
        key = (r["programa"], r["carrera"])
        materia_hist = r["materia"].strip()
        canónicos = malla_map.get(key, set())
        if not canónicos or materia_hist in canónicos:
            continue
        correcto = _safe_match(materia_hist, canónicos)
        if correcto and correcto != materia_hist:
            updates.append((correcto, r["programa"], r["carrera"], materia_hist))

    if not updates:
        return {"actualizados": 0, "detalle": []}

    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            "UPDATE historial_academico SET materia=$1 WHERE programa=$2 AND carrera=$3 AND materia=$4",
            updates,
        )

    return {
        "actualizados": len(updates),
        "detalle": [{"de": u[3], "a": u[0]} for u in updates],
    }


async def buscar_alumno(q: str) -> list[dict]:
    """Busca alumnos por cédula, nombre o correo en todas las fuentes.

    Une el historial académico (planillas) con el directorio sincronizado de
    Canvas, para que aparezcan también los alumnos que todavía no tienen
    historial cargado. Cada resultado indica de dónde salió.
    """
    like = f"%{q}%"

    por_cedula: dict[str, dict] = {}

    # 1) Historial académico (planillas)
    try:
        filas = await db.fetch(
            """SELECT cedula, MAX(nombre) AS nombre, MAX(carrera) AS carrera,
                      MAX(programa) AS programa
               FROM historial_academico
               WHERE (cedula ILIKE ? OR nombre ILIKE ?)
                 AND cedula ~ '^[0-9]+$'
               GROUP BY cedula
               ORDER BY MAX(nombre)
               LIMIT 40""",
            like, like,
        )
        for r in filas:
            por_cedula[r["cedula"]] = {**r, "email": "", "en_historial": True, "en_canvas": False}
    except Exception as exc:
        logger.warning("Búsqueda en historial falló: %s", exc)

    # 2) Directorio sincronizado de Canvas (incluye correo)
    try:
        filas = await db.fetch(
            """SELECT sis_user_id AS cedula, nombre, email, login_id
               FROM sync_alumnos
               WHERE sis_user_id ILIKE ? OR nombre ILIKE ?
                  OR email ILIKE ? OR login_id ILIKE ?
               ORDER BY nombre
               LIMIT 40""",
            like, like, like, like,
        )
        for r in filas:
            ced = (r["cedula"] or "").strip()
            correo = r["email"] or r["login_id"] or ""
            if ced and ced in por_cedula:
                por_cedula[ced]["en_canvas"] = True
                por_cedula[ced]["email"] = por_cedula[ced]["email"] or correo
            else:
                clave = ced or correo or r["nombre"]
                por_cedula.setdefault(clave, {
                    "cedula": ced, "nombre": r["nombre"], "carrera": None,
                    "programa": None, "email": correo,
                    "en_historial": False, "en_canvas": True,
                })
    except Exception as exc:
        logger.warning("Búsqueda en directorio Canvas falló: %s", exc)

    resultados = sorted(por_cedula.values(), key=lambda a: (a.get("nombre") or "").upper())
    return resultados[:30]


async def ficha_alumno(cedula: str) -> dict:
    """Vista 360 de la vida académica de un alumno: datos, resumen de avance,
    historial, qué puede cursar y su actividad en Canvas, todo en una consulta."""
    historial = await historial_alumno(cedula)
    estado = await estado_inscripcion(cedula)

    datos = await db.fetchrow(
        """SELECT MAX(nombre) AS nombre, MAX(carrera) AS carrera, MAX(programa) AS programa
           FROM historial_academico WHERE cedula = ?""",
        cedula,
    ) or {}

    # Si todavía no tiene historial cargado, al menos mostramos su identidad
    # desde el directorio sincronizado de Canvas.
    if not datos.get("nombre"):
        try:
            desde_canvas = await db.fetchrow(
                """SELECT nombre FROM sync_alumnos
                   WHERE sis_user_id = ? OR login_id = ? OR email = ? LIMIT 1""",
                cedula, cedula, cedula,
            )
            if desde_canvas:
                datos = {**datos, "nombre": desde_canvas["nombre"]}
        except Exception as exc:
            logger.warning("No se pudo completar datos desde Canvas: %s", exc)

    materias = estado.get("materias", [])
    cuenta = {"aprobada": 0, "reprobada": 0, "cursando": 0, "bloqueada": 0, "puede_inscribir": 0}
    for m in materias:
        if m["estado"] in cuenta:
            cuenta[m["estado"]] += 1
    total = len(materias)
    avance = round(cuenta["aprobada"] / total * 100) if total else 0

    notas = [float(r["nota_num"]) for r in historial
             if r.get("nota_num") is not None and r.get("aprobado") is True]
    promedio = round(sum(notas) / len(notas), 2) if notas else None

    # Materias reprobadas que son prerrequisito de otras: traban el avance
    bloqueantes = []
    reprobadas = {m["materia"].lower() for m in materias if m["estado"] == "reprobada"}
    if reprobadas:
        for m in materias:
            for falta in (m.get("faltantes") or []):
                if falta.lower() in reprobadas:
                    bloqueantes.append({"reprobada": falta, "traba": m["materia"]})

    return {
        "cedula": cedula,
        "nombre": datos.get("nombre") or "",
        "carrera": estado.get("carrera") or datos.get("carrera") or "",
        "programa": estado.get("programa") or datos.get("programa") or "",
        "resumen": {
            "total_materias": total,
            "avance_pct": avance,
            "promedio": promedio,
            **cuenta,
        },
        "materias": materias,
        "historial": historial,
        "bloqueantes": bloqueantes,
    }
