"""
APScheduler integration for FastAPI.
Starts an AsyncIOScheduler that runs the enrollment cron daily at CRON_HORA.
Import `lifespan` and pass it to FastAPI(..., lifespan=lifespan).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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


def _setup_jobs() -> None:
    cron_hora = getattr(settings, "cron_hora", "07:00") or "07:00"
    try:
        hora, minuto = cron_hora.split(":")
        hora_int, minuto_int = int(hora), int(minuto)
    except (ValueError, AttributeError):
        logger.warning("CRON_HORA '%s' inválido, usando 07:00", cron_hora)
        hora_int, minuto_int = 7, 0

    trigger = CronTrigger(hour=hora_int, minute=minuto_int, timezone="UTC")
    scheduler.add_job(
        _cron_job,
        trigger=trigger,
        id="matriculacion_diaria",
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    logger.info("Cron programado para las %02d:%02d UTC diariamente", hora_int, minuto_int)


def get_next_run() -> str | None:
    job = scheduler.get_job("matriculacion_diaria")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    # Init audit DB + users table
    await audit_service.init_db()
    # Seed initial admin from .env if no users exist yet
    if settings.admin_password_hash:
        await user_service.seed_admin(settings.admin_username, settings.admin_password_hash)

    # Configure and start scheduler
    _setup_jobs()
    scheduler.start()
    logger.info("Scheduler iniciado")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("Scheduler detenido")
