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

function jsq(s) {
  // esc() is html-safe but NOT js-string-safe inside an inline
  // onclick attribute: the browser html-decodes the attribute
  // value before the js parser runs, so a label containing '
  // re-terminates the string (broken buttons, script injection).
  // jsq escapes the js string first, then html-escapes - the
  // decoded attribute value is a correctly quoted js literal.
  return esc(
    String(s ?? "").split("\\").join("\\\\").split("'").join("\\'")
  );
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
  const input = document.getElementById("mInput");
  input.style.display = "none";
  input.value = "";
  document.getElementById("modalBackdrop").style.display = "flex";
}

function openInputModal(title, text, field1, field2, action) {
  // field1/field2: {label, placeholder, value} or null
  openModal(title, text, "save", action);
  const input = document.getElementById("mInput");
  const label1 = document.getElementById("mLabel1");
  const input2 = document.getElementById("mInput2");
  const label2 = document.getElementById("mLabel2");
  input.style.display = "block";
  label1.style.display = "block";
  label1.textContent = field1.label || "value";
  input.placeholder = field1.placeholder || "";
  input.value = field1.value == null ? "" : String(field1.value);
  if (field2) {
    input2.style.display = "block";
    label2.style.display = "block";
    label2.textContent = field2.label || "value 2";
    input2.placeholder = field2.placeholder || "";
    input2.value = field2.value == null ? "" : String(field2.value);
  } else {
    input2.style.display = "none";
    label2.style.display = "none";
    input2.value = "";
  }
  setTimeout(function() { input.focus(); }, 50);
}

function modalInputValue(id) {
  return document.getElementById(id || "mInput").value.trim();
}

function closeModal() {
  document.getElementById("modalBackdrop").style.display = "none";
  document.getElementById("mInput").style.display = "none";
  document.getElementById("mInput2").style.display = "none";
  document.getElementById("mLabel1").style.display = "none";
  document.getElementById("mLabel2").style.display = "none";
  modalAction = null;
}

async function resizePaper(label) {
  openModal(
    "Resize paper stock trades",
    "Bring past paper stock trades up to the tier " +
      "sizing (the original alert's size keyword applies; " +
      "unsized alerts use medium)?",
    "resize",
    async function() { await doPaperResize(label); }
  );
}

function openPaperSettings(label) {
  // the paper account's settings: reset, resize, and the
  // adjust editor (cash pools + holdings)
  const acct = ((typeof lastPayload === "object" && lastPayload
    ? lastPayload.accounts : []) || []).find(
    (a) => a.label === label
  ) || {};
  const rows = (paperPositions || {})[label] || [];

  let html =
    '<div class="set-section"><div class="set-title">maintenance</div>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
    '<button type="button" class="mini-toggle" id="padj-reset">reset ledger</button>' +
    '<button type="button" class="mini-toggle" id="padj-resize" title="bring past stock trades up to the tier sizing">resize stock trades</button>' +
    '</div>' +
    '<div class="field-help">reset drops the ledger and re-seeds it from the live account; resize brings past stock trades up to tier sizing</div></div>';

  html += '<div class="set-section"><div class="set-title">adjust account</div>' +
    '<div class="acct-grid">' +
    _numField("padj-cash-cad", "cash cad",
      acct.paper_cash_cad != null ? acct.paper_cash_cad : "",
      "the cad cash pool", "") +
    _numField("padj-cash-usd", "cash usd",
      acct.paper_cash_usd != null ? acct.paper_cash_usd : "",
      "the usd cash pool (converted at the live fx)", "") +
    '</div>' +
    '<div class="set-title" style="margin-top:10px">holdings</div>' +
    '<div id="padj-holdings"></div>' +
    '<button type="button" class="mini-toggle" id="padj-add">+ add holding</button>' +
    '<div class="field-help">edit qty / avg, remove rows, or add new ones - applied on apply</div></div>' +
    '<div id="padj-msg" style="color:var(--red);font-size:12px"></div>';

  document.getElementById("paperSettingsTitle").textContent =
    "paper · " + label;
  document.getElementById("paperSettingsBody").innerHTML = html;
  document.getElementById("paperSettingsBackdrop").style.display = "flex";
  document.getElementById("paperSettingsFloat").style.display = "flex";
  const list = document.getElementById("padj-holdings");
  function holdingRow(h) {
    const isOpt = !!h.right;
    const div = document.createElement("div");
    div.className = "acct-card";
    div.innerHTML =
      '<div class="acct-head"><span style="font-weight:600">' +
      esc(h.contract_key || h.underlying || "new holding") + '</span>' +
      '<button type="button" class="mini-toggle" data-remove="1">remove</button></div>' +
      '<div class="acct-grid">' +
      '<div class="set-field"><label>type</label><label style="display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)">' +
      '<input type="checkbox" class="padj-h-isopt"' + (isOpt ? " checked" : "") + '> option</label></div>' +
      (isOpt
        ? _txtField("padj-h-underlying", "underlying", h.underlying || "", "", "", true) +
          _txtField("padj-h-expiry", "expiry (yyyy-mm-dd)", h.expiry || "", "", "", true) +
          _numField("padj-h-strike", "strike", h.strike, "", "") +
          '<div class="set-field"><label>right</label><select class="padj-h-right">' +
          '<option value="C"' + (h.right === "C" ? " selected" : "") + '>call</option>' +
          '<option value="P"' + (h.right === "P" ? " selected" : "") + '>put</option>' +
          '</select></div>'
        : _txtField("padj-h-underlying", "symbol", h.underlying || "", "", "", true)) +
      _numField("padj-h-qty", "qty", h.qty, "", "") +
      _numField("padj-h-avg", "avg price", h.avg, "", "") +
      '</div>';
    div.querySelector("[data-remove]").onclick = function() { div.remove(); };
    // the type switch rebuilds the row in place (same position,
    // handlers re-wired) keeping the typed values
    div.querySelector(".padj-h-isopt").onchange = function() {
      const get = (sfx) => {
        const el = div.querySelector("[id$='-" + sfx + "']");
        return el ? el.value : "";
      };
      const h2 = {
        underlying: (get("underlying") || "").trim().toUpperCase(),
        expiry: get("expiry") || "",
        strike: parseFloat(get("strike")) || null,
        qty: parseInt(get("qty")) || 1,
        avg: parseFloat(get("avg")) || null,
        right: this.checked ? (h.right || "C") : null,
        contract_key: this.checked ? "new option" : "new stock",
      };
      div.replaceWith(holdingRow(h2));
    };
    return div;
  }
  rows.forEach(function(h) { list.appendChild(holdingRow(h)); });

  document.getElementById("padj-add").onclick = function() {
    list.appendChild(holdingRow({
      underlying: "", expiry: "", strike: null,
      right: "C", qty: 1, avg: null,
      contract_key: "new holding",
    }));
  };

  document.getElementById("padj-reset").onclick = function() {
    closePaperSettings();
    resetPaper(label);
  };
  document.getElementById("padj-resize").onclick = function() {
    closePaperSettings();
    resizePaper(label);
  };
  document.getElementById("paperSettingsDismiss").onclick =
    closePaperSettings;
  document.getElementById("paperSettingsApply").onclick =
    async function() { await doPaperAdjust(label); };
}

function closePaperSettings() {
  document.getElementById("paperSettingsBackdrop").style.display = "none";
  document.getElementById("paperSettingsFloat").style.display = "none";
}

function _padjHoldingsRowData(row) {
  const get = (suffix) => {
    const el = row.querySelector("[id$='-" + suffix + "']");
    return el ? el.value : "";
  };
  const isOpt = row.querySelector(".padj-h-isopt")
    ? row.querySelector(".padj-h-isopt").checked
    : !!row.querySelector(".padj-h-right");
  const rightSel = row.querySelector(".padj-h-right");
  const h = {
    underlying: (get("underlying") || "").trim().toUpperCase(),
    qty: parseInt(get("qty")),
    avg: parseFloat(get("avg")) || 0,
  };
  if (isOpt) {
    h.expiry = (get("expiry") || "").trim();
    h.strike = parseFloat(get("strike"));
    h.right = rightSel ? rightSel.value : "C";
  }
  return h;
}

async function doPaperAdjust(label) {
  const msg = document.getElementById("padj-msg");
  const holdings = Array.from(
    document.querySelectorAll("#padj-holdings .acct-card")
  ).map(_padjHoldingsRowData).filter((h) => h.underlying);
  const numOrNull = (id) => {
    const el = document.getElementById(id);
    if (!el || el.value === "") return null;
    const v = parseFloat(el.value);
    return isNaN(v) ? null : v;
  };
  try {
    const res = await fetch("/api/paper-adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: label,
        cash_cad: numOrNull("padj-cash-cad"),
        cash_usd: numOrNull("padj-cash-usd"),
        holdings: holdings,
      }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (res.status === 200 && data.status === "ok") {
      closePaperSettings();
    } else {
      msg.textContent = (data.errors || ["adjust failed"]).join("\n");
      return;
    }
  } catch (e) {
    msg.textContent = "adjust failed";
    return;
  }
  try { localStorage.removeItem("dash_last_payload"); } catch (e) {}
  paperPositions = null;
  load();
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
  // the replay snapshot would re-paint the pre-reset ledger on
  // the next page open - drop it
  try { localStorage.removeItem("dash_last_payload"); } catch (e) {}
  paperPositions = null;
  load();
}

function sellPaper(label, key, qty, price) {
  openModal(
    "Manual paper sell",
    "Sell " + qty + "x " + key + " on paper " + label +
      (price != null ? " at the live price (~$" + price + ")" : "") +
      "? The proceeds return to the paper cash and the position " +
      "realizes its pnl.",
    "sell",
    async function() { await doPaperSell(label, key); }
  );
}

function setTp(mode, label, key, tp, trail) {
  openInputModal(
    "Position sell guards",
    key + " on " + mode + " " + label + " - take-profit sells " +
      "the whole rest once the gain vs entry reaches the %; " +
      "trailing sells once the bid falls that % off its peak " +
      "(overrides the global trailing for this position). " +
      "Leave both empty to remove the guards.",
    { label: "take-profit % (gain vs entry)", placeholder: "e.g. 40",
      value: tp },
    { label: "trailing stop % (off this position's peak)",
      placeholder: "e.g. 15", value: trail },
    async function() {
      await doSetTp(mode, label, key,
        modalInputValue("mInput"), modalInputValue("mInput2"));
    }
  );
}

async function doSetTp(mode, label, key, tpRaw, trailRaw) {
  try {
    const res = await fetch("/api/position-tp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: mode, label: label, contract_key: key,
        tp_gain_pct: tpRaw === "" ? null : parseFloat(tpRaw),
        trail_pct: trailRaw === "" ? null : parseFloat(trailRaw),
      }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (res.status !== 200) {
      openModal(
        "Sell guards not saved",
        data.error || "the update did not go through",
        "ok",
        async function() { closeModal(); }
      );
      return;
    }
  } catch (e) { /* surfaced by the next refresh */ }
  try { localStorage.removeItem("dash_last_payload"); } catch (e) {}
  paperPositions = null;
  load();
}

function monModeLive() {
  return lastPayload && lastPayload.summary
    && lastPayload.summary.mode === "live";
}

function monModeLabel() {
  return monModeLive() ? "live" : "paper";
}

function sellPosition(mode, label, key, qty, price, avg) {
  openModal(
    "Manual position sell" + (mode === "live" ? " (LIVE ORDER)" : ""),
    "Sell the whole " + key + " position (" + label + ")" +
      (mode === "live"
        ? " - this places a REAL sell order at the current bid"
        : " - books against the paper ledger at the live price") +
      "?",
    "sell",
    async function() { await doPositionSell(mode, label, key); }
  );
}

async function doPositionSell(mode, label, key) {
  const paper = mode !== "live";
  try {
    const res = await fetch(paper ? "/api/paper-sell" : "/api/position-sell", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: label, contract_key: key, mode: mode,
      }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (res.status === 200) {
      openModal(
        paper ? "Sold" : "Sell order placed",
        "Sold " + data.sold + "x " + key +
          (data.price != null ? " @ ~$" + data.price : "") +
          (data.realized != null
            ? " · realized " + (data.realized >= 0 ? "+$" : "-$") +
              Math.abs(data.realized).toLocaleString("en-CA",
                { maximumFractionDigits: 2 })
            : "") +
          (data.detail ? "\n" + data.detail : ""),
        "ok",
        async function() { closeModal(); }
      );
    } else {
      openModal(
        "Sell failed",
        data.error || "the sell did not go through",
        "ok",
        async function() { closeModal(); }
      );
    }
  } catch (e) { /* surfaced by the next refresh */ }
  try { localStorage.removeItem("dash_last_payload"); } catch (e) {}
  paperPositions = null;
  load();
}

async function doPaperSell(label, key) {
  try {
    const res = await fetch("/api/paper-sell", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label, contract_key: key }),
    });
    if (res.status === 401) { location.href = "/login"; return; }
    const data = await res.json().catch(() => ({}));
    if (res.status === 200) {
      openModal(
        "Sold",
        "Sold " + data.sold + "x " + key + " @ $" + data.price +
          " · realized " + (data.realized >= 0 ? "+$" : "-$") +
          Math.abs(data.realized).toLocaleString("en-CA",
            { maximumFractionDigits: 2 }),
        "ok",
        async function() { closeModal(); }
      );
    } else {
      openModal(
        "Sell failed",
        data.error || "the sell did not go through",
        "ok",
        async function() { closeModal(); }
      );
    }
  } catch (e) { /* surfaced by the next refresh */ }
  try { localStorage.removeItem("dash_last_payload"); } catch (e) {}
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
  // the mode badge already reads "paper" in paper mode - the
  // second badge is only for paper running alongside notify
  paperBadge.style.display =
    data.paper && data.mode !== "paper" ? "" : "none";
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
    // the real card's today line reads the ledger the bot trades
    // against (live ledger in live mode, paper ledger otherwise)
    // - the lotto budget gates on this same number
    const todayGain = a.realized_today != null
      ? a.realized_today : a.paper_realized_today;
    card.innerHTML =
      '<div class="label" style="display:flex;justify-content:space-between;align-items:center;gap:6px;min-width:0">' +
      '<span class="cardtitle" title="' + esc(a.label) + '">' + esc(a.label) + '</span>' +
      '<span style="display:flex;gap:4px;flex-shrink:0;align-items:center">' +
      '<button class="cur-toggle" title="flip account value currency" onclick="flipCardCurrency(\'' + jsq(a.label) + '\')">' + cur.toUpperCase() + ' ⇄</button> ' +
      '<button class="cur-toggle" title="' + (hidden ? "show account value" : "hide account value") + '" onclick="toggleCardHidden(\'' + jsq(a.label) + '\')">' + (hidden ? EYE_OFF_SVG : EYE_SVG) + '</button>' +
      '</span></div>' +
      '<div class="value">' + (hidden ? "••••••" :
        (showUsd ? fmtMoney(a.usd_value) + " USD" : fmtMoney(a.value) + " CAD") +
        (a.value_age ? ' <span style="font-size:12px;color:#d29922">(cached ' + a.value_age + ')</span>' : '') +
        (showUsd
          ? ' <span style="font-size:13px;color:var(--muted)">' + fmtMoney(a.value) + ' CAD</span>'
          : (a.usd_value
            ? ' <span style="font-size:13px;color:var(--muted)">$' + a.usd_value.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' USD</span>'
            : (cur === "usd" ? ' <span style="font-size:12px;color:var(--yellow)">fx unavailable</span>' : '')))) + '</div>' +
      (todayGain != null
        ? '<div class="sub" style="margin-bottom:4px"><span style="color:' + (todayGain >= 0 ? "var(--green)" : "var(--red)") + '">today ' + (hidden ? "\u2022\u2022\u2022\u2022\u2022\u2022" : (todayGain >= 0 ? "+" : "\u2212") + fmtMoney(Math.abs(todayGain))) + '</span><span>realized</span></div>'
        : '') +
      '<div class="riskbar"><div style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<div class="sub"><span style="color:' + color + (pct >= (a.max_open_risk_pct || 30) ? ';font-weight:700' : '') + '">open risk ' + (hidden ? "••••••" : fmtMoney(risk) + " " + (showUsd ? "usd" : "cad")) + ' (' + (a.open_risk_pct ?? 0) + '%)</span>' +
      '<span>cap ' + (a.max_open_risk_pct) + '%</span></div>' +
      ((a.cluster_cap_pct > 0 && a.open_risk_cluster > 0 && a.value &&
        a.open_risk_cluster >= a.value * a.cluster_cap_pct / 100 * 0.8)
        ? '<div class="sub"><span style="color:#d29922">largest cluster ' +
          (a.open_risk_cluster / a.value * 100).toFixed(1) + '% of account' +
          (a.open_risk_cluster >= a.value * a.cluster_cap_pct / 100 ? ' - capped' : '') + '</span>' +
          '<span>cluster cap ' + a.cluster_cap_pct + '%</span></div>'
        : '') +
      ((a.margin_requirement != null && !isNaN(a.margin_requirement))
        ? '<div class="sub mrow"><span class="cell"><span class="lab">total margin used</span><span class="val">' + (hidden ? "••••••" :
            fmtMoney(a.margin_used_usd || 0) + ' usd' +
            ' · ' + fmtMoney(a.margin_used_cad || 0) + ' cad') +
          '</span></span><span class="cell" style="cursor:pointer" onclick="toggleMarginBreakdown(' + cardIdx + ', \'' + jsq(a.label) + '\')"><span class="lab">margin requirement</span><span class="val">' + (hidden ? "••••••" : fmtMoney(a.margin_requirement) + " cad") + ' <span id="mbda-' + cardIdx + '">' + (mbdOpen === a.label ? "▼" : "▲") + '</span></span></span></div>' +
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
        '<button class="mini-toggle" title="flip paper value currency" onclick="flipPaperCurrency(\'' + jsq(a.label) + '\')">' + pcur.toUpperCase() + ' ⇄</button> ' +
        '<button class="mini-toggle" onclick="togglePaper(\'' + jsq(a.label) + '\')">' + "holdings " + (open ? "▼" : "▲") + '</button> ' +
        (isAdmin() ? '<button class="mini-toggle" title="paper account settings: reset, resize, adjust cash + holdings" onclick="openPaperSettings(\'' + jsq(a.label) + '\')">⚙</button> ' : '') +
        '<button class="mini-toggle" title="' + (phidden ? "show paper value" : "hide paper value") + '" onclick="togglePaperHidden(\'' + jsq(a.label) + '\')">' + (phidden ? EYE_OFF_SVG : EYE_SVG) + '</button></span></div>' +
        '<div class="value" style="font-size:20px">' + (phidden? "••••••" : pshowUsd ? fmtMoney(a.paper_usd_value) + " USD" : fmtMoney(a.paper_value) + " CAD") +
        (!phidden && !pshowUsd && a.paper_usd_value ? ' <span style="font-size:12px;color:var(--muted)">$' + a.paper_usd_value.toLocaleString("en-CA", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' USD</span>' : '') +
        (!phidden && pshowUsd ? ' <span style="font-size:12px;color:var(--muted)">' + fmtMoney(a.paper_value) + ' CAD</span>' : '') +
        (pcur === "usd" && !pshowUsd && !phidden ? ' <span style="font-size:12px;color:var(--yellow)">fx unavailable</span>' : '') +
        '</div>' +
        (pnl == null ? '' :
          '<div class="sub"><span style="color:' + pnlColor + '">return ' + (phidden? "••••••" : (pnl >= 0 ? "+" : "") + fmtMoney(pnl)) +
          (pnlPct != null ? ' (' + (pnl >= 0 ? "+" : "") + pnlPct.toFixed(1) + '%)' : '') + '</span></div>') +
        (a.paper_realized_today != null
          ? '<div class="sub"><span style="color:' + (a.paper_realized_today >= 0 ? "var(--green)" : "var(--red)") + '">today ' + (phidden? "••••••" : (a.paper_realized_today >= 0 ? "+" : "\u2212") + fmtMoney(Math.abs(a.paper_realized_today))) + '</span><span>realized</span></div>'
          : '') +
        '<div class="riskbar"><div style="width:' + Math.min(100, Math.round(a.paper_open_risk_pct || 0)) + '%;background:' + (a.paper_open_risk_pct >= (a.max_open_risk_pct || 30) ? "#f85149" : a.paper_open_risk_pct > (a.max_open_risk_pct || 30) * 0.6 ? "#d29922" : "#3fb950") + '"></div></div>' +
        '<div class="sub"><span style="color:' + paperRiskColor + (paperRiskAtCap ? ';font-weight:700' : '') + '">open risk ' + (phidden? "••••••" : fmtMoney(a.paper_open_risk || 0) + " cad") + ' (' + (a.paper_open_risk_pct ?? 0) + '%)</span>' +
        '<span>cap ' + (a.max_open_risk_pct ?? 30) + '%</span></div>' +
        ((a.paper_margin_requirement != null && !isNaN(a.paper_margin_requirement))
          ? '<div class="sub mrow"><span class="cell"><span class="lab">total margin used</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_margin_used || 0) + " cad") +
            (phidden || !(a.paper_margin_used > 0) ? '' : ' · ledger loan') +
            '</span></span><span class="cell" style="cursor:pointer" onclick="togglePaperBreakdown(' + cardIdx + ', \'' + jsq(a.label) + '\')"><span class="lab">margin requirement</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_margin_requirement) + " cad") + ' <span id="pmbda-' + cardIdx + '">' + (pmbdOpen === a.label ? "▼" : "▲") + '</span></span></span></div>' +
          '<div id="pmbd-' + cardIdx + '" class="sub" style="display:' + (pmbdOpen === a.label ? "block" : "none") + ';color:var(--muted);font-size:11px;white-space:pre-line">' + ((a.paper_margin_breakdown || []).length ? breakdownText(a.paper_margin_breakdown, phidden) : "no holdings") + '</div>' +
          '<div class="sub mrow"><span class="cell"><span class="lab">portfolio value</span><span class="val">' + (phidden? "••••••" : (a.paper_portfolio_value != null ? fmtMoney(a.paper_portfolio_value) + " cad" : '')) +
            '</span></span><span class="cell"><span class="lab">max buying power</span><span class="val">' + (phidden? "••••••" : fmtMoney(a.paper_max_buying_power || 0) + " cad") + '</span></span></div>'
          : '') +
        ((a.paper_cash != null || a.paper_cash_usd != null)
          ? '<div class="sub"><span>cash ' + (phidden
              ? "••••••"
              : [
                  (a.paper_cash != null
                    ? fmtMoney(Math.max(0, a.paper_cash)) + " cad" : ""),
                  (a.paper_cash_usd != null
                    ? fmtMoney(Math.max(0, a.paper_cash_usd)) + " usd" : ""),
                ].filter(Boolean).join(" · ") || "—") + '</span>' +
            '<span>available</span></div>'
          : '') +
        '<div class="sub"><span>seeded ' + pair(a.paper_initial, a.paper_initial != null) + '</span>' +
        '<span>' + rows.length + ' positions</span></div>' +
        paperAllocBar(a) +
        ((a.paper_margin_available != null && !isNaN(a.paper_margin_available))
          ? '<div class="sub"><span>margin available ' + (phidden? "••••••" : fmtMoney(a.paper_margin_available) + " cad") + '</span></div>'
          : '') + paperMarginUsageBar(a) +
        (open ? (rows.length ?
          '<div class="pos-scroll" data-scrollkey="paper:' + esc(a.label) + '" style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table class="pos" style="margin-top:10px;font-size:12px"><tr><th>Positions &amp; Holdings</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Price $</th><th class=num>Value $</th><th class=num>Total Cost $</th><th class=num>Return</th>' + (isAdmin() ? '<th></th>' : '') + '</tr>' +
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
            const guardChips =
              (r.tp_gain_pct
                ? ' <span class="tag ignored mini" title="auto-sell the rest at this gain">TP ' +
                  r.tp_gain_pct + '%</span>' : "") +
              (r.trail_pct != null
                ? ' <span class="tag ignored mini" title="' +
                  (r.trail_pct > 0
                    ? "auto-sell once the bid falls this % off its peak"
                    : "trailing disabled for this position") + '">TS ' +
                  r.trail_pct + '%</span>' : "");
            const actionCell = isAdmin() && r.qty > 0
              ? '<td class=num>' +
                (isOpt ? '<button class="mini-toggle" title="take-profit / trailing for this position" onclick="setTp(\'paper\', \'' + jsq(a.label) + '\', \'' + jsq(r.contract_key) + '\', ' + (r.tp_gain_pct == null ? "null" : r.tp_gain_pct) + ', ' + (r.trail_pct == null ? "null" : r.trail_pct) + ')">tp</button> ' : '') +
                '<button class="mini-toggle danger" title="sell at the live price" onclick="sellPaper(\'' + jsq(a.label) + '\', \'' + jsq(r.contract_key) + '\', ' + r.qty + ', ' + (r.price == null ? "null" : r.price) + ')">sell</button></td>'
              : '<td></td>';
            return '<tr><td>' + esc(r.contract_key) + '</td>' +
              '<td class=num>' + r.qty + '</td>' +
              '<td class=num>' + (r.avg != null ? "$" + r.avg : "—") + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : priceCell) + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : fmtMoney(r.value) + cadB(r.value_cad)) + '</td>' +
              '<td class=num>' + (phidden ? "••••••" : (r.cost != null ? fmtMoney(r.cost) + cadB(r.cost_cad) : "—")) + '</td>' +
              '<td class=num style="color:' + rc + '">' + (r.pnl == null ? "—" :
                (r.pnl >= 0 ? "+" : "") + r.pnl.toFixed(1) + "%" + guardChips +
                (phidden ? "" : ' <span class="subv">(' + (r.pnl_dollars >= 0 ? "+" : "-$") +
                  Math.abs(r.pnl_dollars).toLocaleString("en-CA", { maximumFractionDigits: 2 }) + ")</span>")) + '</td>' +
              (isAdmin() ? actionCell : '') + '</tr>';
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
  const monMode = lastPayload && lastPayload.summary
    && lastPayload.summary.mode === "live" ? "live" : "paper";
  renderPositionsInto("positions", rows.filter(r => r.kind !== "stock"), "no open positions", monMode);
  if (showStocks) {
    renderPositionsInto("stock-positions", rows.filter(r => r.kind === "stock"), "no stock holdings", monMode);
  }
}

function keepScroll(el, fn) {
  // the 5s poll rebuilds the tables - keep the horizontal
  // scroll position where the user left it
  const sl = el.scrollLeft;
  fn();
  el.scrollLeft = sl;
}

function renderPositionsInto(elId, rows, emptyText, monMode) {
  const el = document.getElementById(elId);
  if (!rows.length) { el.innerHTML = '<div class="empty">' + emptyText + '</div>'; return; }
  keepScroll(el, function() {
  let html = '<table class="pos"><tr><th>Account</th><th>Contract</th><th class=num>Qty</th><th class=num>Avg $</th><th class=num>Price $</th><th class=num>Value $</th><th class=num>Total Cost $</th><th class=num>Return</th>' + (isAdmin() ? '<th></th>' : '') + '</tr>';
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
      '<td class=num style="color:' + retColor + '">' +
      (p.tp_gain_pct ? '<span class="tag ignored mini" title="auto-sell the rest at this gain">TP ' + p.tp_gain_pct + '%</span> ' : "") +
      (p.trail_pct != null ? '<span class="tag ignored mini" title="' +
        (p.trail_pct > 0
          ? "auto-sell once the bid falls this % off its peak"
          : "trailing disabled for this position") + '">TS ' +
        p.trail_pct + '%</span> ' : "") +
      retMain + plSpan + "</td>" +
      // guards ride the ledger the monitor watches; ws-sourced
      // rows resolve to their ledger row by parts when needed.
      // the buttons act on REAL positions - grey them out until
      // live mode (the paper ledger's buttons live on the paper
      // account cards)
      (isAdmin() && !isStock
        ? '<td class=num>' +
          (monMode === "live"
            ? '<button class="mini-toggle" title="take-profit / trailing for this position" onclick="setTp(\'' + monMode + '\', \'' + jsq(p.account) + '\', \'' + jsq(p.contract_key) + '\', ' + (p.tp_gain_pct == null ? "null" : p.tp_gain_pct) + ', ' + (p.trail_pct == null ? "null" : p.trail_pct) + ')">tp</button> ' +
            '<button class="mini-toggle danger" title="place a REAL sell order at the current bid" onclick="sellPosition(\'' + monMode + '\', \'' + jsq(p.account) + '\', \'' + jsq(p.contract_key) + '\', ' + (p.qty || 0) + ', ' + (p.current_price == null ? "null" : p.current_price) + ', ' + (p.avg_premium == null ? "null" : p.avg_premium) + ')">sell</button></td>'
            : '<button class="mini-toggle" disabled title="live mode only - these buttons act on real positions (the paper card buttons manage the paper ledger)">tp</button> ' +
              '<button class="mini-toggle danger" disabled title="live mode only - these buttons act on real positions">sell</button></td>')
        : '<td></td>') + "</tr>";
  }
  el.innerHTML = html + "</table>";
  });
}

function openSettings() {
  document.getElementById("settingsBackdrop").style.display = "flex";
  document.getElementById("settings-float").style.display = "flex";
  const save = document.getElementById("settings-save");
  if (save) save.style.display = settingsDirty ? "" : "none";
  document.querySelectorAll("#settings textarea").forEach(autoGrow);
}

function closeSettings() {
  document.getElementById("settingsBackdrop").style.display = "none";
  document.getElementById("settings-float").style.display = "none";
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

// a scroll gesture starting on a modal's dim backdrop fires a
// click at touchend (small movements do not cancel the tap) -
// that closed open popups "by themselves" on touch screens
let backdropTouchMoved = false;
document.addEventListener("touchstart", function() { backdropTouchMoved = false; }, {passive: true});
document.addEventListener("touchmove", function() { backdropTouchMoved = true; }, {passive: true});

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
    (data.offset > 0 ? '<button class="btn sm" onclick="historyNav(-1)">&#8592; newer</button> ' : "") +
    (data.offset + data.rows.length < data.total ? '<button class="btn sm" onclick="historyNav(1)">older &#8594;</button>' : "") +
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
  // the save button follows the dirty state; revert stays ready
  // to restore the last saved state at any time
  const save = document.getElementById("settings-save");
  if (save) save.style.display = v ? "" : "none";
}

function revertSettings() {
  // back to the last saved state: re-fetch + re-render (the
  // poll-driven re-render is skipped while the modal is open)
  setSettingsDirty(false);
  load().then(function() {
    renderSettings((lastPayload && lastPayload.settings) || lastSettings);
    const msg = document.getElementById("settings-msg");
    if (msg) {
      msg.textContent = "Reverted";
      setTimeout(() => msg.textContent = "", 3000);
    }
  });
}

document.getElementById("settings-save").onclick = saveSettings;
document.getElementById("settings-revert").onclick = revertSettings;

function _fieldHelp(tip) {
  // hover tooltips are useless on a phone - the help text is
  // rendered under the field and shown on touch/small screens
  // (hidden on desktop, where the tooltip works)
  return tip
    ? '<div class="field-help">' + esc(tip) + "</div>" : "";
}

function _numField(id, label, value, tip, cls) {
  return '<div class="set-field"><label>' + esc(label) + '</label>' +
    '<input id="' + id + '" type="number" step="any" value="' + esc(value ?? "") + '"' +
    (cls ? ' class="' + cls + '"' : "") + '>' +
    _fieldHelp(tip) + '</div>';
}

function _txtField(id, label, value, placeholder, tip, full) {
  return '<div class="set-field' + (full ? " full" : "") + '"><label' +
    '>' + esc(label) + '</label>' +
    '<input id="' + id + '" type="text" value="' + esc(value ?? "") + '"' +
    (placeholder ? ' placeholder="' + esc(placeholder) + '"' : "") + '>' +
    _fieldHelp(tip) + '</div>';
}

function _check(id, label, checked, tip) {
  return '<div class="set-check"><label' +
    (tip ? ' title="' + esc(tip) + '"' : "") +
    '><input id="' + id + '" type="checkbox"' + (checked ? " checked" : "") +
    '> ' + esc(label) + '</label>' +
    _fieldHelp(tip) + '</div>';
}

function _section(title, body, tip) {
  return '<div class="set-section"><div class="set-title">' + esc(title) + '</div>' +
    _fieldHelp(tip) + body + '</div>';
}

function _subsection(title, body, tip) {
  return '<div class="set-sub"><div class="set-sub-title">' + esc(title) + '</div>' +
    _fieldHelp(tip) + body + '</div>';
}

function _modeSlider(mode) {
  const stops = ["notify", "paper", "live"];
  return '<div class="mode-slider">' + stops.map(function (m) {
    return '<button type="button" class="mode-stop' +
      (mode === m ? " active mode-" + m : "") +
      '" data-mode="' + m + '" onclick="onModeClick(\'' + m + '\')">' +
      m + '</button>';
  }).join("") + '</div>';
}

let _restarting = false;

function onModeClick(mode) {
  if (_restarting) return;
  const current = (lastSettings.trading || {}).mode;
  if (mode === current) return;
  if (mode === "live") {
    openInputModal(
      "Switch to LIVE mode",
      "live places REAL orders on the Wealthsimple account on " +
      "every parsed alert. The app restarts to apply. " +
      "Type LIVE to confirm.",
      {label: "type LIVE to confirm", placeholder: "LIVE", value: ""},
      null,
      async function () {
        const v = (
          document.getElementById("mInput").value || ""
        ).trim().toUpperCase();
        if (v !== "LIVE") {
          onModeClick("live");   // wrong token - ask again
          return;
        }
        await doModeSwitch("live");
      }
    );
    return;
  }
  const text = mode === "paper"
    ? "paper simulates every fill against the local ledger - " +
      "no real orders. The app restarts to apply."
    : "notify only sends sizing previews to the webhook - " +
      "nothing executes. The app restarts to apply.";
  openModal("Switch to " + mode + " mode", text, "switch",
    async function () { await doModeSwitch(mode); });
}

async function doModeSwitch(mode) {
  try {
    const r = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode }),
    });
    const d = await r.json();
    if (!r.ok) {
      alert((d.errors || [d.error || "mode switch failed"]).join("\n"));
      return;
    }
    if (d.restarting) {
      _restarting = true;
      let el = document.getElementById("restart-overlay");
      if (!el) {
        el = document.createElement("div");
        el.id = "restart-overlay";
        document.body.appendChild(el);
      }
      el.textContent = "pipeline restarting in " + mode +
        " mode - reconnecting\u2026";
      el.style.display = "flex";
      // the process exits ~1.5s after the response; ignore the
      // first 3s of health checks (the old process is still
      // up), then reload as soon as it answers again
      const started = Date.now();
      const deadline = started + 60000;
      const poll = setInterval(async function () {
        if (Date.now() > deadline) {
          clearInterval(poll);
          location.reload();
          return;
        }
        if (Date.now() - started < 3000) return;
        try {
          const h = await fetch("/health");
          if (h.ok) {
            clearInterval(poll);
            location.reload();
          }
        } catch (e) { /* down - keep polling */ }
      }, 1000);
    } else {
      setSettingsDirty(false);
      load();
    }
  } catch (e) {
    alert("mode switch failed: " + e);
  }
}

function renderSettings(s) {
  lastSettings = s;
  // leave the form alone while the user has unsaved edits - the
  // periodic refresh used to wipe them mid-typing
  if (settingsDirty) return;
  const el = document.getElementById("settings");
  const t = s.trading || {};
  const dc = s.discord || {};
  const ws = s.wealthsimple || {};
  const au = s.auto_update || {};
  const q = s.quotes || {};

  // 1. automation: one section for everything the bot runs on.
  // top row: the mode slider + global behaviour toggles. below:
  // grouped sub-sections - paper ledger, 0dte, risk caps,
  // stops & exits, live fills, quotes provider, github code
  // update and account value monitoring
  let html = _section("automation",
    '<div class="set-checks" style="margin-bottom:0">' +
      '<div class="set-check"><label>mode</label>' +
        _modeSlider(t.mode) +
        _fieldHelp("notify = alerts only \u00b7 paper = simulated fills \u00b7 live = real orders - switching warns, saves and restarts the app") +
      '</div>' +
      _check("set-notify", "phone notifications", dc.notify !== false,
        "send parsed trade alerts and results to the discord webhook") +
      _check("set-trading_paused", "trading paused (kill switch)",
        t.trading_paused,
        "emergency stop: blocks every new BUY immediately, no restart needed - exits (alert sells, stops) stay allowed") +
    '</div>' +
    _subsection("paper ledger",
      '<div class="set-checks" style="margin-bottom:10px">' +
        _check("set-paper-enabled", "paper alongside notify", s.paper && s.paper.enabled,
          "adds simulated execution to notify mode - the mode slider above is the primary switch") +
        _check("set-paper-mirror", "mirror real fills", s.paper && s.paper.mirror,
          "copy real wealthsimple fills into the paper ledger") +
      '</div>' +
      '<div class="set-grid">' +
        _numField("set-mirror-interval", "mirror every (s)",
          s.paper && s.paper.mirror_interval_seconds,
          "seconds between real-fill mirror scans") +
        _numField("set-paper_account_value", "paper value $",
          t.paper_account_value,
          "fallback paper equity when live account values are unavailable (fresh paper accounts seed from the live value)") +
      '</div>') +
    _subsection("0dte",
      '<div class="set-checks" style="margin-bottom:10px">' +
        _check("set-back_to_entry_enabled", "auto b2e sell on 0dte",
          t.back_to_entry_enabled !== false,
          "sell a 0dte option back to its entry when the day's gain evaporates (per-tier override in the size tiers below)") +
      '</div>' +
      '<div class="set-grid">' +
        _numField("set-lotto_gain_budget_pct", "lotto budget %",
          t.lotto_gain_budget_pct,
          "hero-or-zero / profits-only buys may spend at most this % of today's realized sell gains", "big") +
      '</div>') +
    _subsection("risk caps",
      '<div class="set-grid">' +
        _numField("set-risk_per_trade_pct", "default risk %", t.risk_per_trade_pct,
          "% of account value risked per trade when no size keyword is given", "big") +
        _numField("set-max_contracts_per_trade", "max contracts", t.max_contracts_per_trade,
          "hard cap on contracts per trade across all accounts", "big") +
        _numField("set-max_open_risk_pct", "open risk cap %", t.max_open_risk_pct,
          "stop opening new risk once deployed capital exceeds this % of account value", "big") +
        _numField("set-cluster_cap_pct", "cluster cap %", t.cluster_cap_pct,
          "one underlying+expiry+direction cluster (e.g. several SPX 0dte calls) may never exceed this % of account value - a buy into a capped cluster is skipped even when the global cap has room (0 = off)", "big") +
        _numField("set-max_daily_loss_pct", "daily loss cap %", t.max_daily_loss_pct,
          "hard daily-loss circuit breaker: once today's realized pnl sinks below this % of account value, new buys pause until tomorrow (0 = off)", "big") +
      '</div>') +
    _subsection("stops & exits",
      '<div class="set-grid">' +
        _numField("set-stop_loss_pct", "stop loss %", t.stop_loss_pct,
          "global stop loss % below entry (per-size overrides live in the size tiers below)", "big") +
        _numField("set-trailing_stop_pct", "trailing stop %", t.trailing_stop_pct,
          "trailing stop distance once in profit; 0 disables", "big") +
        _numField("set-stop_check_seconds", "stop check (s)", t.stop_check_seconds,
          "how often the stop monitor polls quotes") +
      '</div>') +
    _subsection("live fills",
      '<div class="set-grid">' +
        '<div class="set-field"><label>order type</label>' +
        '<div class="field-help">order type used for live executions</div>' +
        '<select id="set-order_type" title="order type used for live executions">' +
          '<option value="market"' + (t.order_type === "market" ? " selected" : "") + '>market</option>' +
          '<option value="limit"' + (t.order_type === "limit" ? " selected" : "") + '>limit</option>' +
        '</select></div>' +
        _numField("set-limit_offset_pct", "limit offset %", t.limit_offset_pct,
          "how far past the market price a limit order chases (limit order type only)") +
        _numField("set-max_slippage_pct", "slippage notice %", t.max_slippage_pct,
          "a live fill landing this % away from the order's estimated price posts a discord notice with both prices (data only, 0 = off)", "big") +
        _numField("set-partial_fill_cancel_pct", "partial-fill cancel %", t.partial_fill_cancel_pct,
          "a partially-filled order whose price runs this % away from the estimate gets its remainder cancelled - the filled part stays as the position (0 = off)", "big") +
      '</div>') +
    _subsection("quotes provider",
      '<div class="set-checks" style="margin-bottom:10px">' +
        _check("set-quotes-enabled", "live option quotes (stop monitor)", q.enabled,
          "fetch live option quotes for the stop monitor. off does not leave positions unguarded: paper mode prices stops from the paper ledger's own quote map, and live mode falls back to the ws option chains") +
      '</div>' +
      '<div class="set-grid">' +
        '<div class="set-field"><label>' +
        'quotes provider</label>' +
        '<div class="field-help">quote source for the stop monitor - takes effect after restart: ws = wealthsimple, moomoo = local OpenD feed</div>' +
        '<select id="set-quotes-provider" title="quote source for the stop monitor - takes effect after restart: ws = wealthsimple, moomoo = OpenD feed">' +
          '<option value="ws"' + (q.provider === "ws" ? " selected" : "") + '>ws</option>' +
          '<option value="moomoo"' + (q.provider === "moomoo" ? " selected" : "") + '>moomoo</option>' +
        '</select></div>' +
        _txtField("set-quotes-moomoo_host", "moomoo host", q.moomoo_host, "127.0.0.1",
          "OpenD gateway address for moomoo quotes") +
        _numField("set-quotes-moomoo_port", "moomoo port", q.moomoo_port,
          "OpenD gateway port for moomoo quotes") +
      '</div>') +
    _subsection("github code update",
      '<div class="set-checks" style="margin-bottom:10px">' +
        _check("set-au-enabled", "auto-update", au.enabled,
          "pull and apply code updates from github automatically") +
      '</div>' +
      '<div class="set-grid">' +
        _numField("set-au-interval", "update check (s)", au.interval_seconds,
          "seconds between github update checks") +
      '</div>') +
    _subsection("account value monitoring",
      '<div class="set-grid">' +
        _numField("set-ws-positions", "positions refresh (s)", ws.positions_refresh_seconds,
          "seconds between wealthsimple position refreshes") +
        _numField("set-ws-values", "values refresh (s)", ws.values_refresh_seconds,
          "seconds between wealthsimple account value refreshes") +
        _numField("set-ws-margin-rate", "stock margin rate", ws.stock_margin_rate,
          "maintenance margin rate applied to stock holdings (0.30 = 30%)") +
      '</div>'));

  // 2. size tiers: the option tiers feed the 0dte controls above
  // (per-tier b2e override) so they live directly under them
  let tiers = '<div class="set-grid">' +
    '<div class="set-field full" style="color:var(--muted);font-size:11px">' +
    'option tiers - risk % cap / min / max contracts / stop loss % (empty = global)</div>';
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
      '</div>' +
      '<input id="tier-' + esc(name) + '-stop" type="number" step="any" value="' +
      (tier.stop_loss_pct == null ? "" : esc(tier.stop_loss_pct)) +
      '" placeholder="\u2014" title="per-size stop loss % (empty = the global stop applies)">' +
      (tier.back_to_entry != null
        ? '<label style="font-size:10px;display:flex;gap:3px;align-items:center"><input type="checkbox" id="tier-' + esc(name) + '-b2e"' + (tier.back_to_entry ? " checked" : "") + ' title="0dte back-to-entry sell for this size">b2e</label>'
        : '') +
      '</div>';
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
  tiers += _numField("set-position_size_cad", "stock fallback size $",
    t.position_size_cad,
    "flat per-trade dollars for stock buys when no size tier matches the alert");
  tiers += '</div>';
  html += _section("size tiers", tiers);

  // 3. trading limits
  html += _section("trading limits", '<div class="set-grid">' +
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
    _numField("set-history_retention_days", "history retention (d)", t.history_retention_days,
      "days to keep signals and trades; 0 = keep forever (takes effect after restart)") +
    '</div>' +
    '<div class="set-checks" style="margin:10px 0 0">' +
      _check("set-sell_only_if_held", "sell only if held",
        t.sell_only_if_held,
        "refuse sells when the ledger shows no open position") +
    '</div>');

  // 4. filters
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

  // 6. discord webhooks
  const hooks = [
    ["set-discord-trade_alert_webhook_url", "trade alerts",
      "webhook for parsed alerts and execution results",
      "main alerts channel", dc.trade_alert_webhook_url || ""],
    ["set-discord-consumer_log_webhook_url", "consumer log",
      "consumer.log tail; empty = off",
      "empty = off", dc.consumer_log_webhook_url || ""],
    ["set-discord-update_webhook_url", "update notices",
      "restart and update notices; empty = the trade alerts channel",
      "empty = trade alerts channel", dc.update_webhook_url || ""],
  ];
  html += _section("discord webhooks (take effect after restart)",
    '<div class="set-grid wide">' +
    hooks.map(h =>
      '<div class="set-field full"><label title="' + esc(h[2]) + '">' + esc(h[1]) + '</label>' +
      '<textarea id="' + h[0] + '" rows="2" title="' + esc(h[2]) + '" placeholder="' + esc(h[3]) + '">' + esc(h[4]) + '</textarea></div>'
    ).join("") + '</div>');

  // 7. accounts: add / remove / per-account overrides (empty =
  // inherit global) - last: the longest section, rarely touched.
  // always rendered so a blank config can add its first account
  renderSettings.newCount = 0;
  let accts = '<div id="set-accounts-list">';
  (s.accounts || []).forEach((a, i) => { accts += _acctCard(a, i, false); });
  accts += '</div>';
  html += _section("accounts (take effect after restart)", accts +
    '<button type="button" id="set-acct-add" class="btn">+ add account</button>',
    "accounts size and execute alerts; empty override fields inherit the global settings");

  el.innerHTML = html;
  el.querySelectorAll("textarea").forEach(function(t) {
    autoGrow(t);
    t.addEventListener("input", function() { autoGrow(t); });
  });
  _wireSettingsFields(el);

  // add account: append a blank editable card (existing labels
  // key the ledgers, so only new rows get a label field)
  const addBtn = document.getElementById("set-acct-add");
  if (addBtn) addBtn.addEventListener("click", function() {
    const list = document.getElementById("set-accounts-list");
    if (!list) return;
    const base = (lastSettings.accounts || []).length;
    const idx = base + (renderSettings.newCount = (
      (renderSettings.newCount || 0) + 1));
    const card = _acctCard(
      {label: "", account_id: "", type: "", enabled: true},
      idx, true
    );
    list.insertAdjacentHTML("beforeend", card);
    const node = list.lastElementChild;
    node.querySelectorAll("textarea").forEach(function(t) {
      autoGrow(t);
      t.addEventListener("input", function() { autoGrow(t); });
    });
    _wireSettingsFields(node);
    const labelInput = document.getElementById(
      "set-acct-" + idx + "-label"
    );
    if (labelInput) labelInput.focus();
  });
}

function _wireSettingsFields(container) {
  container.querySelectorAll("input,select,textarea").forEach(function(i) {
    i.addEventListener("input", function() { setSettingsDirty(true); });
    i.addEventListener("change", function() { setSettingsDirty(true); });
  });
}

function _acctCard(a, i, isNew) {
  // existing labels key the ledgers - they are fixed; only new
  // rows get an editable label
  const head = isNew
    ? '<div class="acct-head"><span>' +
      _txtField("set-acct-" + i + "-label", "label", a.label || "",
        "e.g. TFSA", "unique name shown in the dashboard - it keys "
        + "the account ledger, choose it once", true) + '</span>' +
      '<span>' + _check("set-acct-" + i + "-enabled", "on",
        a.enabled !== false,
        "include this account in sizing and paper trading") +
      '</span></div>'
    : '<div class="acct-head"><span' +
      ' title="per-account overrides; empty fields inherit the global settings"' +
      '>' + esc(a.label) + '</span>' +
      '<span>' + _check("set-acct-" + i + "-enabled", "on", a.enabled,
        "include this account in sizing and paper trading") + '</span>' +
      '<span>' + _check("set-acct-" + i + "-remove", "remove", false,
        "delete this account from the config on save (its ledger "
        + "rows stay in the database)") + '</span></div>';
  const typeTip = "margin accounts borrow against holdings (the "
    + "margin requirement is real); non_margin covers registered "
    + "plans - RRSP, TFSA, FHSA model as non_margin (cash covers "
    + "the position, no borrowing); auto-detect reads the type "
    + "from Wealthsimple";
  const typeSel = '<div class="set-field"><label title="' + esc(typeTip) +
    '">account type</label><select id="set-acct-' + i + '-type"' +
    ' title="' + esc(typeTip) + '">' +
    [["", "auto-detect"], ["margin", "margin"],
     ["non_margin", "non-margin (registered)"]].map(function(o) {
      return '<option value="' + o[0] + '"' +
        ((a.type || "") === o[0] ? " selected" : "") + '>' +
        esc(o[1]) + '</option>';
    }).join("") + '</select>' + _fieldHelp(typeTip) + '</div>';
  return '<div class="acct-card" data-acct="' + i + '"' +
    (isNew ? ' data-new="1"' : '') + '>' + head +
    '<div class="acct-grid">' +
    _txtField("set-acct-" + i + "-id", "account id", a.account_id || "",
      "", "wealthsimple account id (from scripts/ws_login.py)") +
    typeSel +
    _numField("set-acct-" + i + "-max", "max contracts",
      a.max_contracts_per_trade,
      "per-account contract cap; empty = inherit global") +
    _numField("set-acct-" + i + "-risk", "risk %", a.risk_per_trade_pct,
      "per-account risk override; empty = inherit global") +
    _numField("set-acct-" + i + "-orisk", "open risk cap %",
      a.max_open_risk_pct,
      "per-account open-risk cap - a small account may deploy a high share of its own value without raising the cap for the others; empty = inherit global") +
    _numField("set-acct-" + i + "-paper", "paper value $", a.paper_value,
      "fallback paper equity when live values are unavailable") +
    '</div></div>';
}

async function saveSettings() {
  const val = (id) => document.getElementById(id).value;
  const num = (id) => parseFloat(val(id));
  const trading = {};
  for (const k of ["risk_per_trade_pct","max_contracts_per_trade","max_open_risk_pct","cluster_cap_pct","max_slippage_pct","partial_fill_cancel_pct","stop_loss_pct","max_daily_loss_pct","trailing_stop_pct","stop_check_seconds","max_consecutive_losses","min_dte_days","max_trades_per_day","cooldown_seconds","dedupe_window_minutes","limit_offset_pct","history_retention_days","lotto_gain_budget_pct","position_size_cad","paper_account_value"]) {
    trading[k] = num("set-" + k);
  }
  trading.order_type = val("set-order_type");
  trading.sell_only_if_held = document.getElementById("set-sell_only_if_held").checked;
  trading.trading_paused = document.getElementById("set-trading_paused").checked;
  trading.back_to_entry_enabled = document.getElementById("set-back_to_entry_enabled").checked;
  const toList = (id) => val(id).split(",").map(function(s) { return s.trim(); }).filter(Boolean);
  trading.ticker_whitelist = toList("set-ticker_whitelist");
  trading.skip_underlyings = toList("set-skip_underlyings");
  const tiers = {};
  document.querySelectorAll("[id^=tier-]").forEach(() => {});
  const names = new Set();
  document.querySelectorAll("[id^=tier-]").forEach(el => names.add(el.id.split("-")[1]));
  for (const name of names) {
    tiers[name] = { risk_pct_max: num("tier-" + name + "-risk"), contracts_min: parseInt(val("tier-" + name + "-min")), contracts_max: parseInt(val("tier-" + name + "-max")) };
    // per-size stop loss: empty = the global stop applies
    const stopV = val("tier-" + name + "-stop");
    if (stopV !== "") tiers[name].stop_loss_pct = parseFloat(stopV);
    // per-size back-to-entry: only sent when the tier exposes it
    const b2e = document.getElementById("tier-" + name + "-b2e");
    if (b2e) tiers[name].back_to_entry = b2e.checked;
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
  const accounts = Array.from(
    document.querySelectorAll("#set-accounts-list .acct-card")
  ).map((card) => {
    const i = card.dataset.acct;
    const isNew = card.dataset.new === "1";
    const g = (id) => {
      const el = document.getElementById(id);
      return el ? el.value : "";
    };
    const numOrNull = (id) => {
      const v = g(id);
      return v === "" ? null : parseFloat(v);
    };
    const entry = {
      label: isNew
        ? g("set-acct-" + i + "-label").trim()
        : (lastSettings.accounts || [])[Number(i)]?.label,
      account_id: g("set-acct-" + i + "-id"),
      type: g("set-acct-" + i + "-type"),
      max_contracts_per_trade: g("set-acct-" + i + "-max") === ""
        ? null : parseInt(g("set-acct-" + i + "-max")),
      risk_per_trade_pct: numOrNull("set-acct-" + i + "-risk"),
      max_open_risk_pct: numOrNull("set-acct-" + i + "-orisk"),
      paper_value: numOrNull("set-acct-" + i + "-paper"),
      enabled: (document.getElementById(
        "set-acct-" + i + "-enabled"
      ) || {checked: true}).checked,
    };
    if (!isNew) {
      entry.remove = (document.getElementById(
        "set-acct-" + i + "-remove"
      ) || {checked: false}).checked;
    }
    return entry;
  }).filter((e) => e.label);
  const payload = {
    trading,
    accounts,
    auto_update: { enabled: document.getElementById("set-au-enabled").checked, interval_seconds: parseInt(val("set-au-interval")) },
    wealthsimple: {
      positions_refresh_seconds: parseInt(val("set-ws-positions")),
      values_refresh_seconds: parseInt(val("set-ws-values")),
      stock_margin_rate: num("set-ws-margin-rate"),
    },
    discord: {
      notify: document.getElementById("set-notify").checked,
      trade_alert_webhook_url: val("set-discord-trade_alert_webhook_url").trim(),
      consumer_log_webhook_url: val("set-discord-consumer_log_webhook_url").trim(),
      update_webhook_url: val("set-discord-update_webhook_url").trim(),
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
  const msg = document.getElementById("settings-msg");
  if (res.status !== 200) {
    if (msg) msg.textContent = "";
    alert("save failed:\n" + (data.errors || []).join("\n"));
  } else {
    setSettingsDirty(false);
    await load();
    // the poll-driven re-render is skipped while the modal is
    // open - without this the form keeps showing the pre-save
    // numbers and the save looks like it failed
    renderSettings((lastPayload && lastPayload.settings) || lastSettings);
    if (msg) {
      msg.textContent = "Saved";
      setTimeout(() => msg.textContent = "", 3000);
    }
  }
}

function applyDashboard(data) {
  me = data.me || null;
  renderMe();
  paperPositions = data.paper_positions || {};
  renderSummary(data.summary);
  renderPositions(data.positions || []);
  renderSignals(data.signals || []);
  renderTrades(data.trades || []);
  // the settings form re-renders on open only - a poll-driven
  // re-render while the modal is open wipes unsaved edits and
  // freshly added account rows (they vanish seconds after the
  // + add click)
  if (
    document.getElementById("settingsBackdrop").style.display !== "flex"
  ) {
    renderSettings(data.settings || {});
  }
  gitStatus = data.update_status || null;
  renderGitStatus(gitStatus);
  lastRefresh = Date.now();
}

let lastPayload = null;

async function load() {
  try {
    const data = await api("/api/dashboard");
    lastPayload = data;
    try {
      localStorage.setItem("dash_last_payload", JSON.stringify(data));
    } catch (e) { /* storage full - the fresh fetch still renders */ }
    applyDashboard(data);
  } catch (e) { /* handled in api() */ }
}

function replayLastPayload() {
  // paint the previous session's payload before the first fetch
  // lands - the page renders at full height immediately and the
  // first data arrival cannot shift anything (the layout-shift
  // traces kept flagging the load-time expansion)
  try {
    const saved = JSON.parse(
      localStorage.getItem("dash_last_payload") || "null"
    );
    if (saved && saved.summary) applyDashboard(saved);
  } catch (e) { /* stale or absent - the fresh fetch renders */ }
}

replayLastPayload();
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
  // them anyway - this keeps the ui honest). the save button
  // also follows the dirty state - this runs on every poll and
  // would otherwise force it visible on a clean form
  const save = document.getElementById("settings-save");
  const revert = document.getElementById("settings-revert");
  if (save) save.style.display = (
    isAdmin() && settingsDirty
  ) ? "" : "none";
  if (revert) revert.style.display = isAdmin() ? "" : "none";
}

let levelsNow = null;
let levelsStale = false;
let levelsSpy = null;
let levelsSpyStatus = null;
let levelsError = null;
let levelsPoll = null;

async function openLevels() {
  document.getElementById("levelsBackdrop").style.display = "flex";
  // levels are read-only here: the info server is the source of
  // truth and pushes them via the feed. the cached copy renders
  // instantly; the server copy replaces it when it differs
  const saved = localStorage.getItem("spx_levels_raw") || "";
  if (saved) renderLevelsChart();
  try {
    const data = await api("/api/spx");
    if (data.text != null && data.text !== "" && data.text !== saved) {
      localStorage.setItem("spx_levels_raw", data.text);
      const parsed = parseLevelsText(data.text);
      if (parsed.levels.length || parsed.pivot != null) {
        localStorage.setItem("spx_levels", JSON.stringify(parsed));
      }
      renderLevelsChart();
    }
  } catch (e) { /* the banner surfaces network issues */ }
  // realtime spx spot while the popup is open
  clearInterval(levelsPoll);
  const poll = async function() {
    try {
      const data = await api("/api/spx");
      const changed = data.price !== levelsNow
        || data.spy !== levelsSpy
        || data.stale !== levelsStale
        || (data.spy_status || "") !== (levelsSpyStatus || "");
      levelsNow = data.price;
      // spy's own realtime quote - it trades overnight and
      // post-market, so the spy ladder stays live when the
      // index is closed
      levelsSpy = data.spy;
      levelsSpyStatus = data.spy_status || null;
      levelsStale = !!data.stale;
      levelsError = data.error || null;
      if (changed) renderLevelsChart();
      // the spot refresh rides the position refresh setting
      const interval = (data.refresh_seconds || 30) * 1000;
      if (levelsPoll && levelsPoll._period !== interval) {
        clearInterval(levelsPoll);
        levelsPoll = setInterval(poll, interval);
        levelsPoll._period = interval;
      }
    } catch (e) { /* the banner surfaces network issues */ }
  };
  poll();
  levelsPoll = setInterval(poll, 30000);
  levelsPoll._period = 30000;
}

function closeLevels() {
  document.getElementById("levelsBackdrop").style.display = "none";
  clearInterval(levelsPoll);
  levelsPoll = null;
}

function parseLevelsText(text) {
  // format A: "🔄 Pivot: 7704" +
  // "📈 Resistance: 7712 (R1), 7753 (R2), ..."
  const out = { pivot: null, levels: [], tickers: {} };
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
  if (out.levels.length) {
    // format A's levels are spx levels: keep the spx ticker
    out.tickers.SPX = out.levels;
    // derive the spy ladder when the plan has no spy chain
    // (spy = spx / 10.0391, the converter ratio)
    if (!out.tickers.SPY) {
      out.derived_spy = true;
      out.tickers.SPY = out.levels.map(function(l) {
        return {
          label: l.label,
          price: Math.round(l.price / 10.0391 * 100) / 100,
        };
      });
    }
    return out;
  }

  // format B: a KEY LEVELS block with per-ticker chains
  //   Support:
  //   SPX: 7712 \u2794 7687 \u2794 7662
  // supports count up from S1 in the given order; resistances
  // count up from R1
  let section = null;
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (/^Resistance\b/i.test(line)) { section = "R"; continue; }
    if (/^Support\b/i.test(line)) { section = "S"; continue; }
    if (!section) continue;
    if (!line) { section = null; continue; }
    const tm = line.match(/^(SPX|SPY)\b/i);
    if (!tm) continue;
    const nums = (line.match(/\d+(?:\.\d+)?/g) || [])
      .map(parseFloat);
    const ticker = tm[1].toUpperCase();
    out.tickers[ticker] = out.tickers[ticker] || [];
    nums.forEach(function(p, i) {
      out.tickers[ticker].push({
        label: section + (i + 1),
        price: p,
      });
    });
  }
  // the spy ladder derives from spx when the plan has no spy
  // chain: spy = spx / 10.0391 (the spxplays converter ratio)
  if (out.tickers.SPX && !out.tickers.SPY) {
    out.derived_spy = true;
    out.tickers.SPY = out.tickers.SPX.map(function(l) {
      return {
        label: l.label,
        price: Math.round(l.price / 10.0391 * 100) / 100,
      };
    });
  }
  if (out.tickers.SPX) out.levels = out.tickers.SPX;
  else if (out.tickers.SPY) out.levels = out.tickers.SPY;
  return out;
}

let levelsView = null;

function setLevelsView(ticker) {
  levelsView = ticker;
  renderLevelsChart();
}

function buildLevelsLadder(host, ticker, rows, pivot, headerHtml,
                           spot, tag) {
  rows = rows.slice();
  if (pivot != null) {
    // the pivot is an spx level: convert it for the spy ladder
    const p = ticker === "SPY" ? Math.round(pivot / 10.0391 * 100) / 100 : pivot;
    rows.push({ label: "Pivot", price: p, pivot: true });
  }
  if (!rows.length) {
    host.innerHTML = '<div class="empty">no levels</div>';
    return;
  }
  rows.sort(function(a, b) { return b.price - a.price; });
  const prices = rows.map(function(r) { return r.price; });
  const max = Math.max.apply(null, prices);
  const min = Math.min.apply(null, prices);
  const span = (max - min) || 1;
  let html = "";
  if (headerHtml) {
    html += '<div class="levels-nowline">' + headerHtml + "</div>";
  }
  const nowPrice = spot;
  html += '<div class="levels-ladder">';
  // the spot marker, inside the level range
  if (nowPrice != null && nowPrice >= min && nowPrice <= max) {
    const nowPct = ((max - nowPrice) / span * 100).toFixed(1);
    html += '<div class="levels-row now" style="top:' + nowPct + '%">' +
      '<span class="levels-chip now">' + tag + '</span>' +
      '<div class="levels-line now"><span class="levels-nowprice">' +
      nowPrice.toLocaleString("en-CA", { minimumFractionDigits: 2 }) +
      "</span></div></div>";
  }
  for (const r of rows) {
    const topPct = ((max - r.price) / span * 100).toFixed(1);
    const kind = r.pivot ? "p" : r.label.charAt(0).toLowerCase();
    html += '<div class="levels-row" style="top:' + topPct + '%">' +
      '<span class="levels-chip ' + kind + '">' + esc(r.label) + "</span>" +
      '<span class="levels-price">' + r.price.toLocaleString("en-CA", { minimumFractionDigits: 0 }) + "</span>" +
      '<div class="levels-line ' + kind + '"></div></div>';
  }
  html += "</div>";
  host.innerHTML = html;
}

function renderLevelsChart() {
  const el = document.getElementById("levels-chart");
  let data = null;
  try { data = JSON.parse(localStorage.getItem("spx_levels") || "null"); } catch (e) {}
  if (!data || (!data.levels.length && data.pivot == null &&
      !(data.tickers && Object.keys(data.tickers).length))) {
    el.innerHTML = '<div class="empty">no levels parsed yet</div>';
    return;
  }
  const tickers = Object.keys(data.tickers || {});
  // both ladders side by side: spx left, spy right (spy drops
  // under spx on narrow screens via css)
  el.innerHTML = '<div class="levels-duo" id="levels-duo"></div>';
  const duo = document.getElementById("levels-duo");
  const views = tickers.length ? tickers : ["SPX"];
  // spx always first (left column, top when stacked)
  views.sort(function(a, b) {
    return (a === "SPX" ? -1 : b === "SPX" ? 1 : 0);
  });
  for (const t of views) {
    const rows = (data.tickers && data.tickers[t] ? data.tickers[t] : data.levels).slice();
    const headerParts = [];
    let spot = null;
    let tag = "now";
    if (t === "SPX") {
      if (levelsError) {
        headerParts.push('<span style="color:#f85149">spx spot unavailable: ' + esc(levelsError) + "</span>");
      } else if (levelsNow != null) {
        // no trading on the index after-mkt or overnight: the
        // close shows marked as close, not a live spot
        tag = levelsStale ? "close" : "now";
        headerParts.push("SPX " + tag + ": <b>" +
          levelsNow.toLocaleString("en-CA", { minimumFractionDigits: 2 }) + "</b>" +
          (levelsStale ? " (market closed)" : ""));
      }
      spot = levelsNow;
    } else if (t === "SPY") {
      if (levelsSpy != null) {
        // spy trades overnight - its own quote is the live
        // marker even when the index is closed. no status
        // (moomoo) means a realtime feed: not a close
        tag = levelsSpyStatus === "CLOSED" ? "close" : "now";
        headerParts.push("SPY " + tag + ": <b>" +
          levelsSpy.toLocaleString("en-CA", { minimumFractionDigits: 2 }) + "</b>" +
          (tag === "close" ? " (market closed)" : ""));
        spot = levelsSpy;
      } else if (levelsNow != null) {
        // no spy quote: fall back to the spx-derived value
        tag = levelsStale ? "close" : "now";
        spot = levelsNow / 10.0391;
        headerParts.push("SPY " + tag + " (derived): <b>" +
          spot.toLocaleString("en-CA", { minimumFractionDigits: 2 }) + "</b>");
      }
    }
    const pane = document.createElement("div");
    pane.className = "levels-pane";
    pane.innerHTML = '<div class="levels-pane-title">' + esc(t) +
      (t === "SPY" && data.derived_spy ? ' <span class="subv">(converted)</span>' : "") +
      '</div>';
    duo.appendChild(pane);
    const host = document.createElement("div");
    pane.appendChild(host);
    // generated html (ticker + spot number); error strings are
    // escaped where they are appended
    buildLevelsLadder(host, t, rows, data.pivot,
      headerParts.join(" · ") || null, spot, tag);
  }
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
          '<button class="btn sm" onclick="deleteUser(\'' + jsq(u.username) + '\')">remove</button>') +
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
  const msg = document.getElementById("pw-msg");
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
