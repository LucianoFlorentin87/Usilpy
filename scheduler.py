"""
APScheduler integration for FastAPI.
Starts an AsyncIOScheduler that runs the enrollment cron daily at CRON_HORA.
Import `lifespan` and pass it to FastAPI(..., lifespan=lifespan).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import FastAPI

import audit_service
import user_service
import matriculacion_service
import teams_notify_service
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler(timezone="UTC")


async def _cron_job() -> None:
    logger.info("Cron matriculación iniciando: %s", datetime.now(timezone.utc).isoformat())
    try:
        summary = await matriculacion_service.run_matriculacion(dry_run=False)
        logger.info(
            "Cron completado — total=%d creados=%d errores=%d",
            summary.get("total", 0),
            summary.get("creados", 0),
            summary.get("errores", 0),
        )
    except Exception as exc:
        logger.exception("Error en cron de matriculación: %s", exc)
        await teams_notify_service.send_matriculacion_summary({
            "tipo": "cron",
            "estado": "fallido",
            "error_global": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "semestre": settings.semestre_actual,
            "total": 0, "creados": 0, "existentes": 0, "inscripciones": 0, "errores": 1,
        })


async def _sync_job() -> None:
    """Sincronización diaria Canvas→BD (cursos, alumnos, matrículas, notas, asistencias)."""
    import sync_service
    logger.info("Sync Canvas iniciando: %s", datetime.now(timezone.utc).isoformat())
    try:
        await sync_service.init_db()
        stats = await sync_service.run_sync("cron")
        logger.info("Sync Canvas completado — %s", stats)
    except Exception as exc:
        logger.exception("Error en sync diario de Canvas: %s", exc)
    # Sincronizar también el directorio 365/Teams
    try:
        stats365 = await sync_service.run_sync_365()
        logger.info("Sync 365 completado — %s", stats365)
    except Exception as exc:
        logger.exception("Error en sync diario de 365: %s", exc)


async def _programar_sync_si_vencida(horas: int = 20) -> None:
    """Recupera la sincronización cuando la tarea nocturna no llegó a correr.

    En hospedajes que apagan la instancia por inactividad, el horario programado
    puede pasar con el proceso dormido y la sincronización no ejecutarse nunca.
    Al arrancar comprobamos la antigüedad de los datos y, si están vencidos,
    programamos la sincronización unos minutos más tarde — el tiempo suficiente
    para no competir con la primera pantalla que abre el usuario.
    """
    import db as _db
    try:
        ultima = await _db.fetchval("SELECT MAX(completado_en) FROM sync_log")
    except Exception as exc:
        logger.warning("No se pudo consultar la última sincronización: %s", exc)
        return

    ahora = datetime.now(timezone.utc)
    if ultima is not None:
        if ultima.tzinfo is None:
            ultima = ultima.replace(tzinfo=timezone.utc)
        antiguedad = ahora - ultima
        if antiguedad < timedelta(hours=horas):
            logger.info("Datos al día (última sincronización hace %.1f h)",
                        antiguedad.total_seconds() / 3600)
            return
        logger.info("Datos vencidos: última sincronización hace %.1f h", 
                    antiguedad.total_seconds() / 3600)
    else:
        logger.info("Sin sincronizaciones previas registradas")

    cuando = ahora + timedelta(minutes=3)
    scheduler.add_job(
        _sync_job,
        trigger=DateTrigger(run_date=cuando),
        id="sync_recuperacion",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Sincronización de recuperación programada para %s UTC",
                cuando.strftime("%H:%M:%S"))


def _parse_hora(valor: str, defecto: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(valor).split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        logger.warning("Hora '%s' inválida, usando %02d:%02d", valor, *defecto)
        return defecto


def _setup_jobs() -> None:
    hora_int, minuto_int = _parse_hora(getattr(settings, "cron_hora", "07:00") or "07:00", (7, 0))
    trigger = CronTrigger(hour=hora_int, minute=minuto_int, timezone="UTC")
    scheduler.add_job(
        _cron_job,
        trigger=trigger,
        id="matriculacion_diaria",
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    logger.info("Cron matriculación programado para las %02d:%02d UTC diariamente", hora_int, minuto_int)

    # Sincronización diaria Canvas→BD
    s_hora, s_min = _parse_hora(getattr(settings, "sync_hora", "05:00") or "05:00", (5, 0))
    scheduler.add_job(
        _sync_job,
        trigger=CronTrigger(hour=s_hora, minute=s_min, timezone="UTC"),
        id="sync_canvas_diaria",
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    logger.info("Sync Canvas programado para las %02d:%02d UTC diariamente", s_hora, s_min)


def get_next_run() -> str | None:
    job = scheduler.get_job("matriculacion_diaria")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def get_next_sync() -> str | None:
    job = scheduler.get_job("sync_canvas_diaria")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


async def _enable_rls() -> None:
    """Activa Row Level Security en todas las tablas del esquema public.

    Supabase expone las tablas de `public` por su API REST (PostgREST), donde
    alcanza la clave `anon` — que no es secreta — para leerlas y escribirlas.
    Con RLS activado y sin políticas, esa puerta queda cerrada.

    No afecta a la aplicación: nos conectamos con el rol `postgres`, dueño de
    las tablas, y los dueños omiten RLS.
    """
    import db as _db
    await _db.execute("""
        DO $$
        DECLARE t record;
        BEGIN
            FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
            LOOP
                EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t.tablename);
            END LOOP;
        END $$;
    """)
    pendientes = await _db.fetchval(
        "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity"
    )
    logger.info("RLS verificado en esquema public — tablas sin RLS: %s", pendientes)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    # Init DB pool first, then tables
    import db as _db
    from config import validate_settings
    validate_settings(settings)

    # La app NO debe morir si la BD no responde al arrancar (p.ej. caída
    # transitoria del pooler de Supabase). Se levanta igual y reconecta sola
    # en el primer pedido que necesite la base.
    try:
        await _db.init_pool()
        await audit_service.init_db()
        await _db.execute("""
            CREATE TABLE IF NOT EXISTS cursos (
                id SERIAL PRIMARY KEY,
                materia TEXT NOT NULL,
                periodo TEXT NOT NULL,
                canvas_id TEXT DEFAULT '',
                teams_id TEXT DEFAULT '',
                canvas_status TEXT DEFAULT '',
                teams_status TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (materia, periodo)
            )
        """)
        # Seed initial admin from .env if no users exist yet
        if settings.admin_password_hash:
            await user_service.seed_admin(settings.admin_username, settings.admin_password_hash)
        # Tablas del módulo académico (historial y mallas)
        import academic_service
        await academic_service.init_db()
        # Tablas de sincronización con Canvas y 365
        import sync_service as _sync
        await _sync.init_db()
        # Cerrar el acceso anónimo por la API REST de Supabase
        try:
            await _enable_rls()
        except Exception as rls_exc:
            logger.warning("No se pudo activar RLS automáticamente: %s", rls_exc)
    except Exception as exc:
        logger.error(
            "No se pudo inicializar la BD al arrancar (%s). La aplicación inicia "
            "igualmente y reintentará conectarse en el primer pedido.", exc
        )

    # Configure and start scheduler
    _setup_jobs()
    scheduler.start()
    logger.info("Scheduler iniciado")

    # Si la tarea nocturna no llegó a correr (instancia dormida), recuperarla
    try:
        await _programar_sync_si_vencida()
    except Exception as exc:
        logger.warning("No se pudo programar la sincronización de recuperación: %s", exc)

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("Scheduler detenido")
