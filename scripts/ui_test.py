#!/usr/bin/env python3
"""Headless-Chrome UI smoke test for the dashboard.

Unit tests assert on the dashboard's JS source; this drives the
real page in a real browser. The api() helper is stubbed with
canned summary data (the backend has its own coverage), the
page runs its full render path, and the harness clicks through
the interactions that have historically broken:

  - paper currency flip (was dead when fx fell back to 1.0)
  - paper eye masking cash/seeded (fell through to plain text)
  - margin breakdown open/mask/arrow (arrow stuck on collapse)
  - holdings arrow on an empty ledger (static glyph)
  - negative-zero P&L rendering ("+$-0.00")

Usage:  python scripts/ui_test.py
Needs:  chrome/chromium on PATH (or CHROME_BIN=...). Skips
with a note when no browser is available.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from trader.dashboard import DASHBOARD_HTML  # noqa: E402

CANNED = {
    "mode": "notify",
    "paper": True,
    "accounts": [
        {
            "label": "NON-REGISTERED MARGIN ACCOUNT",
            "value": 27400.0,
            "usd_value": 20000.0,
            "open_risk": 410.0,
            "open_risk_pct": 1.5,
            "max_open_risk_pct": 15,
            "stock_value": None,
            "option_value": None,
            "alloc_base": None,
            "cash_cad": 100.0,
            "cash_usd": None,
            "margin_requirement": 1279.73,
            "margin_breakdown": [
                "SPX 12793.70 x 10% = 1279.37 usd",
                "SPX put credit spread 1x width 5 = 500.00 usd",
            ],
            "margin_used": 367.83,
            "margin_used_cad": 160.35,
            "margin_used_usd": 207.48,
            "margin_available": 16120.27,
            "max_buying_power": 53734.23,
            "portfolio_value": 27767.83,
            "paper_value": 20500.0,
            "paper_initial": 10000.0,
            "paper_pnl": 10500.0,
            "paper_usd_value": 14642.86,
            "paper_cash": 20000.0,
            "paper_open_risk": 500.0,
            "paper_open_risk_pct": 2.4,
            "paper_stock_value": 500.0,
            "paper_option_value": 0.0,
            "paper_alloc_base": 20500.0,
            "paper_margin_requirement": 150.0,
            "paper_margin_breakdown": ["ZWC 500.00 x 30% = 150.00 cad"],
            "paper_margin_used": 0.0,
            "paper_margin_used_usd": 0.0,
            "paper_margin_used_cad": 0.0,
            "paper_margin_available": 20350.0,
            "paper_max_buying_power": 67833.33,
            "paper_portfolio_value": 20500.0,
        },
        {
            "label": "RRSP",
            "value": 6000.0,
            "usd_value": 4379.56,
            "open_risk": 0,
            "open_risk_pct": 0,
            "max_open_risk_pct": 15,
            "stock_value": None,
            "option_value": None,
            "alloc_base": None,
            "cash_cad": 50.0,
            "cash_usd": None,
            "margin_requirement": None,
            "margin_breakdown": [],
            "paper_value": 5000.0,
            "paper_initial": 5000.0,
            "paper_pnl": 0.0,
            "paper_usd_value": 3649.64,
            "paper_cash": 5000.0,
            "paper_open_risk": 0.0,
            "paper_open_risk_pct": 0.0,
            "paper_stock_value": None,
            "paper_option_value": None,
            "paper_alloc_base": 5000.0,
            "paper_margin_requirement": None,
            "paper_margin_breakdown": [],
            "paper_margin_used": None,
            "paper_margin_used_usd": None,
            "paper_margin_used_cad": None,
            "paper_margin_available": None,
            "paper_max_buying_power": None,
            "paper_portfolio_value": None,
        },
    ],
    "reader": {
        "channel": "spx-plays",
        "age_seconds": 3,
        "desired": "spx-plays",
    },
    "stops": {
        "stop_loss_pct": 50,
        "trailing_stop_pct": 30,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
    },
}

PAPER_POSITIONS = {
    "NON-REGISTERED MARGIN ACCOUNT": [
        {
            "contract_key": "ZWC",
            "kind": "stock",
            "qty": 15,
            "avg": 32.0,
            "value": 500.0,
            "pnl": 1.2,
        }
    ],
    "RRSP": [],
}

HARNESS = r"""
<script>
window.__out = [];
window.__t = function(name, ok, detail) {
  if (detail != null) detail = String(detail)
    .replace(/[\r\n]+/g, " | ");
  window.__out.push(name + "\t" + (ok ? "PASS" : "FAIL") +
    (detail ? "\t" + detail : ""));
};
(async function() {
  try {
    await loadSummary();

    // 1. cards render
    const cards = document.querySelectorAll(".card").length;
    const papers = document.querySelectorAll(".papercard").length;
    __t("cards-render", cards >= 2, "cards=" + cards);
    __t("paper-cards-render", papers === 2, "papers=" + papers);

    // 2. paper currency flip
    const paperVal = () => document.querySelector(".papercard .value").textContent;
    const before = paperVal();
    flipPaperCurrency("NON-REGISTERED MARGIN ACCOUNT");
    await new Promise(r => setTimeout(r, 250));
    const after = paperVal();
    __t("paper-flip-changes-value",
        before !== after && /USD/.test(after),
        "before=[" + before + "] after=[" + after + "]");

    // 3. paper eye masks cash and seeded
    const txt = () => document.querySelector(".papercard").textContent;
    const cashBefore = txt().indexOf("cash") >= 0;
    togglePaperHidden("NON-REGISTERED MARGIN ACCOUNT");
    await new Promise(r => setTimeout(r, 250));
    const masked = txt();
    __t("paper-eye-masks",
        masked.indexOf("$20,000.00") < 0 &&
        masked.indexOf("$10,000.00") < 0,
        "cash/seeded leaked through the mask");
    togglePaperHidden("NON-REGISTERED MARGIN ACCOUNT");
    await new Promise(r => setTimeout(r, 250));

    // 4. margin breakdown: opens, arrow flips, masking
    const reqCell = document.querySelector(
      '[onclick^="toggleMarginBreakdown"]');
    const mbdArrow = document.getElementById("mbda-0");
    __t("breakdown-collapsed-arrow",
        mbdArrow.textContent === "\u25B2",
        "arrow=" + mbdArrow.textContent);
    reqCell.click();
    const mbd = document.getElementById("mbd-0");
    __t("breakdown-opens", mbd.style.display === "block");
    __t("breakdown-open-arrow",
        mbdArrow.textContent === "\u25BC",
        "arrow=" + mbdArrow.textContent);
    __t("breakdown-visible-text",
        mbd.textContent.indexOf("1279.37") >= 0,
        mbd.textContent.slice(0, 80));

    // hide the card -> breakdown masks to structure only
    toggleCardHidden("NON-REGISTERED MARGIN ACCOUNT");
    await new Promise(r => setTimeout(r, 250));
    const maskedMbd = document.getElementById("mbd-0").textContent;
    __t("breakdown-masked",
        maskedMbd.indexOf("1279") < 0 && maskedMbd.indexOf(" x ") >= 0,
        maskedMbd.slice(0, 80));
    toggleCardHidden("NON-REGISTERED MARGIN ACCOUNT");
    await new Promise(r => setTimeout(r, 250));

    // 5. paper breakdown + holdings on a populated ledger
    const pReq = document.querySelector(
      '[onclick^="togglePaperBreakdown"]');
    pReq.click();
    const pmbd = document.getElementById("pmbd-0");
    __t("paper-breakdown-opens", pmbd.style.display === "block");

    const holdBtn = document.querySelector('[onclick^="togglePaper"]');
    __t("holdings-collapsed-arrow",
        holdBtn.textContent.indexOf("\u25B2") >= 0,
        holdBtn.textContent);
    holdBtn.click();
    await new Promise(r => setTimeout(r, 250));
    const holdBtn2 = document.querySelector('[onclick^="togglePaper"]');
    const tbl = document.querySelector(".papercard table");
    __t("holdings-open-arrow",
        holdBtn2.textContent.indexOf("\u25BC") >= 0,
        holdBtn2.textContent);
    __t("holdings-table-shows", !!tbl);

    // 6. empty ledger (RRSP): arrow still flips, says no positions
    const rrspCard = [...document.querySelectorAll(".papercard")]
      .find(c => c.textContent.indexOf("RRSP") >= 0);
    const rrspBtn = [...rrspCard.querySelectorAll("button")]
      .find(b => b.textContent.indexOf("holdings") >= 0);
    __t("empty-ledger-arrow",
        rrspBtn.textContent.indexOf("\u25B2") >= 0,
        rrspBtn.textContent);
    rrspBtn.click();
    await new Promise(r => setTimeout(r, 250));
    const rrspCard2 = [...document.querySelectorAll(".papercard")]
      .find(c => c.textContent.indexOf("RRSP") >= 0);
    const rrspBtn2 = [...rrspCard2.querySelectorAll("button")]
      .find(b => b.textContent.indexOf("holdings") >= 0);
    __t("empty-ledger-arrow-flips",
        rrspBtn2.textContent.indexOf("\u25BC") >= 0,
        rrspBtn2.textContent);
    __t("empty-ledger-says-no-positions",
        rrspCard2.textContent.indexOf("no positions") >= 0);

    // 7. no negative-zero or $- artifacts in the *rendered*
    // text (scripts contain the string in comments)
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll("script, style").forEach(n => n.remove());
    const page = clone.textContent;
    __t("no-negative-zero", page.indexOf("$-") < 0, "found $-");
  } catch (e) {
    __t("harness-error", false, e.message + " | " + e.stack);
  }

  const pre = document.createElement("pre");
  pre.id = "uitest-out";
  pre.textContent = (window.__out || []).join("\n");
  document.body.appendChild(pre);
})();
</script>
"""


def find_chrome():
    env = os.environ.get("CHROME_BIN")
    if env and os.path.exists(env):
        return env
    for name in (
        "google-chrome", "google-chrome-stable", "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_page():
    # replace the whole api() helper with the canned stub - the
    # marker check catches dashboard refactors of api() so the
    # stub silently not applying can never pass
    stub = (
        'async function api(path) {\n'
        '  if (path === "/api/summary") return '
        + json.dumps(CANNED) + ';\n'
        '  if (path === "/api/paper-positions") return '
        + json.dumps(PAPER_POSITIONS) + ';\n'
        '  return {};\n'
        '}'
    )
    m = re.search(
        r'async function api\(path\) \{.*?\n\}',
        DASHBOARD_HTML, re.S,
    )
    if not m:
        raise RuntimeError(
            "api() not found in DASHBOARD_HTML - update the stub"
        )
    html = DASHBOARD_HTML[:m.start()] + stub + \
        DASHBOARD_HTML[m.end():]
    if "__UITEST_STUB__" in html:
        raise RuntimeError("stub marker collision")
    html = html.replace(
        stub, stub.replace(
            "async function api(path)",
            "async function api(path) { /* __UITEST_STUB__ */",
            1,
        ).replace(
            "async function api(path) { /* __UITEST_STUB__ */",
            "async function api(path)",
        )
    )
    if '"mode": "notify"' not in html:
        raise RuntimeError("canned data missing from page")
    return html.replace("</body>", HARNESS + "</body>")


def main():
    chrome = find_chrome()
    if not chrome:
        print("SKIP: no chrome/chromium found (set CHROME_BIN)")
        return 0

    page = build_page()
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".html", delete=False, encoding="utf-8"
    )
    tmp.write(page)
    tmp.close()

    try:
        proc = subprocess.run(
            [
                chrome, "--headless=new", "--disable-gpu",
                "--no-sandbox", "--virtual-time-budget=8000",
                "--dump-dom", "file://" + tmp.name,
            ],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        os.unlink(tmp.name)

    m = re.search(
        r'<pre id="uitest-out">(.*?)</pre>', proc.stdout, re.S
    )
    if not m:
        print("FAIL: harness output missing")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return 1

    failures = 0
    for line in m.group(1).strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue   # stray continuation text, not a result
        name, status = parts[0], parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        if status == "PASS":
            print(f"  PASS  {name}")
        else:
            failures += 1
            print(f"  FAIL  {name}  {detail}")

    total = m.group(1).strip().count("\n") + 1
    print(f"\nUI RESULT: {total - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
