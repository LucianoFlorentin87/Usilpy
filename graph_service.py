import httpx
import msal
from config import get_settings

settings = get_settings()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]


def _get_token() -> str:
    app = msal.ConfidentialClientApplication(
        settings.azure_client_id,
        authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
        client_credential=settings.azure_client_secret,
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Azure token error: {result.get('error_description')}")
    return result["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


async def get_users(top: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/users",
            headers=_headers(),
            params={"$top": top, "$select": "id,displayName,mail,userPrincipalName"},
        )
        resp.raise_for_status()
        return resp.json().get("value", [])


async def create_user(display_name: str, mail_nickname: str, upn: str, password: str) -> dict:
    payload = {
        "accountEnabled": True,
        "displayName": display_name,
        "mailNickname": mail_nickname,
        "userPrincipalName": upn,
        "passwordProfile": {
            "forceChangePasswordNextSignIn": True,
            "password": password,
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GRAPH_BASE}/users", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


async def get_groups(top: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/groups",
            headers=_headers(),
            params={"$top": top, "$select": "id,displayName,mail"},
        )
        resp.raise_for_status()
        return resp.json().get("value", [])


async def add_member_to_group(group_id: str, user_id: str) -> bool:
    payload = {"@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GRAPH_BASE}/groups/{group_id}/members/$ref",
            headers=_headers(),
            json=payload,
        )
        return resp.status_code in (200, 204)
