# 📅 Attendance Portal — Vercel Single-Repo Deployment

Day-by-day historical attendance tracking: **FastAPI + OCR.space + Groq (`openai/gpt-oss-20b`) + Supabase**,
deployed as **one repository on Vercel** — static frontend at the root, backend as a
Python serverless function under `/api/*`.

```
attendance-portal/                ← Vercel project root
├── index.html                    # frontend (served as static files)
├── app.js
├── styles.css
├── vercel.json                   # @vercel/python build + /api routing + static
├── api/                          # ← Vercel serverless functions
│   ├── index.py                  # FastAPI app (formerly main.py) — exports `app`
│   ├── ocr.py                    # image → text via OCR.space API (requests only)
│   ├── groq_analyzer.py          # exact Groq streaming config (openai/gpt-oss-20b)
│   ├── db.py                     # supabase client
│   └── requirements.txt          # fastapi, requests, groq, supabase, …
├── database/schema.sql           # run once in Supabase SQL Editor
├── smoke_test.py                 # 38-check self-test
├── test_timetable.png            # sample image to try uploads
├── .env / .env.example           # secrets (git-ignored)
└── README.md
```

> Replaced architecture: **no Docker / no Tesseract** — OCR is done by the free
> hosted **OCR.space API**, which works inside Vercel's serverless sandbox.

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
OCR_SPACE_API_KEY=helloworld                         # free key: https://ocr.space/ocrapi
```

`OCR_SPACE_API_KEY=helloworld` is OCR.space's public **test** key (rate-limited) —
register for your own personal key once you're using it regularly.

---

## 2. Run locally

```bash
pip install -r api/requirements.txt

# full app (frontend + /api proxy, exactly like production):
npm i -g vercel && vercel dev            # → http://localhost:3000

# or API only:
uvicorn api.index:app --reload           # → http://localhost:8000/docs
```

Self-test (no Vercel needed): `python smoke_test.py`

---

## 3. Deploy to Vercel (single repo)

1. **Push to GitHub** (project root = this folder):
   ```bash
   git init && git add -A && git commit -m "Attendance Portal"
   git remote add origin https://github.com/<you>/attendance-portal.git
   git push -u origin main
   ```
2. **Import** at <https://vercel.com/new> → select the repo → Framework preset
   **Other** → don't change build settings (`vercel.json` already configures
   `@vercel/python`, routes, and static files).
3. **Add environment variables** (Settings → Environment Variables):
   `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `OCR_SPACE_API_KEY` — paste
   the same values from your `.env`.
4. **Deploy** → open `https://<project>.vercel.app`:
   - `/` serves `index.html`; `app.js`/`styles.css` are static files;
   - every `/api/*` request is routed by `vercel.json` to the FastAPI function
     (`api/index.py`) — same origin, so **no CORS issues**;
   - the Today/Analytics/Schedule tabs talk to Supabase, and timetable uploads
     flow OCR.space → Groq → your database.

---

## 4. How the pieces work

| Piece | File | Notes |
|---|---|---|
| Static frontend | `index.html`, `app.js`, `styles.css` | served by Vercel's CDN; all fetches use relative `/api/...` paths |
| Serverless function | `api/index.py` | exports `app = FastAPI(...)`; **no StaticFiles mount** (deleted — Vercel serves the frontend) |
| OCR | `api/ocr.py` | base64 → `POST https://api.ocr.space/parse/image` with `OCR_SPACE_API_KEY`; returns parsed text; clear errors on failures. No pytesseract/Pillow/numpy |
| AI | `api/groq_analyzer.py` | exact spec: `openai/gpt-oss-20b`, `temperature=1`, `max_completion_tokens=2048`, `top_p=1`, `reasoning_effort="medium"`, `stream=True` |
| Database | `api/db.py` | supabase-py → `weekly_schedule` / `attendance_log` / `holidays` |

**API reference**

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | groq / ocr / supabase status |
| `GET` / `POST` | `/api/schedule`, `/api/schedule/upload`, `/api/schedule/text` | read / OCR+AI save / text-only save |
| `GET` | `/api/today?date=` | weekday filter + marks + holiday |
| `POST` / `DELETE` | `/api/attendance` | log / remove a `(date, subject, status)` row |
| `POST` / `DELETE` | `/api/holiday`, `/api/holidays/{date}` | mark / undo holiday |
| `GET` | `/api/analytics?weeks=` | week-by-week percentages |

---

## 5. Troubleshooting

- **Upload fails with a clear503** → `OCR_SPACE_API_KEY` missing in Vercel env vars.
- **413 / body too large** → Vercel caps request bodies ≈ 4.5 MB and OCR.space works
  best with images ≤ ~1 MB — compress screenshots before uploading.
- **Function timeout on upload** → OCR + Groq + DB can exceed the default duration on
  the Hobby plan; keep images small, or raise `maxDuration` for `api/index.py`
  (Vercel Settings → Functions, or the modern `functions` key in `vercel.json`,
  which replaces the legacy `builds` array).
- **503 "Supabase tables not found"** → run `database/schema.sql`.
- **502 "Groq call failed"** → check `GROQ_API_KEY` and model access to `openai/gpt-oss-20b`.
- **Old docs?** The previous Hugging Face/Docker/Tesseract setup is retired —
  `Dockerfile`, `backend/README.md` and `pytesseract`/`Pillow`/`numpy` are gone.
