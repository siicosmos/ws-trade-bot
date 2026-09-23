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

function showReconnect(on) {
  const el = document.getElementById("reconnect");
  if (el) el.style.display = on ? "block" : "none";
}

async function api(path) {
  let res;
  try {
    res = await fetch(path);
  } catch (e) {
    // network down / server restarting - surfaced by the
    // banner instead of raw fetch errors
    showReconnect(true);
    throw e;
  }
  if (res.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  try {
    const data = await res.json();
    showReconnect(false);
    return data;
  } catch (e) {
    showReconnect(true);
    throw e;
  }
}

function maskBreakdown(lines) {
  // structure only when hidden: symbols, amounts and rates all
  // become dots, just the "x" and "=" separators stay
  return (lines || []).map(function(l) {
    return esc(l).replace(
      /[A-Za-z0-9][A-Za-z0-9.,%]*/g,
      function(m) { return m === "x" ? m : "\u2022\u2022\u2022\u2022"; }
    );
  }).join("\n");
}

function breakdownText(lines, masked) {
  if (!(lines || []).length) return "";
  return masked ? maskBreakdown(lines)
    : esc(lines.join("\n"));
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  // -0 (a rounding artifact) must not render as "$-0.00"
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(
    "en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

let paperOpen = [];
try {
  const _po = localStorage.getItem("ws_paper_open");
  if (_po) {
    // older versions stored a single plain label, not a JSON array
    paperOpen = _po.startsWith("[")
      ? JSON.parse(_po)
      : [_po];
  }
} catch (e) { paperOpen = []; }
let paperPositions = null;

let pmbdOpen = null;

function togglePaperBreakdown(i, label) {
  pmbdOpen = (pmbdOpen === label) ? null : label;
  const el = document.getElementById("pmbd-" + i);
  if (el) el.style.display = pmbdOpen === label ? "block" : "none";
  const arrow = document.getElementById("pmbda-" + i);
  if (arrow) arrow.textContent = pmbdOpen === label ? "\u25BC" : "\u25B2";
}

let modalAction = null;

function openModal(title, text, actionLabel, action) {
  modalAction = action;
  document.getElementById("mTitle").textContent = title;
  document.getElementById("mText").textContent = text;
  document.getElementById("mGo").textContent = actionLabel;
  document.getElementById("modalBackdrop").style.display = "flex";
}

function closeModal() {
  document.getElementById("modalBackdrop").style.display = "none";
  modalAction = null;
}

async function resetPaper(label) {
  openModal(
    "Reset paper ledger",
    "Reset the paper ledger for " + label + "? It will be " +
      "re-seeded from the live account.",
    "reset",
    async function() { await doPaperReset(label); }
  );
}

async function doPaperReset(label) {
  try {
    const res = await fetch("/api/paper-reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
  } catch (e) { /* surfaced by the next refresh */ }
  paperPositions = null;
  load();
}

function togglePaper(label) {
  paperOpen = paperOpen.indexOf(label) >= 0
    ? paperOpen.filter(function(l) { return l !== label; })
    : paperOpen.concat([label]);
  localStorage.setItem("ws_paper_open", JSON.stringify(paperOpen));
  load();
}

let readerInfo = null;

function renderReader() {
  // ticks on its own - button clicks never rewrite this line
  const el = document.getElementById("reader");
  if (!el || !readerInfo) return;
  const r = readerInfo;
  const age = r.age_seconds === null ? null
    : Math.round(
        r.age_seconds + (Date.now() - r.receivedAt) / 1000
      );
  const ageTxt =
    age === null || age > 30 ? "offline" : age + "s ago";
  el.textContent = r.channel
    ? "watching: " + r.channel + " (" + ageTxt + ")"
    : "waiting for: " + (r.desired || "any open channel") +
      " (" + ageTxt + ")";
  el.style.color =
    age !== null && age <= 30 ? "var(--green)" : "var(--yellow)";
}

function renderSummary(data) {
  const modeEl = document.getElementById("mode");
  modeEl.textContent = data.mode;
  modeEl.className = "badge " + data.mode;
  const paperBadge = document.getElementById("paper-badge");
  paperBadge.style.display = data.paper ? "" : "none";
  if (data.stops) {
    const s = data.stops;
    const streak = s.consecutive_losses + "/" + s.max_consecutive_losses;
    document.getElementById("stops").textContent =
      "stop-loss " + s.stop_loss_pct + "% · loss streak " + streak +
      (s.trailing_stop_pct > 0 ? " · trailing " + s.trailing_stop_pct + "%" : "");
  }
  if (data.reader) {
    readerInfo = Object.assign({}, data.reader, {
      receivedAt: Date.now(),
    });
  }
  const el = document.getElementById("accounts");
  el.innerHTML = "";
  let cardIdx = 0;
  for (const a of data.accounts) {
    const cur = cardCurrency[a.label] || "cad";
    const hidden = !!cardHidden[a.label];
    const pct = Math.min(100, Math.round((a.open_risk_pct || 0)));
    const color = pct >= (a.max_open_risk_pct || 30) ? "#f85149" : pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950";
    const usd = a.usd_value && a.value;
    const showUsd = cur === "usd" && usd;
    const fx = usd ? a.usd_value / a.value : null;
    const risk = showUsd ? a.open_risk * fx : a.open_risk;
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="label">' + esc(a.label) +
      '<span style="float:right">' +
      '<button class="cur-toggle" style="padding:1px 8px;margin:0" title="flip account value currency" onclick="flipCardCurrency(\'' + esc(a.label) + '\')">' + cur.toUpperCase() + ' ⇄</button> ' +
      '<button class="cur-toggle" style="padding:1px 7px;margin:0" title="' + (hidden ? "show account value" : "hide account value") + '" onclick="toggleCardHidden(\'' + esc(a.label) + '\')">' + (hidden ? EYE_OFF_SVG : EYE_SVG) + '</button>' +
      '</span></div>' +
      '<div class="value">' + (hidden ? "••••••" :
        (showUsd ? fmtMoney(a.usd_value) + " USD" : fmtMoney(a.value) + " CAD") +
        (a.value_age ? ' <span style="font-size:12px;color:#d29922">(cached ' + a.value_age + ')</span>' : '') +
        (showUsd
          ? ' <span style="font-size:13px;color:var(--muted)">' + fmtMoney(a.value) + ' CAD</span>'
          : (a.usd_value
            ? ' <span style="font-size:13px;color:var(--muted)">$' + a.usd_value.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' USD</span>'
            : (cur === "usd" ? ' <span style="font-size:12px;color:var(--yellow)">fx unavailable</span>' : '')))) + '</div>' +
      '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<div class="sub"><span style="color:' + color + (pct >= (a.max_open_risk_pct || 30) ? ';font-weight:700' : '') + '">open risk ' + (hidden ? "••••••" : fmtMoney(risk) + " " + (showUsd ? "usd" : "cad")) + ' (' + (a.open_risk_pct ?? 0) + '%)</span>' +
      '<span>cap ' + (a.max_open_risk_pct) + '%</span></div>' +
      ((a.margin_requirement != null && !isNaN(a.margin_requirement))
        ? '<div class="sub mrow"><span class="cell"><span class="lab">total margin used</span><span class="val">' + (hidden ? "••••••" :
            fmtMoney(a.margin_used_usd || 0) + ' usd' +
            ' · ' + fmtMoney(a.margin_used_cad || 0) + ' cad') +
          '</span></span><span class="cell" style="cursor:pointer" onclick="toggleMarginBreakdown(' + cardIdx + ', \'' + esc(a.label) + '\')"><span class="lab">margin requirement</span><span class="val">' + (hidden ? "••••••" : fmtMoney(a.margin_requirement) + " cad") + ' <span id="mbda-' + cardIdx + '">' + (mbdOpen === a.label ? "▼" : "▲") + '</span></span></span></div>' +
          '<div id="mbd-' + cardIdx + '" class="sub" style="display:' + (mbdOpen === a.label ? "block" : "none") + ';color:var(--muted);font-size:11px;white-space:pre-line">' + breakdownText(a.margin_breakdown, hidden) + '</div>' +
          '<div class="sub mrow"><span class="cell"><span class="lab">portfolio value</span><span class="val">' + (hidden ? "••••••" : (a.portfolio_value != null ? fmtMoney(a.portfolio_value) + " cad" : '')) +
          '</span></span><span class="cell"><span class="lab">max buying power</span><span class="val">' + (hidden ? "••••••" : fmtMoney(a.max_buying_power || 0) + " cad") + '</span></span></div>'
        : '') +
      ((a.cash_cad != null || a.cash_usd != null)
        ? '<div class="sub"><span>cash ' + (hidden ? "••••••" :
            (a.cash_cad != null ? fmtMoney(Math.max(0, a.cash_cad)) + " cad" : "—") +
            (a.cash_usd != null
              ? ' · ' + fmtMoney(Math.max(0, a.cash_usd)) + " usd"
              : '')) + '</span>' +
          '<span>available</span></div>'
        : '') + allocBar(a) +
      ((a.margin_available != null && !isNaN(a.margin_available))
        ? '<div class="sub"><span>margin available ' + (hidden ? "••••••" : fmtMoney(a.margin_available) + " cad") + '</span></div>'
        : '') + marginUsageBar(a);
    const wrap = document.createElement("div");
    wrap.className = "cardcol";
    wrap.appendChild(card);
    if (a.paper_value != null && !isNaN(a.paper_value)) {
      const phidden = !!paperHidden[a.label];
      const pnl = a.paper_pnl;
      const pnlPct = a.paper_initial
        ? (pnl / a.paper_initial * 100) : null;
      const pnlColor = pnl == null ? "var(--muted)"
        : pnl >= 0 ? "var(--green)" : "var(--red)";
      const pc = document.createElement("div");
      pc.className = "card papercard";
      const open = paperOpen.indexOf(a.label) >= 0;
      const rows = (paperPositions || {})[a.label] || [];
      const paperPct = Math.min(100, Math.round(a.paper_open_risk_pct || 0));
      const paperRiskColor = paperPct >= (a.max_open_risk_pct || 30) ? "#f85149" : paperPct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950";
      const paperRiskAtCap = paperPct >= (a.max_open_risk_pct || 30);
      const pcur = paperCurrency[a.label] || "cad";
      const pshowUsd = pcur === "usd" && a.paper_usd_value && a.paper_value;
      const pfx = (a.paper_usd_value && a.paper_value)
        ? a.paper_value / a.paper_usd_value : null;
      const pair = function(cad, hasUsd) {
        if (phidden) return "••••••";
        if (cad == null) return "—";
        const usd = pfx ? cad / pfx : null;
        if (!hasUsd || usd == null) return fmtMoney(cad) + " cad";
        return pshowUsd
          ? fmtMoney(usd) + " usd · " + fmtMoney(cad) + " cad"
          : fmtMoney(cad) + " cad · " + fmtMoney(usd) + " usd";
      };
      pc.innerHTML =
        '<div class="label" style="display:flex;justify-content:space-between;align-items:center;gap:6px;min-width:0">paper · <span class="ptitle">' + esc(a.label).replace(/-/g, "\u2011") + '</span>' +
        '<span style="display:flex;gap:4px;flex-shrink:0;align-items:center">' +
        '<button class="mini-toggle" title="flip paper value currency" onclick="flipPaperCurrency(\'' + esc(a.label) + '\')">' + pcur.toUpperCase() + ' ⇄</button> ' +
        '<button class="mini-toggle" onclick="togglePaper(\'' + esc(a.label) + '\')">' + "holdings " + (open ? "▼" : "▲") + '</button> ' +
        '<button class="mini-toggle" onclick="resetPaper(\'' + esc(a.label) + '\')">reset</button> ' +
        '<button class="mini-toggle" title="' + (phidden ? "show paper value" : "hide paper value") + '" onclick="togglePaperHidden(\'' + esc(a.label) + '\')">' + (phidden ? EYE_OFF_SVG : EYE_SVG) + '</button></span></div>' +
        '<div class="value" style="font-size:20px">' + (phidden? "••••••" : pshowUsd ? fmtMoney(a.paper_usd_value) + " USD" : fmtMoney(a.paper_value) + " CAD") +
        (!phidden && !pshowUsd && a.paper_usd_value ? ' <span style="font-size:12px;color:var(--muted)">$' + a.paper_usd_value.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' USD</span>' : '') +
        (!phidden && pshowUsd ? ' <span style="font-size:12px;color:var(--muted)">' + fmtMoney(a.paper_value) + ' CAD</span>' : '') +
        (pcur === "usd" && !pshowUsd && !phidden ? ' <span style="font-size:12px;color:var(--yellow)">fx unavailable</span>' : '') +
        '</div>' +
        (pnl == null ? '' :
          '<div class="sub"><span style="color:' + pnlColor + '">return ' + (phidden? "••••••" : (pnl >= 0 ? "+" : "") + fmtMoney(pnl)) +
          (pnlPct != null ? ' (' + (pnl >= 0 ? "+" : "") + pnlPct.toFixed(1) + '%)' : '') + '</span></div>') +
        '<div class="riskbar"><div style="width:' + Math.min(100, Math.round(a.paper_open_risk_pct || 0)) + '%;background:' + (a.paper_open_risk_pct >= (a.max_open_risk_pct || 30) ? "#f85149" : a.paper_open_risk_pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950") + '"></div></div>' +
        '<div class="sub"><span style="color:' + paperRiskColor + (paperRiskAtCap ? ';font-weight:700' : '') + '">open risk ' + (phidden? "••••••" : fmtMoney(a.paper_open_risk || 0) + " cad") + ' (' + (a.paper_open_risk_pct ?? 0) + '%)</span>' +
        '<span>cap ' + (a.max_open_risk_pct ?? 30) + '%</span></div>' +
        ((a.paper_margin_requirement != null && !isNaN(a.paper_margin_requirement))
          ? '<div class="sub mrow"><span class="cell"><span class="lab">total margin used</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_margin_used || 0) + " cad") +
            (phidden || !(a.paper_margin_used > 0) ? '' : ' · ledger loan') +
            '</span></span><span class="cell" style="cursor:pointer" onclick="togglePaperBreakdown(' + cardIdx + ', \'' + esc(a.label) + '\')"><span class="lab">margin requirement</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_margin_requirement) + " cad") + ' <span id="pmbda-' + cardIdx + '">' + (pmbdOpen === a.label ? "▼" : "▲") + '</span></span></span></div>' +
          '<div id="pmbd-' + cardIdx + '" class="sub" style="display:' + (pmbdOpen === a.label ? "block" : "none") + ';color:var(--muted);font-size:11px;white-space:pre-line">' + ((a.paper_margin_breakdown || []).length ? breakdownText(a.paper_margin_breakdown, phidden) : "no holdings") + '</div>' +
          '<div class="sub mrow"><span class="cell"><span class="lab">portfolio value</span><span class="val">' + (phidden? "••••••" : (a.paper_portfolio_value != null ? fmtMoney(a.paper_portfolio_value) + " cad" : '')) +
            '</span></span><span class="cell"><span class="lab">max buying power</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_max_buying_power || 0) + " cad") + '</span></span></div>'
          : '') +
        ((a.paper_cash != null)
          ? '<div class="sub"><span>cash ' + pair(Math.max(0, a.paper_cash), a.paper_cash != null) + '</span>' +
            '<span>available</span></div>'
          : '') +
        '<div class="sub"><span>seeded ' + pair(a.paper_initial, a.paper_initial != null) + '</span>' +
        '<span>' + rows.length + ' positions</span></div>' +
        paperAllocBar(a) +
        ((a.paper_margin_available != null && !isNaN(a.paper_margin_available))
          ? '<div class="sub"><span>margin available ' + (phidden? "••••••" : fmtMoney(a.paper_margin_available) + " cad") + '</span></div>'
          : '') + paperMarginUsageBar(a) +
        (open ? (rows.length ?
          '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table class="pos" style="margin-top:10px;font-size:12px"><tr><th>Holding</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Value</th><th class=num>Return</th></tr>' +
          rows.map(function(r) {
            const rc = r.pnl == null ? "var(--muted)" : r.pnl >= 0 ? "var(--green)" : "var(--red)";
            return '<tr><td>' + esc(r.contract_key) + '</td>' +
              '<td class=num>' + r.qty + '</td>' +
              '<td class=num>' + (r.avg != null ? "$" + r.avg : "—") + '</td>' +
              '<td class=num>' + (phidden? "••••••" : fmtMoney(r.value)) + '</td>' +
              '<td class=num style="color:' + rc + '">' + (r.pnl == null ? "—" : (r.pnl >= 0 ? "+" : "") + r.pnl + "%") + '</td></tr>';
          }).join("") + '</table></div>' : '<div class="empty" style="font-size:12px;padding:8px">no positions</div>') : '');
      wrap.appendChild(pc);
    }
    el.appendChild(wrap);
    cardIdx += 1;
  }
}

function paperAllocBar(a) {
  if (a.paper_value == null) return "";
  const base = (a.paper_alloc_base != null && !isNaN(a.paper_alloc_base))
    ? a.paper_alloc_base : a.paper_value;
  const sp = Math.min(100, (a.paper_stock_value || 0) / base * 100);
  const op = Math.min(100, (a.paper_option_value || 0) / base * 100);
  return '<div class="riskbar"><div style="width:' + sp + '%;background:#4493f8"></div>' +
    '<div style="width:' + op + '%;background:#ab7df6"></div></div>' +
    '<div class="sub"><span style="color:#4493f8">stocks ' + ((a.paper_stock_value || 0) / base * 100).toFixed(1) + '%</span>' +
    '<span style="color:#ab7df6">options ' + ((a.paper_option_value || 0) / base * 100).toFixed(1) + '%</span></div>';
}

function paperMarginUsageBar(a) {
  if (a.paper_margin_available == null || isNaN(a.paper_margin_available)) return "";
  const used = a.paper_margin_used || 0;
  const total = used + a.paper_margin_available;
  const pct = total > 0 ? Math.min(100, used / total * 100) : 0;
  const color = pct >= 80 ? "#f85149" : pct >= 50 ? "#d29922" : "#3fb950";
  return '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
    '<div class="sub"><span style="color:' + color + '">margin used ' + pct.toFixed(1) + '%</span><span>utilization</span></div>';
}

function marginUsageBar(a) {
  if (a.margin_available == null || isNaN(a.margin_available)) return "";
  const used = a.margin_used || 0;
  const total = used + a.margin_available;
  const pct = total > 0 ? Math.min(100, used / total * 100) : 0;
  const color = pct >= 80 ? "#f85149" : pct >= 50 ? "#d29922" : "#3fb950";
  return '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
    '<div class="sub"><span style="color:' + color + '">margin used ' + pct.toFixed(1) + '%</span><span>utilization</span></div>';
}

function stratTag(s) {
  s = (s || "").toLowerCase();
  if (s.includes("iron condor")) return s.includes("broken") ? "BW condor" : "condor";
  if (s.includes("iron butterfly")) return "iron fly";
  if (s.includes("butterfly")) return s.includes("broken") ? "BWB" : "fly";
  if (s.includes("ratio")) return "ratio";
  return "spread";
}

function allocBar(a) {
  if (!(a.stock_value || a.option_value) || !a.value) return "";
  // base on gross assets (holdings + positive cash) so borrowed
  // funds never push the percentages past 100
  const base = (a.alloc_base != null && !isNaN(a.alloc_base))
    ? a.alloc_base : a.value;
  const sp = Math.min(100, a.stock_value / base * 100);
  const op = Math.min(100, a.option_value / base * 100);
  return '<div class="riskbar"><div style="width:' + sp + '%;background:#4493f8"></div>' +
    '<div style="width:' + op + '%;background:#ab7df6"></div></div>' +
    '<div class="sub"><span style="color:#4493f8">stocks ' + (a.stock_value ? (a.stock_value / base * 100).toFixed(1) : 0) + '%</span>' +
    '<span style="color:#ab7df6">options ' + (a.option_value ? (a.option_value / base * 100).toFixed(1) : 0) + '%</span></div>';
}

function renderPositions(rows) {
  const stocksEl = document.getElementById("stock-positions");
  stocksEl.style.display = showStocks ? "" : "none";
  document.getElementById("toggle-stocks").textContent = showStocks ? "Hide holdings" : "Show holdings";
  renderPositionsInto("positions", rows.filter(r => r.kind !== "stock"), "no open positions");
  if (showStocks) {
    renderPositionsInto("stock-positions", rows.filter(r => r.kind === "stock"), "no stock holdings");
  }
}

function renderPositionsInto(elId, rows, emptyText) {
  const el = document.getElementById(elId);
  if (!rows.length) { el.innerHTML = '<div class="empty">' + emptyText + '</div>'; return; }
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
      '<td class=num>' + (p.short ? "-" + p.qty : p.qty) +
      ((p.short || p.spread)
        ? '<div class="cellbadges">' +
          (p.short ? '<span class="tag skip mini" title="short position">short</span>' : "") +
          (p.spread ? '<span class="tag ignored mini" title="' + esc(p.strategy_type || "multi-leg spread") + '">' + stratTag(p.strategy_type) + '</span>' : "") +
          "</div>"
        : "") + "</td>" +
      '<td class=num>' + (p.avg_premium == null
        ? "—"
        : (isStock
          ? "$" + p.avg_premium.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur
          : (p.spread
            ? "$" + Math.abs(p.avg_premium * 100).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
            : "$" + p.avg_premium.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })))) + "</td>" +
      '<td class=num>' + (isStock
        ? (p.current_price != null
            ? "$" + p.current_price.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur
            : "—")
        : (p.current_price != null
        ? (p.spread ? fmtSigned(p.current_price) : "$" + p.current_price) + (mv != null
          ? '<span class="subv">(' + (p.short || (p.spread && mv < 0) ? "-$" : "$") +
            Math.abs(mv).toLocaleString("en-CA", { maximumFractionDigits: 2 }) + ")</span>"
          : "")
        : "—")) + "</td>" +
      '<td class=num style="color:' + retColor + '">' + retMain + plSpan + "</td>" +
      '<td class=num>' + (isStock
        ? (p.cost_usd != null ? "$" + p.cost_usd.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur : "—") +
          (p.currency === "CAD" || p.cost_cad == null
            ? ""
            : '<span class="subv">(' + fmtMoney(p.cost_cad) + ')</span>')
        : (p.cost_usd != null ? (p.spread ? fmtSigned(p.cost_usd) : (p.short ? "-" : "") + fmtMoney(p.cost_usd)) + (p.cost_cad != null ? '<span class="subv">(' + (p.spread ? fmtSigned(p.cost_cad) : fmtMoney(p.cost_cad)) + ')</span>' : "") : '<span class="subv">(' + fmtMoney(avgTotal) + ")</span>")) + "</td></tr>";
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

let cardCurrency = JSON.parse(localStorage.getItem("ws_card_currency") || "{}");
let mbdOpen = null;

function toggleMarginBreakdown(i, label) {
  mbdOpen = mbdOpen === label ? null : label;
  const el = document.getElementById("mbd-" + i);
  if (el) el.style.display = mbdOpen === label ? "block" : "none";
  const arrow = document.getElementById("mbda-" + i);
  if (arrow) arrow.textContent = mbdOpen === label ? "\u25BC" : "\u25B2";
}
let cardHidden = JSON.parse(localStorage.getItem("ws_card_hidden") || "{}");
let paperHidden = JSON.parse(localStorage.getItem("ws_paper_hidden") || "{}");
let paperCurrency = JSON.parse(localStorage.getItem("ws_paper_currency") || "{}");

const EYE_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

function flipCardCurrency(label) {
  cardCurrency[label] = (cardCurrency[label] || "cad") === "cad" ? "usd" : "cad";
  localStorage.setItem("ws_card_currency", JSON.stringify(cardCurrency));
  load();
}

function flipPaperCurrency(label) {
  paperCurrency[label] = (paperCurrency[label] || "cad") === "cad"
    ? "usd" : "cad";
  localStorage.setItem("ws_paper_currency",
    JSON.stringify(paperCurrency));
  load();
}

function togglePaperHidden(label) {
  paperHidden[label] = !paperHidden[label];
  localStorage.setItem("ws_paper_hidden", JSON.stringify(paperHidden));
  load();
}

function toggleCardHidden(label) {
  cardHidden[label] = !cardHidden[label];
  localStorage.setItem("ws_card_hidden", JSON.stringify(cardHidden));
  load();
}

let showStocks = localStorage.getItem("ws_show_stocks") === "1";
let showAlerts = localStorage.getItem("ws_alerts_open") !== "0";
let showTrades = localStorage.getItem("ws_trades_open") !== "0";

function applySectionVisibility() {
  document.getElementById("toggle-alerts").textContent = showAlerts ? "Hide" : "Show";
  document.getElementById("signals").style.display = showAlerts ? "" : "none";
  document.getElementById("toggle-trades").textContent = showTrades ? "Hide" : "Show";
  document.getElementById("trades").style.display = showTrades ? "" : "none";
}

function toggleAlerts() {
  showAlerts = !showAlerts;
  localStorage.setItem("ws_alerts_open", showAlerts ? "1" : "0");
  applySectionVisibility();
}

function toggleTrades() {
  showTrades = !showTrades;
  localStorage.setItem("ws_trades_open", showTrades ? "1" : "0");
  applySectionVisibility();
}

function toggleStocks() {
  showStocks = !showStocks;
  localStorage.setItem("ws_show_stocks", showStocks ? "1" : "0");
  document.getElementById("toggle-stocks").textContent = showStocks ? "Hide holdings" : "Show holdings";
  document.getElementById("stock-positions").style.display = showStocks ? "" : "none";
  load();
}

function toggleIgnored() {
  showIgnored = !showIgnored;
  document.getElementById("toggle-ignored").textContent = showIgnored ? "Hide ignored" : "Show ignored";
  load();
}

function renderSignals(rows) {
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

function fitText(el, floor) {
  // shrink the font until the text fits its box; wrap as a last
  // resort instead of clipping
  if (!el) return;
  el.style.fontSize = "";
  el.style.whiteSpace = "";
  el.style.wordBreak = "";
  let size = parseFloat(getComputedStyle(el).fontSize) || 12;
  const min = floor || 9;
  while (size > min && el.scrollWidth > el.clientWidth) {
    size -= 0.5;
    el.style.fontSize = size + "px";
  }
  if (el.scrollWidth > el.clientWidth) {
    el.style.whiteSpace = "normal";
    el.style.wordBreak = "break-word";
  }
}

function renderGitStatus(s) {
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
  // liveness: a live updater never lets last_check go much
  // past its interval - a stuck thread shows here in red
  let stuck = false;
  if (s.status === "active" && s.last_check) {
    const since = Date.now() / 1000 - s.last_check;
    if (since > (s.interval_seconds || 600) * 3) {
      stuck = true;
      text += " · UPDATER STUCK";
    }
  }
  el.textContent = text;
  el.style.color = stuck || (s.result || "").indexOf("error") >= 0 || (s.result || "").indexOf("failed") >= 0 ? "var(--red)" : "var(--muted)";
  fitText(el, 9);
}
window.addEventListener("resize", function() {
  fitText(document.getElementById("git"), 9);
});

function renderTrades(rows) {
  const el = document.getElementById("trades");
  if (!rows.length) { el.innerHTML = '<div class="empty">no trades yet</div>'; return; }
  let html = "<table class=\"tlog\"><tr><th>Time</th><th>Mode</th><th>Action</th><th class=num>Qty</th><th>Ticker</th><th class=num>Price</th><th>Status</th><th>Detail</th></tr>";
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

let historyOffset = 0;

function historyQuery(offset) {
  const p = new URLSearchParams();
  p.set("kind", document.getElementById("hs-kind").value);
  for (const [id, k] of [["hs-q","q"],["hs-ticker","ticker"],["hs-status","status"],["hs-since","since"],["hs-until","until"]]) {
    const v = document.getElementById(id).value.trim();
    if (v) p.set(k, v);
  }
  p.set("limit", "50");
  p.set("offset", String(offset));
  return p.toString();
}

async function runHistorySearch() {
  historyOffset = 0;
  await fetchHistoryPage();
}

async function historyNav(dir) {
  historyOffset = Math.max(0, historyOffset + dir * 50);
  await fetchHistoryPage();
}

async function fetchHistoryPage() {
  const res = await fetch("/api/history?" + historyQuery(historyOffset));
  if (!res.ok) return;
  renderHistoryResults(await res.json());
}

function renderHistoryResults(data) {
  const el = document.getElementById("history-results");
  const kind = data.kind;
  let html = '<div class="meta">' + data.total + " result" + (data.total === 1 ? "" : "s") +
    ' <span class="nav-btns">' +
    (data.offset > 0 ? '<button onclick="historyNav(-1)">&#8592; newer</button> ' : "") +
    (data.offset + data.rows.length < data.total ? '<button onclick="historyNav(1)">older &#8594;</button>' : "") +
    "</span></div>";
  if (!data.rows.length) {
    el.innerHTML = html + '<div class="empty">no matches</div>';
    return;
  }
  if (kind === "signals") {
    html += "<table><tr><th>Time</th><th>Author</th><th>Message</th><th>Status</th><th>Channel</th></tr>";
    for (const s of data.rows) {
      const tag = s.parsed ? '<span class="tag buy">signal</span>' : (s.correction ? '<span class="tag skip">correction</span>' : '<span class="tag ignored">ignored</span>');
      html += "<tr><td>" + fmtTime(s.ts) + "</td><td>" + esc(s.author || "") + "</td><td>" + esc(s.text || "") + "</td><td>" + tag + "</td><td>" + esc(s.channel || "") + "</td></tr>";
    }
    el.innerHTML = html + "</table>";
  } else {
    html += "<table class=\"tlog\"><tr><th>Time</th><th>Mode</th><th>Action</th><th class=num>Qty</th><th>Ticker</th><th class=num>Price</th><th>Status</th><th>Detail</th></tr>";
    for (const t of data.rows) {
      const actionTag = t.action === "BUY" ? "buy" : "sell";
      let statusTag = "ignored";
      if (t.status === "executed") statusTag = "ok";
      else if (t.status === "skipped") statusTag = "skip";
      else if (t.status === "error") statusTag = "error";
      else if (t.status === "notified") statusTag = "info";
      html += "<tr><td>" + fmtTime(t.ts) + "</td><td>" + esc(t.mode) + "</td>" +
        '<td><span class="tag ' + actionTag + '">' + esc(t.action) + "</span></td>" +
        '<td class=num>' + t.qty + "</td><td>" + esc(t.ticker) + "</td>" +
        '<td class=num>' + (t.price ?? "\u2014") + "</td>" +
        '<td><span class="tag ' + statusTag + '">' + esc(t.status) + "</span></td>" +
        '<td class="detail">' + esc(t.detail || "") + "</td></tr>";
    }
    el.innerHTML = html + "</table>";
  }
}

function clearHistorySearch() {
  for (const id of ["hs-q","hs-ticker","hs-status","hs-since","hs-until"]) document.getElementById(id).value = "";
  document.getElementById("history-results").innerHTML = "";
  historyOffset = 0;
}

function historyKindChanged() {
  const signals = document.getElementById("hs-kind").value === "signals";
  document.getElementById("hs-status").disabled = signals;
  document.getElementById("hs-ticker").placeholder = signals ? "ticker (text match)" : "ticker";
}

let lastSettings = null;

let settingsDirty = false;

function setSettingsDirty(v) {
  settingsDirty = v;
  document.getElementById("settings-float").style.display = v ? "flex" : "none";
}

function revertSettings() {
  setSettingsDirty(false);
  load();
}

document.getElementById("settings-save").onclick = saveSettings;
document.getElementById("settings-revert").onclick = revertSettings;

function renderSettings(s) {
  lastSettings = s;
  // leave the form alone while the user has unsaved edits - the
  // periodic refresh used to wipe them mid-typing
  if (settingsDirty) return;
  const el = document.getElementById("settings");
  const t = s.trading;
  const quick = {
    risk_per_trade_pct: "default risk %",
    max_contracts_per_trade: "max contracts",
    max_open_risk_pct: "open risk cap %",
    stop_loss_pct: "stop loss %",
    trailing_stop_pct: "trailing stop %",
  };
  let html = '<div style="border:1px solid var(--border);background:#161b22;border-radius:8px;padding:14px;margin-bottom:14px">' +
    '<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-bottom:12px">' +
    '<span style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.8px">quick controls</span>' +
    '<label style="color:var(--text);font-size:13px;font-weight:600"><input id="set-notify" type="checkbox"' + ((s.discord || {}).notify !== false ? " checked" : "") + '> notify</label>' +
    '<label style="color:var(--text);font-size:13px;font-weight:600"><input id="set-paper-enabled" type="checkbox"' + (s.paper && s.paper.enabled ? " checked" : "") + '> paper trading</label>' +
    '<label style="color:var(--text);font-size:13px;font-weight:600"><input id="set-paper-mirror" type="checkbox"' + (s.paper && s.paper.mirror ? " checked" : "") + '> mirror real fills</label></div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">' +
    Object.entries(quick).map(([k, label]) =>
      '<div><label style="color:var(--text);font-size:11px;text-transform:uppercase">' + label + '</label>' +
      '<input id="set-' + k + '" type="number" step="any" value="' + t[k] + '" style="width:100%;background:#0d1117;color:var(--text);border:1px solid #30363d;border-radius:6px;padding:8px;font-size:15px;font-weight:600"></div>'
    ).join("") + '</div></div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">';
  const labels = {
    stop_check_seconds: "stop check (s)",
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
    discord: { notify: document.getElementById("set-notify").checked },
    paper: { enabled: document.getElementById("set-paper-enabled").checked, mirror: document.getElementById("set-paper-mirror").checked },
  };
  const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await res.json();
  if (res.status !== 200) {
    alert("save failed:\n" + (data.errors || []).join("\n"));
  } else {
    document.getElementById("settings-save").textContent = "Saved";
    setTimeout(() => document.getElementById("settings-save").textContent = "Save", 1500);
    setSettingsDirty(false);
    load();
  }
}

async function load() {
  try {
    const data = await api("/api/dashboard");
    paperPositions = data.paper_positions || {};
    renderSummary(data.summary);
    renderPositions(data.positions || []);
    renderSignals(data.signals || []);
    renderTrades(data.trades || []);
    renderSettings(data.settings || {});
    renderGitStatus(data.update_status);
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

applySectionVisibility();
tickClock();
setInterval(tickClock, 1000);
renderReader();
setInterval(renderReader, 1000);
