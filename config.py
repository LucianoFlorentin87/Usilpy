from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Canvas LMS
    canvas_base_url: str = ""
    canvas_api_token: str = ""

    # Azure AD / Microsoft Graph
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""

    # Teams
    teams_default_team_id: str = ""
    teams_base_url: str = "https://teams.microsoft.com"
    teams_webhook_url: str = ""
    teams_owner_upn: str = ""   # UPN del admin owner de equipos (ej: admin@usil.edu.py)

    # OneDrive (Excel de matriculación)
    onedrive_file_id: str = ""
    onedrive_drive_id: str = ""   # optional; required for app-only auth

    # Email
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    admin_email: str = ""
    email_sender: str = ""        # UPN de la cuenta que envía emails via Graph (ej: noreply@usil.edu.py)

    # Webhook (para integración con sistema académico externo)
    webhook_api_key: str = ""   # key estática que el sistema académico envía en X-API-Key

    # App
    app_secret_key: str = "change_this_in_production"
    allowed_origins: str = ""   # comma-separated, e.g. "https://myapp.onrender.com,http://localhost:8000"
    debug: bool = False

    # Auth
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 horas
    admin_username: str = "admin"
    admin_password_hash: str = ""
    azure_redirect_uri: str = "http://localhost:8000/api/auth/azure/callback"

    # Matriculación automática
    semestre_actual: str = "2025-2"
    cron_hora: str = "07:00"   # HH:MM UTC

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


def get_settings() -> Settings:
    return Settings()


def validate_settings(s: Settings) -> None:
    """Raise on insecure startup configuration."""
    if s.app_secret_key == "change_this_in_production" and not s.debug:
        raise RuntimeError(
            "APP_SECRET_KEY must be set to a strong random value before deploying. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
