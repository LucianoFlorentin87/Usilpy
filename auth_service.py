"""
Autenticación JWT + Azure AD OAuth2.

Flujos soportados:
  1. Login local  → POST /api/auth/login  (usuario/contraseña)
  2. Azure SSO    → GET  /api/auth/azure/login  →  Azure  →  /api/auth/azure/callback
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import secrets

from jose import JWTError, jwt
import bcrypt
import httpx

from config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Raises JWTError si el token es inválido o expiró."""
    return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------------
# Login local
# ---------------------------------------------------------------------------

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def authenticate_local(username: str, password: str) -> Optional[dict]:
    """Devuelve el payload del usuario si las credenciales son válidas, None si no.
    Falls back to env-based admin if no DB users exist yet."""
    import asyncio
    import user_service

    async def _check_db() -> Optional[dict]:
        user = await user_service.get_user_by_username(username)
        if not user:
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        await user_service.update_last_login(user["id"])
        return {
            "sub": user["id"],
            "username": user["username"],
            "name": user["full_name"] or user["username"],
            "email": user["email"],
            "role": user["role"],
            "provider": "local",
        }

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _check_db())
                result = future.result(timeout=5)
        else:
            result = loop.run_until_complete(_check_db())
        if result:
            return result
    except Exception:
        pass

    # Fallback: env-based admin (used during initial setup before any DB user exists)
    if username == settings.admin_username and settings.admin_password_hash:
        if verify_password(password, settings.admin_password_hash):
            return {"sub": username, "username": username, "name": username, "role": "admin", "provider": "local"}
    return None


# ---------------------------------------------------------------------------
# Azure AD OAuth2
# ---------------------------------------------------------------------------

AZURE_AUTHORIZE_URL = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
)
AZURE_TOKEN_URL = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

# Estado CSRF en memoria (producción: usar Redis o similar)
_pending_states: set[str] = set()


def build_azure_login_url() -> str:
    state = secrets.token_urlsafe(16)
    _pending_states.add(state)
    params = {
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": settings.azure_redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
    }
    from urllib.parse import urlencode
    base = AZURE_AUTHORIZE_URL.format(tenant=settings.azure_tenant_id)
    return f"{base}?{urlencode(params)}"


async def exchange_azure_code(code: str, state: str) -> dict:
    """
    Intercambia el código por un token, obtiene el perfil del usuario
    y devuelve un dict con los datos para emitir el JWT interno.
    """
    if state not in _pending_states:
        raise ValueError("Estado OAuth inválido o expirado")
    _pending_states.discard(state)

    token_url = AZURE_TOKEN_URL.format(tenant=settings.azure_tenant_id)
    data = {
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "code": code,
        "redirect_uri": settings.azure_redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(token_url, data=data)
        token_resp.raise_for_status()
        tokens = token_resp.json()

        me_resp = await client.get(
            GRAPH_ME_URL,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        me_resp.raise_for_status()
        profile = me_resp.json()

    return {
        "sub": profile.get("userPrincipalName") or profile.get("mail"),
        "name": profile.get("displayName", ""),
        "email": profile.get("mail") or profile.get("userPrincipalName"),
        "azure_id": profile.get("id"),
        "provider": "azure",
        "roles": ["user"],
    }
