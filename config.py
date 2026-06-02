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

    # Email
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # App
    app_secret_key: str = "change_this_in_production"
    debug: bool = False

    # Auth
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480  # 8 horas
    # Usuario admin local por defecto (cambiar en .env)
    admin_username: str = "admin"
    admin_password_hash: str = ""  # generado con passlib, vacío = sin login local
    # Azure AD OAuth2 redirect
    azure_redirect_uri: str = "http://localhost:8000/api/auth/azure/callback"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
