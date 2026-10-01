/* Attendance Portal v3 — manual daily schedule + Groq chatbot frontend */

const $ = (sel) => document.querySelector(sel);

const state = {
  tab: "dashboard",
  stats: null,        // GET /api/stats payload
  subjects: [],       // GET /api/subjects payload
  setup: null,        // GET /api/schedule?date payload (Manage tab)
  week: null,         // GET /api/week payload
  weekStart: null,    // ISO date of the week's Monday (browser-local)
  busy: false,
  chatBusy: false,
  chatWelcomed: false,
};

/* ---------------- utilities ---------------- */

function localISO(d = new Date()) {
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function mondayOf(d = new Date()) {
  const m = new Date(d);
  const shift = (m.getDay() + 6) % 7; // Mon=0 … Sun=6
  m.setDate(m.getDate() - shift);
  return m;
}

function addDays(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return localISO(d);
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

  // Vercel can serve static index.html instead of the function if a route is
  // misconfigured — detect that instead of parsing HTML to null.
  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const isJSON = contentType.includes("application/json");
  let data = null;
  if (isJSON) data = await res.json().catch(() => null);

  if (!res.ok) {
    let msg = (data && data.detail) || `Request failed (HTTP ${res.status})`;
    if (Array.isArray(msg)) msg = msg.map((m) => m.msg || JSON.stringify(m)).join("; ");
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
  if (tab === "dashboard" || tab === "subjects") loadStats();
  if (tab === "manage") { loadSubjects(); loadSetupDay(); }
  if (tab === "attendance") loadWeek();
}

/* ---------------- STATS (Dashboard + Subject-wise) ---------------- */

async function loadStats() {
  try {
    const data = await api(`/api/stats?date=${localISO()}`);
    state.stats = data;
    renderDashboard();
    renderSubjectsTable();
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderDashboard() {
  const s = state.stats;
  if (!s) return;
  const o = s.overall;

  $("#statScheduled").textContent = o.scheduled;
  $("#statPresent").textContent = o.present;
  $("#statAbsent").textContent = o.absent;
  $("#statPct").textContent = o.percentage === null ? "—" : o.percentage + "%";
  $("#statMeter").style.width = (o.percentage || 0) + "%";
  $("#statMeter").className = toneOf(o.percentage);

  const tags = [`<span class="badge blue">${o.subjects} subjects</span>`];
  if (o.pending > 0)
    tags.push(`<span class="badge warn">${o.pending} lecture${o.pending === 1 ? "" : "s"} unmarked</span>`);
  if (s.today.is_holiday)
    tags.push(`<span class="badge purple">🌴 Today is a holiday</span>`);
  else if (s.today.total_scheduled)
    tags.push(`<span class="badge">${s.today.total_scheduled} lecture${s.today.total_scheduled === 1 ? "" : "s"} today</span>`);
  $("#dashTags").innerHTML = tags.join("");
}

function renderSubjectsTable() {
  const s = state.stats;
  const tbody = $("#subjectsStatsTable tbody");
  const rows = s ? s.subjects : [];
  const hasSubjects = rows.length > 0;
  $("#subjEmpty").hidden = hasSubjects;
  $("#subjectsStatsTable").hidden = !hasSubjects;
  if (!hasSubjects) { tbody.innerHTML = ""; return; }

  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td class="subj-cell">${esc(r.name)}</td>
      <td class="num">${r.scheduled}</td>
      <td class="num good-text">${r.present}</td>
      <td class="num bad-text">${r.absent}</td>
      <td class="num">
        <span class="pct ${toneOf(r.percentage)}">${r.percentage === null ? "—" : r.percentage + "%"}</span>
      </td>
    </tr>`).join("");
}

$("#reloadStats").addEventListener("click", loadStats);

/* ---------------- MANAGE: subjects ---------------- */

async function loadSubjects() {
  try {
    const data = await api("/api/subjects");
    state.subjects = data.subjects || [];
    renderSubjectList();
    if (state.setup) loadSetupDay(); // row set may have changed
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderSubjectList() {
  const list = $("#subjectManageList");
  list.innerHTML = state.subjects.map((s) => `
    <li class="subject-item">
      <span>${esc(s.name)}</span>
      <button class="chip-x" data-del-subject="${s.id}" data-name="${esc(s.name)}"
              title="Delete subject">🗑</button>
    </li>`).join("");
}

$("#addSubjectForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#subjectNameInput");
  const name = input.value.trim();
  if (!name) return;
  try {
    await api("/api/subjects", { method: "POST", body: JSON.stringify({ name }) });
    input.value = "";
    toast(`Added “${name}” ✓`, "ok");
    await loadSubjects();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#subjectManageList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-del-subject]");
  if (!btn || state.busy) return;
  const id = btn.dataset.delSubject;
  const name = btn.dataset.name;
  if (!confirm(`Delete “${name}”? Its schedule and attendance will be removed too.`)) return;
  state.busy = true;
  try {
    await api(`/api/subjects/${id}`, { method: "DELETE" });
    toast(`Deleted “${name}”`, "info");
    await loadSubjects();
    loadStats();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

$("#seedSubjects").addEventListener("click", async () => {
  if (state.busy) return;
  state.busy = true;
  try {
    const data = await api("/api/subjects/seed", { method: "POST", body: "{}" });
    state.subjects = data.subjects || [];
    renderSubjectList();
    toast(`Default subjects restored — ${state.subjects.length} total ✓`, "ok");
    loadStats();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

/* ---------------- MANAGE: daily schedule setup ---------------- */

$("#setupDate").addEventListener("change", loadSetupDay);

async function loadSetupDay() {
  const iso = $("#setupDate").value || localISO();
  try {
    const data = await api(`/api/schedule?date=${iso}`);
    state.setup = data;
    renderSetupRows();
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderSetupRows() {
  const s = state.setup;
  if (!s) return;
  $("#setupHolidayWarn").hidden = !s.is_holiday;
  $("#setupStatus").textContent =
    `${s.total_lectures} lecture${s.total_lectures === 1 ? "" : "s"} scheduled on ` +
    fmtDate(s.date, { weekday: "short", day: "numeric", month: "short" });

  $("#setupRows").innerHTML = s.entries.map((e) => `
    <div class="setup-row">
      <label for="cnt-${e.subject_id}">${esc(e.subject)}</label>
      <input id="cnt-${e.subject_id}" type="number" min="0" max="20" step="1"
             value="${e.lecture_count}" data-subject="${e.subject_id}" />
    </div>`).join("") ||
    `<p class="muted">No subjects yet — add some above.</p>`;
}

function collectSetupEntries() {
  return [...document.querySelectorAll("#setupRows input[data-subject]")]
    .map((inp) => ({
      subject_id: Number(inp.dataset.subject),
      lecture_count: Math.max(0, Math.min(20, Number(inp.value) || 0)),
    }));
}

async function saveSetupDay() {
  if (state.busy) return;
  const iso = $("#setupDate").value || localISO();
  state.busy = true;
  $("#saveSetup").disabled = true;
  try {
    const entries = collectSetupEntries();
    const data = await api("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ date: iso, entries }),
    });
    state.setup = data;
    renderSetupRows();
    toast(`Day saved ✓ ${data.total_lectures} lecture${data.total_lectures === 1 ? "" : "s"} scheduled`, "ok");
    loadStats();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
    $("#saveSetup").disabled = false;
  }
}

$("#saveSetup").addEventListener("click", saveSetupDay);

$("#clearSetup").addEventListener("click", async () => {
  if (state.busy) return;
  const iso = $("#setupDate").value || localISO();
  if (!confirm(`Clear all scheduled lectures on ${iso}?`)) return;
  state.busy = true;
  try {
    const data = await api("/api/schedule", {
      method: "POST",
      body: JSON.stringify({ date: iso, entries: [] }),
    });
    state.setup = data;
    renderSetupRows();
    toast("Day cleared", "info");
    loadStats();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
  }
});

/* ---------------- MARK ATTENDANCE (week-by-week) ---------------- */

$("#prevWeek").addEventListener("click", () => shiftWeek(-7));
$("#nextWeek").addEventListener("click", () => shiftWeek(7));
$("#thisWeek").addEventListener("click", () => {
  state.weekStart = localISO(mondayOf());
  loadWeek();
});

function shiftWeek(days) {
  state.weekStart = addDays(state.weekStart || localISO(mondayOf()), days);
  loadWeek();
}

async function loadWeek() {
  if (!state.weekStart) state.weekStart = localISO(mondayOf());
  try {
    const data = await api(`/api/week?start=${state.weekStart}`);
    state.week = data;
    renderWeek();
  } catch (err) {
    toast(err.message, "err");
  }
}

function buildSlots(entry) {
  const slots = new Array(entry.lecture_count).fill(null);
  for (let i = 0; i < entry.present && i < slots.length; i++) slots[i] = "p";
  for (let i = entry.present; i < entry.present + entry.absent && i < slots.length; i++)
    slots[i] = "a";
  return slots;
}

function renderWeek() {
  const w = state.week;
  if (!w) return;
  $("#weekLabel").textContent = `Week of ${w.label}`;

  const todayISO = localISO();
  const anyContent = w.days.some((d) => d.is_holiday || d.entries.length > 0);
  $("#weekGrid").hidden = !anyContent;
  $("#weekEmpty").hidden = anyContent;
  if (!anyContent) { $("#weekGrid").innerHTML = ""; return; }

  $("#weekGrid").innerHTML = w.days.map((d) => {
    const isToday = d.date === todayISO;
    const isFuture = d.date > todayISO;
    const cls = ["day-card", isToday ? "today" : "", isFuture ? "future" : "",
                 d.is_holiday ? "holiday" : ""].join(" ");

    const head = `
      <div class="day-card-head">
        <div><b>${d.day_of_week.slice(0, 3)}</b>
          <span class="muted">${fmtDate(d.date, { day: "numeric", month: "short" })}</span></div>
        <div class="day-tags">${isToday ? '<span class="badge">Today</span>' : ""}</div>
      </div>`;

    let body;
    if (d.is_holiday) {
      body = `
        <div class="day-holiday">
          <span>🌴</span> <b>Holiday</b>
          <p class="muted">${esc(d.holiday_reason || "")}</p>
        </div>
        <div class="day-card-foot">
          <button class="btn ghost small" data-unholiday="${d.date}">↩ Undo holiday</button>
        </div>`;
    } else if (!d.entries.length) {
      body = `<p class="day-none muted">no classes scheduled</p>
        <div class="day-card-foot">
          <input type="text" maxlength="200" data-reason="${d.date}"
                 placeholder="Reason (optional)" />
          <button class="btn purple small" data-holiday="${d.date}">🌴 Holiday</button>
        </div>`;
    } else {
      const rows = d.entries.map((e) => {
        const slots = buildSlots(e);
        const chips = slots.map((v, i) => {
          const letter = v === "p" ? "✓" : v === "a" ? "✗" : "·";
          return `<button class="slot ${v || ""}" data-sid="${e.subject_id}"
                    data-i="${i}" ${isFuture ? "disabled" : ""}
                    title="${v === "p" ? "Present" : v === "a" ? "Absent" : "Not marked"}">${letter}</button>`;
        }).join("");
        return `
          <div class="att-row ${e.pending === 0 && (e.present + e.absent) > 0 ? "done" : ""}">
            <div class="att-name">${esc(e.subject)} <span class="cnt">×${e.lecture_count}</span></div>
            <div class="slots">${chips}</div>
            <div class="att-meta">
              <span class="mini p">${e.present} ✓</span>
              <span class="mini a">${e.absent} ✗</span>
              <button class="chip-x" data-clearrow="${e.subject_id}" title="Reset this subject's marks">✕</button>
            </div>
          </div>`;
      }).join("");

      body = `${rows}
        <div class="day-card-foot">
          <input type="text" maxlength="200" data-reason="${d.date}"
                 placeholder="Reason (optional)" />
          <button class="btn purple small" data-holiday="${d.date}">🌴 Holiday</button>
        </div>`;
    }

    return `<div class="${cls}" data-date="${d.date}">${head}${body}</div>`;
  }).join("");
}

$("#weekGrid").addEventListener("click", async (e) => {
  if (state.busy || !state.week) return;
  const card = e.target.closest(".day-card");
  if (!card) return;
  const iso = card.dataset.date;
  const day = state.week.days.find((x) => x.date === iso);
  if (!day) return;

  // slot chip: pending → present → absent → pending
  const slot = e.target.closest(".slot");
  if (slot && !slot.disabled) {
    const sid = Number(slot.dataset.sid);
    const entry = day.entries.find((x) => x.subject_id === sid);
    if (!entry) return;
    const slots = buildSlots(entry);
    const i = Number(slot.dataset.i);
    slots[i] = slots[i] === null ? "p" : slots[i] === "p" ? "a" : null;
    const present = slots.filter((v) => v === "p").length;
    const absent = slots.filter((v) => v === "a").length;
    state.busy = true;
    try {
      await api("/api/attendance", {
        method: "POST",
        body: JSON.stringify({ date: iso, subject_id: sid, present_count: present, absent_count: absent }),
      });
      await loadWeek();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      state.busy = false;
    }
    return;
  }

  // reset one subject's marks for the day
  const clearRow = e.target.closest("[data-clearrow]");
  if (clearRow) {
    const sid = Number(clearRow.dataset.clearrow);
    state.busy = true;
    try {
      await api(`/api/attendance?date=${iso}&subject_id=${sid}`, { method: "DELETE" });
      await loadWeek();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      state.busy = false;
    }
    return;
  }

  // mark / undo holiday
  const holBtn = e.target.closest("[data-holiday]");
  if (holBtn) {
    const reason = (card.querySelector("[data-reason]") || {}).value || "";
    state.busy = true;
    try {
      await api("/api/holiday", {
        method: "POST",
        body: JSON.stringify({ date: iso, reason: reason.trim() || "Holiday" }),
      });
      toast(`${iso} marked as holiday — day cleared, nothing penalised`, "info");
      await loadWeek();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      state.busy = false;
    }
    return;
  }

  const unholBtn = e.target.closest("[data-unholiday]");
  if (unholBtn) {
    state.busy = true;
    try {
      await api(`/api/holidays/${iso}`, { method: "DELETE" });
      toast("Holiday removed — schedule visible again (marks were cleared)", "info");
      await loadWeek();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      state.busy = false;
    }
  }
});

/* ---------------- AI CHATBOT WIDGET ---------------- */

const chatFab = $("#chatFab");
const chatPanel = $("#chatPanel");

function openChat(prefill) {
  chatPanel.hidden = false;
  chatFab.hidden = true;
  if (!state.chatWelcomed) {
    state.chatWelcomed = true;
    appendChatMsg("bot",
      "Hi! I'm your attendance assistant — I can see your live stats. " +
      "Ask me things like “Can I bunk C Programming tomorrow and stay above 75%?”");
  }
  if (prefill) $("#chatInput").value = prefill;
  $("#chatInput").focus();
  chatScroll();
}

function closeChat() {
  chatPanel.hidden = true;
  chatFab.hidden = false;
}

chatFab.addEventListener("click", () => openChat());
$("#chatClose").addEventListener("click", closeChat);
$("#chatClear").addEventListener("click", () => {
  $("#chatLog").innerHTML = "";
  state.chatWelcomed = false;
  openChat();
});

document.querySelectorAll("[data-ask]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const q = btn.dataset.ask;
    openChat();
    sendChat(q);
  });
});

document.querySelectorAll(".chat-quick .qchip").forEach((btn) => {
  btn.addEventListener("click", () => sendChat(btn.textContent.trim()));
});

function appendChatMsg(role, text) {
  const el = document.createElement("div");
  el.className = `chat-msg ${role}`;
  el.textContent = text;
  $("#chatLog").appendChild(el);
  chatScroll();
  return el;
}

function chatScroll() {
  const log = $("#chatLog");
  log.scrollTop = log.scrollHeight;
}

async function sendChat(text) {
  text = (text || "").trim();
  if (!text || state.chatBusy) return;
  if (chatPanel.hidden) openChat();

  appendChatMsg("user", text);
  const botEl = appendChatMsg("bot", "…");
  botEl.classList.add("streaming");

  state.chatBusy = true;
  $("#chatSend").disabled = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, date: localISO() }),
    });

    if (!res.ok) {
      let msg = `Request failed (HTTP ${res.status})`;
      try {
        const j = await res.json();
        if (j && j.detail) msg = typeof j.detail === "string" ? j.detail : msg;
      } catch (_) { /* not JSON — keep generic msg */ }
      throw new Error(msg);
    }

    // stream plain-text chunks (works buffered or streamed)
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let out = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      out += decoder.decode(value, { stream: true });
      botEl.textContent = out;
      chatScroll();
    }
    botEl.classList.remove("streaming");
    if (!out.trim()) botEl.textContent = "(empty response — try again)";
  } catch (err) {
    botEl.classList.remove("streaming");
    botEl.classList.add("err");
    botEl.textContent = err.message;
  } finally {
    state.chatBusy = false;
    $("#chatSend").disabled = false;
    chatScroll();
  }
}

$("#chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#chatInput");
  const text = input.value;
  input.value = "";
  input.style.height = "auto";
  sendChat(text);
});

$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chatForm").requestSubmit();
  }
});
$("#chatInput").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
});

/* ---------------- boot ---------------- */

(function init() {
  const now = new Date();
  $("#topDate").textContent = now.toLocaleDateString("en-IN",
    { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  $("#setupDate").value = localISO();
  state.weekStart = localISO(mondayOf());

  api("/api/health").then((h) => {
    const badges = [];
    badges.push(h.groq
      ? `<span class="badge">🤖 Groq ready</span>`
      : `<span class="badge warn">🤖 Groq offline</span>`);
    badges.push(h.supabase
      ? `<span class="badge">🗄️ Supabase</span>`
      : `<span class="badge warn">🗄️ No Supabase config</span>`);
    badges.push(`<span class="badge subtle">🖐 Manual schedule</span>`);
    $("#envBadges").innerHTML = badges.join("");
  }).catch(() => {});

  loadStats();
})();
