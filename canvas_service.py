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
    from urllib.parse import quote
    # El SIS puede tener tildes, espacios y paréntesis: hay que codificarlo
    # para que la búsqueda no falle y el sistema no intente recrear el curso.
    sis_enc = quote(f"sis_course_id:{sis_id}", safe="")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_base()}/api/v1/courses/{sis_enc}",
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

# ── Roll Call: detalle por fecha (via LTI launch) ─────────────────────────────

async def _rollcall_login(client: "httpx.AsyncClient", course_id: int | str, debug: dict | None = None) -> str | None:
    """Realiza el launch LTI de la herramienta Attendance y deja la sesión en el
    cookie-jar del client. Retorna la URL base de Roll Call o None si falla.
    Si se pasa `debug`, se registra el paso en el que falló."""
    import html as _html
    import re as _re

    def _dbg(k, v):
        if debug is not None:
            debug[k] = v

    # 1. Encontrar la herramienta Attendance en el curso (o cuenta padre)
    tool_id = None
    resp = await client.get(
        f"{_base()}/api/v1/courses/{course_id}/external_tools",
        headers=_headers(),
        params={"include_parents": True, "per_page": 100},
    )
    _dbg("external_tools_status", resp.status_code)
    if resp.status_code == 200:
        tools = resp.json()
        _dbg("tools", [{"id": t.get("id"), "name": t.get("name"), "domain": t.get("domain")} for t in tools])
        for t in tools:
            name = (t.get("name") or "").lower()
            url = (t.get("url") or "") + (t.get("domain") or "")
            if "roll call" in name or "attendance" in name or "rollcall" in url:
                tool_id = t.get("id")
                break
    if not tool_id:
        _dbg("fallo", "No se encontró la herramienta Attendance/Roll Call entre las external tools del curso")
        return None
    _dbg("tool_id", tool_id)

    # 2. Sessionless launch
    resp = await client.get(
        f"{_base()}/api/v1/courses/{course_id}/external_tools/sessionless_launch",
        headers=_headers(),
        params={"id": tool_id},
    )
    _dbg("sessionless_status", resp.status_code)
    if resp.status_code != 200:
        _dbg("fallo", f"sessionless_launch devolvió {resp.status_code}: {resp.text[:300]}")
        return None
    launch_url = resp.json().get("url")
    if not launch_url:
        _dbg("fallo", "sessionless_launch sin URL")
        return None

    # 3. Seguir el launch: Canvas devuelve un form LTI auto-submit hacia Roll Call
    r = await client.get(launch_url)
    _dbg("launch_page_status", r.status_code)
    m = _re.search(r'<form[^>]+action="([^"]+)"', r.text)
    if not m:
        _dbg("fallo", f"La página de launch no contiene form LTI (status {r.status_code}): {r.text[:300]}")
        return None
    action = _html.unescape(m.group(1))
    _dbg("form_action", action)
    fields = {
        _html.unescape(mm.group(1)): _html.unescape(mm.group(2))
        for mm in _re.finditer(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', r.text)
    }
    _dbg("form_fields_count", len(fields))
    r2 = await client.post(action, data=fields)
    _dbg("lti_post_status", r2.status_code)
    _dbg("lti_final_url", str(r2.url))
    if r2.status_code >= 400:
        _dbg("fallo", f"POST LTI a Roll Call devolvió {r2.status_code}: {r2.text[:300]}")
        return None
    # Extraer CSRF token de la página de Roll Call (meta tag)
    mcsrf = _re.search(r'name="csrf-token"\s+content="([^"]+)"', r2.text) or \
            _re.search(r'content="([^"]+)"\s+name="csrf-token"', r2.text)
    csrf = _html.unescape(mcsrf.group(1)) if mcsrf else None
    _dbg("csrf_encontrado", bool(csrf))
    _dbg("cookies", list(client.cookies.jar and {c.name for c in client.cookies.jar} or []))
    from urllib.parse import urlparse
    p = urlparse(action)
    return {"base": f"{p.scheme}://{p.netloc}", "csrf": csrf}


async def get_roll_call_detail(course_id: int | str, dias_atras: int = 210) -> dict:
    """Detalle día-por-día de asistencia desde Roll Call.

    Retorna {"disponible": bool, "registros": [{"student_id", "fecha", "estado"}]}
    donde estado ∈ {"present", "absent", "late"}.
    """
    import asyncio
    from datetime import date, timedelta, datetime as _dt

    debug: dict = {}
    _browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "es-PY,es;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_browser_headers) as client:
        sesion = await _rollcall_login(client, course_id, debug)
        if not sesion:
            return {"disponible": False, "registros": [], "debug": debug}
        base = sesion["base"]
        csrf = sesion.get("csrf")

        # Probar variantes de encabezados automáticamente hasta encontrar la que
        # Roll Call acepte (el 403 depende de la configuración de su WAF/nginx)
        variantes = []
        v1 = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest", "Referer": base + "/"}
        if csrf:
            v1 = {**v1, "X-CSRF-Token": csrf}
        variantes.append(("json+csrf+xrw", v1))
        variantes.append(("json+referer", {"Accept": "application/json", "Referer": base + "/"}))
        variantes.append(("json", {"Accept": "application/json"}))
        variantes.append(("html", {"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}))
        variantes.append(("vacio", {}))

        # Secciones del curso
        resp = await client.get(
            f"{_base()}/api/v1/courses/{course_id}/sections",
            headers=_headers(), params={"per_page": 100},
        )
        if resp.status_code != 200:
            debug["fallo"] = f"No se pudieron listar secciones: {resp.status_code}"
            return {"disponible": False, "registros": [], "debug": debug}
        section_ids = [s["id"] for s in resp.json()]
        debug["sections"] = section_ids

        # Sonda: encontrar automáticamente la variante de encabezados aceptada
        _rc_headers = None
        from datetime import date as _pd, timedelta as _ptd
        fecha_sonda = (_pd.today() - _ptd(days=3)).isoformat()
        for nombre_v, hdrs in variantes:
            try:
                probe = await client.get(
                    f"{base}/statuses.json",
                    params={"section_id": section_ids[0], "class_date": fecha_sonda},
                    headers=hdrs,
                )
                debug[f"probe_{nombre_v}"] = probe.status_code
                if probe.status_code == 200:
                    try:
                        probe.json()
                        _rc_headers = hdrs
                        debug["variante_usada"] = nombre_v
                        break
                    except Exception:
                        debug[f"probe_{nombre_v}_nota"] = "200 pero no es JSON"
            except Exception as exc:
                debug[f"probe_{nombre_v}"] = f"error: {exc}"
        if _rc_headers is None:
            debug["fallo"] = "Ninguna variante de encabezados fue aceptada por Roll Call (statuses.json)"
            try:
                debug["probe_body"] = probe.text[:300]
            except Exception:
                pass
            return {"disponible": False, "registros": [], "debug": debug}

        # Rango de fechas: desde inicio del curso (si se conoce) hasta hoy
        start = None
        rc = await client.get(f"{_base()}/api/v1/courses/{course_id}", headers=_headers(), params={"include[]": "term"})
        if rc.status_code == 200:
            cjson = rc.json()
            for raw in (cjson.get("start_at"), (cjson.get("term") or {}).get("start_at")):
                if raw:
                    try:
                        start = _dt.fromisoformat(raw.replace("Z", "+00:00")).date()
                        break
                    except Exception:
                        pass
        today = date.today()
        if not start or (today - start).days > dias_atras:
            start = today - timedelta(days=dias_atras)

        fechas = [start + timedelta(days=i) for i in range((today - start).days + 1)]
        registros = []
        sem = asyncio.Semaphore(10)

        async def _fetch(sid, f):
            async with sem:
                try:
                    r = await client.get(
                        f"{base}/statuses.json",
                        params={"section_id": sid, "class_date": f.isoformat()},
                        headers=_rc_headers,
                    )
                    if r.status_code != 200:
                        return
                    for st in r.json():
                        est = st.get("attendance")
                        if est:
                            registros.append({
                                "student_id": st.get("student_id"),
                                "fecha": f.isoformat(),
                                "estado": est,
                            })
                except Exception:
                    pass

        await asyncio.gather(*[_fetch(sid, f) for sid in section_ids for f in fechas])
        # dedup (alumno puede estar en varias secciones)
        vistos = set()
        unicos = []
        for r_ in registros:
            k = (r_["student_id"], r_["fecha"])
            if k not in vistos:
                vistos.add(k)
                unicos.append(r_)
        debug["registros_total"] = len(unicos)
        return {"disponible": True, "registros": unicos, "debug": debug}
