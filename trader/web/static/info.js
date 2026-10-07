// info server dashboard: consumers, alert feed, spx levels.
// deliberately small - the trading dashboard lives on the
// consumer apps.
"use strict";

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function ago(ts) {
  if (ts == null) return "never";
  const s = Math.max(0, Math.round(ts));
  if (s < 5) return "just now";
  if (s < 3600) return s + "s ago";
  if (s < 86400) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
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
          "<td class=num>" + esc(c.cursor == null ? "\u2014" : c.cursor) + "</td>" +
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
    const el = document.getElementById("reader-line");
    const age = d.age_seconds;
    const ok = age != null && age < 30 && d.channel !== null;
    el.textContent = "reader: " + (d.channel ? "watching " + d.channel : "waiting") +
      (age != null ? " (" + ago(age) + ")" : "");
    el.style.color = ok ? "#3fb950" : "#f85149";
  } catch (e) { /* transient */ }
}

async function loadLevels() {
  try {
    const d = await jget("/api/spx");
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
  loadLevels();
}
load();
setInterval(loadFeedStatus, 5000);
setInterval(loadSignals, 5000);
setInterval(loadReader, 1000);
