DASHBOARD_HTML = """<!DOCTYPE html>
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
  #updated { color: var(--muted); font-size: 12px; margin-left: auto; }
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
  <h1>WS Trade Bot</h1>
  <span id="mode" class="badge notify">notify</span>
  <span id="updated"></span>
</header>

<div class="cards" id="accounts"></div>

<h2>Open Positions</h2>
<div id="positions"></div>

<h2>Recent Alerts</h2>
<div id="signals"></div>

<h2>Trade Log</h2>
<div id="trades"></div>

<script>
let token = localStorage.getItem("ws_token") || "";

function headers() { return token ? { "X-Auth-Token": token } : {}; }

function askToken() {
  const t = prompt("Access token:");
  if (t) { token = t; localStorage.setItem("ws_token", t); load(); }
}

async function api(path) {
  const res = await fetch(path, { headers: headers() });
  if (res.status === 401) { askToken(); throw new Error("unauthorized"); }
  return res.json();
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toLocaleString("en-CA", { maximumFractionDigits: 0 });
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
  const el = document.getElementById("accounts");
  el.innerHTML = "";
  for (const a of data.accounts) {
    const pct = Math.min(100, Math.round((a.open_risk_pct || 0)));
    const color = pct >= (a.max_open_risk_pct || 30) ? "#f85149" : pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="label">' + a.label + '</div>' +
      '<div class="value">' + fmtMoney(a.value) + '</div>' +
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
    html += "<tr><td>" + p.account + "</td><td>" + p.contract_key + "</td>" +
      '<td class=num>' + p.qty + "</td>" +
      '<td class=num>' + (p.avg_premium ?? "—") + "</td>" +
      '<td class=num>' + fmtMoney((p.qty || 0) * (p.avg_premium || 0) * 100) + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

async function loadSignals() {
  const rows = await api("/api/signals");
  const el = document.getElementById("signals");
  if (!rows.length) { el.innerHTML = '<div class="empty">no alerts yet</div>'; return; }
  let html = "<table><tr><th>Time</th><th>Message</th><th>Status</th></tr>";
  for (const s of rows) {
    const tag = s.parsed ? '<span class="tag buy">signal</span>' : '<span class="tag ignored">ignored</span>';
    html += "<tr><td>" + fmtTime(s.ts) + '</td><td class="msg">' + (s.text || "").replace(/</g, "&lt;") + "</td><td>" + tag + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
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
    html += "<tr><td>" + fmtTime(t.ts) + "</td><td>" + t.mode + "</td>" +
      '<td><span class="tag ' + actionTag + '">' + t.action + "</span></td>" +
      '<td class=num>' + t.qty + "</td><td>" + t.ticker + "</td>" +
      '<td class=num>' + (t.price ?? "—") + "</td>" +
      '<td><span class="tag ' + statusTag + '">' + t.status + "</span></td>" +
      '<td class="detail">' + (t.detail || "").replace(/</g, "&lt;") + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
}

async function load() {
  try {
    await Promise.all([loadSummary(), loadPositions(), loadSignals(), loadTrades()]);
    document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) { /* handled in api() */ }
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""
