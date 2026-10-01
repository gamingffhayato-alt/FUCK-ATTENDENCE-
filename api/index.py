"""
Attendance Tracking Portal — FastAPI backend v3 (manual schedule + chatbot).

No OCR / image parsing / timetable extraction anywhere: lecture counts are
entered manually in the Manage tab, attendance is marked per scheduled
class, and Groq powers ONLY the attendance chatbot (/api/chat).

Routes
------
GET    /api/health                       config status (groq / supabase / mode)
GET    /api/subjects                     list subjects
POST   /api/subjects                     add a subject
DELETE /api/subjects/{id}                delete a subject (cascade)
POST   /api/subjects/seed                re-insert the default Semester-1 12
GET    /api/schedule?date=YYYY-MM-DD     one day's manual schedule
POST   /api/schedule                     replace a day {date, entries[]}
GET    /api/week?start=YYYY-MM-DD        full week payload (Mon..Sun) for Tab 4
POST   /api/attendance                   {date, subject_id, present_count, absent_count}
POST   /api/attendance/batch             finalize a whole day in ONE request (replace-day)
DELETE /api/attendance?date=&subject_id= clear one subject's marks for a day
POST   /api/holiday                      {date, reason?} + clear day (no penalty)
DELETE /api/holidays/{date}              undo a holiday
POST   /api/history/clear                {before?} wipe day-wise logs, keep subjects baseline
GET    /api/stats?date=YYYY-MM-DD        cumulative = subjects.initial_* + day logs
POST   /api/chat                         streaming Groq chatbot (stats-injected)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Vercel runs this file as the serverless entrypoint — make sibling modules
# (db.py) importable regardless of the working dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
from db import DEFAULT_SUBJECTS, TablesMissingError  # noqa: E402

# Chatbot model — llama3-8b-8192 was shut down (08/2025) and its successor
# llama-3.1-8b-instant was shut down (08/2026); Groq's official fast
# replacement for that class is openai/gpt-oss-20b.
CHAT_MODEL = "openai/gpt-oss-20b"

GROQ_IMPORT_ERROR: Optional[str] = None
try:
    from groq import Groq
except Exception as _exc:  # package missing
    Groq = None  # type: ignore[assignment]
    GROQ_IMPORT_ERROR = str(_exc)

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

app = FastAPI(title="Attendance Tracking Portal API", version="3.0.0")
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

class SubjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class ScheduleEntryIn(BaseModel):
    subject_id: int = Field(ge=1)
    lecture_count: int = Field(ge=0, le=20)


class ScheduleIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    entries: list[ScheduleEntryIn] = Field(default_factory=list, max_length=100)


class AttendanceIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    subject_id: int = Field(ge=1)
    present_count: int = Field(ge=0, le=20)
    absent_count: int = Field(ge=0, le=20)


class HolidayIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(default="Holiday", max_length=200)


class BatchEntryIn(BaseModel):
    subject_id: int = Field(ge=1)
    present_count: int = Field(ge=0, le=20)
    absent_count: int = Field(ge=0, le=20)


class BatchIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    entries: list[BatchEntryIn] = Field(default_factory=list, max_length=100)


class HistoryClearIn(BaseModel):
    before: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


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


def _pct(present: int, absent: int) -> Optional[float]:
    """percentage = present / (present + absent); null when nothing is marked."""
    marked = present + absent
    return round(100.0 * present / marked, 1) if marked else None


def _baseline(row: dict) -> dict:
    """Baseline (pre-day-wise) totals stored on the subject row.
    .get() keeps older databases (columns not yet added) working at 0."""
    return {
        "total": int(row.get("initial_total") or 0),
        "present": int(row.get("initial_present") or 0),
        "absent": int(row.get("initial_absent") or 0),
    }


def _monday(d: date) -> date:
    return d.fromordinal(d.toordinal() - d.weekday())


# --------------------------------------------------------------------------
# Aggregation (pure functions over rows — unit-testable)
# --------------------------------------------------------------------------

def _build_stats(today_iso: str) -> dict:
    """Cumulative stats: baseline (subjects.initial_*) + day-wise logs.

        Overall Present = initial_present + Σ(day-log present)
        Overall Absent  = initial_absent  + Σ(day-log absent)
        Overall Total   = initial_total   + Σ(day-log scheduled)
    """
    subjects = _run_db(db.get_subjects)
    sched_rows = _run_db(db.get_all_schedule_rows)
    att_rows = _run_db(db.get_all_attendance_rows)
    holidays = _run_db(db.get_all_holidays)
    holiday_set = {h["date"] for h in holidays}
    names = {s["id"]: s["name"] for s in subjects}

    def slot_for(sid: int, name: str) -> dict:
        return {
            "id": sid, "name": name, "baseline": {"total": 0, "present": 0, "absent": 0},
            "day_scheduled": 0, "day_present": 0, "day_absent": 0,
        }

    agg: dict[int, dict] = {}
    for s in subjects:
        agg[s["id"]] = {
            "id": s["id"], "name": s["name"], "baseline": _baseline(s),
            "day_scheduled": 0, "day_present": 0, "day_absent": 0,
        }

    # day-wise scheduled counts only from non-holiday dates
    for r in sched_rows:
        if r["date"] in holiday_set:
            continue
        sid = r["subject_id"]
        if sid not in agg:
            agg[sid] = slot_for(sid, names.get(sid, f"Subject #{sid}"))
        agg[sid]["day_scheduled"] += int(r["lecture_count"])

    for r in att_rows:
        sid = r["subject_id"]
        if sid not in agg:
            agg[sid] = slot_for(sid, names.get(sid, f"Subject #{sid}"))
        agg[sid]["day_present"] += int(r["present_count"])
        agg[sid]["day_absent"] += int(r["absent_count"])

    subject_stats = []
    tot_sched = tot_p = tot_a = 0
    base_t = base_p = base_a = 0
    for s in sorted(agg.values(), key=lambda x: x["name"].casefold()):
        b = s["baseline"]
        scheduled = b["total"] + s["day_scheduled"]
        present = b["present"] + s["day_present"]
        absent = b["absent"] + s["day_absent"]
        marked = present + absent
        entry = {
            "id": s["id"],
            "name": s["name"],
            "baseline": b,
            "scheduled": scheduled,
            "present": present,
            "absent": absent,
            "marked": marked,
            "pending": max(0, scheduled - marked),
            "percentage": _pct(present, absent),
        }
        subject_stats.append(entry)
        tot_sched += scheduled
        tot_p += present
        tot_a += absent
        base_t += b["total"]
        base_p += b["present"]
        base_a += b["absent"]

    def day_block(iso: str) -> dict:
        is_hol = iso in holiday_set
        d = date.fromisoformat(iso)
        entries = []
        if not is_hol:
            by_subject = {e["subject_id"]: e for e in entries_for(sched_rows, iso)}
            att_by_subject = {a["subject_id"]: a for a in entries_for(att_rows, iso)}
            for sid, e in by_subject.items():
                a = att_by_subject.get(sid, {})
                p, ab = int(a.get("present_count", 0)), int(a.get("absent_count", 0))
                sched = int(e["lecture_count"])
                entries.append({
                    "subject_id": sid,
                    "subject": names.get(sid, f"Subject #{sid}"),
                    "scheduled": sched,
                    "present": p,
                    "absent": ab,
                    "pending": max(0, sched - p - ab),
                })
            entries.sort(key=lambda x: x["subject"].casefold())
        hol = next((h for h in holidays if h["date"] == iso), None)
        return {
            "date": iso,
            "day_of_week": d.strftime("%A"),
            "is_holiday": is_hol,
            "holiday_reason": hol["reason"] if hol else None,
            "total_scheduled": sum(e["scheduled"] for e in entries),
            "entries": entries,
        }

    today = date.fromisoformat(today_iso)
    tomorrow = today + timedelta(days=1)

    return {
        "as_of": today_iso,
        "overall": {
            "scheduled": tot_sched,
            "present": tot_p,
            "absent": tot_a,
            "marked": tot_p + tot_a,
            "pending": max(0, tot_sched - (tot_p + tot_a)),
            "percentage": _pct(tot_p, tot_a),
            "holidays": len(holidays),
            "subjects": len(subjects),
            "baseline": {"total": base_t, "present": base_p, "absent": base_a},
        },
        "subjects": subject_stats,
        "today": day_block(today.isoformat()),
        "tomorrow": day_block(tomorrow.isoformat()),
    }


def entries_for(rows: list[dict], iso: str) -> list[dict]:
    return [r for r in rows if r.get("date") == iso]


def _build_week(start: date) -> dict:
    end = start + timedelta(days=6)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    sched_rows = _run_db(lambda: db.get_schedule_rows_between(start_iso, end_iso))
    att_rows = _run_db(lambda: db.get_attendance_rows_between(start_iso, end_iso))
    holidays = _run_db(lambda: db.get_holidays_between(start_iso, end_iso))
    subjects = _run_db(db.get_subjects)
    names = {s["id"]: s["name"] for s in subjects}
    holiday_map = {h["date"]: h["reason"] for h in holidays}

    days = []
    for i, day_name in enumerate(DAYS):
        d = start + timedelta(days=i)
        iso = d.isoformat()
        is_holiday = iso in holiday_map
        day_sched = [r for r in sched_rows if r["date"] == iso]
        day_att = {r["subject_id"]: r for r in att_rows if r["date"] == iso}

        entries = []
        if not is_holiday:
            for r in sorted(day_sched, key=lambda x: names.get(x["subject_id"], "").casefold()):
                a = day_att.get(r["subject_id"], {})
                p = int(a.get("present_count", 0))
                ab = int(a.get("absent_count", 0))
                sched = int(r["lecture_count"])
                entries.append({
                    "subject_id": r["subject_id"],
                    "subject": names.get(r["subject_id"], f"Subject #{r['subject_id']}"),
                    "lecture_count": sched,
                    "present": p,
                    "absent": ab,
                    "pending": max(0, sched - p - ab),
                })

        days.append({
            "date": iso,
            "day_of_week": day_name,
            "is_holiday": is_holiday,
            "holiday_reason": holiday_map.get(iso),
            "entries": entries,
            "total_scheduled": sum(e["lecture_count"] for e in entries),
            "present": sum(e["present"] for e in entries),
            "absent": sum(e["absent"] for e in entries),
        })

    return {
        "start": start_iso,
        "end": end_iso,
        "label": f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}",
        "days": days,
    }


def _day_schedule_payload(iso: str) -> dict:
    sched_rows = _run_db(lambda: db.get_schedule_rows_for_date(iso))
    subjects = _run_db(db.get_subjects)
    by_id = {r["subject_id"]: int(r["lecture_count"]) for r in sched_rows}
    holiday = _run_db(lambda: db.get_holiday(iso))
    entries = []
    for s in subjects:
        entries.append({
            "subject_id": s["id"],
            "subject": s["name"],
            "lecture_count": by_id.get(s["id"], 0),
        })
    return {
        "date": iso,
        "is_holiday": bool(holiday),
        "holiday_reason": holiday["reason"] if holiday else None,
        "entries": entries,
        "total_lectures": sum(by_id.values()),
    }


# --------------------------------------------------------------------------
# Groq chatbot (stats injected into a system prompt, response streamed)
# --------------------------------------------------------------------------

def _groq_client():
    if Groq is None:
        raise HTTPException(
            status_code=503,
            detail=f"Groq unavailable: {GROQ_IMPORT_ERROR or 'groq package not installed'}",
        )
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise HTTPException(status_code=503, detail="GROQ_API_KEY missing — set it in Vercel env vars.")
    try:
        return Groq()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Groq unavailable: {exc}")


def _chat_system_prompt(stats: dict) -> str:
    snapshot = json.dumps(stats, ensure_ascii=False, separators=(",", ": "))
    return (
        "You are the Attendance Assistant inside the user's Semester-1 attendance "
        "portal. You are given a JSON snapshot of their LIVE attendance data — it is "
        "authoritative; never invent subjects or numbers.\n"
        f"Snapshot:\n{snapshot}\n\n"
        "Rules:\n"
        "- percentage = round(100 * present / (present + absent), 1). Pending "
        "(unmarked) lectures do not affect it yet.\n"
        "- 'scheduled' counts lectures planned on non-holiday dates only; holiday "
        "days are already excluded and never penalise the user.\n"
        "- For bunk questions ('can I skip X tomorrow and stay above 75%?') use "
        "tomorrow's entries from the snapshot and show the arithmetic briefly: "
        "new_percentage = 100 * present / (present + absent + skipped).\n"
        "- For schedule questions read today's/tomorrow's entries from the snapshot; "
        "if a day is a holiday or has no entries, say so.\n"
        "- Be friendly and concise (2-5 sentences; short lists when helpful) and "
        "reply in the user's language.\n"
        "- Only attendance/schedule topics — politely decline anything else."
    )


def _open_chat_stream(messages: list[dict]) -> Iterator[str]:
    """Open the Groq stream. Raises HTTPException on auth/config problems.

    Kept as a module-level seam so tests can substitute a fake stream.
    """
    client = _groq_client()
    try:
        stream = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            stream=True,
            temperature=0.6,
            max_completion_tokens=1024,
            reasoning_effort="low",
            stop=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Groq call failed: {exc}")

    def gen() -> Iterator[str]:
        for chunk in stream:
            if not chunk.choices:  # skip trailing usage-only chunks
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    return gen()


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    groq_ok = Groq is not None and bool(os.getenv("GROQ_API_KEY", "").strip())
    return {
        "status": "ok",
        "groq": groq_ok,
        "groq_error": GROQ_IMPORT_ERROR,
        "groq_model": CHAT_MODEL,
        "groq_purpose": "attendance chatbot",
        "supabase": bool(os.getenv("SUPABASE_URL", "").strip()),
        "mode": "manual-schedule",
    }


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------

@app.get("/api/subjects")
def list_subjects():
    return {"subjects": _run_db(db.get_subjects)}


@app.post("/api/subjects")
def add_subject(body: SubjectIn):
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Subject name is empty")
    existing = _run_db(db.get_subjects)
    if any(s["name"].casefold() == name.casefold() for s in existing):
        raise HTTPException(status_code=409, detail=f"Subject '{name}' already exists")
    try:
        subject = _run_db(lambda: db.insert_subject(name))
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            raise HTTPException(status_code=409, detail=f"Subject '{name}' already exists")
        raise
    return {"ok": True, "subject": subject}


@app.delete("/api/subjects/{subject_id}")
def remove_subject(subject_id: int):
    # explicit existence check first — PostgREST deletes are not relied upon
    # to return the removed rows (varies by client configuration)
    exists = any(s["id"] == subject_id for s in _run_db(db.get_subjects))
    if not exists:
        raise HTTPException(status_code=404, detail=f"No subject with id {subject_id}")
    _run_db(lambda: db.delete_subject(subject_id))
    return {"ok": True}


@app.post("/api/subjects/seed")
def seed_subjects():
    """Idempotently insert the default Semester-1 subject list."""
    return {"subjects": _run_db(lambda: db.seed_subjects(DEFAULT_SUBJECTS))}


# --------------------------------------------------------------------------
# Daily schedule (manual setup)
# --------------------------------------------------------------------------

@app.get("/api/schedule")
def get_day_schedule(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return _day_schedule_payload(_parse_date(date).isoformat())


@app.post("/api/schedule")
def set_day_schedule(body: ScheduleIn):
    d = _parse_date(body.date)
    iso = d.isoformat()

    known = {s["id"] for s in _run_db(db.get_subjects)}
    merged: dict[int, int] = {}
    for e in body.entries:
        if e.subject_id not in known:
            raise HTTPException(status_code=422, detail=f"Unknown subject_id {e.subject_id}")
        if e.lecture_count > 0:
            merged[e.subject_id] = merged.get(e.subject_id, 0) + e.lecture_count
    entries = [
        {"subject_id": sid, "lecture_count": min(cnt, 50)}
        for sid, cnt in merged.items()
        if cnt > 0
    ]

    _run_db(lambda: db.replace_schedule_for_date(iso, entries))
    return _day_schedule_payload(iso)


@app.get("/api/week")
def get_week(start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    d = _parse_date(start)
    monday = _monday(d)  # snap any date back to its Monday
    return _build_week(monday)


# --------------------------------------------------------------------------
# Attendance (counts per scheduled class)
# --------------------------------------------------------------------------

@app.post("/api/attendance")
def mark_attendance(body: AttendanceIn):
    d = _parse_date(body.date)
    iso = d.isoformat()

    if _run_db(lambda: db.get_holiday(iso)):
        raise HTTPException(status_code=409, detail=f"{iso} is a holiday — nothing to mark")

    sched_rows = _run_db(lambda: db.get_schedule_rows_for_date(iso))
    sched = next((int(r["lecture_count"]) for r in sched_rows
                  if r["subject_id"] == body.subject_id), None)
    if sched is None:
        raise HTTPException(
            status_code=409,
            detail="No classes are scheduled for that subject on this date — "
                   "set the day's lectures in Manage Semester first.",
        )

    total = body.present_count + body.absent_count
    if total > sched:
        raise HTTPException(
            status_code=422,
            detail=f"Only {sched} lecture(s) scheduled that day — you tried to mark {total}.",
        )

    record = _run_db(
        lambda: db.upsert_attendance(iso, body.subject_id,
                                     body.present_count, body.absent_count)
    )
    return {"ok": True, "record": record, "scheduled": sched}


@app.delete("/api/attendance")
def unmark_attendance(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    subject_id: int = Query(..., ge=1),
):
    _run_db(lambda: db.delete_attendance(date, subject_id))
    return {"ok": True}


@app.post("/api/attendance/batch")
def mark_attendance_batch(body: BatchIn):
    """Finalize a whole day in one request (Replace-Day semantics).

    The submitted entries become the day's complete attendance log:
    subjects sent with 0/0 are unmarked, subjects omitted from `entries`
    are cleared for that date. No writes happen if any entry fails
    validation (holiday / unscheduled / over-scheduled).
    """
    d = _parse_date(body.date)
    iso = d.isoformat()

    if _run_db(lambda: db.get_holiday(iso)):
        raise HTTPException(status_code=409, detail=f"{iso} is a holiday — nothing to mark")

    sched_map = {
        r["subject_id"]: int(r["lecture_count"])
        for r in _run_db(lambda: db.get_schedule_rows_for_date(iso))
    }
    names = {s["id"]: s["name"] for s in _run_db(db.get_subjects)}

    for e in body.entries:
        cnt = sched_map.get(e.subject_id)
        if cnt is None:
            raise HTTPException(
                status_code=422,
                detail=f"“{names.get(e.subject_id, f'#{e.subject_id}')}” has no classes "
                       f"scheduled on {iso} — set the day's lectures in Manage Semester first.",
            )
        if e.present_count + e.absent_count > cnt:
            raise HTTPException(
                status_code=422,
                detail=f"Only {cnt} lecture(s) of "
                       f"“{names.get(e.subject_id, f'#{e.subject_id}')}” scheduled on {iso} — "
                       f"you tried to mark {e.present_count + e.absent_count}.",
            )

    saved = _run_db(lambda: db.replace_attendance_for_date(
        iso, [e.model_dump() for e in body.entries]))
    return {"ok": True, "date": iso, "saved": len(saved),
            "entries": [{"subject_id": e.subject_id,
                         "present_count": e.present_count,
                         "absent_count": e.absent_count} for e in body.entries]}


@app.post("/api/history/clear")
def clear_history(body: Optional[HistoryClearIn] = None):
    """Delete day-wise logs (daily_schedule + attendance_log), optionally only
    rows dated before `before` (e.g. this week's Monday). The subjects table —
    including baseline figures (initial_*) — is never modified."""
    before = body.before if body else None
    if before:
        _parse_date(before)
    counts = _run_db(lambda: db.clear_daily_history(before))
    return {"ok": True, "before": before, "deleted": counts,
            "baseline_preserved": True}


# --------------------------------------------------------------------------
# Holidays (clear a day — never penalised)
# --------------------------------------------------------------------------

@app.post("/api/holiday")
def mark_holiday(body: HolidayIn):
    d = _parse_date(body.date)
    iso = d.isoformat()
    holiday = _run_db(lambda: db.set_holiday(iso, body.reason.strip() or "Holiday"))
    _run_db(lambda: db.clear_attendance_for_date(iso))  # never penalise a holiday
    return {"ok": True, "holiday": holiday}


@app.delete("/api/holidays/{day}")
def unmark_holiday(day: str):
    d = _parse_date(day)
    _run_db(lambda: db.remove_holiday(d.isoformat()))
    # attendance already cleared when the holiday was set — re-mark if needed
    return {"ok": True, "date": d.isoformat()}


# --------------------------------------------------------------------------
# Stats (Dashboard + Subject-wise tabs, chatbot snapshot)
# --------------------------------------------------------------------------

@app.get("/api/stats")
def stats(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return _build_stats(_parse_date(date).isoformat())


# --------------------------------------------------------------------------
# Chatbot — stats injected into the system prompt, reply streamed back
# --------------------------------------------------------------------------

@app.post("/api/chat")
def chat(body: ChatIn):
    today = body.date or datetime.utcnow().date().isoformat()
    stats_payload = _build_stats(today)
    messages = [
        {"role": "system", "content": _chat_system_prompt(stats_payload)},
        {"role": "user", "content": body.message.strip()},
    ]
    stream = _open_chat_stream(messages)
    return StreamingResponse(stream, media_type="text/plain; charset=utf-8")


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
