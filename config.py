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

    # OneDrive (Excel de matriculación)
    onedrive_file_id: str = ""
    onedrive_drive_id: str = ""   # optional; required for app-only auth

    # Email
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    admin_email: str = ""

    # App
    app_secret_key: str = "change_this_in_production"
    debug: bool = False

    # Auth
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480
    admin_username: str = "admin"
    admin_password_hash: str = ""
    azure_redirect_uri: str = "http://localhost:8000/api/auth/azure/callback"

    # Matriculación automática
    semestre_actual: str = "2025-2"
    cron_hora: str = "07:00"   # HH:MM UTC

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
