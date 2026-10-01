# 📅 Attendance Portal — Vercel Single-Repo Deployment

Day-by-day attendance tracking built around **manual daily setup** and an **AI
attendance chatbot**: FastAPI + Groq + Supabase, deployed as **one repository
on Vercel** — static frontend at the root, backend as a Python serverless
function under `/api/*`.

> **No OCR, no image parsing, no timetable AI.** You tell the portal how many
> lectures of each subject happen on a date; you mark each scheduled class
> Present/Absent; Groq powers only the floating **Attendance Chatbot**, which
> receives your live stats and streams answers ("Can I bunk C Programming
> tomorrow and stay above 75%?").

## How the data flows

```
subjects (seeded ×12)
   └─ daily_schedule  (date, subject_id, lecture_count)   ← Manage Semester tab
        └─ attendance_log (date, subject_id, present_count, absent_count)
             └─ stats: % = present / (present + absent)   ← Dashboard / Subject Stats
                  └─ /api/chat: stats snapshot → Groq → streamed reply
holidays (date, reason) clear a day — excluded from "scheduled", never penalise
```

## Setup

1. **Database** — Supabase → SQL Editor → New query → paste
   `database/schema.sql` → **Run**. It creates the four tables, enables RLS
   with permissive policies (the backend uses your publishable key), and
   seeds your **12 Semester-1 subjects**. Your v2 (OCR-era) attendance rows
   are preserved as `attendance_log_v2_backup`.
2. **Environment** — on Vercel → Settings → Environment Variables add:
   `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`.
   (Locally: copy `.env.example` → `.env` — the `.env` file is git-ignored and
   never deployed.)
3. **Deploy** — push the repo to Vercel. `vercel.json` rewrites both
   `/api/(.*)` and `/api` to `api/index.py` with `maxDuration: 60`.
4. **Local dev (optional):**
   ```bash
   pip install -r api/requirements.txt
   uvicorn api.index:app --port 8000
   python3 smoke_test.py
   ```

## Project structure

```
├── index.html                    # 4 tabs + floating chatbot widget
├── app.js                        # dashboard / stats / setup / week marking / chat streaming
├── styles.css                    # full UI (cards, tables, week grid, chat panel)
├── vercel.json                   # /api rewrites + functions.maxDuration 60
├── api/
│   ├── index.py                  # FastAPI app — exports `app` (routes + chatbot)
│   ├── db.py                     # supabase client row layer + default subjects
│   └── requirements.txt          # fastapi, supabase, groq (+ uvicorn, python-dotenv)
├── database/schema.sql           # tables + RLS + seed of 12 subjects — run once
├── smoke_test.py                 # self-test (in-memory PostgREST fake, no DB needed)
├── .env.example                  # template only — real keys live in Vercel env vars
└── README.md
```

## The four tabs + chatbot

| Tab | What it does |
|---|---|
| 📊 **Dashboard** | Total lectures scheduled, total present, total absent, overall attendance % (with meter). |
| 📚 **Subject Stats** | Table of every subject: total lectures, present, absent, percentage (≥75% green, ≥60% amber, below red). |
| 🛠️ **Manage Semester** | Add / delete subjects (delete cascades), restore the default 12, and a **date picker** where you enter lecture counts per subject (e.g. C Programming: 3, Front-End: 1). Saving replaces that day's scheduled totals. |
| ✅ **Mark Attendance** | Week-by-week grid (Mon→Sun, prev/next navigation). Each scheduled class is a chip: tap cycles **pending → Present → Absent → pending**. Per-day **🌴 Mark Holiday** (with optional reason) clears the day so nothing is penalised; ↩ Undo restores the schedule view. |
| 💬 **Chatbot** | Floating button → chat panel. `POST /api/chat` builds a JSON snapshot of your live stats server-side, injects it into the system prompt, and **streams** Groq's reply chunk-by-chunk. |

## Stats math

- `percentage = 100 × present / (present + absent)` — pending (unmarked)
  lectures don't count against you until marked.
- `scheduled` counts only lectures on **non-holiday** dates.
- Marking a holiday deletes that date's attendance rows (no penalty) but
  keeps the schedule rows; stats simply exclude holiday dates.

## API reference

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/health` | groq / supabase / `groq_model` / `mode: manual-schedule` |
| GET | `/api/subjects` | list subjects |
| POST | `/api/subjects` | add `{name}` (409 on duplicate) |
| DELETE | `/api/subjects/{id}` | delete subject + its schedule + attendance |
| POST | `/api/subjects/seed` | idempotently restore the default 12 |
| GET | `/api/schedule?date=` | one day's lecture counts (all subjects) |
| POST | `/api/schedule` | **replace** a day `{date, entries:[{subject_id, lecture_count}]}` |
| GET | `/api/week?start=` | full week payload for the Mark Attendance tab (snaps to Monday) |
| POST | `/api/attendance` | `{date, subject_id, present_count, absent_count}` — validated against the scheduled count; 409 on holidays/unscheduled |
| DELETE | `/api/attendance?date=&subject_id=` | clear one subject's marks for a day |
| POST | `/api/holiday` | `{date, reason?}` — mark holiday + clear the day's marks |
| DELETE | `/api/holidays/{date}` | undo a holiday |
| GET | `/api/stats?date=` | dashboard + subject-wise totals + today/tomorrow blocks |
| POST | `/api/chat` | `{message, date?}` → **streams** plain-text reply |
| GET | `/` · `/api` | health / rewrite connectivity checks |

## The chatbot

- Model: **`openai/gpt-oss-20b`** (Groq). Your suggested `llama3-8b-8192`
  was shut down in Aug 2025 and its successor `llama-3.1-8b-instant` was
  shut down in Aug 2026 — Groq's official fast replacement for that class
  is `openai/gpt-oss-20b`.
- The route reads the DB first (overall + per-subject + today/tomorrow),
  serialises it into the system prompt, then calls
  `client.chat.completions.create(..., stream=True)` and yields
  `chunk.choices[0].delta.content` as it arrives (usage-only chunks skipped).
- Frontend reads the body with `ReadableStream` and paints tokens as they
  land; errors surface as a red bubble (502 `Groq call failed: …`).

## Smoke test

```bash
python3 smoke_test.py
```
Runs the real FastAPI app against an **in-memory fake of the Supabase
PostgREST API** (no database, no network needed) with the Groq stream
stubbed — validates routes, validation errors, schedule replacement,
attendance limits, holiday clearing, stats math, and the chat streaming path.

## Troubleshooting

| Symptom | Fix |
|---|---|
| 503 `Supabase tables not found — run database/schema.sql` | Run `database/schema.sql` in the Supabase SQL Editor (and `POST /api/subjects/seed` if subjects are empty). |
| 502 `Groq call failed: … 401 Invalid API Key` | Rotate the key at console.groq.com and update `GROQ_API_KEY` in Vercel env vars. |
| Chat says 503 `GROQ_API_KEY missing` | Same as above — the env var isn't set in Vercel. |
| `/api/*` returns the HTML page | `vercel.json` must contain both rewrites (`/api/(.*)` and `/api` → `/api/index.py`). Never add `builds`/`routes`. |
| Want the old data? | v2 history lives in `attendance_log_v2_backup` (old shape: `date, subject_name, status`). The unused `weekly_schedule` table is still there too — drop them manually when done. |

### Retired components (do not reintroduce)

OCR.space API · Tesseract.js browser OCR · `api/groq_analyzer.py` (AI
timetable extraction) · `POST /api/schedule/text` · `POST /api/schedule/upload`
· `/api/today` · `/api/analytics` · `OCR_SPACE_API_KEY` · `requests` /
`python-multipart` dependencies · `weekly_schedule` flow.
