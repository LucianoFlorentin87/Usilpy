import asyncio
import logging
import re

import httpx
import msal

from config import get_settings

logger = logging.getLogger(__name__)
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


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_users(top: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/users",
            headers=_headers(),
            params={"$top": top, "$select": "id,displayName,mail,userPrincipalName"},
        )
        resp.raise_for_status()
        return resp.json().get("value", [])


async def get_user_by_upn(upn: str) -> dict | None:
    """Look up a user by UPN (email). Returns None if not found."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/users/{upn}",
            headers=_headers(),
            params={"$select": "id,displayName,mail,userPrincipalName,accountEnabled"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


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


# ── Groups ────────────────────────────────────────────────────────────────────

async def create_group(display_name: str, description: str = "") -> dict:
    payload = {
        "displayName": display_name,
        "description": description,
        "mailEnabled": False,
        "mailNickname": display_name.replace(" ", "").lower()[:20],
        "securityEnabled": True,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GRAPH_BASE}/groups", headers=_headers(), json=payload)
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


# ── Teams ─────────────────────────────────────────────────────────────────────

async def get_teams(top: int = 50) -> list[dict]:
    """List all Teams teams in the tenant."""
    filter_q = "resourceProvisioningOptions/Any(x:x eq 'Team')"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/groups",
            headers=_headers(),
            params={"$filter": filter_q, "$select": "id,displayName", "$top": top},
        )
        resp.raise_for_status()
        return resp.json().get("value", [])



async def find_team_by_display_name(name: str) -> dict | None:
    """Find a Teams team by exact display name. Returns None if not found."""
    filter_q = f"displayName eq '{name}' and resourceProvisioningOptions/Any(x:x eq 'Team')"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/groups",
            headers=_headers(),
            params={"$filter": filter_q, "$select": "id,displayName"},
        )
        resp.raise_for_status()
        items = resp.json().get("value", [])
        return items[0] if items else None


async def create_team(display_name: str, description: str = "") -> dict:
    """
    Create a Microsoft Teams team.
    Strategy: create an M365 group first, then PUT /group/{id}/team to provision it.
    This uses Group.ReadWrite.All which the app already has.
    """
    import asyncio

    # Step 1: create the underlying Microsoft 365 group
    group_payload = {
        "displayName": display_name,
        "description": description,
        "groupTypes": ["Unified"],
        "mailEnabled": True,
        "mailNickname": re.sub(r"[^a-zA-Z0-9]", "", display_name)[:20] or "team",
        "securityEnabled": False,
        "visibility": "Private",
    }
    hdrs = _headers()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{GRAPH_BASE}/groups", headers=hdrs, json=group_payload)
        r.raise_for_status()
        group = r.json()
        group_id = group["id"]

        # Step 2: wait a few seconds for group replication, then provision as team
        await asyncio.sleep(5)
        team_payload = {
            "memberSettings": {"allowCreateUpdateChannels": True},
            "messagingSettings": {"allowUserEditMessages": True, "allowUserDeleteMessages": True},
            "funSettings": {"allowGiphy": True, "giphyContentRating": "moderate"},
        }
        for attempt in range(6):
            tr = await client.put(
                f"{GRAPH_BASE}/groups/{group_id}/team",
                headers=hdrs,
                json=team_payload,
            )
            if tr.status_code in (200, 201):
                data = tr.json()
                data["id"] = data.get("id") or group_id
                return data
            if tr.status_code == 404:
                await asyncio.sleep(5)
                continue
            tr.raise_for_status()

        # Fallback: return the group with the id so callers can use it
        group["id"] = group_id
        return group


async def add_member_to_team(team_id: str, user_id: str) -> bool:
    """Add a user as a member to a Teams team."""
    payload = {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": [],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{user_id}')",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/teams/{team_id}/members",
            headers=_headers(),
            json=payload,
        )
        if resp.status_code == 409:
            return True  # already a member
        return resp.status_code in (200, 201, 204)
