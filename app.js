/* Attendance Portal (day-by-day) — frontend logic */

const $ = (sel) => document.querySelector(sel);

const state = {
  today: null,        // payload from GET /api/today
  schedule: null,     // {Monday: [...], ...}
  file: null,         // selected timetable image
  tab: "today",
  busy: false,
};

/* ---------------- utilities ---------------- */

function localISO(d = new Date()) {
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function fmtDate(iso, opts) {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-IN", opts);
}

function esc(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, type = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 350);
  }, 5000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  // Vercel can serve a static file (index.html) instead of the function if a
  // route is misconfigured — detect that instead of parsing HTML to null.
  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const isJSON = contentType.includes("application/json");
  let data = null;
  if (isJSON) {
    data = await res.json().catch(() => null);
  }

  if (!res.ok) {
    let msg = (data && data.detail) || `Request failed (HTTP ${res.status})`;
    if (Array.isArray(msg)) {
      msg = msg.map((m) => m.msg || JSON.stringify(m)).join("; ");
    }
    throw new Error(msg);
  }

  if (data === null || !isJSON) {
    throw new Error("Backend route returned invalid response or static file instead of JSON.");
  }
  return data;
}

function toneOf(p) {
  if (p === null || p === undefined) return "none";
  if (p >= 75) return "good";
  if (p >= 60) return "warn";
  return "bad";
}

/* ---------------- tabs ---------------- */

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.hidden = p.id !== `tab-${tab}`;
  });
  if (tab === "today") loadToday();
  if (tab === "analytics") loadAnalytics();
  if (tab === "schedule") loadSchedule();
}

/* ---------------- TODAY ---------------- */

async function loadToday() {
  try {
    const data = await api(`/api/today?date=${localISO()}`);
    if (data) {
      state.today = data;
      renderToday();
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderToday() {
  const t = state.today;
  if (!t) return;

  $("#topDate").textContent = fmtDate(t.date,
    { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  $("#todayDate").textContent = fmtDate(t.date,
    { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  // day tags
  const tags = [];
  if (t.is_weekend) tags.push(`<span class="badge blue">🗓️ Weekend</span>`);
  if (t.is_holiday) tags.push(`<span class="badge purple">🌴 Holiday</span>`);
  if (!t.is_holiday && !t.is_weekend && t.subjects.length)
    tags.push(`<span class="badge">${t.subjects.length} lecture${t.subjects.length > 1 ? "s" : ""} today</span>`);
  $("#dayTags").innerHTML = tags.join("");

  // banners
  $("#holidayBanner").hidden = !t.is_holiday;
  $("#weekendBanner").hidden = !(t.is_weekend && !t.is_holiday);
  if (t.is_holiday) {
    $("#holidayTitle").textContent = `Today (${t.day_of_week}) is a holiday`;
    $("#holidayReasonText").textContent =
      t.holiday_reason ? `Reason: ${t.holiday_reason}` : "No classes will be penalised.";
  }

  // class list (cleared from screen on holidays)
  const list = $("#dayClasses");
  if (t.is_holiday) {
    list.innerHTML = "";
    $("#freeDay").hidden = true;
    $("#holidayControls").hidden = true;
  } else {
    $("#holidayControls").hidden = false;
    $("#freeDay").hidden = t.subjects.length > 0;
    list.innerHTML = t.subjects.map((name) => classRowHtml(name, t.records[name])).join("");
  }
}

function classRowHtml(name, status) {
  const marked = status === "present" || status === "absent";
  const chip = marked
    ? `<span class="chip ${status === "present" ? "ok" : "bad"}">
         ${status === "present" ? "✓ Present" : "✗ Absent"}
         <button class="chip-x" data-clear title="Remove this record">✕</button>
       </span>`
    : "";
  return `
    <div class="class-row ${marked ? `marked-${status}` : ""}" data-subject="${esc(name)}">
      <div class="class-info">
        <span class="class-name">${esc(name)}</span>
        ${chip}
      </div>
      <div class="class-actions">
        <button class="btn good ${status === "present" ? "active" : ""}" data-status="present">✓ Present</button>
        <button class="btn bad ${status === "absent" ? "active" : ""}" data-status="absent">✗ Absent</button>
      </div>
    </div>`;
}

// Present / Absent / clear (event delegation)
$("#dayClasses").addEventListener("click", async (e) => {
  const row = e.target.closest(".class-row");
  if (!row || state.busy || !state.today) return;
  const subject = row.dataset.subject;
  const current = state.today.records[subject] || null;
  const clear = e.target.closest("[data-clear]");
  const btn = e.target.closest("[data-status]");

  let path, options;
  if (clear || (btn && btn.dataset.status === current)) {
    // clicking the already-marked status (or ✕) removes the record
    path = `/api/attendance?date=${state.today.date}&subject_name=${encodeURIComponent(subject)}`;
    options = { method: "DELETE" };
  } else if (btn) {
    path = "/api/attendance";
    options = {
      method: "POST",
      body: JSON.stringify({
        date: state.today.date,
        subject_name: subject,
        status: btn.dataset.status,
      }),
    };
  } else {
    return;
  }

  state.busy = true;
  try {
    await api(path, options);
    await loadToday();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

// Holiday controls
$("#holidayBtn").addEventListener("click", async () => {
  if (state.busy || !state.today) return;
  state.busy = true;
  try {
    const reason = $("#holidayReasonInput").value.trim() || "Holiday";
    state.today = await api("/api/holiday", {
      method: "POST",
      body: JSON.stringify({ date: state.today.date, reason }),
    });
    $("#holidayReasonInput").value = "";
    renderToday();
    toast("Today marked as holiday — classes cleared, nothing penalised", "info");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

$("#undoHolidayBtn").addEventListener("click", async () => {
  if (state.busy || !state.today) return;
  state.busy = true;
  try {
    state.today = await api(`/api/holidays/${state.today.date}`, { method: "DELETE" });
    renderToday();
    toast("Holiday removed — schedule restored", "info");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

/* ---------------- ANALYTICS ---------------- */

async function loadAnalytics() {
  const weeks = $("#weeksSelect").value;
  try {
    const data = await api(`/api/analytics?weeks=${weeks}&end_date=${localISO()}`);
    renderAnalytics(data);
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderAnalytics(data) {
  const o = data.overall;
  $("#anSummary").innerHTML = `
    <div class="pill"><b>${o.present}</b><span>Present</span></div>
    <div class="pill"><b>${o.absent}</b><span>Absent</span></div>
    <div class="pill"><b>${o.holidays}</b><span>Holidays</span></div>
    <div class="pill accent"><b>${o.percent === null ? "—" : o.percent + "%"}</b><span>Overall</span></div>`;

  const anyLogged = data.weeks.some((w) => w.present + w.absent > 0);
  $("#anEmpty").hidden = anyLogged;
  $("#weeksGrid").hidden = !anyLogged;
  if (!anyLogged) return;

  const todayISO = localISO();
  $("#weeksGrid").innerHTML = data.weeks.map((w) => {
    const tone = toneOf(w.percent);
    const rows = w.days.map((d) => {
      const cls = [
        "day-row",
        d.date === todayISO ? "today" : "",
        d.future ? "future" : "",
      ].join(" ");
      let right = "";
      if (d.holiday_reason) {
        right = `<span class="mini h">🌴 ${esc(d.holiday_reason)}</span>`;
      } else if (d.future) {
        right = `<span class="mini f">upcoming</span>`;
      } else if (!d.has_classes) {
        right = `<span class="mini free">free</span>`;
      } else {
        right = `<span class="counts">
            <span class="mini p">${d.present} ✓</span>
            <span class="mini a">${d.absent} ✗</span>
          </span>`;
      }
      const pct = d.percent === null
        ? `<span class="day-pct none">—</span>`
        : `<span class="day-pct ${toneOf(d.percent)}">${d.percent}%</span>`;
      return `
        <div class="${cls}">
          <span class="dow">${d.day_of_week.slice(0, 3)}</span>
          <span class="dte">${fmtDate(d.date, { day: "numeric", month: "short" })}</span>
          ${right}
          ${pct}
        </div>`;
    }).join("");

    return `
      <div class="week-card">
        <div class="week-head">
          <b>Week of ${esc(w.label)}</b>
          <span class="week-pct ${tone}">${w.percent === null ? "no data" : w.percent + "%"}</span>
        </div>
        <div class="day-rows">${rows}</div>
      </div>`;
  }).join("");
}

$("#weeksSelect").addEventListener("change", loadAnalytics);

/* ---------------- SCHEDULE ---------------- */

async function loadSchedule() {
  try {
    const data = await api("/api/schedule");
    state.schedule = (data && data.schedule) ? data.schedule : {};
    renderScheduleGrid();
  } catch (err) {
    toast(err.message, "err");
    // still show empty state if tables are missing
    $("#scheduleGrid").innerHTML = "";
    $("#schedEmpty").hidden = false;
  }
}

function renderScheduleGrid() {
  const sched = state.schedule || {};
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const todayDay = new Date().toLocaleDateString("en-US", { weekday: "long" });
  const hasAny = days.some((d) => (sched[d] || []).length > 0);

  $("#schedEmpty").hidden = hasAny;
  $("#scheduleGrid").hidden = !hasAny;
  if (!hasAny) { $("#scheduleGrid").innerHTML = ""; return; }

  $("#scheduleGrid").innerHTML = days.map((day) => {
    const subs = sched[day] || [];
    const items = subs.length
      ? subs.map((s) => `<li>${esc(s)}</li>`).join("")
      : `<li class="none-row">no classes</li>`;
    return `
      <div class="day-col ${day === todayDay ? "today-col" : ""}">
        <h4>${day.slice(0, 3)}</h4>
        <ul>${items}</ul>
      </div>`;
  }).join("");
}

$("#reloadSched").addEventListener("click", loadSchedule);

/* ---- image selection ---- */

const dropZone = $("#dropZone");
const fileInput = $("#fileInput");

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
["dragenter", "dragover"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.remove("drag"); }));
dropZone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) selectFile(file);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) selectFile(fileInput.files[0]);
});

function selectFile(file) {
  if (!file.type.startsWith("image/")) {
    toast("Please choose an image file", "err");
    return;
  }
  state.file = file;
  const url = URL.createObjectURL(file);
  $("#preview").src = url;
  $("#preview").hidden = false;
  $("#dzIdle").hidden = true;
  $("#fileName").hidden = false;
  $("#fileName").textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
  $("#extractBtn").disabled = false;
  $("#uploadStatus").textContent = "";
}

/* ---- extract (OCR + Groq) ---- */

$("#extractBtn").addEventListener("click", () => runExtract());
$("#parseTextBtn").addEventListener("click", () => runExtract(true));

async function runExtract(useText = false) {
  const status = $("#uploadStatus");
  const btn = useText ? $("#parseTextBtn") : $("#extractBtn");

  if (useText && !$("#scheduleText").value.trim()) {
    toast("Paste your timetable text first", "err");
    return;
  }
  if (!useText && !state.file) {
    toast("Choose a timetable image first", "err");
    return;
  }

  btn.disabled = true;

  try {
    let scheduleText;

    if (useText) {
      status.textContent = "Sending text to Groq AI…";
      scheduleText = $("#scheduleText").value.trim();
    } else {
      // 1) OCR runs entirely in the browser (Tesseract.js) — no image upload
      status.textContent = "Scanning image on your device (this may take a moment)...";
      scheduleText = await ocrInBrowser(state.file);
      if (!scheduleText) {
        throw new Error(
          "No readable text found in the image — try a clearer photo, or paste the text instead."
        );
      }
      // 2) send the browser-extracted text to the existing text endpoint
      status.textContent = "Scan complete — sending extracted text to Groq AI…";
    }

    const data = await api("/api/schedule/text", {
      method: "POST",
      body: JSON.stringify({ schedule_text: scheduleText }),
    });

    state.schedule = (data && data.schedule) ? data.schedule : {};
    renderScheduleGrid();

    const shownText = (data && data.extracted_text) || scheduleText;
    if (shownText) {
      $("#ocrText").textContent = shownText;
      $("#ocrDetails").hidden = false;
      $("#ocrDetails").open = false;
    }

    const count = Object.values(state.schedule).reduce((n, a) => n + a.length, 0);
    status.textContent = "";
    toast(`Schedule saved ✓ ${count} lecture${count === 1 ? "" : "s"} across the week`, "ok");
    loadToday(); // today's list may have changed
  } catch (err) {
    status.textContent = "";
    toast(err.message, "err");
  } finally {
    btn.disabled = useText ? false : !state.file;
  }
}

/* Local OCR via Tesseract.js (CDN script in index.html). Returns raw text.
 *
 * Runs up to 4 passes with different page-segmentation modes: the default
 * works for most photos, but dense timetable grids sometimes only respond
 * to a more specific mode. Stops as soon as a usable result is found. */
async function ocrInBrowser(file) {
  if (!window.Tesseract) {
    throw new Error(
      "Tesseract.js failed to load — check your internet connection (the CDN) and retry."
    );
  }

  const attempts = [
    {},                                  // Tesseract default (auto page segmentation)
    { tessedit_pageseg_mode: "4" },      // single column — good for timetable grids
    { tessedit_pageseg_mode: "6" },      // uniform block of text
    { tessedit_pageseg_mode: "11" },     // sparse text
  ];
  const baseMsg = "Scanning image on your device (this may take a moment)";
  let best = "";
  let lastError = null;

  for (let i = 0; i < attempts.length; i++) {
    const suffix = i > 0 ? ` (pass ${i + 1}/${attempts.length})` : "";
    try {
      $("#uploadStatus").textContent = baseMsg + "..." + suffix;
      const { data } = await window.Tesseract.recognize(file, "eng", {
        ...attempts[i],
        logger: (m) => {
          if (m.status === "recognizing text" && typeof m.progress === "number") {
            $("#uploadStatus").textContent =
              baseMsg + "... " + Math.round(m.progress * 100) + "%" + suffix;
          }
        },
      });
      const text = ((data && data.text) ? data.text : "").trim();
      if (text.length > best.length) best = text;
      if (best.split(/\s+/).filter(Boolean).length >= 3) break; // usable — stop early
    } catch (err) {
      lastError = err;
    }
  }

  if (!best && lastError) {
    throw new Error(
      "Browser OCR failed: " + (lastError && lastError.message ? lastError.message : String(lastError))
    );
  }
  return best;
}

/* ---------------- boot ---------------- */

(function init() {
  const now = new Date();
  $("#topDate").textContent = now.toLocaleDateString("en-IN",
    { weekday: "short", day: "numeric", month: "short", year: "numeric" });

  api("/api/health").then((h) => {
    const badges = [];
    badges.push(h.groq
      ? `<span class="badge">🤖 Groq ready</span>`
      : `<span class="badge warn">🤖 Groq offline</span>`);
    badges.push(h.supabase
      ? `<span class="badge">🗄️ Supabase</span>`
      : `<span class="badge warn">🗄️ No Supabase URL</span>`);
    badges.push(`<span class="badge">🧠 OCR in browser</span>`);
    $("#envBadges").innerHTML = badges.join("");
  }).catch(() => {});

  loadToday();
  loadSchedule();

  // refresh today's payload every minute (handles midnight rollover)
  setInterval(() => { if (state.tab === "today") loadToday(); }, 60_000);
})();
