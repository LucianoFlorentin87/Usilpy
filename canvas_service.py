import httpx
from config import get_settings

_account_id_cache: str | None = None


def _base() -> str:
    return get_settings().canvas_base_url.rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().canvas_api_token}"}


async def _account_id() -> str:
    """Discover the root account ID dynamically."""
    global _account_id_cache
    if _account_id_cache:
        return _account_id_cache
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{_base()}/api/v1/accounts", headers=_headers())
        resp.raise_for_status()
        accounts = resp.json()
        if accounts:
            _account_id_cache = str(accounts[0]["id"])
            return _account_id_cache
    return "self"


# ── Enrollment Terms (Períodos) ───────────────────────────────────────────────

async def get_terms(per_page: int = 100) -> list[dict]:
    """List all enrollment terms in the account."""
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/accounts/{acct}/terms",
            headers=_headers(),
            params={"per_page": per_page},
        )
        resp.raise_for_status()
        return resp.json().get("enrollment_terms", [])


async def find_term_by_name(name: str) -> dict | None:
    """Find an enrollment term by exact name. Returns None if not found."""
    terms = await get_terms()
    name_lower = name.strip().lower()
    return next((t for t in terms if t.get("name", "").strip().lower() == name_lower), None)


async def create_term(name: str, start_at: str = "", end_at: str = "") -> dict:
    """Create an enrollment term. start_at/end_at are ISO 8601 strings (optional)."""
    payload: dict = {"enrollment_term": {"name": name}}
    if start_at:
        payload["enrollment_term"]["start_at"] = start_at
    if end_at:
        payload["enrollment_term"]["end_at"] = end_at
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_base()}/api/v1/accounts/{acct}/terms",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def get_or_create_term(name: str) -> dict:
    """Find term by name or create it if it doesn't exist."""
    existing = await find_term_by_name(name)
    if existing:
        return existing
    return await create_term(name)


async def get_courses(per_page: int = 50) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/courses",
            headers=_headers(),
            params={"per_page": per_page, "enrollment_type": "teacher"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_all_courses() -> list[dict]:
    """Fetch ALL courses in the account with pagination, returning id + name + sis_course_id."""
    try:
        acct = await _account_id()
    except Exception as e:
        raise RuntimeError(f"No se pudo obtener account ID de Canvas: {e}") from e

    courses: list[dict] = []
    url = f"{_base()}/api/v1/accounts/{acct}/courses"
    params = {"per_page": 100, "state[]": ["available", "unpublished", "completed"]}
    async with httpx.AsyncClient(timeout=60) as client:
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            if not resp.is_success:
                raise RuntimeError(f"Canvas API error {resp.status_code}: {resp.text[:300]}")
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"Canvas devolvió formato inesperado: {str(batch)[:200]}")
            courses.extend({"id": c["id"], "name": c["name"], "sis_course_id": c.get("sis_course_id", "")} for c in batch)
            # Follow Link header for next page
            link = resp.headers.get("Link", "")
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
            url = next_url
            params = {}
    return courses


async def get_users(per_page: int = 50) -> list[dict]:
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/accounts/{acct}/users",
            headers=_headers(),
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
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_base()}/api/v1/accounts/{acct}/users",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def find_user_by_sis_id(sis_id: str) -> dict | None:
    """Look up a Canvas user by SIS user ID. Returns None if not found."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/users/sis_user_id:{sis_id}",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def get_course_by_sis_id(sis_id: str) -> dict | None:
    """Look up a Canvas course by SIS course ID. Returns None if not found."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/courses/sis_course_id:{sis_id}",
            headers=_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def create_course(name: str, sis_id: str, semestre: str = "", term_id: int | None = None) -> dict:
    course_data: dict = {
        "name": name,
        "course_code": sis_id,
        "sis_course_id": sis_id,
        "is_public": False,
    }
    if term_id:
        course_data["enrollment_term_id"] = term_id
    elif semestre:
        course_data["term_name"] = semestre

    payload = {"course": course_data}
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_base()}/api/v1/accounts/{acct}/courses",
            headers=_headers(),
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
            f"{_base()}/api/v1/courses/{course_id}/enrollments",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def get_enrollments(course_id: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/courses/{course_id}/enrollments",
            headers=_headers(),
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()


async def search_users(query: str, per_page: int = 20) -> list[dict]:
    """Search Canvas users by name or email (account-level search)."""
    acct = await _account_id()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/accounts/{acct}/users",
            headers=_headers(),
            params={"search_term": query, "per_page": per_page},
        )
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        return resp.json()


async def get_all_courses(per_page: int = 100) -> list[dict]:
    """Fetch all courses from the account with pagination."""
    courses = []
    acct = await _account_id()
    url = f"{_base()}/api/v1/accounts/{acct}/courses"
    params = {"per_page": per_page, "include[]": ["total_students", "term"]}
    async with httpx.AsyncClient(timeout=60) as client:
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            courses.extend(resp.json())
            link = resp.headers.get("Link", "")
            url = None
            params = {}
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
    return courses


async def get_course_enrollments(course_id: int | str, per_page: int = 100) -> list[dict]:
    """Fetch all student enrollments for a course with pagination."""
    enrollments = []
    url = f"{_base()}/api/v1/courses/{course_id}/enrollments"
    params = {"per_page": per_page, "type[]": "StudentEnrollment", "state[]": ["active", "invited", "completed"]}
    async with httpx.AsyncClient(timeout=60) as client:
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            enrollments.extend(resp.json())
            link = resp.headers.get("Link", "")
            url = None
            params = {}
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
    return enrollments


async def get_course_grades(course_id: int | str) -> list[dict]:
    """Fetch final grades for all students in a course via enrollments."""
    enrollments = await get_course_enrollments(course_id)
    grades = []
    for e in enrollments:
        grades_data = e.get("grades", {})
        grades.append({
            "course_id": course_id,
            "user_id": e.get("user_id"),
            "user_name": e.get("user", {}).get("name", ""),
            "sis_user_id": e.get("sis_user_id") or e.get("user", {}).get("sis_user_id", ""),
            "login_id": e.get("user", {}).get("login_id", ""),
            "current_score": grades_data.get("current_score"),
            "final_score": grades_data.get("final_score"),
            "current_grade": grades_data.get("current_grade"),
            "final_grade": grades_data.get("final_grade"),
            "enrollment_state": e.get("enrollment_state", ""),
        })
    return grades


async def get_course_attendance(course_id: int | str) -> list[dict]:
    """Fetch attendance records via Canvas Roll Call API (if enabled)."""
    attendances = []
    url = f"{_base()}/api/v1/courses/{course_id}/attendances"
    params = {"per_page": 100}
    async with httpx.AsyncClient(timeout=60) as client:
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            if resp.status_code in (404, 401, 403):
                break
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            attendances.extend(data)
            link = resp.headers.get("Link", "")
            url = None
            params = {}
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
    return attendances


async def get_roll_call_report(course_id: int | str) -> dict:
    """Reporte de asistencia por alumno basado en la tarea oculta 'Roll Call Attendance'.

    El puntaje de esa tarea (0-100) es el % de asistencia calculado por Canvas.
    Retorna {"disponible": bool, "alumnos": [{user_id, nombre, sis_user_id, login_id, porcentaje}]}.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Buscar la tarea de Roll Call
        assignment_id = None
        url = f"{_base()}/api/v1/courses/{course_id}/assignments"
        params = {"per_page": 100}
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            if resp.status_code in (401, 403, 404):
                return {"disponible": False, "alumnos": []}
            resp.raise_for_status()
            for a in resp.json():
                if (a.get("name") or "").strip().lower() == "roll call attendance":
                    assignment_id = a.get("id")
                    break
            if assignment_id:
                break
            link = resp.headers.get("Link", "")
            url = None
            params = {}
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        if not assignment_id:
            return {"disponible": False, "alumnos": []}

        # 2. Traer submissions con datos del alumno
        alumnos = []
        url = f"{_base()}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions"
        params = {"per_page": 100, "include[]": "user"}
        while url:
            resp = await client.get(url, headers=_headers(), params=params)
            if resp.status_code in (401, 403, 404):
                break
            resp.raise_for_status()
            for s in resp.json():
                user = s.get("user") or {}
                if (user.get("name") or "").lower() == "test student":
                    continue
                alumnos.append({
                    "user_id": s.get("user_id"),
                    "nombre": user.get("sortable_name") or user.get("name") or "",
                    "sis_user_id": user.get("sis_user_id") or "",
                    "login_id": user.get("login_id") or "",
                    "porcentaje": round(float(s.get("score")), 1) if s.get("score") is not None else None,
                })
            link = resp.headers.get("Link", "")
            url = None
            params = {}
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        alumnos.sort(key=lambda a: a["nombre"])
        return {"disponible": True, "alumnos": alumnos}
