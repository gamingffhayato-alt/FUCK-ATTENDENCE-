"""
Attendance Tracking Portal — FastAPI backend (day-by-day historical).

Routes
------
GET    /api/health                     config status (groq / supabase / ocr)
GET    /api/schedule                   current weekly schedule
POST   /api/schedule/upload            image file -> OCR -> Groq -> weekly_schedule
POST   /api/schedule/text              raw text (no image) -> Groq -> weekly_schedule
GET    /api/today?date=YYYY-MM-DD      today's date/day + only today's subjects
POST   /api/attendance                 {date, subject_name, status} log a record
DELETE /api/attendance?date=&subject_name=   undo a record
POST   /api/holiday                    {date, reason?} mark holiday + clear day
DELETE /api/holidays/{date}            undo a holiday
GET    /api/analytics?weeks=8&end_date=week-by-week historical percentages
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Vercel runs this file as the serverless entrypoint — make sibling modules
# (db.py, ocr.py, groq_analyzer.py) importable regardless of the working dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
from db import DAYS, TablesMissingError  # noqa: E402
from ocr import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    MAX_IMAGE_BYTES,
    OcrUnavailableError,
    extract_text,
    ocr_available,
)

# Groq module builds its own client from .env (import error => server still boots)
GROQ_IMPORT_ERROR: Optional[str] = None
try:
    from groq_analyzer import analyze_daily_schedule
except Exception as _exc:  # missing key / package
    analyze_daily_schedule = None  # type: ignore[assignment]
    GROQ_IMPORT_ERROR = str(_exc)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Attendance Tracking Portal API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class AttendanceIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    subject_name: str = Field(min_length=1, max_length=160)
    status: Literal["present", "absent"]


class HolidayIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(default="Holiday", max_length=200)


class TextIn(BaseModel):
    schedule_text: str = Field(min_length=1, max_length=20_000)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _parse_date(value: Optional[str]) -> date:
    if not value:
        return datetime.now().date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date '{value}' (use YYYY-MM-DD)")


def _today_payload(d: date) -> dict:
    iso = d.isoformat()
    day_name = d.strftime("%A")
    holiday = _run_db(lambda: db.get_holiday(iso))
    schedule = _run_db(db.get_schedule)
    subjects = [] if holiday else schedule.get(day_name, [])
    records = {r["subject_name"]: r["status"]
               for r in _run_db(lambda: db.get_records_for_date(iso))}
    return {
        "date": iso,
        "day_of_week": day_name,
        "is_weekend": day_name in ("Saturday", "Sunday"),
        "is_holiday": bool(holiday),
        "holiday_reason": holiday["reason"] if holiday else None,
        "subjects": subjects,
        "records": records,
    }


def _run_db(fn):
    """Execute a db call, translating missing-table errors into a clear 503."""
    try:
        return fn()
    except TablesMissingError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        if "Could not find the table" in str(exc) or "PGRST205" in str(exc):
            raise HTTPException(
                status_code=503,
                detail="Supabase tables not found — run database/schema.sql in the "
                       "Supabase SQL Editor, then retry.",
            )
        raise


_DAY_ALIASES = {}
for _d in DAYS:
    _DAY_ALIASES[_d.casefold()] = _d
    _DAY_ALIASES[_d[:3].casefold()] = _d          # mon, tue, ...
_DAY_ALIASES.update({"wk": "Saturday", "sun": "Sunday"})


def _normalize_schedule(raw) -> dict[str, list[str]]:
    """Model output -> {'Monday': [subjects...], ...} with validated days."""
    if isinstance(raw, list):
        # tolerate [{"day": "Monday", "subjects": [...]}, ...]
        as_dict = {}
        for item in raw:
            if isinstance(item, dict):
                day = item.get("day") or item.get("day_of_week")
                subs = item.get("subjects") or item.get("subjects_names") or []
                if day:
                    as_dict[day] = subs
        raw = as_dict
    if not isinstance(raw, dict):
        raise ValueError("AI response was not a JSON object keyed by day")

    schedule = {d: [] for d in DAYS}
    for key, value in raw.items():
        day = _DAY_ALIASES.get(str(key).strip().casefold())
        if not day:
            continue  # ignore non-day keys
        if isinstance(value, str):
            value = re.split(r"[,\n;|]+", value)
        if not isinstance(value, list):
            continue
        for subj in value:
            name = re.sub(r"\s+", " ", str(subj)).strip(" •-–—")
            if not name or len(name) > 160:
                continue
            if name.casefold() in {s.casefold() for s in schedule[day]}:
                continue
            schedule[day].append(name)
    if not any(schedule.values()):
        raise ValueError("Could not recognise any days/subjects in the AI output")
    return schedule


def _analyze_and_save(text: str, source: str) -> dict:
    """OCR text -> Groq day-by-day JSON -> replace weekly_schedule in Supabase."""
    if analyze_daily_schedule is None:
        raise HTTPException(
            status_code=503,
            detail=f"Groq unavailable: {GROQ_IMPORT_ERROR or 'analyze_daily_schedule not loaded'}",
        )
    try:
        raw = analyze_daily_schedule(text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Groq call failed: {exc}")

    try:
        schedule = _normalize_schedule(raw)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"AI returned unusable JSON: {exc}")

    saved = _run_db(lambda: db.replace_schedule(schedule))
    return {
        "schedule": saved,
        "model_output": raw,
        "extracted_text": text,
        "source": source,
    }


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    groq_ok = analyze_daily_schedule is not None and bool(
        os.getenv("GROQ_API_KEY", "").strip()
    )
    return {
        "status": "ok",
        "groq": groq_ok,
        "groq_error": GROQ_IMPORT_ERROR,
        "groq_model": "openai/gpt-oss-20b",
        "supabase": bool(os.getenv("SUPABASE_URL", "").strip()),
        "ocr": ocr_available(),
    }


# --------------------------------------------------------------------------
# Schedule: upload image (OCR + AI) or raw text
# --------------------------------------------------------------------------

@app.get("/api/schedule")
def get_schedule():
    return {"schedule": _run_db(db.get_schedule)}


@app.post("/api/schedule/upload")
async def upload_schedule(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Use an image: "
                   + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image larger than 10 MB")

    try:
        text = extract_text(data)
    except OcrUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail="OCR found no readable text in that image — try a clearer photo, "
                   "or use 'paste text instead'.",
        )
    return _analyze_and_save(text, source="image-ocr")


@app.post("/api/schedule/text")
def schedule_from_text(body: TextIn):
    """Fallback path: text pasted directly (skips OCR, still uses Groq)."""
    return _analyze_and_save(body.schedule_text.strip(), source="text")


# --------------------------------------------------------------------------
# Today (day-by-day dashboard)
# --------------------------------------------------------------------------

@app.get("/api/today")
def today(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return _today_payload(_parse_date(date))


# --------------------------------------------------------------------------
# Attendance log
# --------------------------------------------------------------------------

@app.post("/api/attendance")
def mark_attendance(body: AttendanceIn):
    d = _parse_date(body.date)
    if _run_db(lambda: db.get_holiday(d.isoformat())):
        raise HTTPException(status_code=409, detail=f"{d.isoformat()} is a holiday — nothing to mark")
    record = _run_db(lambda: db.upsert_record(d.isoformat(), body.subject_name.strip(), body.status))
    return {"ok": True, "record": record}


@app.delete("/api/attendance")
def unmark_attendance(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    subject_name: str = Query(..., min_length=1),
):
    _run_db(lambda: db.delete_record(date, subject_name))
    return {"ok": True}


# --------------------------------------------------------------------------
# Holidays
# --------------------------------------------------------------------------

@app.post("/api/holiday")
def mark_holiday(body: HolidayIn):
    d = _parse_date(body.date)
    iso = d.isoformat()
    _run_db(lambda: db.set_holiday(iso, body.reason.strip() or "Holiday"))
    _run_db(lambda: db.clear_records_for_date(iso))  # never penalise a holiday
    return _today_payload(d)


@app.delete("/api/holidays/{day}")
def unmark_holiday(day: str):
    d = _parse_date(day)
    _run_db(lambda: db.remove_holiday(d.isoformat()))
    return _today_payload(d)


# --------------------------------------------------------------------------
# Weekly analytics (historical)
# --------------------------------------------------------------------------

@app.get("/api/analytics")
def analytics(
    weeks: int = Query(8, ge=1, le=52),
    end_date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    end = _parse_date(end_date)
    end_monday = end.fromordinal(end.toordinal() - end.weekday())  # Monday of end week
    start = end_monday.fromordinal(end_monday.toordinal() - 7 * (weeks - 1))
    start_iso, end_iso = start.isoformat(), end.isoformat()

    logs = _run_db(lambda: db.get_logs_between(start_iso, end_iso))
    holidays = {h["date"]: h["reason"] for h in _run_db(lambda: db.get_holidays_between(start_iso, end_iso))}

    by_date: dict[str, list[dict]] = {}
    for row in logs:
        by_date.setdefault(row["date"], []).append(row)

    week_blocks = []
    overall_p = overall_a = 0

    for w in range(weeks):
        week_start = date.fromordinal(start.toordinal() + 7 * w)
        days = []
        wk_p = wk_a = 0
        for i, day_name in enumerate(DAYS):
            d = date.fromordinal(week_start.toordinal() + i)
            iso = d.isoformat()
            records = by_date.get(iso, [])
            p = sum(1 for r in records if r["status"] == "present")
            a = sum(1 for r in records if r["status"] == "absent")
            wk_p += p
            wk_a += a
            days.append({
                "date": iso,
                "day_of_week": day_name,
                "present": p,
                "absent": a,
                "percent": round(100.0 * p / (p + a), 1) if (p + a) else None,
                "holiday_reason": holidays.get(iso),
                "future": d > end,
                "has_classes": (p + a) > 0,
            })
        overall_p += wk_p
        overall_a += wk_a
        week_blocks.append({
            "week_start": week_start.isoformat(),
            "week_end": date.fromordinal(week_start.toordinal() + 6).isoformat(),
            "label": f"{week_start.strftime('%d %b')} – {date.fromordinal(week_start.toordinal() + 6).strftime('%d %b %Y')}",
            "present": wk_p,
            "absent": wk_a,
            "percent": round(100.0 * wk_p / (wk_p + wk_a), 1) if (wk_p + wk_a) else None,
            "days": days,
        })

    return {
        "range": {"start": start_iso, "end": end_iso, "weeks": weeks},
        "overall": {
            "present": overall_p,
            "absent": overall_a,
            "percent": round(100.0 * overall_p / (overall_p + overall_a), 1)
            if (overall_p + overall_a) else None,
            "holidays": len(holidays),
            "logged_classes": overall_p + overall_a,
        },
        "weeks": week_blocks,
    }


# --------------------------------------------------------------------------
# Root health-check (uptime probes / local health checks)
# --------------------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "Attendance API is running"}


# --------------------------------------------------------------------------
# /api fallback — connectivity check for the Vercel rewrite
# (vercel.json routes both "/api/(.*)" and "/api" to this function)
# --------------------------------------------------------------------------
@app.get("/api")
def api_fallback():
    return {"status": "Attendance API is running", "try": "/api/health"}
