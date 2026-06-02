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

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
