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
    padding: 20px; max-width: 1200px; margin: 0 auto;
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
  @media (max-width: 620px) {
    #timebox { order: 10; flex-basis: 100%; margin-left: 0; }
  }
  #logout:hover { color: var(--text); border-color: var(--muted); }
  #updated {
    color: var(--muted); font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
  }
  #clock {
    color: #7ee0ff; font-size: 20px; font-weight: 700; margin-left: 8px;
    border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 12px; background: var(--panel);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-variant-numeric: tabular-nums;
    text-shadow: 0 0 10px rgba(88, 166, 255, 0.55);
  }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px;
  }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 6px; }
  .card .value { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .riskbar { height: 6px; background: #21262d; border-radius: 3px; margin-top: 12px; overflow: hidden; }
  .riskbar > div { height: 100%; border-radius: 3px; background: var(--green); transition: width .4s; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 8px; display: flex; justify-content: space-between; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin: 26px 0 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 600; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
  td { padding: 8px 10px; border-bottom: 1px solid #21262d; font-variant-numeric: tabular-nums; }
  tr:hover td { background: #1c2129; }
  .tag { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }
  .tag.buy { color: var(--green); background: #3fb95022; }
  .tag.sell { color: var(--red); background: #f8514922; }
  .tag.ok { color: var(--green); background: #3fb95022; }
  .tag.skip { color: var(--yellow); background: #d2992222; }
  .tag.ignored { color: var(--muted); background: #8b949e22; }
  .tag.error { color: var(--red); background: #f8514922; }
  .msg { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .detail { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); font-size: 13px; padding: 14px; text-align: center; background: var(--panel); border-radius: 8px; }
  .num { text-align: right; }
</style>
</head>
<body>
<header>
  <div class="headrow">
    <h1 style="margin:0">WS Trade Bot</h1>
    <span id="mode" class="badge notify">notify</span>
    <span id="timebox"><span id="updated"></span><span id="clock"></span></span>
    <a href="/logout" id="logout">log out</a>
  </div>
  <div style="display:flex;flex-basis:100%;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px">
    <span id="reader"></span>
    <span id="git"></span>
    <span id="stops"></span>
  </div>
</header>

<div class="cards" id="accounts"></div>

<h2>Open Positions</h2>
<div id="positions"></div>

<h2>Recent Alerts <button id="toggle-ignored" onclick="toggleIgnored()" style="float:right;background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer">Hide ignored</button></h2>
<div id="signals"></div>

<h2>Trade Log</h2>
<div id="trades"></div>

<h2>Settings <button id="settings-toggle" onclick="toggleSettings()" style="background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer">Show</button> <button id="settings-save" onclick="saveSettings()" style="float:right;background:#238636;color:#fff;border:0;border-radius:6px;padding:4px 14px;font-weight:600;cursor:pointer">Save</button></h2>
<div id="settings" class="card"></div>

<script>
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

function fmtTime(ts) {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(5, 16);
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
  for (const a of data.accounts) {
    const pct = Math.min(100, Math.round((a.open_risk_pct || 0)));
    const color = pct >= (a.max_open_risk_pct || 30) ? "#f85149" : pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="label">' + esc(a.label) + '</div>' +
      '<div class="value">' + fmtMoney(a.value) +
      (a.value_age ? ' <span style="font-size:12px;color:#d29922">(cached ' + a.value_age + ')</span>' : '') + '</div>' +
      '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<div class="sub"><span>open risk ' + fmtMoney(a.open_risk) + ' (' + (a.open_risk_pct ?? 0) + '%)</span>' +
      '<span>cap ' + (a.max_open_risk_pct) + '%</span></div>';
    el.appendChild(card);
  }
}

async function loadPositions() {
  const rows = await api("/api/positions");
  const el = document.getElementById("positions");
  if (!rows.length) { el.innerHTML = '<div class="empty">no open positions</div>'; return; }
  let html = "<table><tr><th>Account</th><th>Contract</th><th class=num>Qty</th><th class=num>Avg Premium</th><th class=num>Cost</th></tr>";
  for (const p of rows) {
    html += "<tr><td>" + esc(p.account) + "</td><td>" + esc(p.contract_key) + "</td>" +
      '<td class=num>' + p.qty + "</td>" +
      '<td class=num>' + (p.avg_premium ?? "—") + "</td>" +
      '<td class=num>' + fmtMoney((p.qty || 0) * (p.avg_premium || 0) * 100) + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

let settingsOpen = localStorage.getItem("ws_settings_open") === "1";

function applySettingsVisibility() {
  document.getElementById("settings").style.display = settingsOpen ? "" : "none";
  document.getElementById("settings-toggle").textContent = settingsOpen ? "Hide" : "Show";
}

function toggleSettings() {
  settingsOpen = !settingsOpen;
  localStorage.setItem("ws_settings_open", settingsOpen ? "1" : "0");
  applySettingsVisibility();
}

let lastRefresh = null;

let showIgnored = true;

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
    html += "<tr><td>" + fmtTime(s.ts) + '</td><td class="msg">' + esc(s.text || "") + "</td><td>" + tag + "</td></tr>";
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
  let checked = "not checked yet";
  if (s.last_check) {
    checked = fmtAge(Math.round(Date.now() / 1000 - s.last_check));
  }
  let text = "git: " + (s.result || "unknown");
  if (s.head) text += " @ " + s.head;
  text += " (" + checked;
  if (s.last_pull && s.last_pull.ts) {
    const how = s.last_pull.how === "pull" ? "" : s.last_pull.how + " ";
    text += " · last pull " + how +
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

async function loadSettings() {
  const s = await api("/api/settings");
  lastSettings = s;
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
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px">';
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
    '<label style="color:var(--muted);font-size:12px"><input id="set-quotes-enabled" type="checkbox"' + (s.quotes.enabled ? " checked" : "") + '> live option quotes (stop monitor)</label>' +
    '<span style="color:var(--muted);font-size:12px">quotes: ' + esc(s.quotes.provider) + '</span></div>';
  el.innerHTML = html;
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
    quotes: { enabled: document.getElementById("set-quotes-enabled").checked },
  };
  const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await res.json();
  if (res.status !== 200) {
    alert("save failed:\n" + (data.errors || []).join("\n"));
  } else {
    document.getElementById("settings-save").textContent = "Saved";
    setTimeout(() => document.getElementById("settings-save").textContent = "Save", 1500);
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
  if (lastRefresh) {
    const secs = Math.max(0, Math.round((Date.now() - lastRefresh) / 1000));
    const el = document.getElementById("updated");
    el.textContent = "data refreshed " + fmtAge(secs);
    el.style.color = secs <= 10 ? "#3fb950" : secs <= 30 ? "#d29922" : "#f85149";
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
