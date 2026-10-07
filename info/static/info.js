// info server dashboard: consumers, alert feed, spx levels.
// deliberately small - the trading dashboard lives on the
// consumer apps. Same design language and header behaviour as
// the consumer dashboard (clock, reader line, git badge).
"use strict";

let lastRefresh = null;

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtAge(secs) {
  if (secs < 5) return "just now";
  if (secs < 3600) return secs + "s ago";
  if (secs < 86400) return Math.round(secs / 60) + "m ago";
  return Math.round(secs / 3600) + "h ago";
}

function ago(ts) {
  if (ts == null) return "never";
  return fmtAge(Math.max(0, Math.round(ts)));
}

function tickClock() {
  document.getElementById("clock").textContent =
    "now " + new Date().toLocaleTimeString();
  const el = document.getElementById("updated");
  if (lastRefresh) {
    const secs = Math.max(0, Math.round((Date.now() - lastRefresh) / 1000));
    el.textContent = "data refreshed " + fmtAge(secs);
    el.style.color = secs <= 10 ? "#3fb950" : secs <= 30 ? "#d29922" : "#f85149";
  }
}

let readerInfo = null;

function renderReader() {
  // same treatment as the consumer dashboard's reader line
  const el = document.getElementById("reader");
  if (!el) return;
  if (!readerInfo) {
    el.textContent = "reader \u2026";
    el.style.color = "var(--muted)";
    return;
  }
  const r = readerInfo;
  const age = r.age_seconds === null ? null
    : Math.round(r.age_seconds + (Date.now() - r.receivedAt) / 1000);
  const ageTxt =
    age === null || age > 30 ? "offline" : age + "s ago";
  el.textContent = r.channel
    ? "watching: " + r.channel + " (" + ageTxt + ")"
    : "waiting for: " + (r.desired || "any open channel") + " (" + ageTxt + ")";
  el.style.color =
    age !== null && age <= 30 ? "var(--green)" : "var(--yellow)";
}

let gitInfo = null;

function renderGit() {
  // same treatment as the consumer dashboard's git badge
  const el = document.getElementById("git");
  if (!el) return;
  const s = gitInfo;
  if (!s || s.status !== "active") { el.textContent = ""; return; }
  const checked = s.last_check
    ? "checked " + fmtAge(Math.round(Date.now() / 1000 - s.last_check))
    : "first check pending";
  let result = s.result || "unknown";
  if (result === "not checked yet") result = "pending";
  let text = "git: " + result;
  if (s.head) text += " @ " + s.head;
  text += " (" + checked;
  if (s.interval_seconds) text += " \u00b7 every " + s.interval_seconds + "s";
  text += ")";
  el.textContent = text;
}

async function loadFeedStatus() {
  try {
    const d = await jget("/api/feed-status");
    const t = document.getElementById("consumers-table");
    t.innerHTML = "<tr><th>label</th><th>push url</th><th>feed last seen</th>" +
      "<th>cursor</th><th>pushed</th><th>failed</th><th>last error</th></tr>" +
      (d.consumers || []).map(function (c) {
        const ok = c.alive;
        const dot = '<span style="color:' + (ok ? "#3fb950" : "#f85149") + '">' +
          (ok ? "\u25cf" : "\u25cb") + "</span>";
        return "<tr><td>" + dot + " " + esc(c.label) + "</td>" +
          "<td>" + esc(c.push_url || "(pull-only)") + "</td>" +
          "<td>" + ago(c.last_seen_age == null ? null : c.last_seen_age) + "</td>" +
          '<td class=num>' + esc(c.cursor == null ? "\u2014" : c.cursor) + "</td>" +
          '<td class=num>' + esc(c.pushed || 0) + "</td>" +
          '<td class=num' + ((c.push_failed || 0) ? ' style="color:#f85149"' : "") + '>' +
          esc(c.push_failed || 0) + "</td>" +
          "<td>" + esc(c.last_error || "\u2014") + "</td></tr>";
      }).join("");
  } catch (e) { /* transient */ }
}

async function loadSignals() {
  try {
    const rows = await jget("/api/signals?limit=20");
    const t = document.getElementById("signals-table");
    t.innerHTML = "<tr><th>time</th><th>channel</th><th>author</th><th>text</th><th>parsed</th></tr>" +
      (rows || []).map(function (r) {
        return "<tr><td>" + esc((r.ts || "").replace("T", " ")) + "</td>" +
          "<td>" + esc(r.channel || "\u2014") + "</td>" +
          "<td>" + esc(r.author || "\u2014") + "</td>" +
          '<td class="msg">' + esc(r.text || "") + "</td>" +
          "<td>" + (r.parsed ? "\u2713" : "\u2014") + "</td></tr>";
      }).join("");
  } catch (e) { /* transient */ }
}

async function loadReader() {
  try {
    const d = await jget("/api/reader_status");
    readerInfo = Object.assign({}, d, { receivedAt: Date.now() });
    renderReader();
  } catch (e) { /* transient */ }
}

async function loadGit() {
  try {
    gitInfo = await jget("/api/update_status");
    renderGit();
  } catch (e) { /* transient */ }
}

async function loadLevels() {
  try {
    const d = await jget("/api/levels");
    if (d.text != null && document.getElementById("levels-input").value === "") {
      document.getElementById("levels-input").value = d.text;
    }
  } catch (e) { /* transient */ }
}

document.getElementById("levels-save").onclick = async function () {
  const btn = document.getElementById("levels-save");
  const status = document.getElementById("levels-status");
  btn.disabled = true;
  try {
    const r = await fetch("/api/spx-levels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: document.getElementById("levels-input").value,
      }),
    });
    status.textContent = r.ok ? "saved" : "save failed (" + r.status + ")";
  } catch (e) {
    status.textContent = "save failed";
  }
  btn.disabled = false;
  setTimeout(function () { status.textContent = ""; }, 3000);
};

function load() {
  loadFeedStatus();
  loadSignals();
  loadReader();
  loadGit();
  loadLevels();
  lastRefresh = Date.now();
  tickClock();
}
load();
setInterval(tickClock, 1000);
setInterval(loadFeedStatus, 5000);
setInterval(loadSignals, 5000);
setInterval(loadReader, 1000);
setInterval(loadGit, 30000);
setInterval(loadLevels, 30000);
