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


async def search_users(query: str, top: int = 20) -> list[dict]:
    """Search Azure AD users by displayName or mail containing the query."""
    safe = query.replace("'", "''")
    filter_q = (
        f"startswith(displayName,'{safe}') or "
        f"startswith(mail,'{safe}') or "
        f"startswith(userPrincipalName,'{safe}')"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GRAPH_BASE}/users",
            headers=_headers(),
            params={"$filter": filter_q, "$top": top, "$select": "id,displayName,mail,userPrincipalName,accountEnabled"},
        )
        if resp.status_code in (400, 404):
            return []
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


# ── Email via Microsoft Graph ─────────────────────────────────────────────────

async def send_welcome_email(to_email: str, nombre: str, canvas_url: str, username: str) -> bool:
    """
    Envía email de bienvenida al alumno recién creado usando Microsoft Graph API.
    Requiere que la app tenga permiso Mail.Send y que EMAIL_SENDER esté configurado.
    Retorna True si se envió correctamente, False si no.
    """
    sender = settings.email_sender
    if not sender:
        logger.warning("EMAIL_SENDER no configurado — email de bienvenida omitido para %s", to_email)
        return False

    html_body = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"></head>
    <body style="font-family: 'Segoe UI', Arial, sans-serif; background:#f0f4f8; margin:0; padding:0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8; padding:32px 0;">
        <tr><td align="center">
          <table width="560" cellpadding="0" cellspacing="0" style="background:#fff; border-radius:16px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08);">
            <!-- Header -->
            <tr>
              <td style="background:#0d1b2e; padding:28px 36px; text-align:center;">
                <div style="font-size:22px; font-weight:800; color:#fff; letter-spacing:.5px;">Gestión Académica</div>
                <div style="font-size:12px; color:#5c7a9e; text-transform:uppercase; letter-spacing:1px; margin-top:4px;">USIL Paraguay</div>
              </td>
            </tr>
            <!-- Body -->
            <tr>
              <td style="padding:36px;">
                <p style="font-size:16px; color:#1e293b; margin:0 0 8px;">Hola, <strong>{nombre}</strong> 👋</p>
                <p style="font-size:14px; color:#64748b; margin:0 0 24px; line-height:1.6;">
                  Tu cuenta en <strong>Canvas LMS</strong> ha sido creada exitosamente.
                  A continuación encontrás tus datos de acceso.
                </p>

                <!-- Credentials box -->
                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:20px 24px; margin-bottom:24px;">
                  <div style="margin-bottom:12px;">
                    <div style="font-size:11px; font-weight:700; color:#94a3b8; text-transform:uppercase; letter-spacing:.08em; margin-bottom:4px;">Usuario</div>
                    <div style="font-size:15px; font-weight:600; color:#1e293b;">{username}</div>
                  </div>
                  <div>
                    <div style="font-size:11px; font-weight:700; color:#94a3b8; text-transform:uppercase; letter-spacing:.08em; margin-bottom:4px;">Contraseña</div>
                    <div style="font-size:14px; color:#64748b;">Usá la opción <em>"¿Olvidé mi contraseña?"</em> en Canvas para establecer tu contraseña.</div>
                  </div>
                </div>

                <!-- CTA button -->
                <div style="text-align:center; margin-bottom:28px;">
                  <a href="{canvas_url}" style="display:inline-block; background:#2563eb; color:#fff; text-decoration:none; font-size:15px; font-weight:700; padding:14px 32px; border-radius:10px; letter-spacing:.3px;">
                    Acceder a Canvas LMS →
                  </a>
                </div>

                <p style="font-size:13px; color:#94a3b8; line-height:1.6; margin:0;">
                  Si tenés alguna duda, respondé este email o contactá al Departamento de Tecnología de USIL Paraguay.
                </p>
              </td>
            </tr>
            <!-- Footer -->
            <tr>
              <td style="background:#f8fafc; border-top:1px solid #e2e8f0; padding:18px 36px; text-align:center;">
                <div style="font-size:12px; color:#94a3b8;">© USIL Paraguay · Departamento de Tecnología</div>
              </td>
            </tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

    payload = {
        "message": {
            "subject": f"Bienvenido/a a USIL Paraguay — Tu cuenta Canvas está lista, {nombre.split()[0]}",
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": False,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{GRAPH_BASE}/users/{sender}/sendMail",
                headers=_headers(),
                json=payload,
            )
            if resp.status_code == 202:
                logger.info("Email de bienvenida enviado a %s", to_email)
                return True
            logger.warning("Graph sendMail → HTTP %d para %s: %s", resp.status_code, to_email, resp.text[:200])
            return False
    except Exception as exc:
        logger.error("Error enviando email a %s: %s", to_email, exc)
        return False
