"""
Supabase data layer — historical, day-by-day storage.

Tables (see database/schema.sql):
    weekly_schedule (day_of_week, subject_name, position)
    attendance_log  (date, subject_name, status)
    holidays        (date, reason)

All calls use the official `supabase` Python client.
"""

from __future__ import annotations

import os
from datetime import date as _date
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

# Load .env next to this file (or in the parent folder)
_here = Path(__file__).resolve().parent
for _env in (_here / ".env", _here.parent / ".env"):
    if _env.is_file():
        load_dotenv(_env)

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class TablesMissingError(RuntimeError):
    """The Supabase tables have not been created yet."""


_client = None


def sb():
    """Lazily create the shared Supabase client from .env."""
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_KEY missing — fill in backend/.env")
        _client = create_client(url, key)
    return _client


def _friendly(exc: Exception) -> Exception:
    """Turn 'table not found' into an actionable message."""
    msg = str(exc)
    if "Could not find the table" in msg or "PGRST205" in msg:
        return TablesMissingError(
            "Supabase tables not found — run database/schema.sql in the "
            "Supabase SQL Editor, then retry."
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
# weekly_schedule
# ---------------------------------------------------------------

def get_schedule() -> dict[str, list[str]]:
    def _q():
        return (
            sb()
            .table("weekly_schedule")
            .select("day_of_week, subject_name, position")
            .order("position")
            .execute()
            .data
        )

    schedule = {d: [] for d in DAYS}
    for row in _run(_q):
        day = row.get("day_of_week")
        if day in schedule and row["subject_name"] not in schedule[day]:
            schedule[day].append(row["subject_name"])
    return schedule


def replace_schedule(schedule: dict[str, list[str]]) -> dict[str, list[str]]:
    """The uploaded timetable replaces whatever was stored before."""

    def _q():
        c = sb()
        c.table("weekly_schedule").delete().gte("id", 0).execute()
        rows = []
        canonical: dict[str, str] = {}  # casefold -> first-seen spelling
        for day in DAYS:
            seen: set[str] = set()
            position = 0
            for subject in schedule.get(day, []):
                key = subject.casefold()
                if key in canonical:
                    subject = canonical[key]  # unify OCR variants like "EvS"/"EVS"
                else:
                    canonical[key] = subject
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {"day_of_week": day, "subject_name": subject, "position": position}
                )
                position += 1
        if rows:
            c.table("weekly_schedule").insert(rows).execute()
        return True

    _run(_q)
    return get_schedule()


# ---------------------------------------------------------------
# attendance_log
# ---------------------------------------------------------------

def get_records_for_date(iso_date: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .select("date, subject_name, status")
            .eq("date", iso_date)
            .execute()
            .data
        )

    return _run(_q)


def upsert_record(iso_date: str, subject_name: str, status: str) -> dict:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .upsert(
                {"date": iso_date, "subject_name": subject_name, "status": status},
                on_conflict="date,subject_name",
            )
            .execute()
            .data
        )

    rows = _run(_q)
    return rows[0] if rows else {"date": iso_date, "subject_name": subject_name, "status": status}


def delete_record(iso_date: str, subject_name: str) -> bool:
    def _q():
        (
            sb()
            .table("attendance_log")
            .delete()
            .eq("date", iso_date)
            .eq("subject_name", subject_name)
            .execute()
        )
        return True

    return _run(_q)


def clear_records_for_date(iso_date: str) -> bool:
    """Called when a day is marked as a holiday — nothing may be penalised."""

    def _q():
        sb().table("attendance_log").delete().eq("date", iso_date).execute()
        return True

    return _run(_q)


def get_logs_between(start_iso: str, end_iso: str) -> list[dict]:
    def _q():
        return (
            sb()
            .table("attendance_log")
            .select("date, subject_name, status")
            .gte("date", start_iso)
            .lte("date", end_iso)
            .execute()
            .data
        )

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
        return (
            sb()
            .table("holidays")
            .upsert({"date": iso_date, "reason": reason or "Holiday"},
                    on_conflict="date")
            .execute()
            .data
        )

    rows = _run(_q)
    return rows[0] if rows else {"date": iso_date, "reason": reason}


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
