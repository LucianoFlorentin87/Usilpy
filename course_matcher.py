"""
Fuzzy course name matching + persistent alias table.

Flow:
  1. Exact match against known aliases (course_aliases table)
  2. Fuzzy match against Canvas courses list (rapidfuzz)
     - score >= HIGH_THRESHOLD  → auto-accept, save alias
     - score >= LOW_THRESHOLD   → save as 'pendiente' for admin review
     - score <  LOW_THRESHOLD   → unresolved, log as warning
  3. If no match at all → return None (caller creates a new course)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from rapidfuzz import fuzz, process

import db

logger = logging.getLogger(__name__)

HIGH_THRESHOLD = 85   # auto-accept
LOW_THRESHOLD  = 60   # flag for review

_CREATE_ALIASES = """
CREATE TABLE IF NOT EXISTS course_aliases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    variant       TEXT UNIQUE NOT NULL,
    canvas_sis_id TEXT NOT NULL,
    canvas_name   TEXT,
    score         REAL,
    estado        TEXT NOT NULL DEFAULT 'auto',
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   TEXT
)
"""

_CREATE_UNRESOLVED = """
CREATE TABLE IF NOT EXISTS course_unresolved (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    variant     TEXT NOT NULL,
    semestre    TEXT,
    best_match  TEXT,
    best_score  REAL,
    seen_count  INTEGER DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
)
"""


async def init_matcher_tables() -> None:
    await db.execute(_CREATE_ALIASES)
    await db.execute(_CREATE_UNRESOLVED)


# ── Alias lookups ─────────────────────────────────────────────────────────────

async def get_alias(variant: str) -> dict | None:
    return await db.fetchrow(
        "SELECT * FROM course_aliases WHERE variant = ? AND estado != 'rechazado'",
        variant.strip(),
    )


async def save_alias(variant: str, canvas_sis_id: str, canvas_name: str,
                     score: float, estado: str = "auto") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO course_aliases (variant, canvas_sis_id, canvas_name, score, estado, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(variant) DO UPDATE SET
             canvas_sis_id=EXCLUDED.canvas_sis_id,
             canvas_name=EXCLUDED.canvas_name,
             score=EXCLUDED.score,
             estado=EXCLUDED.estado""",
        variant.strip(), canvas_sis_id, canvas_name, score, estado, ts,
    )


async def resolve_alias(alias_id: int, canvas_sis_id: str, canvas_name: str,
                        resolved_by: str = "admin") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE course_aliases
           SET canvas_sis_id=?, canvas_name=?, estado='confirmado',
               resolved_at=?, resolved_by=?
           WHERE id=?""",
        canvas_sis_id, canvas_name, ts, resolved_by, alias_id,
    )


async def list_aliases(estado: str | None = None) -> list[dict]:
    if estado:
        return await db.fetch(
            "SELECT * FROM course_aliases WHERE estado=? ORDER BY created_at DESC", estado
        )
    return await db.fetch("SELECT * FROM course_aliases ORDER BY created_at DESC")


async def list_unresolved() -> list[dict]:
    return await db.fetch("SELECT * FROM course_unresolved ORDER BY last_seen DESC")


async def _record_unresolved(variant: str, semestre: str,
                              best_match: str, best_score: float) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    existing = await db.fetchrow(
        "SELECT id, seen_count FROM course_unresolved WHERE variant=? AND semestre=?",
        variant.strip(), semestre,
    )
    if existing:
        await db.execute(
            "UPDATE course_unresolved SET seen_count=seen_count+1, last_seen=?, best_match=?, best_score=? WHERE id=?",
            ts, best_match, best_score, existing["id"],
        )
    else:
        await db.execute(
            """INSERT INTO course_unresolved (variant, semestre, best_match, best_score, first_seen, last_seen)
               VALUES (?,?,?,?,?,?)""",
            variant.strip(), semestre, best_match, best_score, ts, ts,
        )


# ── Main entry point ──────────────────────────────────────────────────────────

async def resolve_course_name(
    variant: str,
    canvas_courses: list[dict],
    semestre: str = "",
) -> dict | None:
    """
    Try to resolve a course name variant to a Canvas course.

    Returns a dict with keys: canvas_sis_id, canvas_name, canvas_id, score, source
    or None if it can't be resolved (caller should create a new course).
    """
    variant = variant.strip()
    if not variant:
        return None

    # 1. Check saved alias
    alias = await get_alias(variant)
    if alias:
        # Find full canvas course data
        for c in canvas_courses:
            if c.get("sis_course_id") == alias["canvas_sis_id"]:
                return {
                    "canvas_sis_id": alias["canvas_sis_id"],
                    "canvas_name": alias["canvas_name"],
                    "canvas_id": c.get("id"),
                    "score": alias["score"],
                    "source": "alias",
                }
        # Alias exists but course not in list — still return sis_id
        return {
            "canvas_sis_id": alias["canvas_sis_id"],
            "canvas_name": alias["canvas_name"],
            "canvas_id": None,
            "score": alias["score"],
            "source": "alias",
        }

    # 2. Fuzzy match against Canvas course names + sis_ids
    if not canvas_courses:
        return None

    choices = {c.get("name", ""): c for c in canvas_courses if c.get("name")}
    choices.update({c.get("sis_course_id", ""): c for c in canvas_courses if c.get("sis_course_id")})

    result = process.extractOne(
        variant,
        choices.keys(),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=LOW_THRESHOLD,
    )

    if not result:
        logger.warning("No fuzzy match for '%s' (semestre=%s)", variant, semestre)
        await _record_unresolved(variant, semestre, "", 0)
        return None

    matched_key, score, _ = result
    matched_course = choices[matched_key]
    canvas_sis_id = matched_course.get("sis_course_id", "")
    canvas_name   = matched_course.get("name", "")
    canvas_id     = matched_course.get("id")

    if score >= HIGH_THRESHOLD:
        logger.info("Auto-matched '%s' → '%s' (score=%.0f)", variant, canvas_name, score)
        await save_alias(variant, canvas_sis_id, canvas_name, score, estado="auto")
        return {
            "canvas_sis_id": canvas_sis_id,
            "canvas_name": canvas_name,
            "canvas_id": canvas_id,
            "score": score,
            "source": "fuzzy_auto",
        }

    # score is between LOW and HIGH — flag for admin review
    logger.warning("Low-confidence match '%s' → '%s' (score=%.0f) — pendiente revisión",
                   variant, canvas_name, score)
    await save_alias(variant, canvas_sis_id, canvas_name, score, estado="pendiente")
    await _record_unresolved(variant, semestre, canvas_name, score)
    return {
        "canvas_sis_id": canvas_sis_id,
        "canvas_name": canvas_name,
        "canvas_id": canvas_id,
        "score": score,
        "source": "fuzzy_pendiente",
    }
