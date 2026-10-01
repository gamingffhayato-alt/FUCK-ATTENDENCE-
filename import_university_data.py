"""
Import your university attendance report (attt.pdf) into the portal.

Source: Quantum University student-attendance printout, 10/1/26.
The PDF gives PER-SUBJECT aggregates only (no dates):

    subject | present | absent | lecture_schedule | lecture_conducted

This script maps those aggregates onto the portal's day-by-day tables:

  * past   = weekdays 2026-08-03 … 2026-10-01  (assumed semester start)
  * future = weekdays 2026-10-01 … 2026-12-18  (remaining planned lectures)
  * each subject's lectures are spread across those weeks (weekly quota,
    staggered weekdays, weekends off) — a plausible reconstruction, since
    the PDF contains no dates.
  * conducted lectures are placed in the past and fully marked
    (absences interleaved evenly, exact P/A totals preserved).
  * planned-but-not-conducted lectures go to future dates (unmarked = pending).

TOTALS ARE EXACT: scheduled = Σ lecture_schedule, present/absent = the PDF's
counts, percentage = 111/163 = 68.1% — identical to the university portal.

Usage:
    python3 import_university_data.py --dry-run   # show plan, write nothing
    python3 import_university_data.py             # replace the window & import

NOTE: a real run DELETES daily_schedule/attendance_log rows dated inside the
import window (2026-08-03 … 2026-12-18) first — this clears test entries and
makes re-runs deterministic. Holidays and dates outside the window are kept.
"""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))

import db  # noqa: E402

# ---------------------------------------------------------------------------
# Data extracted from attt.pdf (verified: per-subject sums == header totals)
#   name: (lecture_schedule, lecture_conducted, present, absent)
# ---------------------------------------------------------------------------

PDF_DATA = {
    "Basics Of Computer and C Programming":            (43, 17, 15, 2),
    "Basics Of Computer and C Programming Lab":        (28, 12, 10, 2),
    "Front-End Web Development":                       (44, 18, 10, 8),
    "Front-End Web Development Lab":                   (30, 12,  5, 7),
    "Environmental Studies - I":                       (30, 12, 10, 2),
    "Communication & Professional Skills I":           (43, 16, 16, 0),
    "Mini Project-I":                                  (14,  1,  1, 0),
    "Fundamentals Of Intelligent & Autonomous Systems": (42, 17, 12, 5),
    "Technical Training - Advance Programming In C":   (43,  0,  0, 0),
    "Computational & Quantum Physics":                 (57, 23, 13, 10),
    "Computational & Quantum Physics Lab":             (30, 12,  4, 8),
    "Computational Mathematics For Intelligent Systems": (56, 23, 15, 8),
}

# Header totals printed on page 1 of the PDF
PDF_TOTALS = {"schedule": 460, "conducted": 163, "present": 111, "absent": 52,
              "percentage": 68.1}

PAST_START = date(2026, 8, 3)     # assumed semester start (Monday)
PAST_END = date(2026, 10, 1)      # report date (inclusive)
FUTURE_END = date(2026, 12, 18)   # assumed end of semester plan


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:  # Mon–Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def group_by_week(days: list[date]) -> list[list[date]]:
    weeks: "OrderedDict[tuple, list[date]]" = OrderedDict()
    for d in days:
        weeks.setdefault(d.isocalendar()[:2], []).append(d)
    return [sorted(v) for v in weeks.values()]


def distribute(total: int, weeks: list[list[date]], stagger: int) -> dict[date, int]:
    """Spread `total` lectures across weeks (weekly quota + carry), picking
    staggered distinct weekdays inside each week. Guarantees Σ == total."""
    n = len(weeks)
    base, rem = divmod(total, n)
    quotas = [base + (1 if w < rem else 0) for w in range(n)]

    assigned: dict[date, int] = {}
    carry = 0
    for wi, days in enumerate(weeks):
        quota = quotas[wi] + carry
        if not days:
            carry = quota
            continue
        take = min(quota, len(days))
        offset = (stagger + wi) % len(days)
        for j in range(take):
            d = days[(offset + j) % len(days)]
            assigned[d] = assigned.get(d, 0) + 1
        carry = quota - take

    if carry:  # only if some weeks were too short — spill onto any days
        flat = [d for days in weeks for d in days]
        while carry > 0:
            for d in flat:
                assigned[d] = assigned.get(d, 0) + 1
                carry -= 1
                if carry == 0:
                    break

    assert sum(assigned.values()) == total, "distribution lost lectures"
    return assigned


def distribute_even(total: int, days: list[date], stagger: int) -> dict[date, int]:
    """Uniformly spread `total` lectures across `days`, starting near day
    `stagger` (per-subject offset) and running to the end of the window.

    day = stagger + floor(t * span / total),  span = len(days) - stagger

    This never wraps (no tail pile-up on the first days) and never exceeds
    the window: heads land on staggered days (~1 subject starts per day) and
    the flow stays even from there to the end of the semester plan."""
    d_len = len(days)
    assigned: dict[date, int] = {}
    if total <= 0 or d_len == 0:
        return assigned
    off = stagger % d_len
    span = d_len - off
    for t in range(total):
        d = days[off + (t * span) // total]
        assigned[d] = assigned.get(d, 0) + 1
    assert sum(assigned.values()) == total
    return assigned


# ---------------------------------------------------------------------------
# Build rows
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return " ".join(name.replace("–", "-").replace("—", "-").split()).casefold()


def build_rows():
    subjects = db.get_subjects()
    ids = {_norm(s["name"]): s["id"] for s in subjects}
    if not ids:
        raise SystemExit("No subjects found — run database/schema.sql first.")

    missing = [n for n in PDF_DATA if _norm(n) not in ids]
    if missing:
        raise SystemExit(f"Subjects missing in DB: {missing} — run POST /api/subjects/seed.")

    past_weeks = group_by_week(weekdays(PAST_START, PAST_END))

    sched_rows, att_rows = [], []
    summary = []
    tot = dict(schedule=0, conducted=0, present=0, absent=0)

    for i, (name, (sched, conducted, present, absent)) in enumerate(PDF_DATA.items()):
        assert conducted <= sched, f"{name}: conducted > schedule"
        assert present + absent == conducted, f"{name}: present+absent != conducted"
        sid = ids[_norm(name)]

        # past: conducted lectures, fully marked
        past = distribute(conducted, past_weeks, stagger=i)
        # future: remaining plan, unmarked — even day-spread (no quota dumps)
        remaining = sched - conducted
        future = (distribute_even(remaining, weekdays(PAST_END, FUTURE_END), stagger=i)
                  if remaining else {})

        # attendance sequence: absences evenly interleaved, totals preserved
        seq = ["P"] * conducted
        if absent:
            for m in range(absent):
                seq[(m * conducted) // absent] = "A"
        seq_i = 0
        for d in sorted(past):
            k = past[d]
            chunk = seq[seq_i:seq_i + k]
            seq_i += k
            att_rows.append({
                "date": d.isoformat(), "subject_id": sid,
                "present_count": chunk.count("P"), "absent_count": chunk.count("A"),
            })
        assert seq_i == conducted

        for d, k in past.items():
            sched_rows.append({"date": d.isoformat(), "subject_id": sid,
                               "lecture_count": k})
        for d, k in future.items():
            sched_rows.append({"date": d.isoformat(), "subject_id": sid,
                               "lecture_count": k})

        marked = present + absent
        summary.append({
            "name": name, "schedule": sched, "conducted": conducted,
            "present": present, "absent": absent,
            "pct": round(100.0 * present / marked, 1) if marked else None,
        })
        tot["schedule"] += sched
        tot["conducted"] += conducted
        tot["present"] += present
        tot["absent"] += absent

    # totals must equal the PDF header exactly
    assert tot["schedule"] == PDF_TOTALS["schedule"], tot
    assert tot["conducted"] == PDF_TOTALS["conducted"], tot
    assert tot["present"] == PDF_TOTALS["present"], tot
    assert tot["absent"] == PDF_TOTALS["absent"], tot
    pct = round(100.0 * tot["present"] / (tot["present"] + tot["absent"]), 1)
    assert pct == PDF_TOTALS["percentage"], pct

    # no day may exceed the API's per-entry cap
    per_day: dict[tuple, int] = {}
    for r in sched_rows:
        key = (r["date"], r["subject_id"])
        per_day[key] = per_day.get(key, 0) + r["lecture_count"]
    assert max(per_day.values(), default=0) <= 20

    # merge past+future rows sharing a date (PAST_END == FUTURE start) so each
    # (date, subject) appears exactly once — one duplicate in a single upsert
    # batch would abort the whole command with 21000.
    sched_rows = [
        {"date": d, "subject_id": sid, "lecture_count": n}
        for (d, sid), n in sorted(per_day.items())
    ]

    return sched_rows, att_rows, summary, tot, pct


def print_summary(summary, tot, pct, sched_rows, att_rows):
    print(f"\n{'Subject':<46} {'Sched':>5} {'Cond':>5} {'P':>4} {'A':>4} {'%':>6}")
    print("-" * 74)
    for s in summary:
        p = f"{s['pct']}%" if s["pct"] is not None else "—"
        print(f"{s['name']:<46} {s['schedule']:>5} {s['conducted']:>5} "
              f"{s['present']:>4} {s['absent']:>4} {p:>6}")
    print("-" * 74)
    print(f"{'TOTAL (matches PDF header)':<46} {tot['schedule']:>5} "
          f"{tot['conducted']:>5} {tot['present']:>4} {tot['absent']:>4} {pct}%")
    print(f"\nschedule rows: {len(sched_rows)}   attendance rows: {len(att_rows)}   "
          f"window: {PAST_START} → {FUTURE_END}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="plan only, no writes")
    args = ap.parse_args()

    sched_rows, att_rows, summary, tot, pct = build_rows()
    print_summary(summary, tot, pct, sched_rows, att_rows)

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return

    c = db.sb()
    start, end = PAST_START.isoformat(), FUTURE_END.isoformat()
    old_s = (c.table("daily_schedule").delete()
             .gte("date", start).lte("date", end).execute().data or [])
    old_a = (c.table("attendance_log").delete()
             .gte("date", start).lte("date", end).execute().data or [])
    print(f"\ncleared existing rows in window: {len(old_s)} schedule, {len(old_a)} attendance")

    for i in range(0, len(sched_rows), 200):
        (c.table("daily_schedule").upsert(sched_rows[i:i + 200],
                                          on_conflict="date,subject_id").execute())
    for i in range(0, len(att_rows), 200):
        (c.table("attendance_log").upsert(att_rows[i:i + 200],
                                          on_conflict="date,subject_id").execute())

    print(f"imported {len(sched_rows)} schedule rows + {len(att_rows)} attendance rows ✓")
    print(f"overall: {tot['present']} present / {tot['absent']} absent = {pct}% "
          f"of {tot['conducted']} conducted (planned {tot['schedule']})")


if __name__ == "__main__":
    main()
