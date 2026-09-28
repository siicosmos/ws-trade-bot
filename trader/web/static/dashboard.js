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
let me = null;          // {username, role} or null
function isAdmin() {
  // null (pre-first-poll) assumes the owner's browser - the
  // server still enforces every write
  return !me || me.role === "admin";
}

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

async function resizePaper(label) {
  openModal(
    "Resize paper stock trades",
    "Re-size " + label + "'s past stock trades to the tier "
      + "sizing (the original alert's size keyword applies; "
      + "unsized alerts use medium)?",
    "resize",
    async function() { await doPaperResize(label); }
  );
}

async function doPaperResize(label) {
  try {
    const res = await fetch("/api/paper-resize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (res.status === 200 && (data.adjusted || []).length) {
      openModal(
        "Resized",
        data.adjusted.join("\n"),
        "ok",
        async function() { closeModal(); }
      );
    }
  } catch (e) { /* surfaced by the next refresh */ }
  paperPositions = null;
  load();
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
  if (!el) return;
  if (!readerInfo) {
    // same pre-first-load treatment as "data refreshed ..."
    el.textContent = "reader \u2026";
    el.style.color = "var(--muted)";
    return;
  }
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
  if (!data || data.error) {
    document.getElementById("accounts").innerHTML =
      '<div class="empty">' + esc((data && data.error) || "summary unavailable") + "</div>";
    return;
  }
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
  // preserve the horizontal scroll of the holdings tables
  // across the 5s re-render
  const scrollMap = {};
  el.querySelectorAll(".pos-scroll[data-scrollkey]").forEach(function(w) {
    if (w.scrollLeft) scrollMap[w.dataset.scrollkey] = w.scrollLeft;
  });
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
      '<div class="label" style="display:flex;justify-content:space-between;align-items:center;gap:6px;min-width:0">' +
      '<span class="cardtitle" title="' + esc(a.label) + '">' + esc(a.label) + '</span>' +
      '<span style="display:flex;gap:4px;flex-shrink:0;align-items:center">' +
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
        '<div class="label" style="display:flex;justify-content:space-between;align-items:center;gap:6px;min-width:0"><span class="cardtitle">paper · ' + esc(a.label).replace(/-/g, "\u2011") + '</span>' +
        '<span style="display:flex;gap:4px;flex-shrink:0;align-items:center">' +
        '<button class="mini-toggle" title="flip paper value currency" onclick="flipPaperCurrency(\'' + esc(a.label) + '\')">' + pcur.toUpperCase() + ' ⇄</button> ' +
        '<button class="mini-toggle" onclick="togglePaper(\'' + esc(a.label) + '\')">' + "holdings " + (open ? "▼" : "▲") + '</button> ' +
        (isAdmin() ? '<button class="mini-toggle" onclick="resetPaper(\'' + esc(a.label) + '\')">reset</button> ' : '') +
        (isAdmin() ? '<button class="mini-toggle" title="bring past stock trades up to the tier sizing" onclick="resizePaper(\'' + esc(a.label) + '\')">resize</button> ' : '') +
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
          '<div class="pos-scroll" data-scrollkey="paper:' + esc(a.label) + '" style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table class="pos" style="margin-top:10px;font-size:12px"><tr><th>Positions &amp; Holdings</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Price $</th><th class=num>Value $</th><th class=num>Total Cost $</th><th class=num>Return</th></tr>' +
          rows.map(function(r) {
            const rc = r.pnl == null ? "var(--muted)" : r.pnl >= 0 ? "var(--green)" : "var(--red)";
            const cur = r.usd ? " usd" : "";
            const isOpt = r.kind === "option";
            // unified: $usd ($cad) for totals, per-contract
            // bracket on option prices, $% (+$$) for returns
            const cadB = (v) => (r.usd && v != null
              ? ' <span class="subv">($' + v.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + ")</span>" : "");
            const priceCell = r.price == null ? "—" :
              "$" + r.price.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) +
              (isOpt && !phidden ? ' <span class="subv">($' + (r.price * 100).toLocaleString("en-CA", { maximumFractionDigits: 0 }) + ")</span>" : "");
            return '<tr><td>' + esc(r.contract_key) + '</td>' +
              '<td class=num>' + r.qty + '</td>' +
              '<td class=num>' + (r.avg != null ? "$" + r.avg : "—") + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : priceCell) + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : fmtMoney(r.value) + cadB(r.value_cad)) + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : (r.cost != null ? fmtMoney(r.cost) + cadB(r.cost_cad) : "—")) + '</td>' +
              '<td class=num style="color:' + rc + '">' + (r.pnl == null ? "—" :
                (r.pnl >= 0 ? "+" : "") + r.pnl.toFixed(1) + "%" +
                (phidden ? "" : ' <span class="subv">(' + (r.pnl_dollars >= 0 ? "+" : "-$") +
                  Math.abs(r.pnl_dollars).toLocaleString("en-CA", { maximumFractionDigits: 2 }) + ")</span>")) + '</td></tr>';
          }).join("") + '</table></div>' : '<div class="empty" style="font-size:12px;padding:8px">no positions</div>') : '');
      wrap.appendChild(pc);
    }
    el.appendChild(wrap);
    cardIdx += 1;
  }
  el.querySelectorAll(".pos-scroll[data-scrollkey]").forEach(function(w) {
    const sl = scrollMap[w.dataset.scrollkey];
    if (sl) w.scrollLeft = sl;
  });
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

function keepScroll(el, fn) {
  // the 5s poll rebuilds the tables - keep the horizontal
  // scroll position where the user left it
  const sl = el.scrollLeft;
  fn();
  el.scrollLeft = sl;
}

function renderPositionsInto(elId, rows, emptyText) {
  const el = document.getElementById(elId);
  if (!rows.length) { el.innerHTML = '<div class="empty">' + emptyText + '</div>'; return; }
  keepScroll(el, function() {
  let html = '<table class="pos"><tr><th>Account</th><th>Contract</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Price $</th><th class=num>Value $</th><th class=num>Total Cost $</th><th class=num>Return</th></tr>';
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
        ? (p.spread ? fmtSigned(p.current_price) : "$" + p.current_price) +
          '<span class="subv">($' + Math.abs(p.current_price * 100).toLocaleString("en-CA", { maximumFractionDigits: 0 }) + ")</span>"
        : "—")) + "</td>" +
      '<td class=num>' + (function() {
        const val = (p.current_price != null && !p.spread)
          ? (p.short ? -1 : 1) * Math.abs((p.qty || 0) * p.current_price * mult)
          : (mv != null ? (p.short || (p.spread && mv < 0) ? -mv : mv) : null);
        if (val == null) return "—";
        const sign = val < 0 ? "-$" : "$";
        const fx = (p.cost_usd && p.cost_cad) ? p.cost_cad / p.cost_usd : null;
        const cad = fx ? Math.abs(val) * fx : null;
        return sign + Math.abs(val).toLocaleString("en-CA", { maximumFractionDigits: 2 }) +
          (isStock ? cur : "") +
          (cad != null && !isStock ? ' <span class="subv">($' + cad.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + ")</span>" : "");
      })() + "</td>" +
      '<td class=num>' + (isStock
        ? (p.cost_usd != null ? "$" + p.cost_usd.toLocaleString("en-CA", { maximumFractionDigits: 2 }) + cur : "—") +
          (p.currency === "CAD" || p.cost_cad == null
            ? ""
            : '<span class="subv">(' + fmtMoney(p.cost_cad) + ')</span>')
        : (p.cost_usd != null ? (p.spread ? fmtSigned(p.cost_usd) : (p.short ? "-" : "") + fmtMoney(p.cost_usd)) + (p.cost_cad != null ? '<span class="subv">(' + (p.spread ? fmtSigned(p.cost_cad) : fmtMoney(p.cost_cad)) + ')</span>' : "") : '<span class="subv">(' + fmtMoney(avgTotal) + ")</span>")) + "</td>" +
      '<td class=num style="color:' + retColor + '">' + retMain + plSpan + "</td></tr>";
  }
  el.innerHTML = html + "</table>";
  });
}

function openSettings() {
  document.getElementById("settingsBackdrop").style.display = "flex";
  document.querySelectorAll("#settings textarea").forEach(autoGrow);
}

function closeSettings() {
  document.getElementById("settingsBackdrop").style.display = "none";
}

function requestCloseSettings() {
  if (settingsDirty) {
    openModal(
      "Unsaved changes",
      "Discard unsaved settings changes?",
      "discard",
      async function() { discardAndCloseSettings(); }
    );
    return;
  }
  closeSettings();
}

function discardAndCloseSettings() {
  setSettingsDirty(false);
  load();
  closeSettings();
}

document.addEventListener("keydown", function(e) {
  if (e.key !== "Escape") return;
  const modal = document.getElementById("modalBackdrop");
  if (modal && modal.style.display === "flex") { closeModal(); return; }
  const sp = document.getElementById("settingsBackdrop");
  if (sp && sp.style.display === "flex") requestCloseSettings();
});

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

let gitStatus = null;

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
  keepScroll(el, function() {
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
  });
}

let historyOffset = 0;

function historyQuery(offset) {
  const p = new URLSearchParams();
  p.set("kind", "both");
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

let _searchDebounce = null;

function autoSearch() {
  clearTimeout(_searchDebounce);
  _searchDebounce = setTimeout(runHistorySearch, 400);
}

async function historyNav(dir) {
  historyOffset = Math.max(0, historyOffset + dir * 50);
  await fetchHistoryPage();
}

async function fetchHistoryPage() {
  try {
    const data = await api("/api/history?" + historyQuery(historyOffset));
    renderHistoryResults(data);
  } catch (e) { /* 401 redirect or network - surfaced by the banner */ }
}

function renderHistoryResults(data) {
  const el = document.getElementById("history-results");
  // the alert->trade link: trades record the message_key of
  // the signal that produced them
  const sigKeys = new Set(
    data.rows.filter(function(r) {
      return r.type === "signal" && r.message_key;
    }).map(function(r) { return r.message_key; })
  );
  const tradeKeys = new Set(
    data.rows.filter(function(r) {
      return r.type === "trade" && r.message_key;
    }).map(function(r) { return r.message_key; })
  );
  const matched = Array.from(sigKeys).filter(function(k) {
    return tradeKeys.has(k);
  }).length;
  let meta = data.total + " result" + (data.total === 1 ? "" : "s");
  if (matched) {
    meta += " \u00b7 " + matched + " alert" + (matched === 1 ? "" : "s") +
      " matched to trades";
  }
  let html = '<div class="meta">' + meta +
    ' <span class="nav-btns">' +
    (data.offset > 0 ? '<button onclick="historyNav(-1)">&#8592; newer</button> ' : "") +
    (data.offset + data.rows.length < data.total ? '<button onclick="historyNav(1)">older &#8594;</button>' : "") +
    "</span></div>";
  if (!data.rows.length) {
    el.innerHTML = html + '<div class="empty">no matches</div>';
    return;
  }
  // merged stream: alerts and the trades they produced side
  // by side, the pair highlighted and the trade indented
  html += "<table><tr><th>Time</th><th>Kind</th><th>Detail</th></tr>";
  for (const r of data.rows) {
    if (r.type === "signal") {
      const tag = r.parsed ? '<span class="tag buy">signal</span>'
        : (r.correction ? '<span class="tag skip">correction</span>'
        : '<span class="tag ignored">ignored</span>');
      const m = r.message_key && tradeKeys.has(r.message_key);
      html += "<tr" + (m ? ' class="linked"' : "") + "><td>" +
        fmtTime(r.ts) + "</td>" +
        '<td><span class="tag info">alert</span></td>' +
        "<td>" + esc(r.author || "") + ": " + esc(r.text || "") +
        " " + tag + "</td></tr>";
    } else {
      const linked = r.message_key && sigKeys.has(r.message_key);
      const actionTag = r.action === "BUY" ? "buy" : "sell";
      let statusTag = "ignored";
      if (r.status === "executed") statusTag = "ok";
      else if (r.status === "skipped") statusTag = "skip";
      else if (r.status === "error") statusTag = "error";
      else if (r.status === "notified") statusTag = "info";
      html += "<tr" + (linked ? ' class="linked"' : "") + "><td>" +
        fmtTime(r.ts) + "</td>" +
        '<td><span class="tag ' + actionTag + '">' + esc(r.action) + "</span></td>" +
        '<td>' + (linked ? "&#8627; " : "") + r.qty + " " + esc(r.ticker) +
        " @ " + (r.price ?? "\u2014") +
        ' <span class="tag ' + statusTag + '">' + esc(r.status) + "</span> " +
        '<span class="detail">' + esc(r.detail || "") + "</span></td></tr>";
    }
  }
  el.innerHTML = html + "</table>";
}

function clearHistorySearch() {
  clearTimeout(_searchDebounce);
  for (const id of ["hs-q","hs-ticker","hs-status","hs-since","hs-until"]) document.getElementById(id).value = "";
  document.getElementById("history-results").innerHTML = "";
  historyOffset = 0;
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

function _numField(id, label, value, tip, cls) {
  return '<div class="set-field"><label>' + esc(label) + '</label>' +
    '<input id="' + id + '" type="number" step="any" value="' + esc(value ?? "") + '"' +
    (tip ? ' title="' + esc(tip) + '"' : "") +
    (cls ? ' class="' + cls + '"' : "") + '></div>';
}

function _txtField(id, label, value, placeholder, tip, full) {
  return '<div class="set-field' + (full ? " full" : "") + '"><label' +
    (tip ? ' title="' + esc(tip) + '"' : "") + '>' + esc(label) + '</label>' +
    '<input id="' + id + '" type="text" value="' + esc(value ?? "") + '"' +
    (placeholder ? ' placeholder="' + esc(placeholder) + '"' : "") +
    (tip ? ' title="' + esc(tip) + '"' : "") + '></div>';
}

function _check(id, label, checked, tip) {
  return '<label' + (tip ? ' title="' + esc(tip) + '"' : "") +
    '><input id="' + id + '" type="checkbox"' + (checked ? " checked" : "") +
    '> ' + esc(label) + '</label>';
}

function _section(title, body, tip) {
  return '<div class="set-section"><div class="set-title"' +
    (tip ? ' title="' + esc(tip) + '"' : "") + '>' + esc(title) + '</div>' +
    body + '</div>';
}

function renderSettings(s) {
  lastSettings = s;
  // leave the form alone while the user has unsaved edits - the
  // periodic refresh used to wipe them mid-typing
  if (settingsDirty) return;
  const el = document.getElementById("settings");
  const t = s.trading || {};
  const rd = s.reader || {};
  const dc = s.discord || {};
  const ws = s.wealthsimple || {};
  const au = s.auto_update || {};
  const q = s.quotes || {};

  // 1. quick controls: the toggles and numbers touched most
  let html = _section("quick controls",
    '<div class="set-checks">' +
      _check("set-notify", "notify", dc.notify !== false,
        "send parsed trade alerts to the discord webhook") +
      _check("set-paper-enabled", "paper trading", s.paper && s.paper.enabled,
        "simulate executions against the paper ledger alongside notify mode") +
      _check("set-paper-mirror", "mirror real fills", s.paper && s.paper.mirror,
        "copy real wealthsimple fills into the paper ledger") +
    '</div>' +
    '<div class="set-grid">' +
      _numField("set-mirror-interval", "mirror every (s)",
        s.paper && s.paper.mirror_interval_seconds,
        "seconds between real-fill mirror scans") +
      _numField("set-risk_per_trade_pct", "default risk %", t.risk_per_trade_pct,
        "% of account value risked per trade when no size keyword is given", "big") +
      _numField("set-max_contracts_per_trade", "max contracts", t.max_contracts_per_trade,
        "hard cap on contracts per trade across all accounts", "big") +
      _numField("set-max_open_risk_pct", "open risk cap %", t.max_open_risk_pct,
        "stop opening new risk once deployed capital exceeds this % of account value", "big") +
      _numField("set-stop_loss_pct", "stop loss %", t.stop_loss_pct,
        "hard stop distance below entry; 0 disables the stop monitor", "big") +
      _numField("set-trailing_stop_pct", "trailing stop %", t.trailing_stop_pct,
        "trailing stop distance once in profit; 0 disables", "big") +
    '</div>');

  // 2. trading limits
  html += _section("trading limits", '<div class="set-grid">' +
    _numField("set-stop_check_seconds", "stop check (s)", t.stop_check_seconds,
      "how often the stop monitor polls quotes") +
    _numField("set-max_consecutive_losses", "max losses in row", t.max_consecutive_losses,
      "pause trading after this many consecutive losses; 0 = off") +
    _numField("set-min_dte_days", "min DTE", t.min_dte_days,
      "skip options expiring sooner than this many days") +
    _numField("set-max_trades_per_day", "max trades/day", t.max_trades_per_day,
      "hard cap on executed trades per calendar day") +
    _numField("set-cooldown_seconds", "cooldown (s)", t.cooldown_seconds,
      "minimum wait between consecutive trades") +
    _numField("set-dedupe_window_minutes", "dedupe (min)", t.dedupe_window_minutes,
      "window for recognizing duplicate alerts") +
    _numField("set-limit_offset_pct", "limit offset %", t.limit_offset_pct,
      "how far past the market price a limit order chases (limit order type only)") +
    _numField("set-history_retention_days", "history retention (d)", t.history_retention_days,
      "days to keep signals and trades; 0 = keep forever (takes effect after restart)") +
    '<div class="set-field"><label title="order type used for live executions">' +
    'order type</label>' +
    '<select id="set-order_type" title="order type used for live executions">' +
      '<option value="market"' + (t.order_type === "market" ? " selected" : "") + '>market</option>' +
      '<option value="limit"' + (t.order_type === "limit" ? " selected" : "") + '>limit</option>' +
    '</select></div>' +
    '</div>' +
    '<div class="set-checks" style="margin:10px 0 0">' +
      _check("set-place_stop_loss", "place stop-loss orders",
        t.place_stop_loss,
        "submit an actual stop-loss order after entry (live mode)") +
      _check("set-sell_only_if_held", "sell only if held",
        t.sell_only_if_held,
        "refuse sells when the ledger shows no open position") +
    '</div>');

  // 3. filters
  html += _section("filters", '<div class="set-grid">' +
    _txtField("set-ticker_whitelist", "ticker whitelist",
      (t.ticker_whitelist || []).join(", "),
      "e.g. SPY, SPX - empty = allow all",
      "only trade these underlyings; empty = allow all", true) +
    _txtField("set-skip_underlyings", "skip underlyings",
      (t.skip_underlyings || []).join(", "),
      "e.g. SPX - empty = none",
      "never trade these underlyings", true) +
    '</div>');

  // 4. size tiers
  let tiers = '<div class="set-grid">' +
    '<div class="set-field full" style="color:var(--muted);font-size:11px">' +
    'risk % cap / min / max contracts</div>';
  // canonical tier order: lotto, micro, tiny, small, medium,
  // large, big, full - then any custom tiers
  const tierOrder = [
    "lotto", "micro", "tiny", "small", "medium", "large",
    "big", "full",
  ];
  const tierEntries = Object.entries(t.size_tiers || {});
  tierEntries.sort(function(a, b) {
    const ia = tierOrder.indexOf(a[0]);
    const ib = tierOrder.indexOf(b[0]);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  for (const [name, tier] of tierEntries) {
    tiers += '<div class="set-field"><label' +
      ' title="alert size keywords map to these risk caps and contract bounds"' +
      '>' + esc(name) + '</label>' +
      '<input id="tier-' + esc(name) + '-risk" type="number" step="any" value="' + esc(tier.risk_pct_max) + '" title="risk % cap">' +
      '<div class="tier-row">' +
      '<input id="tier-' + esc(name) + '-min" type="number" value="' + esc(tier.contracts_min) + '" title="min contracts">' +
      '<input id="tier-' + esc(name) + '-max" type="number" value="' + esc(tier.contracts_max) + '" title="max contracts">' +
      '</div></div>';
  }
  tiers += '</div>';
  // stock tiers: one field per tier, like the option tiers -
  // percent of account value; unsized stock alerts use medium
  tiers += '<div class="set-grid" style="margin-top:10px">' +
    '<div class="set-field full" style="color:var(--muted);font-size:11px">' +
    'stock tiers - % of account value per size keyword (unsized = medium)</div>';
  const stockOrder = ["tiny", "small", "medium", "large", "full"];
  const stockCfg = t.stock_size_tiers || {};
  for (const name of stockOrder) {
    const val = stockCfg[name];
    tiers += '<div class="set-field"><label>' + esc(name) + '</label>' +
      '<input id="set-stocktier-' + esc(name) + '" type="number" step="any" value="' +
      (val == null ? "" : val) + '" placeholder="\u2014"></div>';
  }
  for (const [name, val] of Object.entries(stockCfg)) {
    if (stockOrder.indexOf(name) >= 0) continue;
    tiers += '<div class="set-field"><label>' + esc(name) + '</label>' +
      '<input id="set-stocktier-' + esc(name) + '" type="number" step="any" value="' + val + '"></div>';
  }
  tiers += '</div>';
  html += _section("size tiers", tiers);

  // 5. accounts: per-account overrides (empty = inherit global)
  if (s.accounts && s.accounts.length) {
    let accts = "";
    s.accounts.forEach((a, i) => {
      accts += '<div class="acct-card">' +
        '<div class="acct-head"><span' +
        ' title="per-account overrides; empty fields inherit the global settings"' +
        '>' + esc(a.label) + '</span>' +
        '<span>' + _check("set-acct-" + i + "-enabled", "on", a.enabled,
          "include this account in sizing and paper trading") + '</span></div>' +
        '<div class="acct-grid">' +
        _txtField("set-acct-" + i + "-id", "account id", a.account_id, "",
          "wealthsimple account id") +
        _numField("set-acct-" + i + "-max", "max contracts", a.max_contracts_per_trade,
          "per-account contract cap; empty = inherit global") +
        _numField("set-acct-" + i + "-risk", "risk %", a.risk_per_trade_pct,
          "per-account risk override; empty = inherit global") +
        _numField("set-acct-" + i + "-paper", "paper value $", a.paper_value,
          "fallback paper equity when live values are unavailable") +
        '</div></div>';
    });
    html += _section("accounts", accts);
  }

  // 6. reader
  html += _section("reader", '<div class="set-grid">' +
    _txtField("set-reader-channel_marker", "channel marker",
      rd.channel_marker, "e.g. player-alerts",
      "only read messages from the discord channel containing this text; empty = whatever channel is open") +
    _numField("set-reader-poll_interval", "poll interval (s)", rd.poll_interval,
      "how often the reader scans the discord window") +
    _numField("set-reader-max_items", "max messages kept", rd.max_items,
      "how many recent messages the reader scans each poll") +
    '<div class="set-field"><label title="server=channel pairs: the reader selects the server first, then clicks the channel">' +
    'server \u2192 channel pairs (comma-separated)</label>' +
    '<input id="set-reader-channel_servers" type="text" value="' + esc(
      Object.entries(rd.channel_servers || {})
        .map(([c, s]) => s + "=" + c).join(", ")
    ) + '" placeholder="SPX Plays=player-alerts"></div>' +
    _numField("set-reader-discord_reopen_seconds", "discord reopen (s)",
      rd.discord_reopen_seconds,
      "seconds between attempts to start discord when no window shows") +
    _numField("set-reader-discord_restart_seconds", "restart stuck discord (s)",
      rd.discord_restart_seconds,
      "kill and restart discord when it runs this long without a window (takes effect after reader restart)") +
    _txtField("set-reader-channels", "allowed channels (comma-separated)",
      (rd.channels || []).join(", "), "e.g. test-alerts, player-alerts",
      "only these discord channels are read; empty = any", true) +
    '</div>' +
    '<div class="set-checks" style="margin:10px 0 0">' +
      _check("set-reader-auto_scroll", "auto scroll to newest message", rd.auto_scroll,
        "keep the discord window scrolled to the newest message") +
      _check("set-reader-auto_switch", "auto-switch to the first allowed channel",
        rd.auto_switch !== false,
        "click into the wanted channel when discord opens elsewhere") +
    '</div>');

  // 7. automation: aligned grid like the other sections
  html += _section("automation",
    '<div class="set-checks" style="margin-bottom:10px">' +
      _check("set-au-enabled", "auto-update", au.enabled,
        "pull and apply code updates from github automatically") +
      _check("set-quotes-enabled", "live option quotes (stop monitor)", q.enabled,
        "fetch live option quotes for the stop monitor") +
    '</div>' +
    '<div class="set-grid">' +
      _numField("set-au-interval", "update check (s)", au.interval_seconds,
        "seconds between github update checks") +
      _numField("set-ws-positions", "positions refresh (s)", ws.positions_refresh_seconds,
        "seconds between wealthsimple position refreshes") +
      _numField("set-ws-values", "values refresh (s)", ws.values_refresh_seconds,
        "seconds between wealthsimple account value refreshes") +
      _numField("set-ws-margin-rate", "stock margin rate", ws.stock_margin_rate,
        "maintenance margin rate applied to stock holdings (0.30 = 30%)") +
      '<div class="set-field"><label title="quote source for the stop monitor (takes effect after restart)">' +
      'quotes provider</label>' +
      '<select id="set-quotes-provider" title="quote source for the stop monitor - takes effect after restart: ws = wealthsimple, moomoo = OpenD feed">' +
        '<option value="ws"' + (q.provider === "ws" ? " selected" : "") + '>ws</option>' +
        '<option value="moomoo"' + (q.provider === "moomoo" ? " selected" : "") + '>moomoo</option>' +
      '</select></div>' +
      _txtField("set-quotes-moomoo_host", "moomoo host", q.moomoo_host, "127.0.0.1",
        "OpenD gateway address for moomoo quotes") +
      _numField("set-quotes-moomoo_port", "moomoo port", q.moomoo_port,
        "OpenD gateway port for moomoo quotes") +
    '</div>');

  // 8. discord webhooks
  const hooks = [
    ["set-discord-webhook_url", "trade alerts",
      "webhook for parsed alerts and execution results",
      "main alerts channel", dc.webhook_url || ""],
    ["set-discord-reader_log_webhook_url", "reader log",
      "periodic reader heartbeat; empty = off",
      "empty = off", dc.reader_log_webhook_url || ""],
    ["set-discord-pipeline_log_webhook_url", "pipeline log",
      "pipeline log tail; empty = off",
      "empty = off", dc.pipeline_log_webhook_url || ""],
    ["set-discord-update_webhook_url", "update notices",
      "restart and update notices; empty = the trade alerts channel",
      "empty = trade alerts channel", dc.update_webhook_url || ""],
    ["set-discord-raw_alert_webhook_url", "raw alerts",
      "copy-paste feed: every alert the reader delivers, as raw text",
      "empty = off", dc.raw_alert_webhook_url || ""],
  ];
  html += _section("discord webhooks (take effect after restart)",
    '<div class="set-grid wide">' +
    hooks.map(h =>
      '<div class="set-field full"><label title="' + esc(h[2]) + '">' + esc(h[1]) + '</label>' +
      '<textarea id="' + h[0] + '" rows="2" title="' + esc(h[2]) + '" placeholder="' + esc(h[3]) + '">' + esc(h[4]) + '</textarea></div>'
    ).join("") + '</div>');

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
  for (const k of ["risk_per_trade_pct","max_contracts_per_trade","max_open_risk_pct","stop_loss_pct","trailing_stop_pct","stop_check_seconds","max_consecutive_losses","min_dte_days","max_trades_per_day","cooldown_seconds","dedupe_window_minutes","limit_offset_pct","history_retention_days"]) {
    trading[k] = num("set-" + k);
  }
  trading.order_type = val("set-order_type");
  trading.place_stop_loss = document.getElementById("set-place_stop_loss").checked;
  trading.sell_only_if_held = document.getElementById("set-sell_only_if_held").checked;
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
  trading.stock_size_tiers = (function() {
    const map = {};
    document.querySelectorAll("[id^=set-stocktier-]").forEach(function(inp) {
      const name = inp.id.replace("set-stocktier-", "").toLowerCase();
      const v = parseFloat(inp.value);
      if (name && !isNaN(v)) map[name] = v;
    });
    return map;
  })();
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
      auto_scroll: document.getElementById("set-reader-auto_scroll").checked,
      auto_switch: document.getElementById("set-reader-auto_switch").checked,
      channel_servers: (function() {
        const map = {};
        val("set-reader-channel_servers").split(",").forEach(function(part) {
          const server = part.split("=")[0].trim();
          const name = (part.split("=")[1] || "").trim().toLowerCase();
          if (name && server) map[name] = server;
        });
        return map;
      })(),
      discord_reopen_seconds: parseInt(val("set-reader-discord_reopen_seconds")),
      discord_restart_seconds: parseInt(val("set-reader-discord_restart_seconds")),
    },
    auto_update: { enabled: document.getElementById("set-au-enabled").checked, interval_seconds: parseInt(val("set-au-interval")) },
    wealthsimple: {
      positions_refresh_seconds: parseInt(val("set-ws-positions")),
      values_refresh_seconds: parseInt(val("set-ws-values")),
      stock_margin_rate: num("set-ws-margin-rate"),
    },
    discord: {
      notify: document.getElementById("set-notify").checked,
      webhook_url: val("set-discord-webhook_url").trim(),
      reader_log_webhook_url: val("set-discord-reader_log_webhook_url").trim(),
      pipeline_log_webhook_url: val("set-discord-pipeline_log_webhook_url").trim(),
      update_webhook_url: val("set-discord-update_webhook_url").trim(),
      raw_alert_webhook_url: val("set-discord-raw_alert_webhook_url").trim(),
    },
    quotes: {
      enabled: document.getElementById("set-quotes-enabled").checked,
      provider: val("set-quotes-provider"),
      moomoo_host: val("set-quotes-moomoo_host").trim(),
      moomoo_port: parseInt(val("set-quotes-moomoo_port")),
    },
    paper: {
      enabled: document.getElementById("set-paper-enabled").checked,
      mirror: document.getElementById("set-paper-mirror").checked,
      mirror_interval_seconds: parseInt(val("set-mirror-interval")),
    },
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
    me = data.me || null;
    renderMe();
    paperPositions = data.paper_positions || {};
    renderSummary(data.summary);
    renderPositions(data.positions || []);
    renderSignals(data.signals || []);
    renderTrades(data.trades || []);
    renderSettings(data.settings || {});
    gitStatus = data.update_status || null;
    renderGitStatus(gitStatus);
    lastRefresh = Date.now();
  } catch (e) { /* handled in api() */ }
}

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
// history search runs itself: text inputs debounce 400ms,
// select/date changes fire at once - no button needed
for (const id of ["hs-q", "hs-ticker"]) {
  document.getElementById(id).addEventListener("input", autoSearch);
}
for (const id of ["hs-status", "hs-since", "hs-until"]) {
  document.getElementById(id).addEventListener("change", runHistorySearch);
}

function renderMe() {
  const el = document.getElementById("me");
  if (!el) return;
  if (me) {
    el.innerHTML = '<span class="tag ignored mini">' + esc(me.username) +
      '</span>';
  }
  // legacy token sessions have no user record - the users
  // button must still appear for the owner (isAdmin() covers
  // the legacy and pre-poll cases)
  const ub = document.getElementById("users-btn");
  if (ub) ub.style.display = isAdmin() ? "" : "none";
  // viewer sessions: hide the admin-only actions (server 403s
  // them anyway - this keeps the ui honest)
  const save = document.getElementById("settings-save");
  const revert = document.getElementById("settings-revert");
  if (save) save.style.display = isAdmin() ? "" : "none";
  if (revert) revert.style.display = isAdmin() ? "" : "none";
}

async function openLevels() {
  document.getElementById("levelsBackdrop").style.display = "flex";
  const saved = localStorage.getItem("spx_levels_raw") || "";
  document.getElementById("levels-input").value = saved;
  renderLevelsChart();
}

function closeLevels() {
  document.getElementById("levelsBackdrop").style.display = "none";
}

function parseLevelsText(text) {
  // pivot: "🔄 Pivot: 7704"
  // levels: "📈 Resistance: 7712 (R1), 7753 (R2), ..."
  const out = { pivot: null, levels: [] };
  const pivotM = text.match(/Pivot:\s*([\d.]+)/i);
  if (pivotM) out.pivot = parseFloat(pivotM[1]);
  const re = /([\d.]+)\s*\((R\d+|S\d+)\)/gi;
  let m;
  while ((m = re.exec(text))) {
    out.levels.push({
      label: m[2].toUpperCase(),
      price: parseFloat(m[1]),
    });
  }
  return out;
}

function parseLevels() {
  const text = document.getElementById("levels-input").value;
  const data = parseLevelsText(text);
  const err = document.getElementById("levels-error");
  if (!data.levels.length && data.pivot == null) {
    err.textContent = "no levels found - paste the daily plan with the Pivot/Resistance/Support lines";
    return;
  }
  err.textContent = "";
  localStorage.setItem("spx_levels_raw", text);
  localStorage.setItem("spx_levels", JSON.stringify(data));
  renderLevelsChart();
}

function renderLevelsChart() {
  const el = document.getElementById("levels-chart");
  let data = null;
  try { data = JSON.parse(localStorage.getItem("spx_levels") || "null"); } catch (e) {}
  if (!data || (!data.levels.length && data.pivot == null)) {
    el.innerHTML = '<div class="empty">no levels parsed yet</div>';
    return;
  }
  const rows = data.levels.slice();
  if (data.pivot != null) {
    rows.push({ label: "Pivot", price: data.pivot, pivot: true });
  }
  rows.sort(function(a, b) { return b.price - a.price; });
  const prices = rows.map(function(r) { return r.price; });
  const max = Math.max.apply(null, prices);
  const min = Math.min.apply(null, prices);
  const span = (max - min) || 1;
  let html = '<div class="levels-ladder">';
  for (const r of rows) {
    const topPct = ((max - r.price) / span * 100).toFixed(1);
    const kind = r.label.charAt(0).toLowerCase();   // r / s / pivot
    html += '<div class="levels-row" style="top:' + topPct + '%">' +
      '<span class="levels-chip ' + kind + '">' + esc(r.label) + "</span>" +
      '<span class="levels-price">' + r.price.toLocaleString("en-CA", { minimumFractionDigits: 0 }) + "</span>" +
      '<div class="levels-line ' + kind + '"></div></div>';
  }
  html += "</div>";
  el.innerHTML = html;
}

async function openUsers() {
  document.getElementById("usersBackdrop").style.display = "flex";
  await refreshUsers();
}

function closeUsers() {
  document.getElementById("usersBackdrop").style.display = "none";
}

async function refreshUsers() {
  try {
    const users = await api("/api/users");
    const el = document.getElementById("users-list");
    let html = "<table><tr><th>User</th><th>Role</th><th>Last login</th><th></th></tr>";
    for (const u of users) {
      html += "<tr><td>" + esc(u.username) + "</td>" +
        '<td><span class="role-' + esc(u.role) + '">' + esc(u.role) + "</span></td>" +
        "<td>" + (u.last_login_ts ? fmtIso(u.last_login_ts).slice(0, 16) : "never") + "</td>" +
        '<td>' + (u.username === (me && me.username) ? "" :
          '<button onclick="deleteUser(\'' + esc(u.username) + '\')">remove</button>') +
        "</td></tr>";
    }
    el.innerHTML = html + "</table>";
  } catch (e) { /* surfaced by the banner */ }
}

async function usersPost(payload) {
  const res = await fetch("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  const msg = document.getElementById("users-msg");
  if (msg) msg.textContent = res.status === 200 ? "" : (data.error || "failed");
  if (res.status === 200) await refreshUsers();
}

function createUser() {
  usersPost({
    action: "create",
    username: document.getElementById("nu-name").value.trim(),
    password: document.getElementById("nu-pass").value,
    role: document.getElementById("nu-role").value,
  });
}

function deleteUser(username) {
  openModal(
    "Remove user",
    "Remove " + username + "? They will lose access immediately.",
    "remove",
    async function() { await usersPost({ action: "delete", username: username }); }
  );
}

async function changeMyPassword() {
  const res = await fetch("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action: "set_password",
      username: me ? me.username : "",
      current_password: document.getElementById("pw-current").value,
      password: document.getElementById("pw-new").value,
    }),
  });
  const data = await res.json().catch(() => ({}));
  const msg = document.getElementById("users-msg");
  if (msg) msg.textContent = res.status === 200
    ? "password changed" : (data.error || "failed");
  if (res.status === 200) {
    document.getElementById("pw-current").value = "";
    document.getElementById("pw-new").value = "";
  }
}

renderReader();
setInterval(renderReader, 1000);
setInterval(function() {
  // the checked/pull ages tick in real time like the clock
  if (gitStatus) renderGitStatus(gitStatus);
}, 1000);
