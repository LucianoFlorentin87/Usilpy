"""
Async email service using aiosmtplib.
Three template types:
  1. Welcome (new student): credentials + Canvas/Teams links
  2. Enrollment confirmation (existing student): list of enrolled courses
  3. Admin error report: error details for manual correction
"""
from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_BRAND_COLOR = "#1E3A5F"
_ACCENT = "#2D6CDF"


def _base_html(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:system-ui,-apple-system,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
        <!-- Header -->
        <tr>
          <td style="background:{_BRAND_COLOR};padding:28px 32px;">
            <p style="margin:0;color:#fff;font-size:1.3rem;font-weight:700;">USIL · Gestión Académica</p>
            <p style="margin:4px 0 0;color:#8ca0bf;font-size:0.82rem;">Universidad · Canvas LMS · Microsoft Teams</p>
          </td>
        </tr>
        <!-- Body -->
        <tr><td style="padding:32px;">{content}</td></tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e0e4ea;">
            <p style="margin:0;color:#6b7280;font-size:0.75rem;text-align:center;">
              Este correo fue generado automáticamente por el sistema de gestión académica de USIL.
              Por favor no responda este mensaje.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


async def _send(to: str, subject: str, html: str) -> bool:
    # ── Transporte 1: Microsoft Graph API (preferido) ─────────────────────────
    if settings.email_sender:
        try:
            import httpx, msal
            app = msal.ConfidentialClientApplication(
                settings.azure_client_id,
                authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
                client_credential=settings.azure_client_secret,
            )
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" not in result:
                raise RuntimeError(result.get("error_description", "Token error"))
            headers = {
                "Authorization": f"Bearer {result['access_token']}",
                "Content-Type": "application/json",
            }
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": False,
            }
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://graph.microsoft.com/v1.0/users/{settings.email_sender}/sendMail",
                    headers=headers,
                    json=payload,
                )
            if resp.status_code == 202:
                logger.info("Email (Graph) enviado a %s — %s", to, subject)
                return True
            logger.warning("Graph sendMail HTTP %d para %s: %s", resp.status_code, to, resp.text[:200])
            return False
        except Exception as exc:
            logger.error("Error Graph email a %s: %s", to, exc)
            return False

    # ── Transporte 2: SMTP (fallback) ─────────────────────────────────────────
    if not settings.smtp_user:
        logger.warning("Email omitido (sin EMAIL_SENDER ni SMTP_USER): %s", to)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=True,
        )
        logger.info("Email (SMTP) enviado a %s — %s", to, subject)
        return True
    except Exception as exc:
        logger.error("Error SMTP email a %s: %s", to, exc)
        return False


# ── Template 1: Welcome (new student) ─────────────────────────────────────────

async def send_welcome_email(
    to: str,
    nombre: str,
    password: str,
    canvas_url: str,
    teams_url: str,
    cursos: list[str],
    semestre: str,
) -> bool:
    cursos_html = "".join(
        f'<li style="padding:4px 0;border-bottom:1px solid #f0f2f5;">{c}</li>' for c in cursos
    ) if cursos else "<li>Sin cursos asignados</li>"

    content = f"""
    <h2 style="margin:0 0 6px;color:{_BRAND_COLOR};font-size:1.2rem;">¡Bienvenido/a, {nombre}!</h2>
    <p style="color:#6b7280;margin:0 0 24px;font-size:0.88rem;">Semestre: <strong>{semestre}</strong></p>

    <p style="color:#1a2332;font-size:0.9rem;margin:0 0 20px;">
      Tu cuenta ha sido creada exitosamente en el sistema académico de USIL.
      A continuación encontrás tus credenciales de acceso:
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;margin-bottom:24px;">
      <tr>
        <td style="padding:20px 24px;">
          <p style="margin:0 0 10px;font-size:0.82rem;font-weight:600;color:#1e40af;text-transform:uppercase;letter-spacing:.05em;">
            Credenciales de acceso
          </p>
          <table>
            <tr>
              <td style="padding:4px 16px 4px 0;color:#6b7280;font-size:0.88rem;">Usuario / Email:</td>
              <td style="padding:4px 0;font-weight:600;color:#1a2332;font-size:0.88rem;">{to}</td>
            </tr>
            <tr>
              <td style="padding:4px 16px 4px 0;color:#6b7280;font-size:0.88rem;">Contraseña temporal:</td>
              <td style="padding:4px 0;font-weight:700;color:{_ACCENT};font-family:monospace;font-size:1rem;">{password}</td>
            </tr>
          </table>
          <p style="margin:12px 0 0;font-size:0.78rem;color:#6b7280;">
            ⚠️ Cambiá tu contraseña en el primer inicio de sesión.
          </p>
        </td>
      </tr>
    </table>

    <p style="color:#1a2332;font-size:0.9rem;margin:0 0 12px;font-weight:600;">
      Materias inscriptas — {semestre}:
    </p>
    <ul style="margin:0 0 24px;padding:0 0 0 18px;color:#374151;font-size:0.88rem;">
      {cursos_html}
    </ul>

    <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td style="padding-right:12px;">
          <a href="{canvas_url}"
             style="display:inline-block;background:{_ACCENT};color:#fff;text-decoration:none;
                    padding:10px 20px;border-radius:6px;font-weight:600;font-size:0.88rem;">
            📚 Acceder a Canvas LMS
          </a>
        </td>
        <td>
          <a href="{teams_url}"
             style="display:inline-block;background:#6264a7;color:#fff;text-decoration:none;
                    padding:10px 20px;border-radius:6px;font-weight:600;font-size:0.88rem;">
            💬 Acceder a Microsoft Teams
          </a>
        </td>
      </tr>
    </table>

    <p style="color:#6b7280;font-size:0.82rem;">
      Si tenés algún inconveniente para acceder, contactá a soporte técnico respondiendo este correo
      o escribiendo a <a href="mailto:{settings.admin_email or settings.smtp_user}"
      style="color:{_ACCENT};">{settings.admin_email or settings.smtp_user}</a>.
    </p>
    """

    html = _base_html(f"Bienvenido/a al sistema académico — {nombre}", content)
    return await _send(to, f"Bienvenido/a a USIL — Semestre {semestre}", html)


# ── Template 2: Enrollment confirmation ──────────────────────────────────────

async def send_enrollment_confirmation(
    to: str,
    nombre: str,
    cursos: list[str],
    semestre: str,
) -> bool:
    cursos_html = "".join(
        f"""<tr style="border-bottom:1px solid #f0f2f5;">
              <td style="padding:8px 12px;font-size:0.88rem;color:#374151;">✅ {c}</td>
            </tr>"""
        for c in cursos
    ) if cursos else '<tr><td style="padding:8px 12px;color:#6b7280;">Sin cursos nuevos</td></tr>'

    content = f"""
    <h2 style="margin:0 0 6px;color:{_BRAND_COLOR};font-size:1.2rem;">Hola, {nombre.split()[0]}!</h2>
    <p style="color:#6b7280;margin:0 0 24px;font-size:0.88rem;">Semestre: <strong>{semestre}</strong></p>

    <p style="color:#1a2332;font-size:0.9rem;margin:0 0 20px;">
      Tu inscripción en las siguientes materias del semestre <strong>{semestre}</strong>
      fue procesada exitosamente:
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e0e4ea;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <thead>
        <tr style="background:#f8fafc;">
          <th style="padding:10px 12px;font-size:0.82rem;color:#6b7280;text-align:left;font-weight:600;">
            Materia
          </th>
        </tr>
      </thead>
      <tbody>
        {cursos_html}
      </tbody>
    </table>

    <p style="color:#6b7280;font-size:0.82rem;">
      Podés acceder a tus materias desde
      <a href="{settings.canvas_base_url}" style="color:{_ACCENT};">Canvas LMS</a>
      o desde
      <a href="{settings.teams_base_url}" style="color:#6264a7;">Microsoft Teams</a>.
    </p>
    """

    html = _base_html(f"Inscripción confirmada — {semestre}", content)
    return await _send(to, f"Inscripción confirmada — Semestre {semestre}", html)


# ── Template 3: Admin error report ────────────────────────────────────────────

async def send_admin_error_report(
    to: str,
    semestre: str,
    errores: list[dict],
    tipo: str = "Errores en proceso de matriculación",
) -> bool:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows_html = ""
    for i, e in enumerate(errores[:50], 1):
        sheet = e.get("sheet_name", e.get("cedula", ""))
        cedula = e.get("cedula", "")
        nombre = e.get("nombre", "")
        error = e.get("error", str(e))
        bg = "#fff" if i % 2 == 0 else "#f8fafc"
        rows_html += f"""<tr style="background:{bg};">
          <td style="padding:8px 12px;font-size:0.82rem;color:#6b7280;">{sheet or cedula}</td>
          <td style="padding:8px 12px;font-size:0.82rem;color:#374151;">{nombre}</td>
          <td style="padding:8px 12px;font-size:0.82rem;color:#991b1b;">{error}</td>
        </tr>"""

    if len(errores) > 50:
        rows_html += f"""<tr><td colspan="3" style="padding:8px 12px;font-size:0.82rem;color:#6b7280;font-style:italic;">
          … y {len(errores) - 50} error(es) más. Revisar log completo en el sistema.
        </td></tr>"""

    content = f"""
    <h2 style="margin:0 0 6px;color:#991b1b;font-size:1.2rem;">⚠️ {tipo}</h2>
    <p style="color:#6b7280;margin:0 0 24px;font-size:0.88rem;">
      Semestre: <strong>{semestre}</strong> · Generado: {ts}
    </p>

    <p style="color:#1a2332;font-size:0.9rem;margin:0 0 20px;">
      Se encontraron <strong>{len(errores)} error(es)</strong> durante el proceso de matriculación.
      Los siguientes registros requieren corrección manual:
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #fca5a5;border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <thead>
        <tr style="background:#fef2f2;">
          <th style="padding:10px 12px;font-size:0.82rem;color:#991b1b;text-align:left;font-weight:600;">Hoja / Cédula</th>
          <th style="padding:10px 12px;font-size:0.82rem;color:#991b1b;text-align:left;font-weight:600;">Nombre</th>
          <th style="padding:10px 12px;font-size:0.82rem;color:#991b1b;text-align:left;font-weight:600;">Error</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    <p style="color:#6b7280;font-size:0.82rem;">
      Iniciá sesión en el
      <a href="http://localhost:8000" style="color:{_ACCENT};">sistema de gestión académica</a>
      para revisar el log de auditoría completo y exportarlo a Excel.
    </p>
    """

    html = _base_html(f"Reporte de errores — {semestre}", content)
    return await _send(to, f"⚠️ Errores de matriculación — Semestre {semestre} ({len(errores)} errores)", html)


# ── Legacy sync wrappers (kept for backwards compat) ─────────────────────────

def send_welcome_email_sync(to_address: str, name: str, temp_password: str) -> bool:
    import asyncio
    return asyncio.get_event_loop().run_until_complete(
        send_welcome_email(to_address, name, temp_password, "", "", [], "")
    )


def send_bulk_report_email(to_address: str, report: dict) -> bool:
    import asyncio
    total = report.get("total", 0)
    success = report.get("success", 0)
    errors = report.get("errors", 0)
    errs = [{"error": f"Total: {total}, Exitosos: {success}, Errores: {errors}"}]
    return asyncio.get_event_loop().run_until_complete(
        send_admin_error_report(to_address, "", errs, "Reporte Carga Masiva")
    )
