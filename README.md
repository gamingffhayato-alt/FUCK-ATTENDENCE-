# 📅 Attendance Portal — Vercel Single-Repo Deployment

Day-by-day historical attendance tracking: **FastAPI + Groq (`openai/gpt-oss-20b`) +
Supabase**, deployed as **one repository on Vercel** — static frontend at the root,
backend as a Python serverless function under `/api/*`.

**OCR runs in your browser** with [Tesseract.js](https://github.com/naptha/tesseract.js)
(the image never leaves your device); only the extracted *text* is sent to the backend.

```
attendance-portal/                ← Vercel project root
├── index.html                    # frontend (Tesseract.js CDN loaded in <head>)
├── app.js                        # browser OCR → POST /api/schedule/text
├── styles.css
├── vercel.json                   # /api rewrites + functions.maxDuration
├── api/                          # ← Vercel serverless functions
│   ├── index.py                  # FastAPI app — exports `app` (text-only intake)
│   ├── groq_analyzer.py          # exact Groq streaming config (openai/gpt-oss-20b)
│   ├── db.py                     # supabase client
│   └── requirements.txt          # fastapi, uvicorn, groq, supabase, python-dotenv
├── database/schema.sql           # run once in Supabase SQL Editor
├── smoke_test.py                 # 38-check self-test
├── .env / .env.example           # secrets (git-ignored)
└── README.md
```

> No Docker, no Tesseract binary, no OCR API keys — the browser does the scanning.

---

## 1. One-time setup

### a) Supabase tables
Supabase Dashboard → **SQL Editor** → paste `database/schema.sql` → **Run**
(creates `weekly_schedule`, `attendance_log`, `holidays` + RLS policies).

### b) Environment — `.env` (local) / Vercel → Settings → Environment Variables

```ini
GROQ_API_KEY=gsk_…                                  # console.groq.com → API Keys
SUPABASE_URL=https://<ref>.supabase.co              # Project Settings → API
SUPABASE_KEY=sb_publishable_…                        # publishable or service key
```

---

## 2. Run locally

```bash
pip install -r api/requirements.txt

# full app (frontend + /api proxy, exactly like production):
npm i -g vercel && vercel dev            # → http://localhost:3000

# or API only:
uvicorn api.index:app --reload           # → http://localhost:8000/docs
```

Self-test: `python smoke_test.py`

---

## 3. Deploy to Vercel (single repo)

1. **Push to GitHub** (`git add -A && git commit && git push`).
2. **Import** at <https://vercel.com/new> → repo → preset **Other**
   (`vercel.json` already contains the `/api` → `api/index.py` rewrites and
   `"maxDuration": 60`; static files are served automatically).
3. **Environment variables**: `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`.
4. **Deploy** → open `https://<project>.vercel.app`.

---

## 4. How it works

| Piece | File | Notes |
|---|---|---|
| Browser OCR | `index.html` + `app.js` | Tesseract.js v5 from jsDelivr; multi-pass page-segmentation (default → psm 4 → 6 → 11) with live "Scanning image on your device…" progress; **image is never uploaded** |
| Frontend | `index.html`, `app.js`, `styles.css` | served by Vercel's CDN; all fetches use relative `/api/...` paths |
| Serverless function | `api/index.py` | exports `app = FastAPI(...)`; text-only intake (`/api/schedule/text`); no StaticFiles mount |
| AI | `api/groq_analyzer.py` | exact spec: `openai/gpt-oss-20b`, `temperature=1`, `max_completion_tokens=2048`, `top_p=1`, `reasoning_effort="medium"`, `stream=True` |
| Database | `api/db.py` | supabase-py → `weekly_schedule` / `attendance_log` / `holidays` |

**Schedule extraction flow:** choose image → browser OCR (Tesseract.js) →
`POST /api/schedule/text` with the extracted text → Groq structures it into
`{Monday: [...], …}` → `weekly_schedule` replaced in Supabase.

**API reference**

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | groq / supabase status (`client_ocr: "tesseract.js"`) |
| `GET` / `POST` | `/api/schedule`, `/api/schedule/text` | read / save the parsed timetable |
| `GET` | `/api/today?date=` | weekday filter + marks + holiday |
| `POST` / `DELETE` | `/api/attendance` | log / remove a `(date, subject, status)` row |
| `POST` / `DELETE` | `/api/holiday`, `/api/holidays/{date}` | mark / undo holiday |
| `GET` | `/api/analytics?weeks=` | week-by-week percentages |

---

## 5. Troubleshooting

- **"Tesseract.js failed to load"** → the jsDelivr CDN was unreachable in the
  browser; retry or swap the `<script>` URL for unpkg: `https://unpkg.com/tesseract.js@5/dist/tesseract.min.js`.
- **Scan returns "No readable text"** → the multi-pass OCR found nothing;
  use a brighter/clearer photo, or the *paste text instead* fallback.
- **First scan is slow** → Tesseract.js downloads its WASM core + English
  language data (a few MB) on first use, then it's cached by the browser.
- **"Groq call failed: 401 Invalid API Key"** → your `GROQ_API_KEY` was revoked
  or rotated — paste the fresh key into `.env` / Vercel env vars.
- **503 "Supabase tables not found"** → run `database/schema.sql`.
- **Function timeout** → `maxDuration: 60` is already set in `vercel.json`.
- **Retired components** → `api/ocr.py`, OCR.space keys, `requests`,
  `python-multipart` and the `/api/schedule/upload` route are all removed.
