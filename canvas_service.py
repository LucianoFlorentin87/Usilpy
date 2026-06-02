import httpx
from config import get_settings

settings = get_settings()

BASE = settings.canvas_base_url.rstrip("/")
HEADERS = {"Authorization": f"Bearer {settings.canvas_api_token}"}


async def get_courses(per_page: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE}/api/v1/courses",
            headers=HEADERS,
            params={"per_page": per_page, "enrollment_type": "teacher"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_users(per_page: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE}/api/v1/accounts/self/users",
            headers=HEADERS,
            params={"per_page": per_page},
        )
        resp.raise_for_status()
        return resp.json()


async def create_user(name: str, email: str, sis_id: str = "") -> dict:
    payload = {
        "user": {"name": name, "short_name": name.split()[0]},
        "pseudonym": {
            "unique_id": email,
            "send_confirmation": False,
            "sis_user_id": sis_id,
        },
        "communication_channel": {
            "type": "email",
            "address": email,
            "skip_confirmation": True,
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/api/v1/accounts/self/users",
            headers=HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def enroll_user(course_id: str, user_id: str, role: str = "StudentEnrollment") -> dict:
    payload = {
        "enrollment": {
            "user_id": user_id,
            "type": role,
            "enrollment_state": "active",
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/api/v1/courses/{course_id}/enrollments",
            headers=HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def get_enrollments(course_id: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE}/api/v1/courses/{course_id}/enrollments",
            headers=HEADERS,
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()
