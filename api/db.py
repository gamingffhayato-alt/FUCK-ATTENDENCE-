"""
Supabase data layer — v3 manual daily schedule.

Tables (see database/schema.sql):
    subjects        (id, name, created_at)
    daily_schedule  (date, subject_id, lecture_count)
    attendance_log  (date, subject_id, present_count, absent_count)
    holidays        (date, reason)

This module only fetches/writes ROWS; all aggregation (stats,
week payloads) lives in index.py. All calls use the official
`supabase` Python client.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

# Load .env next to this file (or in the parent folder)
_here = Path(__file__).resolve().parent
for _env in (_here / ".env", _here.parent / ".env"):
    if _env.is_file():
        load_dotenv(_env)

# Semester 1 — mirrored in database/schema.sql (seed is idempotent).
DEFAULT_SUBJECTS = [
    "Basics Of Computer and C Programming",
    "Basics Of Computer and C Programming Lab",
    "Front-End Web Development",
    "Front-End Web Development Lab",
    "Environmental Studies - I",
    "Communication & Professional Skills I",
    "Mini Project-I",
    "Fundamentals Of Intelligent & Autonomous Systems",
    "Technical Training - Advance Programming In C",
    "Computational & Quantum Physics",
    "Computational & Quantum Physics Lab",
    "Computational Mathematics For Intelligent Systems",
]

_client = None


class TablesMissingError(RuntimeError):
    """The Supabase tables have not been created yet."""


def sb():
    """Lazily create the shared Supabase client from .env."""
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_KEY missing — add them to .env (locally) and Vercel env vars.")
        _client = create_client(url, key)
    return _client


def _friendly(exc: Exception) -> Exception:
    """Turn 'table/column not found' into an actionable message."""
    msg = str(exc)
    if "Could not find the table" in msg or "PGRST205" in msg:
        return TablesMissingError(
            "Supabase tables not found — run database/schema.sql in the "
            "Supabase SQL Editor, then retry."
        )
    if "PGRST204" in msg or "Could not find the column" in msg:
        return TablesMissingError(
            "Supabase schema is out of date — run "
            "database/clear_logs_keep_baseline.sql in the Supabase SQL Editor "
            "(adds the subjects baseline columns), then retry."
        )
    return exc


def _run(fn):
    try:
        return fn()
    except TablesMissingError:
        raise
    except Exception as exc:  # postgrest APIError etc.
        raise _friendly(exc) from exc


# ---------------------------------------------------------------
# subjects
# ---------------------------------------------------------------

def get_subjects() -> list[dict]:
    """All subjects, including baseline columns (initial_* may be absent on
    databases that predate the baseline migration — callers use .get())."""

    def _q():
        return (
            sb()
            .table("subjects")
            .select("*")
            .order("name")
            .execute()
            .data
        )

    return _run(_q)


def insert_subject(name: str) -> dict:
    def _q():
        rows = sb().table("subjects").insert({"name": name}).execute().data
        return rows[0] if rows else {"name": name}

    return _run(_q)


def delete_subject(subject_id: int) -> list[dict]:
    """FK `on delete cascade` also removes this subject's schedule + attendance."""

    def _q():
        return (
            sb()
            .table("subjects")
            .delete()
            .eq("id", subject_id)
            .execute()
            .data
        )

    return _run(_q)


def seed_subjects(names: list[str]) -> list[dict]:
    """Insert any of `names` that do not exist yet (case-insensitive)."""

    def _q():
        c = sb()
        existing = {
            s["name"].casefold()
            for s in c.table("subjects").select("name").execute().data
        }
        new_rows = [{"name": n} for n in names if n.casefold() not in existing]
        if new_rows:
            c.table("subjects").insert(new_rows).execute()
        return c.table("subjects").select("id, name, created_at").order("name").execute().data

    return _run(_q)


# ---------------------------------------------------------------
# daily_schedule
# ---------------------------------------------------------------

def get_schedule_rows_for_date(iso_date: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("daily_schedule")
            .select("date, subject_id, lecture_count")
            .eq("date", iso_date)
            .execute()
            .data
        )

    return _run(_q)


def get_schedule_rows_between(start_iso: str, end_iso: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("daily_schedule")
            .select("date, subject_id, lecture_count")
            .gte("date", start_iso)
            .lte("date", end_iso)
            .execute()
            .data
        )

    return _run(_q)


def get_all_schedule_rows() -> list[dict]:
    def _q():
        return (
            sb()
            .table("daily_schedule")
            .select("date, subject_id, lecture_count")
            .order("date")
            .execute()
            .data
        )

    return _run(_q)


def replace_schedule_for_date(iso_date: str, entries: list[dict]) -> list[dict]:
    """Replace the whole day: delete rows for the date, insert non-zero counts."""

    def _q():
        c = sb()
        c.table("daily_schedule").delete().eq("date", iso_date).execute()
        rows = [
            {
                "date": iso_date,
                "subject_id": int(e["subject_id"]),
                "lecture_count": int(e["lecture_count"]),
            }
            for e in entries
            if int(e.get("lecture_count", 0)) > 0
        ]
        if rows:
            c.table("daily_schedule").insert(rows).execute()
        return rows

    return _run(_q)


# ---------------------------------------------------------------
# attendance_log (counts)
# ---------------------------------------------------------------

def get_attendance_rows_for_date(iso_date: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .select("date, subject_id, present_count, absent_count")
            .eq("date", iso_date)
            .execute()
            .data
        )

    return _run(_q)


def get_attendance_rows_between(start_iso: str, end_iso: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .select("date, subject_id, present_count, absent_count")
            .gte("date", start_iso)
            .lte("date", end_iso)
            .execute()
            .data
        )

    return _run(_q)


def get_all_attendance_rows() -> list[dict]:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .select("date, subject_id, present_count, absent_count")
            .order("date")
            .execute()
            .data
        )

    return _run(_q)


def upsert_attendance(iso_date: str, subject_id: int, present: int, absent: int) -> dict:
    """0/0 removes the row entirely (unmarked)."""

    def _q():
        c = sb()
        if present <= 0 and absent <= 0:
            (
                c.table("attendance_log")
                .delete()
                .eq("date", iso_date)
                .eq("subject_id", subject_id)
                .execute()
            )
            return {
                "date": iso_date,
                "subject_id": subject_id,
                "present_count": 0,
                "absent_count": 0,
            }
        row = {
            "date": iso_date,
            "subject_id": subject_id,
            "present_count": present,
            "absent_count": absent,
        }
        rows = (
            c.table("attendance_log")
            .upsert(row, on_conflict="date,subject_id")
            .execute()
            .data
        )
        return rows[0] if rows else row

    return _run(_q)


def delete_attendance(iso_date: str, subject_id: int) -> bool:
    def _q():
        (
            sb()
            .table("attendance_log")
            .delete()
            .eq("date", iso_date)
            .eq("subject_id", subject_id)
            .execute()
        )
        return True

    return _run(_q)


def replace_attendance_for_date(iso_date: str, entries: list[dict]) -> list[dict]:
    """Batch finalize: the submitted entries become the day's complete log
    (subjects with 0/0 are unmarked). One delete + one insert."""

    def _q():
        c = sb()
        c.table("attendance_log").delete().eq("date", iso_date).execute()
        rows = [
            {
                "date": iso_date,
                "subject_id": int(e["subject_id"]),
                "present_count": int(e["present_count"]),
                "absent_count": int(e["absent_count"]),
            }
            for e in entries
            if int(e.get("present_count", 0)) + int(e.get("absent_count", 0)) > 0
        ]
        if rows:
            c.table("attendance_log").insert(rows).execute()
        return rows

    return _run(_q)


def clear_daily_history(before_iso: str | None = None) -> dict:
    """Delete day-wise logs (attendance_log + daily_schedule) — optionally
    only rows dated BEFORE `before_iso`. The `subjects` table (baseline
    figures) is never touched. Pre-counted so results don't depend on the
    DELETE response representation."""

    def _q():
        c = sb()

        def count(table: str) -> int:
            q = c.table(table).select("id")
            if before_iso:
                q = q.lt("date", before_iso)
            else:
                # Supabase rejects DELETE without a WHERE clause (21000);
                # an always-true filter keeps the full-wipe variant working.
                q = q.gte("date", "1900-01-01")
            return len(q.execute().data or [])

        n_sched = count("daily_schedule")
        n_att = count("attendance_log")

        q_s = c.table("daily_schedule").delete()
        q_a = c.table("attendance_log").delete()
        if before_iso:
            q_s = q_s.lt("date", before_iso)
            q_a = q_a.lt("date", before_iso)
        else:
            q_s = q_s.gte("date", "1900-01-01")
            q_a = q_a.gte("date", "1900-01-01")
        q_s.execute()
        q_a.execute()

        return {"schedule_rows": n_sched, "attendance_rows": n_att}

    return _run(_q)


def clear_attendance_for_date(iso_date: str) -> bool:
    """Called when a day is marked as a holiday — nothing may be penalised."""

    def _q():
        sb().table("attendance_log").delete().eq("date", iso_date).execute()
        return True

    return _run(_q)


# ---------------------------------------------------------------
# holidays
# ---------------------------------------------------------------

def get_holiday(iso_date: str) -> dict | None:
    def _q():
        return (
            sb()
            .table("holidays")
            .select("date, reason")
            .eq("date", iso_date)
            .limit(1)
            .execute()
            .data
        )

    rows = _run(_q)
    return rows[0] if rows else None


def set_holiday(iso_date: str, reason: str) -> dict:
    def _q():
        rows = (
            sb()
            .table("holidays")
            .upsert({"date": iso_date, "reason": reason or "Holiday"},
                    on_conflict="date")
            .execute()
            .data
        )
        return rows[0] if rows else {"date": iso_date, "reason": reason}

    return _run(_q)


def remove_holiday(iso_date: str) -> bool:
    def _q():
        sb().table("holidays").delete().eq("date", iso_date).execute()
        return True

    return _run(_q)


def get_holidays_between(start_iso: str, end_iso: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("holidays")
            .select("date, reason")
            .gte("date", start_iso)
            .lte("date", end_iso)
            .execute()
            .data
        )

    return _run(_q)


def get_all_holidays() -> list[dict]:
    def _q():
        return (
            sb()
            .table("holidays")
            .select("date, reason")
            .order("date")
            .execute()
            .data
        )

    return _run(_q)
