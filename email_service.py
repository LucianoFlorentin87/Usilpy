import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import get_settings

settings = get_settings()


def send_welcome_email(to_address: str, name: str, temp_password: str) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Bienvenido/a a la plataforma académica"
    msg["From"] = settings.smtp_user
    msg["To"] = to_address

    body = f"""
    <html><body>
    <p>Hola <strong>{name}</strong>,</p>
    <p>Tu cuenta ha sido creada exitosamente.</p>
    <p>Contraseña temporal: <strong>{temp_password}</strong></p>
    <p>Por favor cambia tu contraseña en el primer inicio de sesión.</p>
    </body></html>
    """
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, to_address, msg.as_string())
        return True
    except Exception:
        return False


def send_bulk_report_email(to_address: str, report: dict) -> bool:
    total = report.get("total", 0)
    success = report.get("success", 0)
    errors = report.get("errors", 0)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Reporte Carga Masiva — {success}/{total} exitosos"
    msg["From"] = settings.smtp_user
    msg["To"] = to_address

    body = f"""
    <html><body>
    <p>El proceso de carga masiva ha finalizado.</p>
    <ul>
      <li>Total filas: {total}</li>
      <li>Exitosos: {success}</li>
      <li>Errores: {errors}</li>
    </ul>
    </body></html>
    """
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, to_address, msg.as_string())
        return True
    except Exception:
        return False
