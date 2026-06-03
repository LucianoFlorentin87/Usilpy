"""
Send notification cards to a Microsoft Teams channel via an incoming webhook.
Supports both the legacy O365 connector card format and the new workflow webhook format.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _build_card(summary: dict) -> dict:
    semestre = summary.get("semestre", "—")
    ts = summary.get("timestamp", datetime.now(timezone.utc).isoformat())
    total = summary.get("total", 0)
    creados = summary.get("creados", 0)
    existentes = summary.get("existentes", 0)
    inscripciones = summary.get("inscripciones", 0)
    errores = summary.get("errores", 0)
    tipo = summary.get("tipo", "manual").upper()
    estado = summary.get("estado", "exitoso")
    color = "00B050" if estado == "exitoso" else "FF0000"

    facts = [
        {"name": "Semestre", "value": semestre},
        {"name": "Total alumnos procesados", "value": str(total)},
        {"name": "Alumnos nuevos creados", "value": str(creados)},
        {"name": "Alumnos existentes matriculados", "value": str(existentes)},
        {"name": "Inscripciones realizadas", "value": str(inscripciones)},
        {"name": "Errores", "value": str(errores)},
    ]

    if errores and summary.get("detalles_errores"):
        facts.append({
            "name": "Detalle de errores",
            "value": "; ".join(str(e) for e in summary["detalles_errores"][:5]),
        })

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": f"Matriculación Automática — {estado}",
        "sections": [
            {
                "activityTitle": f"📋 Reporte de Matriculación — {tipo}",
                "activitySubtitle": f"Ejecución: {ts[:19].replace('T', ' ')} UTC",
                "activityImage": "https://img.icons8.com/color/48/000000/graduation-cap--v1.png",
                "facts": facts,
                "markdown": True,
            }
        ],
    }
    return card


async def send_matriculacion_summary(summary: dict) -> bool:
    webhook_url = settings.teams_webhook_url
    if not webhook_url:
        logger.warning("TEAMS_WEBHOOK_URL no configurado, notificación omitida")
        return False

    card = _build_card(summary)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=card)
            if resp.status_code not in (200, 202):
                logger.error("Teams webhook respondió %s: %s", resp.status_code, resp.text[:200])
                return False
        logger.info("Notificación Teams enviada correctamente")
        return True
    except Exception as exc:
        logger.exception("Error enviando notificación a Teams: %s", exc)
        return False


async def send_validation_error_alert(errors: list[dict]) -> bool:
    """Notify Teams that validation failed and no processing was done."""
    webhook_url = settings.teams_webhook_url
    if not webhook_url:
        return False

    facts = [{"name": e.get("sheet_name", "?"), "value": e.get("error", "")} for e in errors[:10]]

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "FF0000",
        "summary": "Error de validación en Excel de matriculación",
        "sections": [
            {
                "activityTitle": "⚠️ Validación fallida — Excel de Matriculación",
                "activitySubtitle": "El proceso NO fue ejecutado. Corrija los errores y vuelva a intentarlo.",
                "facts": facts,
                "markdown": True,
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=card)
            return resp.status_code in (200, 202)
    except Exception as exc:
        logger.exception("Error enviando alerta Teams: %s", exc)
        return False
