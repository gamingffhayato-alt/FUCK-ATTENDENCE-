"""
End-to-end smoke test for the Attendance Portal v3
(manual daily schedule + Groq chatbot).

Runs the REAL FastAPI app against an in-memory fake of the Supabase
PostgREST API (no database / no network needed), with the Groq stream
stubbed for determinism (the live Groq call is verified at deploy time
with your real key).

    python3 smoke_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# In-memory fake of supabase-py (table().select/insert/upsert/delete/execute)
# --------------------------------------------------------------------------

UNIQUE_KEYS = {
    "subjects": ("name",),
    "daily_schedule": ("date", "subject_id"),
    "attendance_log": ("date", "subject_id"),
    "holidays": ("date",),
}
ID_COLS = {"subjects", "daily_schedule", "attendance_log", "holidays"}


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

    def _assert_unique(self, rows, candidate):
        cols = UNIQUE_KEYS.get(self.name)
        if not cols:
            return
        for r in rows:
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
# App setup: fake DB + stubbed Groq stream
# --------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "api"))

import db                     # noqa: E402
import index as appmod        # noqa: E402  (Vercel serverless entrypoint)

db._client = FakeSupabase()

captured_messages: list = []


def fake_stream(messages):
    captured_messages.append(messages)
    return iter(["Hello", " from", " the", " chatbot"])


appmod._open_chat_stream = fake_stream

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


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# Fixed dates: Monday 2026-10-05, Tuesday 2026-10-06
MON, TUE = "2026-10-05", "2026-10-06"
SUBJ_A = "Basics Of Computer and C Programming"
SUBJ_B = "Front-End Web Development"
DEFAULTS = [
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


def main():
    # ------------------------------------------------------------------
    print("\n[1] static deliverables & retirement checks")
    for rel in ("database/schema.sql", "api/index.py", "api/db.py",
                "api/requirements.txt", "index.html", "app.js",
                "styles.css", "vercel.json"):
        check(f"{rel} exists", (ROOT / rel).is_file())

    frontend_ocr = "tesseract" in (read("index.html") + read("app.js") + read("styles.css")).lower()
    check("no Tesseract/OCR in frontend", not frontend_ocr)
    api_src = read("api/index.py") + read("api/db.py")
    check("no groq_analyzer / timetable AI left in backend",
          "groq_analyzer" not in api_src and "analyze_daily_schedule" not in api_src)
    check("no OCR_SPACE key anywhere in code", "OCR_SPACE" not in api_src + read("app.js") + read("index.html"))
    reqs = read("api/requirements.txt")
    check("requirements: fastapi + supabase + groq",
          all(x in reqs for x in ("fastapi", "supabase", "groq")))
    check("requirements: no requests / python-multipart",
          "python-multipart" not in reqs and "requests>" not in reqs and not any(
              l.strip() == "requests" for l in reqs.splitlines()))

    schema = read("database/schema.sql")
    for table in ("subjects", "daily_schedule", "attendance_log", "holidays"):
        check(f"schema creates {table}", f"create table if not exists public.{table}" in schema)
    check("schema seeds all 12 subjects", all(f"'{n}'" in schema for n in DEFAULTS))
    check("schema preserves v2 history (backup rename)", "attendance_log_v2_backup" in schema)
    check("schema frees legacy index names (42P07 fix)",
          "attendance_log_v2_backup_pkey" in schema
          and "attendance_log_v2_backup_date_subject_unique" in schema)

    vc = json.loads(read("vercel.json"))
    srcs = [r.get("source") for r in vc.get("rewrites", [])]
    check("vercel.json rewrites /api/(.*)", "/api/(.*)" in srcs)
    check("vercel.json rewrites /api", "/api" in srcs)
    check("vercel.json maxDuration 60",
          vc.get("functions", {}).get("api/index.py", {}).get("maxDuration") == 60)

    js = read("app.js")
    check("frontend calls /api/chat", '"/api/chat"' in js)
    check("CSS honours [hidden] (chat panel actually closes)",
          "[hidden] { display: none !important; }" in read("styles.css"))
    check("frontend has4 tabs + chat widget",
          read("index.html").count('role="tab"') == 4 and 'id="chatFab"' in read("index.html"))
    check("chat model is openai/gpt-oss-20b", 'CHAT_MODEL = "openai/gpt-oss-20b"' in read("api/index.py"))

    # ------------------------------------------------------------------
    print("\n[2] removed routes + health/root")
    r = client.post("/api/schedule/text", json={"schedule_text": "x"})
    check("old /api/schedule/text removed (404)", r.status_code == 404, str(r.status_code))
    r = client.post("/api/schedule/upload",
                    files={"file": ("t.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    check("old /api/schedule/upload removed (404)", r.status_code == 404, str(r.status_code))
    r = client.get("/api/today")
    check("old /api/today removed (404)", r.status_code == 404, str(r.status_code))
    r = client.get("/api/analytics")
    check("old /api/analytics removed (404)", r.status_code == 404, str(r.status_code))

    h = client.get("/api/health").json()
    check("health keys", all(k in h for k in ("groq", "supabase", "groq_model", "mode")))
    check("health model", h["groq_model"] == "openai/gpt-oss-20b", h.get("groq_model"))
    check("health mode manual-schedule", h["mode"] == "manual-schedule")
    r = client.get("/")
    check("root health-check for uptime probes",
          r.status_code == 200 and r.json() == {"status": "Attendance API is running"}, r.text)
    r = client.get("/api")
    check("/api fallback route answers (Vercel connectivity check)",
          r.status_code == 200 and r.json().get("status") == "Attendance API is running", r.text)

    # ------------------------------------------------------------------
    print("\n[3] subjects CRUD + default seed")
    r = client.get("/api/subjects")
    check("subjects list starts empty (fake DB)", r.status_code == 200 and r.json()["subjects"] == [])

    r = client.post("/api/subjects", json={"name": "  Software   Engineering  "})
    check("add subject 200 (name normalised)",
          r.status_code == 200 and r.json()["subject"]["name"] == "Software Engineering", r.text)
    extra_id = r.json()["subject"]["id"]

    r = client.post("/api/subjects", json={"name": "software engineering"})
    check("duplicate subject → 409", r.status_code == 409, r.text)
    r = client.post("/api/subjects", json={"name": "   "})
    check("whitespace name → 422", r.status_code == 422, str(r.status_code))

    r = client.post("/api/subjects/seed", json={})
    names = [s["name"] for s in r.json()["subjects"]]
    check("seed adds the 12 defaults", len(names) == 13 and all(n in names for n in DEFAULTS), len(names))
    r = client.post("/api/subjects/seed", json={})
    check("seed is idempotent", len(r.json()["subjects"]) == 13)

    by_name = {s["name"]: s["id"] for s in r.json()["subjects"]}
    id_a, id_b = by_name[SUBJ_A], by_name[SUBJ_B]

    r = client.delete(f"/api/subjects/{extra_id}")
    check("delete subject 200", r.status_code == 200, r.text)
    r = client.get("/api/subjects")
    check("back to 12 subjects", len(r.json()["subjects"]) == 12)
    r = client.delete("/api/subjects/99999")
    check("delete unknown subject → 404", r.status_code == 404, str(r.status_code))

    # ------------------------------------------------------------------
    print("\n[4] daily schedule (manual setup)")
    r = client.post("/api/schedule", json={
        "date": MON,
        "entries": [{"subject_id": id_a, "lecture_count": 3},
                    {"subject_id": id_b, "lecture_count": 1}],
    })
    check("save day 200", r.status_code == 200, r.text)
    check("day totals", r.json()["total_lectures"] == 4)

    r = client.get(f"/api/schedule?date={MON}")
    entries = {e["subject_id"]: e["lecture_count"] for e in r.json()["entries"]}
    check("GET day returns all subjects with counts",
          entries.get(id_a) == 3 and entries.get(id_b) == 1 and len(entries) == 12)

    r = client.post("/api/schedule", json={
        "date": MON, "entries": [{"subject_id": 99999, "lecture_count": 2}]})
    check("unknown subject → 422", r.status_code == 422, r.text)
    r = client.post("/api/schedule", json={
        "date": MON, "entries": [{"subject_id": id_a, "lecture_count": 25}]})
    check("lecture_count > 20 → 422", r.status_code == 422, str(r.status_code))

    # zero counts are dropped from storage
    client.post("/api/schedule", json={
        "date": MON, "entries": [{"subject_id": id_a, "lecture_count": 3},
                                 {"subject_id": id_b, "lecture_count": 0}]})
    rows = db.get_schedule_rows_for_date(MON)
    check("zero-count entries not stored",
          len(rows) == 1 and rows[0]["subject_id"] == id_a, rows)

    # replace semantics: second save wins
    client.post("/api/schedule", json={
        "date": TUE, "entries": [{"subject_id": id_a, "lecture_count": 2}]})
    check("two days scheduled", len(db.get_schedule_rows_between(MON, TUE)) == 2)

    r = client.post("/api/schedule", json={"date": MON, "entries": []})
    check("clear day empties it", r.json()["total_lectures"] == 0)
    # restore the scenario used by stats/week checks below
    client.post("/api/schedule", json={
        "date": MON,
        "entries": [{"subject_id": id_a, "lecture_count": 3},
                    {"subject_id": id_b, "lecture_count": 1}]})

    # ------------------------------------------------------------------
    print("\n[5] attendance counts against the scheduled total")
    r = client.post("/api/attendance", json={
        "date": MON, "subject_id": id_a, "present_count": 2, "absent_count": 1})
    check("mark 2P+1A of 3 → 200", r.status_code == 200, r.text)
    check("response echoes scheduled=3", r.json().get("scheduled") == 3)

    r = client.post("/api/attendance", json={
        "date": MON, "subject_id": id_a, "present_count": 3, "absent_count": 1})
    check("marking more than scheduled → 422", r.status_code == 422, r.text)

    r = client.post("/api/attendance", json={
        "date": MON, "subject_id": id_b, "present_count": 1, "absent_count": 0})
    check("second subject marked", r.status_code == 200)

    unscheduled = next(s["id"] for s in client.get("/api/subjects").json()["subjects"]
                       if s["name"] not in (SUBJ_A, SUBJ_B))
    r = client.post("/api/attendance", json={
        "date": MON, "subject_id": unscheduled, "present_count": 1, "absent_count": 0})
    check("unscheduled subject on that date → 409", r.status_code == 409, r.text)

    r = client.post("/api/attendance", json={
        "date": "05-10-2026", "subject_id": id_a, "present_count": 1, "absent_count": 0})
    check("bad date → 422", r.status_code == 422, str(r.status_code))

    rows = db.get_attendance_rows_for_date(MON)
    a_row = next(r for r in rows if r["subject_id"] == id_a)
    check("counts stored per (date, subject)",
          a_row["present_count"] == 2 and a_row["absent_count"] == 1)

    # upsert replaces counts
    client.post("/api/attendance", json={
        "date": MON, "subject_id": id_a, "present_count": 1, "absent_count": 2})
    a_row = next(r for r in db.get_attendance_rows_for_date(MON) if r["subject_id"] == id_a)
    check("re-mark replaces the row", a_row["present_count"] == 1 and a_row["absent_count"] == 2)
    client.post("/api/attendance", json={
        "date": MON, "subject_id": id_a, "present_count": 2, "absent_count": 1})

    r = client.delete(f"/api/attendance?date={MON}&subject_id={id_a}")
    check("clear one subject's marks 200", r.status_code == 200, r.text)
    check("row gone after clear",
          all(r2["subject_id"] != id_a for r2 in db.get_attendance_rows_for_date(MON)))
    client.post("/api/attendance", json={
        "date": MON, "subject_id": id_a, "present_count": 2, "absent_count": 1})
    r = client.delete(f"/api/attendance?date={MON}")
    check("missing subject_id → 422", r.status_code == 422, str(r.status_code))

    # ------------------------------------------------------------------
    print("\n[6] week payload (Mark Attendance tab)")
    r = client.get(f"/api/week?start={MON}")
    w = r.json()
    check("week 200", r.status_code == 200, r.text)
    check("7 days Mon→Sun",
          len(w["days"]) == 7 and w["days"][0]["day_of_week"] == "Monday"
          and w["days"][6]["day_of_week"] == "Sunday")
    mon = w["days"][0]
    check("Monday entries carry counts",
          any(e["subject_id"] == id_a and e["lecture_count"] == 3
              and e["present"] == 2 and e["absent"] == 1 and e["pending"] == 0
              for e in mon["entries"]), mon["entries"])
    check("Monday shows B fully marked", any(
        e["subject_id"] == id_b and e["present"] == 1 and e["pending"] == 0
        for e in mon["entries"]))
    r = client.get("/api/week?start=2026-10-07")  # Wednesday snaps back to Monday
    check("week start snaps to Monday", r.json()["start"] == MON, r.json().get("start"))

    # ------------------------------------------------------------------
    print("\n[7] stats math + holiday effect")
    # pre-holiday: TUE has A×2 scheduled & unmarked; add a mark on TUE to prove clearing
    client.post("/api/attendance", json={
        "date": TUE, "subject_id": id_a, "present_count": 1, "absent_count": 0})

    s = client.get(f"/api/stats?date={MON}").json()
    o = s["overall"]
    check("overall scheduled = 6 (MON 4 + TUE 2)", o["scheduled"] == 6, o)
    check("overall 4P 1A → 80%", o["present"] == 4 and o["absent"] == 1
          and o["percentage"] == 80.0, o)
    check("overall pending = 1", o["pending"] == 1, o)
    sa = next(x for x in s["subjects"] if x["name"] == SUBJ_A)
    check("subject A: 5 sched, 3P 1A → 75%",
          sa["scheduled"] == 5 and sa["present"] == 3 and sa["absent"] == 1
          and sa["percentage"] == 75.0, sa)
    check("12 subjects in stats", len(s["subjects"]) == 12)
    check("today block (MON) has both classes",
          s["today"]["date"] == MON and s["today"]["total_scheduled"] == 4
          and len(s["today"]["entries"]) == 2, s["today"])
    check("tomorrow block = TUE", s["tomorrow"]["date"] == TUE)

    # holiday on TUE clears its marks and excludes its schedule from totals
    r = client.post("/api/holiday", json={"date": TUE, "reason": "Festival"})
    check("mark holiday 200", r.status_code == 200, r.text)
    check("holiday reason stored", r.json()["holiday"]["reason"] == "Festival")
    check("holiday cleared TUE attendance",
          db.get_attendance_rows_for_date(TUE) == [])

    s = client.get(f"/api/stats?date={MON}").json()
    o = s["overall"]
    check("holiday excluded from scheduled (6 → 4)", o["scheduled"] == 4, o)
    check("holiday marks cleared (4P → 3P, 80% → 75%)",
          o["present"] == 3 and o["percentage"] == 75.0, o)

    r = client.post("/api/attendance", json={
        "date": TUE, "subject_id": id_a, "present_count": 1, "absent_count": 0})
    check("marking on holiday → 409", r.status_code == 409, r.text)

    w = client.get(f"/api/week?start={MON}").json()
    check("week shows TUE as holiday with no entries",
          w["days"][1]["is_holiday"] and w["days"][1]["entries"] == []
          and w["days"][1]["holiday_reason"] == "Festival", w["days"][1])

    r = client.delete(f"/api/holidays/{TUE}")
    check("undo holiday 200", r.status_code == 200, r.text)
    s = client.get(f"/api/stats?date={MON}").json()
    check("undo restores TUE schedule (scheduled 4 → 6, marks stay cleared)",
          s["overall"]["scheduled"] == 6 and s["overall"]["present"] == 3, s["overall"])

    # ------------------------------------------------------------------
    print("\n[8] chatbot streaming (Groq stubbed)")
    r = client.post("/api/chat", json={"message": "Can I bunk tomorrow?", "date": MON})
    check("chat 200", r.status_code == 200, r.text)
    check("chat streams plain text",
          (r.headers.get("content-type") or "").startswith("text/plain"), r.headers.get("content-type"))
    check("chat body streamed", r.text == "Hello from the chatbot", r.text)

    check("system prompt captured", len(captured_messages) == 1)
    sys_prompt = captured_messages[0][0]["content"]
    check("system prompt carries live stats snapshot", '"overall"' in sys_prompt)
    check("system prompt carries the percentage formula", "present / (present + absent)" in sys_prompt)
    check("user message passed through",
          captured_messages[0][1]["content"] == "Can I bunk tomorrow?")

    r = client.post("/api/chat", json={})
    check("chat without message → 422", r.status_code == 422, str(r.status_code))
    r = client.post("/api/chat", json={"message": "", "date": MON})
    check("empty message → 422", r.status_code == 422, str(r.status_code))

    print(f"\n=== ALL {len(PASSED)} CHECKS PASSED ===")


if __name__ == "__main__":
    main()
