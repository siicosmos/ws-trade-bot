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

function fmtIso(ts) {
  let s = String(ts);
  // stored values are UTC; append Z when a value lacks a timezone
  if (!/[zZ+]/.test(s.slice(-6))) s += "Z";
  return s;
}

function fmtTime(ts) {
  if (!ts) return "\u2014";
  const d = new Date(fmtIso(ts));
  if (isNaN(d)) return String(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
    pad(d.getHours()) + ":" + pad(d.getMinutes());
}

let showIgnored = true;

function toggleIgnored() {
  showIgnored = !showIgnored;
  document.getElementById("toggle-ignored").textContent =
    showIgnored ? "Hide ignored" : "Show ignored";
  loadSignals();
}

function renderSignals(rows) {
  // identical to the consumer dashboard's Recent Alerts table:
  // time / message / status tags - the .msg cell wraps long
  // chatter inside its 420px column instead of blowing the
  // table boundary
  const el = document.getElementById("signals");
  const visible = showIgnored
    ? rows
    : rows.filter(function (s) { return s.parsed || s.correction; });
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty">no alerts yet</div>';
    return;
  }
  if (!visible.length) {
    el.innerHTML =
      '<div class="empty">no matching alerts (ignored hidden)</div>';
    return;
  }
  rows = visible;
  let html = "<table><tr><th>Time</th><th>Message</th><th>Status</th></tr>";
  for (const s of rows) {
    const test = (s.channel || "").toLowerCase().indexOf("test") >= 0
      ? ' <span class="tag skip" title="' + esc(s.channel || "") + '">test</span>'
      : "";
    const tag = (s.parsed
      ? '<span class="tag buy">signal</span>'
      : (s.correction
        ? '<span class="tag skip">correction</span>'
        : '<span class="tag ignored">ignored</span>')) + test;
    html += "<tr><td>" + fmtTime(s.ts) + '</td><td class="msg">' +
      esc(s.text || "") + "</td><td>" + tag + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

async function loadSignals() {
  try {
    const rows = await jget("/api/signals?limit=20");
    renderSignals(rows);
    lastRefresh = Date.now();
    tickClock();
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

// ---- settings: auto-update + this app's webhooks (the
// reader's settings live in the reader's own yaml, the
// consumer's in the consumer app)
let settingsDirty = false;
let lastSettings = null;

function setSettingsDirty(v) {
  settingsDirty = v;
  const save = document.getElementById("settings-save");
  const revert = document.getElementById("settings-revert");
  if (save) save.style.display = v ? "" : "none";
  if (revert) revert.style.display = v ? "" : "none";
}

function openSettings() {
  document.getElementById("settingsBackdrop").style.display = "flex";
  loadSettings();
}

function closeSettings() {
  document.getElementById("settingsBackdrop").style.display = "none";
}

function loadSettings() {
  jget("/api/settings").then(function (s) {
    lastSettings = s;
    const au = s.auto_update || {};
    const dc = s.discord || {};
    document.getElementById("set-au-enabled").checked = au.enabled;
    document.getElementById("set-au-interval").value = au.interval_seconds;
    document.getElementById("set-discord-webhook_url").value = dc.webhook_url || "";
    document.getElementById("set-discord-pipeline_log_webhook_url").value = dc.pipeline_log_webhook_url || "";
    document.getElementById("set-discord-update_webhook_url").value = dc.update_webhook_url || "";
    setSettingsDirty(false);
  });
}

async function saveSettings() {
  const val = (id) => document.getElementById(id).value;
  const num = (id) => parseFloat(val(id));
  const payload = {
    auto_update: {
      enabled: document.getElementById("set-au-enabled").checked,
      interval_seconds: parseInt(val("set-au-interval")),
    },
    discord: {
      webhook_url: val("set-discord-webhook_url"),
      pipeline_log_webhook_url: val("set-discord-pipeline_log_webhook_url"),
      update_webhook_url: val("set-discord-update_webhook_url"),
    },
  };
  const r = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok && d.status === "ok") {
    lastSettings = d;
    setSettingsDirty(false);
    const msg = document.getElementById("settings-msg");
    if (msg) msg.textContent = "Saved";
    setTimeout(function () {
      if (msg) msg.textContent = "";
    }, 1500);
  } else {
    alert((d.errors || ["save failed"]).join("\n"));
  }
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
