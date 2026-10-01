"""
End-to-end smoke test for the Attendance Portal.

Runs the REAL FastAPI app + REAL OCR against an in-memory fake of the
Supabase PostgREST API (no database needed), with the Groq call stubbed
for determinism (the live Groq integration is verified separately).

    cd backend && python3 smoke_test.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from types import SimpleNamespace

# --------------------------------------------------------------------------
# In-memory fake of supabase-py (table().select/insert/upsert/delete/execute)
# --------------------------------------------------------------------------

UNIQUE_KEYS = {
    "attendance_log": ("date", "subject_name"),
    "holidays": ("date",),
    "weekly_schedule": ("day_of_week", "subject_name"),
}
ID_COLS = {"weekly_schedule", "attendance_log", "holidays"}


class FakeResult(SimpleNamespace):
    pass


class FakeTable:
    def __init__(self, store: dict, name: str):
        self.store, self.name = store, name
        self._mode = None
        self._filters: list = []
        self._payload = None
        self._on_conflict = None
        self._order = None
        self._limit = None

    # chainable API (mirrors postgrest-py) ---------------------------------
    def select(self, cols="*"):
        if self._mode is None:
            self._mode = "select"
        return self

    def insert(self, rows):
        self._mode = "insert"
        self._payload = rows
        return self

    def upsert(self, row, on_conflict=None):
        self._mode = "upsert"
        self._payload = row
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def order(self, col, asc=True):
        self._order = (col, asc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    # execution -------------------------------------------------------------
    def _match(self, row) -> bool:
        for op, col, val in self._filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "gte" and not str(row.get(col)) >= str(val):
                return False
            if op == "lte" and not str(row.get(col)) <= str(val):
                return False
        return True

    def _next_id(self, rows) -> int:
        return max((r.get("id", 0) for r in rows), default=0) + 1

    def _assert_unique(self, rows, candidate, skip_row=None):
        cols = UNIQUE_KEYS.get(self.name)
        if not cols:
            return
        for r in rows:
            if r is skip_row:
                continue
            if all(r.get(c) == candidate.get(c) for c in cols):
                raise Exception(
                    f'duplicate key value violates unique constraint "{self.name}_{"_".join(cols)}_unique"'
                )

    def execute(self) -> FakeResult:
        rows = self.store.setdefault(self.name, [])

        if self._mode == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                row = dict(p)
                self._assert_unique(rows, row)
                if self.name in ID_COLS:
                    row["id"] = self._next_id(rows)
                rows.append(row)
                out.append(dict(row))
            return FakeResult(data=out)

        if self._mode == "upsert":
            p = dict(self._payload)
            cols = [c.strip() for c in (self._on_conflict or "").split(",") if c.strip()]
            hit = None
            if cols:
                for r in rows:
                    if all(r.get(c) == p.get(c) for c in cols):
                        hit = r
                        break
            if hit is not None:
                hit.update(p)
                return FakeResult(data=[dict(hit)])
            self._assert_unique(rows, p)
            if self.name in ID_COLS:
                p["id"] = self._next_id(rows)
            rows.append(p)
            return FakeResult(data=[dict(p)])

        if self._mode == "delete":
            keep = [r for r in rows if not self._match(r)]
            removed = len(rows) - len(keep)
            rows[:] = keep
            return FakeResult(data=[])

        # select
        out = [dict(r) for r in rows if self._match(r)]
        if self._order:
            col, asc = self._order
            out.sort(key=lambda r: r.get(col), reverse=not asc)
        if self._limit is not None:
            out = out[: self._limit]
        return FakeResult(data=out)


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list] = {}

    def table(self, name: str) -> FakeTable:
        return FakeTable(self.store, name)


# --------------------------------------------------------------------------
# App setup: fake DB + stubbed Groq (real OCR.space call still runs)
# --------------------------------------------------------------------------

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))

import db                     # noqa: E402
import index as appmod        # noqa: E402  (Vercel serverless entrypoint)

db._client = FakeSupabase()

CANNED_MODEL_OUTPUT = {
    "Monday": ["Engineering Mathematics", "Physics", "EVS"],
    "Tuesday": ["Engineering Mathematics", "Chemistry", "Physics"],
    "Wednesday": ["Computer Networks", "EVS", "Chemistry"],
    "Thursday": ["Engineering Mathematics", "Physics", "Chemistry"],
    "Friday": ["Computer Networks", "EVS", "Engineering Mathematics"],
}
appmod.analyze_daily_schedule = lambda text: json.loads(json.dumps(CANNED_MODEL_OUTPUT))

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(appmod.app)

PASSED = []


def check(name: str, cond: bool, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ FAILED: {name} {detail}")
        sys.exit(1)


# Monday 2026-10-05, Saturday 2026-10-03, previous Monday 2026-09-28
MON, SAT, PREV_MON = "2026-10-05", "2026-10-03", "2026-09-28"


def main():
    print("\n[1] schedule pipelines")
    r = client.post("/api/schedule/text", json={"schedule_text": "Monday: Physics"})
    check("text pipeline 200", r.status_code == 200, r.text)
    check("weekly_schedule saved", db.get_schedule()["Monday"] == CANNED_MODEL_OUTPUT["Monday"])

    img_bytes = open(
        "/home/user/attendance-portal/test_timetable.png", "rb"
    ).read() if _has_sample() else None
    if img_bytes:
        r = client.post(
            "/api/schedule/upload",
            files={"file": ("timetable.png", img_bytes, "image/png")},
        )
        if r.status_code == 503 and "ocr.space" in r.text.lower():
            # external dependency: the shared "helloworld" key gets throttled —
            # not a code regression (set your own OCR_SPACE_API_KEY to avoid it)
            print("  ~ SKIPPED image upload: OCR.space is throttling the shared test key "
                  "(set your own OCR_SPACE_API_KEY)")
        else:
            check("image upload 200", r.status_code == 200, r.text)
            body = r.json()
            check("OCR text captured", "Monday" in body["extracted_text"])
            check("schedule replaced from image",
                  body["schedule"]["Tuesday"] == CANNED_MODEL_OUTPUT["Tuesday"])

    print("\n[2] today: exact day filtering")
    r = client.get(f"/api/today?date={MON}")
    t = r.json()
    check("day_of_week computed", t["day_of_week"] == "Monday")
    check("only Monday's subjects", t["subjects"] == CANNED_MODEL_OUTPUT["Monday"])
    check("not a holiday/weekend", not t["is_holiday"] and not t["is_weekend"])

    tue = client.get("/api/today?date=2026-10-06").json()
    check("Tuesday filter (no EVS from Wed etc.)",
          tue["subjects"] == CANNED_MODEL_OUTPUT["Tuesday"])

    weekend = client.get(f"/api/today?date={SAT}").json()
    check("weekend flagged, no Sat classes", weekend["is_weekend"] and weekend["subjects"] == [])

    print("\n[3] day-by-day attendance logging")
    subj = "Engineering Mathematics"
    r = client.post("/api/attendance",
                    json={"date": MON, "subject_name": subj, "status": "present"})
    check("mark present 200", r.status_code == 200, r.text)
    recs = db.get_records_for_date(MON)
    check("one row for (date, subject)", len(recs) == 1 and recs[0]["status"] == "present")

    client.post("/api/attendance",
                json={"date": MON, "subject_name": subj, "status": "absent"})
    recs = db.get_records_for_date(MON)
    check("switch present→absent stays one row", len(recs) == 1 and recs[0]["status"] == "absent")

    client.post("/api/attendance",
                json={"date": MON, "subject_name": "Physics", "status": "present"})
    check("second subject logged", len(db.get_records_for_date(MON)) == 2)
    t = client.get(f"/api/today?date={MON}").json()
    check("today shows marks", t["records"] == {subj: "absent", "Physics": "present"})

    r = client.delete(f"/api/attendance?date={MON}&subject_name={subj}")
    check("delete record 200", r.status_code == 200)
    check("record gone", len(db.get_records_for_date(MON)) == 1)

    r = client.post("/api/attendance",
                    json={"date": MON, "subject_name": "X", "status": "maybe"})
    check("bad status → 422", r.status_code == 422, str(r.status_code))

    print("\n[4] holiday feature")
    r = client.post("/api/holiday", json={"date": MON, "reason": "College function"})
    t = r.json()
    check("holiday flagged", t["is_holiday"] and t["holidays"] if False else t["is_holiday"])
    check("schedule cleared from screen", t["subjects"] == [])
    check("day's records wiped (no penalty)",
          db.get_records_for_date(MON) == [], db.get_records_for_date(MON))
    check("reason stored", t["holiday_reason"] == "College function")

    r = client.post("/api/attendance",
                    json={"date": MON, "subject_name": "Physics", "status": "absent"})
    check("marking on holiday → 409", r.status_code == 409, r.text)

    r = client.delete(f"/api/holidays/{MON}")
    t = r.json()
    check("undo holiday restores schedule", not t["is_holiday"] and t["subjects"] == CANNED_MODEL_OUTPUT["Monday"])

    print("\n[5] weekly analytics (historical)")
    # previous week: Mon 09-28 → 2P+1A that week
    for s, st in [("Maths", "present"), ("Physics", "present"), ("EVS", "absent")]:
        client.post("/api/attendance",
                    json={"date": PREV_MON, "subject_name": s, "status": st})
    client.post("/api/holiday", json={"date": "2026-10-01", "reason": "Holiday test"})
    client.post("/api/attendance",
                json={"date": MON, "subject_name": "Maths", "status": "present"})

    r = client.get("/api/analytics?weeks=2&end_date=2026-10-07")
    check("analytics 200", r.status_code == 200, r.text)
    a = r.json()
    check("two week blocks", len(a["weeks"]) == 2)
    w1, w2 = a["weeks"]
    check("prev week label", w1["week_start"] == "2026-09-28")
    check("prev week 2P+1A → 66.7%", w1["percent"] == 66.7, str(w1["percent"]))
    day_map = {d["date"]: d for d in w1["days"]}
    check("holiday day recorded", day_map["2026-10-01"]["holiday_reason"] == "Holiday test")
    check("holiday day has no penalty", day_map["2026-10-01"]["percent"] is None)
    w2_days = {d["date"]: d for d in w2["days"]}
    check("days after end_date are future", w2_days["2026-10-08"]["future"] is True)
    check("end_date itself is not future", w2_days["2026-10-07"]["future"] is False)
    check("today counted this week", w2_days[MON]["present"] == 1)
    check("overall 3P 1A → 75.0%", a["overall"]["percent"] == 75.0, str(a["overall"]))

    print("\n[6] health & root endpoint")
    r = client.get("/api/health").json()
    check("health keys", all(k in r for k in ("groq", "ocr", "supabase")))
    check("ocr ready", r["ocr"] is True)
    r = client.get("/")
    check("root health-check for uptime probes",
          r.status_code == 200 and r.json() == {"status": "Attendance API is running"},
          r.text)
    r = client.get("/api")
    check("/api fallback route answers (Vercel connectivity check)",
          r.status_code == 200 and r.json().get("status") == "Attendance API is running",
          r.text)

    print("\n[7] schedule replace + ordering")
    check("replace dedupes (case-insensitive)",
          db.replace_schedule({"Monday": ["Physics", "physics", "Chemistry"]})["Monday"]
          == ["Physics", "Chemistry"])

    print(f"\n=== ALL {len(PASSED)} CHECKS PASSED ===")


def _has_sample() -> bool:
    import os
    return os.path.exists("/home/user/attendance-portal/test_timetable.png")


if __name__ == "__main__":
    main()
