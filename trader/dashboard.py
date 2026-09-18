DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WS Trade Bot</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --blue: #58a6ff; --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 20px; max-width: 1400px; margin: 0 auto;
  }
  header {
    display: flex; align-items: center; gap: 14px;
    padding-bottom: 16px; margin-bottom: 20px;
    border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }
  header h1 { font-size: 22px; font-weight: 600; }
  .badge {
    padding: 3px 12px; border-radius: 999px; font-size: 12px;
    font-weight: 700; letter-spacing: .5px; text-transform: uppercase;
  }
  .badge.notify { background: #1f6feb33; color: var(--blue); }
  .badge.paper { background: #d2992233; color: var(--yellow); }
  .badge.live { background: #f8514933; color: var(--red); }
  .badge.live::before { content: "\\25CF "; animation: pulse 1.5s infinite; }
  @keyframes pulse { 50% { opacity: .3; } }
  #timebox {
    margin-left: auto; display: flex; align-items: center;
    white-space: nowrap; flex-shrink: 0;
  }
  .headrow {
    display: flex; align-items: center; flex-basis: 100%;
    gap: 10px; flex-wrap: wrap;
  }
  #logout {
    color: var(--muted); font-size: 12px; text-decoration: none;
    border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 10px; flex-shrink: 0;
  }
  #logout:hover { color: var(--text); border-color: var(--muted); }
  #updated {
    color: var(--muted); font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }
  #clock {
    color: #7ee0ff; font-size: 13px; font-weight: 700; margin-left: auto;
    border: 1px solid var(--border); border-radius: 6px;
    padding: 2px 7px; background: var(--panel);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
    text-shadow: 0 0 10px rgba(88, 166, 255, 0.55);
  }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px;
  }
  #settings-float {
    position: fixed; bottom: 24px; right: 24px; display: none;
    gap: 10px; z-index: 60;
  }
  #settings-float button {
    border: 0; border-radius: 8px; padding: 10px 22px;
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  #settings-save { background: #238636; color: #fff; }
  #settings-save:hover { background: #2ea043; }
  #settings-revert { background: #21262d; color: var(--text); border: 1px solid var(--border); }
  .cur-toggle {
    color: var(--muted); font-size: 11px; font-weight: 700;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 3px 10px; cursor: pointer;
    margin-bottom: 8px; letter-spacing: .5px;
  }
  .cur-toggle:hover { color: var(--text); border-color: var(--muted); }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 6px; }
  .card .value { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .riskbar { height: 6px; background: #21262d; border-radius: 3px; margin-top: 12px; overflow: hidden; }
  .riskbar > div { height: 100%; border-radius: 3px; background: var(--green); transition: width .4s; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 8px; display: flex; justify-content: space-between; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin: 26px 0 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .pos { table-layout: fixed; font-size: 12px; }
  .pos th, .pos td { padding: 6px 5px; word-break: break-word; }
  .pos th:nth-child(1), .pos td:nth-child(1) { width: 12%; }
  .pos th:nth-child(2), .pos td:nth-child(2) { width: 17%; }
  .pos th:nth-child(3), .pos td:nth-child(3) { width: 9%; }
  .pos th:nth-child(4), .pos td:nth-child(4) { width: 13%; }
  .pos th:nth-child(5), .pos td:nth-child(5) { width: 13%; }
  .pos th:nth-child(6), .pos td:nth-child(6) { width: 14%; }
  .pos th:nth-child(7), .pos td:nth-child(7) { width: 20%; }
  th { text-align: left; color: var(--muted); font-weight: 600; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
  td { padding: 8px 10px; border-bottom: 1px solid #21262d; font-variant-numeric: tabular-nums; }
  tr:hover td { background: #1c2129; }
  .tag { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }
  .tag.mini { padding: 0 5px; font-size: 10px; }
  .tag.buy { color: var(--green); background: #3fb95022; }
  .tag.sell { color: var(--red); background: #f8514922; }
  .tag.ok { color: var(--green); background: #3fb95022; }
  .tag.skip { color: var(--yellow); background: #d2992222; }
  .tag.ignored { color: var(--muted); background: #8b949e22; }
  .tag.error { color: var(--red); background: #f8514922; }
  .tag.info { color: var(--blue); background: #1f6feb22; }
  .msg { max-width: 420px; white-space: normal; word-break: break-word; color: var(--text); }
  .detail { max-width: 360px; white-space: normal; word-break: break-word; color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); font-size: 13px; padding: 14px; text-align: center; background: var(--panel); border-radius: 8px; }
  .num { text-align: right; }
  .subv { display: block; font-size: 11px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <div class="headrow">
    <h1 style="margin:0">WS Trade Bot</h1>
    <span id="mode" class="badge notify">notify</span>
    <span id="timebox"><span id="clock"></span></span>
  </div>
  <div style="display:flex;flex-basis:100%;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;align-items:center">
    <span id="reader"></span>
    <span id="updated">data refreshed</span>
    <span id="git"></span>
    <span id="stops"></span>
    <a href="/logout" id="logout" style="margin-left:auto">log out</a>
  </div>
</header>

<div class="cards" id="accounts"></div>

<h2>Open Positions</h2>
<div id="positions"></div>

<h2>Recent Alerts <button id="toggle-ignored" onclick="toggleIgnored()" style="float:right;background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer">Hide ignored</button></h2>
<div id="signals"></div>

<h2>Trade Log</h2>
<div id="trades"></div>

<h2>Settings <button id="settings-toggle" onclick="toggleSettings()" style="background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer">Show</button></h2>
<div id="settings" class="card"></div>

<div id="settings-float">
  <button id="settings-revert">Revert</button>
  <button id="settings-save">Save</button>
</div>
<script>
function fmtSigned(v) {
  return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-CA",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function dteBadge(expiry) {
  if (!expiry) return "";
  const parts = String(expiry).slice(0, 10).split("-");
  if (parts.length !== 3) return "";
  const eMs = Date.UTC(+parts[0], +parts[1] - 1, +parts[2]);
  const t = new Date();
  const nMs = Date.UTC(t.getFullYear(), t.getMonth(), t.getDate());
  const dte = Math.round((eMs - nMs) / 86400000);
  const days = Math.max(0, dte);
  let color;
  if (days <= 0) color = "#f85149";
  else if (days === 1) color = "#db6d28";
  else if (days <= 7) color = "#d29922";
  else color = "#3fb950";
  return '<span style="padding:0 5px;border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap;background:' +
    color + "22;color:" + color + '">' + days + "dte</span>";
}

function autoGrow(el) {
  if (!el.offsetParent) return;   // hidden: measuring gives scrollHeight 0
  el.style.height = "auto";
  el.style.height = (el.scrollHeight + 2) + "px";
}

function esc(s) {
  let out = "";
  for (const ch of String(s ?? "")) {
    if (ch === "<") out += "&lt;";
    else if (ch === ">") out += "&gt;";
    else if (ch === "&") out += "&amp;";
    else if (ch === '"') out += "&quot;";
    else if (ch === "'") out += "&#39;";
    else out += ch;
  }
  return out;
}

async function api(path) {
  const res = await fetch(path);
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  return res.json();
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtIso(ts) {
  let s = String(ts);
  // stored values are UTC; append Z when a value lacks a timezone
  if (!/[zZ+]/.test(s.slice(-6))) s += "Z";
  return s;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(fmtIso(ts));
  if (isNaN(d)) return String(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
    pad(d.getHours()) + ":" + pad(d.getMinutes());
}

async function loadSummary() {
  const data = await api("/api/summary");
  const modeEl = document.getElementById("mode");
  modeEl.textContent = data.mode;
  modeEl.className = "badge " + data.mode;
  if (data.stops) {
    const s = data.stops;
    const streak = s.consecutive_losses + "/" + s.max_consecutive_losses;
    document.getElementById("stops").textContent =
      "stop-loss " + s.stop_loss_pct + "% · loss streak " + streak +
      (s.trailing_stop_pct > 0 ? " · trailing " + s.trailing_stop_pct + "%" : "");
  }
  if (data.reader) {
    const r = data.reader;
    const age = r.age_seconds === null || r.age_seconds > 30 ? "offline" : r.age_seconds + "s ago";
    document.getElementById("reader").textContent = r.channel
      ? "watching: " + r.channel + " (" + age + ")"
      : "waiting for: " + (r.desired || "any open channel") + " (" + age + ")";
    document.getElementById("reader").style.color =
      r.age_seconds !== null && r.age_seconds <= 30 ? "var(--green)" : "var(--yellow)";
  }
  const el = document.getElementById("accounts");
  el.innerHTML = "";
  const toggle = document.createElement("button");
  toggle.className = "cur-toggle";
  toggle.title = "flip account value currency";
  toggle.textContent = valueCurrency.toUpperCase() + " ⇄";
  toggle.onclick = toggleValueCurrency;
  el.appendChild(toggle);
  for (const a of data.accounts) {
    const pct = Math.min(100, Math.round((a.open_risk_pct || 0)));
    const color = pct >= (a.max_open_risk_pct || 30) ? "#f85149" : pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950";
    const usd = a.usd_value && a.value;
    const showUsd = valueCurrency === "usd" && usd;
    const fx = usd ? a.usd_value / a.value : null;
    const risk = showUsd ? a.open_risk * fx : a.open_risk;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="label">' + esc(a.label) + '</div>' +
      '<div class="value">' + (showUsd ? fmtMoney(a.usd_value) : fmtMoney(a.value)) +
      (a.value_age ? ' <span style="font-size:12px;color:#d29922">(cached ' + a.value_age + ')</span>' : '') +
      (showUsd
        ? ' <span style="font-size:13px;color:var(--muted)">≈ ' + fmtMoney(a.value) + ' CAD</span>'
        : (a.usd_value
          ? ' <span style="font-size:13px;color:var(--muted)">≈ $' + a.usd_value.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' USD</span>'
          : (valueCurrency === "usd" ? ' <span style="font-size:12px;color:var(--yellow)">fx unavailable</span>' : ''))) + '</div>' +
      '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<div class="sub"><span style="color:' + color + (pct >= (a.max_open_risk_pct || 30) ? ';font-weight:700' : '') + '">open risk ' + fmtMoney(risk) + ' (' + (a.open_risk_pct ?? 0) + '%)</span>' +
      '<span>cap ' + (a.max_open_risk_pct) + '%</span></div>' +
      ((a.cash_cad != null || a.cash_usd != null)
        ? '<div class="sub"><span>cash ' + (a.cash_cad != null ? fmtMoney(a.cash_cad) : "—") +
          (a.cash_usd != null
            ? (a.cash_usd >= 0
              ? ' · $' + a.cash_usd.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' usd'
              : ' · <span style="color:var(--yellow)">usd margin used $' +
                Math.abs(a.cash_usd).toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</span>')
            : '') + '</span>' +
          '<span>buying power</span></div>'
        : '');
    el.appendChild(card);
  }
}

async function loadPositions() {
  const rows = await api("/api/positions");
  const el = document.getElementById("positions");
  if (!rows.length) { el.innerHTML = '<div class="empty">no open positions</div>'; return; }
  let html = '<table class="pos"><tr><th>Account</th><th>Contract</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Price $</th><th class=num>Return</th><th class=num>Total Cost $</th></tr>';
  for (const p of rows) {
    const ret = p.pct_return ?? null;
    const retColor = ret === null ? "var(--muted)" : ret >= 0 ? "var(--green)" : "var(--red)";
    const isStock = p.kind === "stock";
    const cur = isStock && p.currency === "USD" ? " usd" : "";
    const mult = isStock ? 1 : 100;
    const avgTotal = (p.qty || 0) * (p.avg_premium || 0) * mult;
    const mv = p.market_value;
    let pl = null;
    if (mv != null && p.cost_usd != null) {
      pl = p.short ? p.cost_usd - mv : mv - p.cost_usd;
    }
    const retMain = ret === null ? "—" : (ret > 0 ? "+" : "") + ret + "%";
    const plSpan = pl === null ? "" :
      '<span class="subv">(' + (pl > 0 ? "+$" : pl < 0 ? "-$" : "$") +
      Math.abs(pl).toLocaleString("en-CA", { maximumFractionDigits: 2 }) + (isStock ? cur : "") + ")</span>";
    html += "<tr><td>" + esc(p.account) + (
      p.source === "ws"
        ? ' <span class="tag ignored mini" title="live from Wealthsimple">ws</span>'
        : ""
    ) + "</td><td>" + (p.kind === "stock"
      ? '<span style="font-weight:600">' + esc(p.underlying || "") + '</span><br><span style="font-size:11px;color:var(--muted)">stock</span>'
      : '<span style="font-weight:600">' + esc(p.underlying || "") + " " + esc(p.strike || "") + esc(p.right || "") +
        '</span><br><span style="font-size:11px;color:var(--muted)">' +
        esc(String(p.expiry || "").slice(0, 10)) + "</span> " +
        dteBadge(p.expiry)) + "</td>" +
      '<td class=num>' + (p.short ? "-" + p.qty : p.qty) + (p.short ? ' <span class="tag skip mini" title="short position">short</span>' : (p.spread ? ' <span class="tag ignored mini" title="multi-leg spread">spread</span>' : "")) + "</td>" +
      '<td class=num>' + (p.avg_premium != null
        ? (p.spread ? fmtSigned(avgTotal) : "$" + avgTotal.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur) +
          '<span class="subv">(' + (p.spread ? fmtSigned(p.avg_premium) : "$" + p.avg_premium + cur) + ")</span>"
        : "—") + "</td>" +
      '<td class=num>' + (p.current_price != null
        ? (p.spread ? fmtSigned(p.current_price) : "$" + p.current_price + cur) + (mv != null
          ? '<span class="subv">(' + (p.short || (p.spread && mv < 0) ? "-$" : "$") +
            Math.abs(mv).toLocaleString("en-CA", { maximumFractionDigits: 2 }) + (isStock ? cur : "") + ")</span>"
          : "")
        : "—") + "</td>" +
      '<td class=num style="color:' + retColor + '">' + retMain + plSpan + "</td>" +
      '<td class=num>' + (isStock
        ? (p.cost_usd != null ? "$" + p.cost_usd.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur : "—") +
          (p.currency === "CAD" || p.cost_cad == null
            ? ""
            : '<span class="subv">(' + fmtMoney(p.cost_cad) + ')</span>')
        : (p.cost_usd != null ? (p.spread ? fmtSigned(p.cost_usd) : fmtMoney(p.cost_usd)) + (p.cost_cad != null ? '<span class="subv">(' + (p.spread ? fmtSigned(p.cost_cad) : fmtMoney(p.cost_cad)) + ')</span>' : "") : '<span class="subv">(' + fmtMoney(avgTotal) + ")</span>")) + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

let settingsOpen = localStorage.getItem("ws_settings_open") === "1";

function applySettingsVisibility() {
  document.getElementById("settings").style.display = settingsOpen ? "" : "none";
  document.getElementById("settings-toggle").textContent = settingsOpen ? "Hide" : "Show";
  if (settingsOpen) {
    document.querySelectorAll("#settings textarea").forEach(autoGrow);
  }
}

function toggleSettings() {
  settingsOpen = !settingsOpen;
  localStorage.setItem("ws_settings_open", settingsOpen ? "1" : "0");
  applySettingsVisibility();
}

let lastRefresh = null;

let showIgnored = true;

let valueCurrency = localStorage.getItem("ws_value_currency") || "cad";

function toggleValueCurrency() {
  valueCurrency = valueCurrency === "cad" ? "usd" : "cad";
  localStorage.setItem("ws_value_currency", valueCurrency);
  loadSummary();
}

function toggleIgnored() {
  showIgnored = !showIgnored;
  document.getElementById("toggle-ignored").textContent = showIgnored ? "Hide ignored" : "Show ignored";
  loadSignals();
}

async function loadSignals() {
  const rows = await api("/api/signals");
  const el = document.getElementById("signals");
  const visible = showIgnored ? rows : rows.filter(function(s) { return s.parsed || s.correction; });
  if (!rows.length) { el.innerHTML = '<div class="empty">no alerts yet</div>'; return; }
  if (!visible.length) { el.innerHTML = '<div class="empty">no matching alerts (ignored hidden)</div>'; return; }
  let html = "<table><tr><th>Time</th><th>Message</th><th>Status</th></tr>";
  for (const s of visible) {
    const test = (s.channel || "").toLowerCase().indexOf("test") >= 0 ? ' <span class="tag skip" title="from ' + esc(s.channel || "") + '">test</span>' : "";
    const tag = (s.parsed ? '<span class="tag buy">signal</span>' : (s.correction ? '<span class="tag skip">correction</span>' : '<span class="tag ignored">ignored</span>')) + test;
    let cell = fmtTime(s.ts);
    if (s.received_ts) {
      const lag = Math.round(
        (new Date(fmtIso(s.received_ts)) - new Date(fmtIso(s.ts))) / 1000
      );
      if (!isNaN(lag) && s.ts) {
        const lbl = lag >= 0 ? "+" : "-";
        const a = Math.abs(lag);
        const lagTxt = a >= 3600
          ? lbl + Math.floor(a / 3600) + "h" + Math.round((a % 3600) / 60) + "m"
          : a >= 60 ? lbl + Math.round(a / 60) + "m" : lbl + a + "s";
        cell += '<span class="subv">parsed ' + fmtTime(s.received_ts) +
          " (" + lagTxt + ")</span>";
      } else {
        cell += '<span class="subv">parsed ' + fmtTime(s.received_ts) + "</span>";
      }
    }
    html += "<tr><td>" + cell + '</td><td class="msg">' + esc(s.text || "") + "</td><td>" + tag + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

function fmtAge(secs) {
  if (secs < 60) return secs + "s ago";
  if (secs < 3600) return Math.round(secs / 60) + "m ago";
  if (secs < 86400) return Math.round(secs / 3600) + "h ago";
  return Math.round(secs / 86400) + "d ago";
}

async function loadGitStatus() {
  const s = await api("/api/update_status");
  const el = document.getElementById("git");
  if (!s || s.status !== "active") { el.textContent = ""; return; }
  const checked = s.last_check
    ? "checked " + fmtAge(Math.round(Date.now() / 1000 - s.last_check))
    : "first check pending";
  let result = s.result || "unknown";
  if (result === "not checked yet") result = "pending";
  let text = "git: " + result;
  if (s.head) text += " @ " + s.head;
  text += " (" + checked;
  if (s.interval_seconds) text += " · every " + s.interval_seconds + "s";
  if (s.last_pull && s.last_pull.ts) {
    const how =
      s.last_pull.how === "auto" || s.last_pull.how === "manual"
        ? s.last_pull.how + " "
        : "";
    text += " · last " + how + "pull " +
      fmtAge(Math.round(Date.now() / 1000 - s.last_pull.ts));
  }
  text += ")";
  if (s.errors) text += " · " + s.errors + " errors";
  el.textContent = text;
  el.style.color = (s.result || "").indexOf("error") >= 0 || (s.result || "").indexOf("failed") >= 0 ? "var(--red)" : "var(--muted)";
}

async function loadTrades() {
  const rows = await api("/api/trades");
  const el = document.getElementById("trades");
  if (!rows.length) { el.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  let html = "<table><tr><th>Time</th><th>Mode</th><th>Action</th><th class=num>Qty</th><th>Ticker</th><th class=num>Price</th><th>Status</th><th>Detail</th></tr>";
  for (const t of rows) {
    const actionTag = t.action === "BUY" ? "buy" : "sell";
    let statusTag = "ignored";
    if (t.status === "executed") statusTag = "ok";
    else if (t.status === "skipped") statusTag = "skip";
    else if (t.status === "error") statusTag = "error";
    else if (t.status === "notified") statusTag = "info";
    html += "<tr><td>" + fmtTime(t.ts) + "</td><td>" + esc(t.mode) + "</td>" +
      '<td><span class="tag ' + actionTag + '">' + esc(t.action) + "</span></td>" +
      '<td class=num>' + t.qty + "</td><td>" + esc(t.ticker) + "</td>" +
      '<td class=num>' + (t.price ?? "—") + "</td>" +
      '<td><span class="tag ' + statusTag + '">' + esc(t.status) + "</span></td>" +
      '<td class="detail">' + esc(t.detail || "") + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

let lastSettings = null;

let settingsDirty = false;

function setSettingsDirty(v) {
  settingsDirty = v;
  document.getElementById("settings-float").style.display = v ? "flex" : "none";
}

function revertSettings() {
  setSettingsDirty(false);
  loadSettings();
}

document.getElementById("settings-save").onclick = saveSettings;
document.getElementById("settings-revert").onclick = revertSettings;

async function loadSettings() {
  const s = await api("/api/settings");
  lastSettings = s;
  // leave the form alone while the user has unsaved edits - the
  // periodic refresh used to wipe them mid-typing
  if (settingsDirty) return;
  const el = document.getElementById("settings");
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">';
  const t = s.trading;
  const labels = {
    risk_per_trade_pct: "default risk %", max_contracts_per_trade: "max contracts",
    max_open_risk_pct: "open risk cap %", stop_loss_pct: "stop loss %",
    trailing_stop_pct: "trailing stop %", stop_check_seconds: "stop check (s)",
    max_consecutive_losses: "max losses in row", min_dte_days: "min DTE",
    max_trades_per_day: "max trades/day", cooldown_seconds: "cooldown (s)",
    dedupe_window_minutes: "dedupe (min)",
  };
  for (const [k, label] of Object.entries(labels)) {
    html += '<div><label style="color:var(--muted);font-size:11px;text-transform:uppercase">' + label + '</label>' +
      '<input id="set-' + k + '" type="number" step="any" value="' + t[k] + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>';
  }
  for (const [k, hint] of [["ticker_whitelist", "e.g. SPY, SPX - empty = allow all"], ["skip_underlyings", "e.g. SPX - empty = none"]]) {
    html += '<div style="grid-column:1/-1"><label style="color:var(--muted);font-size:11px;text-transform:uppercase">' + k.replace('_', ' ') + ' (comma-separated)</label>' +
      '<input id="set-' + k + '" type="text" value="' + esc((t[k] || []).join(', ')) + '" placeholder="' + hint + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>';
  }
  html += "</div>";
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:14px">';
  html += '<div style="color:var(--muted);font-size:12px;grid-column:1/-1">size tiers (risk % cap / min / max contracts)</div>';
  for (const [name, tier] of Object.entries(t.size_tiers)) {
    html += '<div><label style="color:var(--muted);font-size:11px;text-transform:uppercase">' + esc(name) + '</label>' +
      '<input id="tier-' + name + '-risk" type="number" step="any" value="' + tier.risk_pct_max + '" title="risk % cap" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">' +
      '<div style="display:flex;gap:6px;margin-top:4px"><input id="tier-' + name + '-min" type="number" value="' + tier.contracts_min + '" title="min contracts" style="width:50%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">' +
      '<input id="tier-' + name + '-max" type="number" value="' + tier.contracts_max + '" title="max contracts" style="width:50%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div></div>';
  }
  html += "</div>";
  if (s.accounts && s.accounts.length) {
    html += '<div style="color:var(--muted);font-size:12px;margin-top:14px">accounts (numeric overrides: empty = inherit global)</div>';
    html += '<div style="display:grid;grid-template-columns:1fr;gap:10px">';
    s.accounts.forEach((a, i) => {
      html += '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div style="color:var(--text);font-weight:600;margin-bottom:6px">' + esc(a.label) +
        ' <label style="float:right;color:var(--muted);font-size:11px"><input id="set-acct-' + i + '-enabled" type="checkbox"' + (a.enabled ? " checked" : "") + '> on</label></div>' +
        '<label style="color:var(--muted);font-size:10px;text-transform:uppercase">account id</label>' +
        '<input id="set-acct-' + i + '-id" type="text" value="' + esc(a.account_id || "") + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:12px">' +
        '<label style="color:var(--muted);font-size:10px;text-transform:uppercase;margin-top:4px;display:block">max contracts</label>' +
        '<input id="set-acct-' + i + '-max" type="number" value="' + (a.max_contracts_per_trade ?? "") + '" placeholder="global 10" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:12px">' +
        '<label style="color:var(--muted);font-size:10px;text-transform:uppercase;margin-top:4px;display:block">risk % (default trades)</label>' +
        '<input id="set-acct-' + i + '-risk" type="number" step="any" value="' + (a.risk_per_trade_pct ?? "") + '" placeholder="global 5" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:12px">' +
        '<label style="color:var(--muted);font-size:10px;text-transform:uppercase;margin-top:4px;display:block">paper value $</label>' +
        '<input id="set-acct-' + i + '-paper" type="number" step="any" value="' + (a.paper_value ?? "") + '" placeholder="10000" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:12px"></div>';
    });
    html += "</div>";
  }
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:14px">' +
    '<div style="color:var(--muted);font-size:12px;grid-column:1/-1">reader (channel marker: empty = follow whatever channel is open in Discord)</div>' +
    '<div><label style="color:var(--muted);font-size:11px;text-transform:uppercase">channel marker</label>' +
    '<input id="set-reader-channel_marker" type="text" value="' + esc(s.reader.channel_marker || "") + '" placeholder="e.g. player-alerts" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>' +
    '<div><label style="color:var(--muted);font-size:11px;text-transform:uppercase">poll interval (s)</label>' +
    '<input id="set-reader-poll_interval" type="number" step="any" value="' + s.reader.poll_interval + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>' +
    '<div><label style="color:var(--muted);font-size:11px;text-transform:uppercase">max messages kept</label>' +
    '<input id="set-reader-max_items" type="number" value="' + s.reader.max_items + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>' +
            '<div style="margin-bottom:6px"><label style="color:var(--muted);font-size:12px">Allowed channels (comma-separated, empty = any)</label>' +
            '<input id="set-reader-channels" type="text" value="' + esc((s.reader.channels || []).join(",")) + '" placeholder="e.g. test-alerts, player-alerts" style="width:100%;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"></div>' +
    "</div>";
  html += '<div style="display:flex;gap:10px;margin-top:14px;align-items:center;flex-wrap:wrap">' +
    '<label style="color:var(--muted);font-size:12px"><input id="set-au-enabled" type="checkbox"' + (s.auto_update.enabled ? " checked" : "") + '> auto-update</label>' +
    '<label style="color:var(--muted);font-size:12px">every <input id="set-au-interval" type="number" value="' + s.auto_update.interval_seconds + '" style="width:80px;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:13px">s</label>' +
    '<div style="grid-column:1/-1;color:var(--muted);font-size:12px">discord webhooks (take effect after restart)</div>' +
    [["set-discord-webhook_url", "trade alerts", "main alerts channel", (s.discord || {}).webhook_url || ""],
     ["set-discord-reader_log_webhook_url", "reader log", "empty = off", (s.discord || {}).reader_log_webhook_url || ""],
     ["set-discord-pipeline_log_webhook_url", "pipeline log", "empty = off", (s.discord || {}).pipeline_log_webhook_url || ""],
     ["set-discord-update_webhook_url", "update notices", "empty = trade alerts channel", (s.discord || {}).update_webhook_url || ""]].map(hook =>
      '<div style="flex-basis:100%"><label style="color:var(--muted);font-size:11px;text-transform:uppercase">' + hook[1] + '</label>' +
      '<textarea id="' + hook[0] + '" rows="2" placeholder="' + hook[2] + '" style="width:100%;box-sizing:border-box;resize:none;overflow:hidden;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">' + esc(hook[3]) + '</textarea></div>'
    ).join("") +
    '<label style="color:var(--muted);font-size:12px">positions every <input id="set-ws-positions" type="number" value="' + (s.wealthsimple ? s.wealthsimple.positions_refresh_seconds : 30) + '" style="width:70px;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:13px">s</label>' +
    '<label style="color:var(--muted);font-size:12px">account values every <input id="set-ws-values" type="number" value="' + (s.wealthsimple ? s.wealthsimple.values_refresh_seconds : 60) + '" style="width:70px;background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:13px">s</label>' +
    '<label style="color:var(--muted);font-size:12px"><input id="set-quotes-enabled" type="checkbox"' + (s.quotes.enabled ? " checked" : "") + '> live option quotes (stop monitor)</label>' +
    '<label style="color:var(--muted);font-size:12px">quotes: <select id="set-quotes-provider" style="background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 6px;font-size:13px"><option value="ws"' + (s.quotes.provider === "ws" ? " selected" : "") + '>ws</option><option value="moomoo"' + (s.quotes.provider === "moomoo" ? " selected" : "") + '>moomoo</option></select></label></div>';
  el.innerHTML = html;
  el.querySelectorAll("textarea").forEach(function(t) {
    autoGrow(t);
    t.addEventListener("input", function() { autoGrow(t); });
  });
  el.querySelectorAll("input,select,textarea").forEach(function(i) {
    i.addEventListener("input", function() { setSettingsDirty(true); });
    i.addEventListener("change", function() { setSettingsDirty(true); });
  });
}

async function saveSettings() {
  const val = (id) => document.getElementById(id).value;
  const num = (id) => parseFloat(val(id));
  const trading = {};
  for (const k of ["risk_per_trade_pct","max_contracts_per_trade","max_open_risk_pct","stop_loss_pct","trailing_stop_pct","stop_check_seconds","max_consecutive_losses","min_dte_days","max_trades_per_day","cooldown_seconds","dedupe_window_minutes"]) {
    trading[k] = num("set-" + k);
  }
  const toList = (id) => val(id).split(",").map(function(s) { return s.trim(); }).filter(Boolean);
  trading.ticker_whitelist = toList("set-ticker_whitelist");
  trading.skip_underlyings = toList("set-skip_underlyings");
  const tiers = {};
  document.querySelectorAll("[id^=tier-]").forEach(() => {});
  const names = new Set();
  document.querySelectorAll("[id^=tier-]").forEach(el => names.add(el.id.split("-")[1]));
  for (const name of names) {
    tiers[name] = { risk_pct_max: num("tier-" + name + "-risk"), contracts_min: parseInt(val("tier-" + name + "-min")), contracts_max: parseInt(val("tier-" + name + "-max")) };
  }
  trading.size_tiers = tiers;
  const accounts = (lastSettings.accounts || []).map((a, i) => {
    const numOrNull = (id) => {
      const v = val(id);
      return v === "" ? null : parseFloat(v);
    };
    return {
      label: a.label,
      account_id: val("set-acct-" + i + "-id"),
      max_contracts_per_trade: val("set-acct-" + i + "-max") === "" ? null : parseInt(val("set-acct-" + i + "-max")),
      risk_per_trade_pct: numOrNull("set-acct-" + i + "-risk"),
      paper_value: numOrNull("set-acct-" + i + "-paper"),
      enabled: document.getElementById("set-acct-" + i + "-enabled").checked,
    };
  });
  const payload = {
    trading,
    accounts,
    reader: {
      channel_marker: val("set-reader-channel_marker"),
      poll_interval: num("set-reader-poll_interval"),
      max_items: parseInt(val("set-reader-max_items")),
      channels: val("set-reader-channels").split(",").map(function(s) { return s.trim(); }).filter(Boolean),
    },
    auto_update: { enabled: document.getElementById("set-au-enabled").checked, interval_seconds: parseInt(val("set-au-interval")) },
    wealthsimple: { positions_refresh_seconds: parseInt(val("set-ws-positions")), values_refresh_seconds: parseInt(val("set-ws-values")) },
    discord: { webhook_url: val("set-discord-webhook_url").trim(), reader_log_webhook_url: val("set-discord-reader_log_webhook_url").trim(), pipeline_log_webhook_url: val("set-discord-pipeline_log_webhook_url").trim(), update_webhook_url: val("set-discord-update_webhook_url").trim() },
    quotes: { enabled: document.getElementById("set-quotes-enabled").checked, provider: val("set-quotes-provider") },
  };
  const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await res.json();
  if (res.status !== 200) {
    alert("save failed:\n" + (data.errors || []).join("\n"));
  } else {
    document.getElementById("settings-save").textContent = "Saved";
    setTimeout(() => document.getElementById("settings-save").textContent = "Save", 1500);
    setSettingsDirty(false);
    await loadSettings();
    load();
  }
}

async function load() {
  try {
    await Promise.all([loadSummary(), loadPositions(), loadSignals(), loadTrades(), loadSettings(), loadGitStatus()]);
    lastRefresh = Date.now();
  } catch (e) { /* handled in api() */ }
}

applySettingsVisibility();
load();
setInterval(load, 5000);
function tickClock() {
  document.getElementById("clock").textContent =
    "now " + new Date().toLocaleTimeString();
  const el = document.getElementById("updated");
  if (lastRefresh) {
    const secs = Math.max(0, Math.round((Date.now() - lastRefresh) / 1000));
    el.textContent = "data refreshed " + fmtAge(secs);
    el.style.color = secs <= 10 ? "#3fb950" : secs <= 30 ? "#d29922" : "#f85149";
  } else {
    el.textContent = "data refreshed …";
    el.style.color = "var(--muted)";
  }
}
window.addEventListener("pageshow", (e) => {
  if (e.persisted) {
    fetch("/api/summary").then((r) => {
      if (r.status === 401) location.href = "/login";
    }).catch(() => {});
  }
});

tickClock();
setInterval(tickClock, 1000);
</script>
</body>
</html>
"""


def LOGIN_HTML(error=None):
    import html

    message = (
        f'<p style="color:#f85149;margin:0 0 14px">{html.escape(str(error))}</p>'
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WS Trade Bot - log in</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #0d1117;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #e6edf3;
  }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 32px; width: 320px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p {{ color: #8b949e; font-size: 12px; margin: 0 0 20px; }}
  input {{
    width: 100%; box-sizing: border-box; background: #0d1117;
    color: #e6edf3; border: 1px solid #30363d; border-radius: 6px;
    padding: 8px 10px; font-size: 14px; margin-bottom: 12px;
  }}
  input:focus {{ outline: none; border-color: #58a6ff; }}
  button {{
    width: 100%; background: #238636; color: #fff; border: 1px solid #2ea043;
    border-radius: 6px; padding: 8px 10px; font-size: 14px; font-weight: 600;
    cursor: pointer;
  }}
  button:hover {{ background: #2ea043; }}
</style>
</head>
<body>
<div class="card">
  <h1>WS Trade Bot</h1>
  <p>enter your access token to continue</p>
  {message}
  <form method="post" action="/login">
    <input type="password" name="password" placeholder="access token" autofocus>
    <button type="submit">Log in</button>
  </form>
</div>
</body>
</html>"""
