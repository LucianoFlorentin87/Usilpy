"""
Servicio de sincronización Canvas → PostgreSQL.
Tablas: cursos, alumnos, matriculaciones, calificaciones, asistencias, sync_log.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

import db
import canvas_service

logger = logging.getLogger(__name__)


async def run_sync_365(incluir_grupos: bool = True) -> dict:
    """Sincroniza el directorio de Microsoft 365/Azure AD a sync_usuarios_365:
    email, UPN, estado activo/inactivo y (opcional) grupos y equipos de cada usuario."""
    import graph_service
    await init_db()
    stats = {"usuarios": 0, "con_grupos": 0, "errores": 0}
    try:
        usuarios = await graph_service.get_all_users()
    except Exception as exc:
        logger.error("Error listando usuarios 365: %s", exc)
        return {**stats, "error": str(exc)}

    # Traer grupos de todos los usuarios en paralelo (con límite de concurrencia)
    import asyncio
    grupos_por_usuario: dict = {}
    if incluir_grupos:
        sem = asyncio.Semaphore(8)

        async def _grupos(uid):
            async with sem:
                try:
                    grupos_por_usuario[uid] = await graph_service.get_user_groups(uid)
                except Exception as exc:
                    logger.debug("Grupos no disponibles para %s: %s", uid, exc)
                    grupos_por_usuario[uid] = None

        await asyncio.gather(*[_grupos(u["id"]) for u in usuarios if u.get("id")])

    for u in usuarios:
        azure_id = u.get("id")
        if not azure_id:
            continue
        grupos_txt, equipos_txt = None, None
        if incluir_grupos:
            gs = grupos_por_usuario.get(azure_id)
            if gs is None:
                stats["errores"] += 1
            else:
                grupos_txt = ", ".join(g["nombre"] for g in gs if g.get("nombre"))
                equipos_txt = ", ".join(g["nombre"] for g in gs if g.get("es_team") and g.get("nombre"))
                if gs:
                    stats["con_grupos"] += 1
        try:
            await db.execute("""
                INSERT INTO sync_usuarios_365
                    (azure_id, nombre, email, upn, activo, grupos, equipos, ultima_sync)
                VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
                ON CONFLICT (azure_id) DO UPDATE SET
                    nombre = EXCLUDED.nombre, email = EXCLUDED.email,
                    upn = EXCLUDED.upn, activo = EXCLUDED.activo,
                    grupos = EXCLUDED.grupos, equipos = EXCLUDED.equipos,
                    ultima_sync = NOW()
            """,
                azure_id, u.get("displayName"),
                u.get("mail") or u.get("userPrincipalName"),
                u.get("userPrincipalName"), u.get("accountEnabled"),
                grupos_txt, equipos_txt,
            )
            stats["usuarios"] += 1
        except Exception as exc:
            logger.warning("Error guardando usuario 365 %s: %s", azure_id, exc)
            stats["errores"] += 1

    logger.info("Sync 365 completado — %s", stats)
    return stats

# ---------------------------------------------------------------------------
# DDL — tablas
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS sync_cursos (
    canvas_course_id    BIGINT PRIMARY KEY,
    nombre              TEXT NOT NULL,
    sis_course_id       TEXT,
    semestre            TEXT,
    programa            TEXT,
    estado              TEXT,
    teams_group_id      TEXT,
    total_alumnos       INT DEFAULT 0,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sync_alumnos (
    canvas_user_id      BIGINT PRIMARY KEY,
    nombre              TEXT,
    email               TEXT,
    sis_user_id         TEXT,
    login_id            TEXT,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sync_matriculaciones (
    id                  BIGSERIAL PRIMARY KEY,
    canvas_course_id    BIGINT NOT NULL REFERENCES sync_cursos(canvas_course_id) ON DELETE CASCADE,
    canvas_user_id      BIGINT NOT NULL REFERENCES sync_alumnos(canvas_user_id) ON DELETE CASCADE,
    estado              TEXT,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(canvas_course_id, canvas_user_id)
);

CREATE TABLE IF NOT EXISTS sync_calificaciones (
    id                  BIGSERIAL PRIMARY KEY,
    canvas_course_id    BIGINT NOT NULL,
    canvas_user_id      BIGINT NOT NULL,
    nota_actual         NUMERIC(5,2),
    nota_final          NUMERIC(5,2),
    letra_actual        TEXT,
    letra_final         TEXT,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(canvas_course_id, canvas_user_id)
);

CREATE TABLE IF NOT EXISTS sync_asistencias (
    id                  BIGSERIAL PRIMARY KEY,
    canvas_course_id    BIGINT NOT NULL,
    canvas_user_id      BIGINT NOT NULL,
    fecha_clase         DATE,
    estado              TEXT,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(canvas_course_id, canvas_user_id, fecha_clase)
);

CREATE TABLE IF NOT EXISTS sync_usuarios_365 (
    azure_id            TEXT PRIMARY KEY,
    nombre              TEXT,
    email               TEXT,
    upn                 TEXT,
    activo              BOOLEAN,
    grupos              TEXT,
    equipos             TEXT,
    ultima_sync         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                  BIGSERIAL PRIMARY KEY,
    tipo                TEXT NOT NULL,
    cursos_procesados   INT DEFAULT 0,
    alumnos_procesados  INT DEFAULT 0,
    matriculas_sync     INT DEFAULT 0,
    calificaciones_sync INT DEFAULT 0,
    asistencias_sync    INT DEFAULT 0,
    errores             INT DEFAULT 0,
    duracion_seg        NUMERIC(8,2),
    iniciado_en         TIMESTAMPTZ DEFAULT NOW(),
    completado_en       TIMESTAMPTZ
);
"""


async def init_db() -> None:
    for stmt in _DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


# ---------------------------------------------------------------------------
# Sync principal
# ---------------------------------------------------------------------------

async def run_sync(tipo: str = "manual") -> dict:
    await init_db()
    inicio = datetime.now(timezone.utc)
    stats = {
        "cursos": 0, "alumnos": 0, "matriculas": 0,
        "calificaciones": 0, "asistencias": 0, "errores": 0,
    }

    log_id = await db.fetchval(
        "INSERT INTO sync_log (tipo) VALUES (?) RETURNING id", tipo
    )

    try:
        courses = await canvas_service.get_all_courses()
        logger.info("Sync: %d cursos encontrados en Canvas", len(courses))

        for course in courses:
            cid = course.get("id")
            if not cid:
                continue
            try:
                await db.execute("""
                    INSERT INTO sync_cursos
                        (canvas_course_id, nombre, sis_course_id, semestre, estado, total_alumnos, ultima_sync)
                    VALUES (?, ?, ?, ?, ?, ?, NOW())
                    ON CONFLICT (canvas_course_id) DO UPDATE SET
                        nombre        = EXCLUDED.nombre,
                        sis_course_id = EXCLUDED.sis_course_id,
                        semestre      = EXCLUDED.semestre,
                        estado        = EXCLUDED.estado,
                        total_alumnos = EXCLUDED.total_alumnos,
                        ultima_sync   = NOW()
                """,
                    cid,
                    course.get("name", ""),
                    course.get("sis_course_id") or "",
                    (course.get("term") or {}).get("name") or "",
                    course.get("workflow_state", ""),
                    course.get("total_students") or 0,
                )
                stats["cursos"] += 1
            except Exception as exc:
                logger.warning("Error sync curso %s: %s", cid, exc)
                stats["errores"] += 1
                continue

            # Matriculaciones + alumnos + notas
            try:
                enrollments = await canvas_service.get_course_enrollments(cid)
                for e in enrollments:
                    uid = e.get("user_id")
                    if not uid:
                        continue
                    user = e.get("user") or {}
                    # Upsert alumno
                    await db.execute("""
                        INSERT INTO sync_alumnos
                            (canvas_user_id, nombre, email, sis_user_id, login_id, ultima_sync)
                        VALUES (?, ?, ?, ?, ?, NOW())
                        ON CONFLICT (canvas_user_id) DO UPDATE SET
                            nombre      = EXCLUDED.nombre,
                            email       = EXCLUDED.email,
                            sis_user_id = EXCLUDED.sis_user_id,
                            login_id    = EXCLUDED.login_id,
                            ultima_sync = NOW()
                    """,
                        uid,
                        user.get("name", ""),
                        user.get("login_id", ""),
                        e.get("sis_user_id") or user.get("sis_user_id") or "",
                        user.get("login_id", ""),
                    )
                    stats["alumnos"] += 1

                    # Matriculación
                    await db.execute("""
                        INSERT INTO sync_matriculaciones (canvas_course_id, canvas_user_id, estado, ultima_sync)
                        VALUES (?, ?, ?, NOW())
                        ON CONFLICT (canvas_course_id, canvas_user_id) DO UPDATE SET
                            estado = EXCLUDED.estado, ultima_sync = NOW()
                    """, cid, uid, e.get("enrollment_state", ""))
                    stats["matriculas"] += 1

                    # Calificación
                    grades = e.get("grades") or {}
                    if grades:
                        nota_actual = grades.get("current_score")
                        nota_final  = grades.get("final_score")
                        if nota_actual is not None or nota_final is not None:
                            await db.execute("""
                                INSERT INTO sync_calificaciones
                                    (canvas_course_id, canvas_user_id, nota_actual, nota_final,
                                     letra_actual, letra_final, ultima_sync)
                                VALUES (?, ?, ?, ?, ?, ?, NOW())
                                ON CONFLICT (canvas_course_id, canvas_user_id) DO UPDATE SET
                                    nota_actual  = EXCLUDED.nota_actual,
                                    nota_final   = EXCLUDED.nota_final,
                                    letra_actual = EXCLUDED.letra_actual,
                                    letra_final  = EXCLUDED.letra_final,
                                    ultima_sync  = NOW()
                            """,
                                cid, uid,
                                nota_actual, nota_final,
                                grades.get("current_grade"), grades.get("final_grade"),
                            )
                            stats["calificaciones"] += 1

            except Exception as exc:
                logger.warning("Error sync enrollments curso %s: %s", cid, exc)
                stats["errores"] += 1

            # Asistencias (Roll Call)
            try:
                attendances = await canvas_service.get_course_attendance(cid)
                for a in attendances:
                    uid = a.get("student_id") or a.get("user_id")
                    if not uid:
                        continue
                    fecha_raw = a.get("class_date") or a.get("date") or ""
                    try:
                        fecha = datetime.strptime(fecha_raw[:10], "%Y-%m-%d").date() if fecha_raw else None
                    except Exception:
                        fecha = None
                    if not fecha:
                        continue
                    await db.execute("""
                        INSERT INTO sync_asistencias
                            (canvas_course_id, canvas_user_id, fecha_clase, estado, ultima_sync)
                        VALUES (?, ?, ?, ?, NOW())
                        ON CONFLICT (canvas_course_id, canvas_user_id, fecha_clase) DO UPDATE SET
                            estado = EXCLUDED.estado, ultima_sync = NOW()
                    """, cid, uid, fecha, a.get("attendance") or a.get("status") or "")
                    stats["asistencias"] += 1
            except Exception as exc:
                logger.debug("Asistencias no disponibles para curso %s: %s", cid, exc)

    except Exception as exc:
        logger.error("Error global en sync: %s", exc)
        stats["errores"] += 1

    fin = datetime.now(timezone.utc)
    duracion = (fin - inicio).total_seconds()

    await db.execute("""
        UPDATE sync_log SET
            cursos_procesados   = ?,
            alumnos_procesados  = ?,
            matriculas_sync     = ?,
            calificaciones_sync = ?,
            asistencias_sync    = ?,
            errores             = ?,
            duracion_seg        = ?,
            completado_en       = NOW()
        WHERE id = ?
    """,
        stats["cursos"], stats["alumnos"], stats["matriculas"],
        stats["calificaciones"], stats["asistencias"],
        stats["errores"], duracion, log_id,
    )

    return {**stats, "duracion_seg": round(duracion, 1)}
