import os
import re
import types

from trader.web.dashboard import DASHBOARD_HTML, DASHBOARD_CSS, DASHBOARD_JS


def _strip_complete_strings(line):
    code = re.sub(r"(?<!:)//.*", "", line)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "", code)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', "", code)
    return code


def test_dashboard_script_strings_terminated():
    for line in DASHBOARD_JS.split("\n"):
        code = _strip_complete_strings(line)
        assert '"' not in code, f"unterminated string: {line}"
        assert "'" not in code, f"unterminated string: {line}"


def test_clock_has_no_inline_style_override():
    # an inline style once silently beat the #clock rule, freezing it at 12px
    import re
    import trader.web.dashboard as dash

    m = re.search(r'<span id="clock"[^>]*>', dash.DASHBOARD_HTML)
    assert m and "style" not in m.group(0)
    css = re.search(r"#clock \{(.*?)\}", dash.DASHBOARD_CSS, re.S)
    assert css
    block = css.group(1)
    assert "font-size: 13px" in block
    assert "#7ee0ff" in block
    assert "text-shadow" in block


def test_header_rows_and_mobile_wrap():
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML + dash.DASHBOARD_CSS
    # row 1: title, mode badge, timebox (age + clock) pinned right
    row = re.search(r'<div class="headrow">(.*?)</div>', html, re.S)
    assert row
    row_html = row.group(1)
    for needle in ("<h1", 'id="mode"', 'id="timebox"', 'id="clock"'):
        assert needle in row_html, needle
    assert 'id="updated"' not in row_html
    assert 'href="/logout"' not in row_html
    # row 3: refreshed label + stops; settings and logout moved
    # to their own right-aligned row, logout in danger red
    assert '<span id="updated">data refreshed</span>' in html
    css = dash.DASHBOARD_CSS
    assert 'id="settings-btn" class="btn" onclick="openSettings()"' in html
    # every button on the site shares the .btn treatment
    assert ".btn {" in css
    assert 'id="levels-btn"' in html
    assert "justify-content:flex-end" in html
    assert "#logout {" in css
    assert "#f85149" in css
    assert 'id="logout"' in html
    assert 'id="logout" style="margin-left:auto"' not in html
    # refresh label renders before the first data cycle
    # the clock never drops to its own line - top-right on all screens
    assert "#timebox { order: 10" not in html


def test_pageshow_rechecks_auth_after_back_button():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    handler = js[js.index("pageshow"):js.index("pageshow") + 400]
    assert "/api/summary" in handler
    assert "/login" in handler


def test_settings_list_inputs_span_full_row():
    # narrow grid cells clipped the whitelist / skip-underlyings hints
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    css = dash.DASHBOARD_CSS
    assert '"set-ticker_whitelist"' in js
    assert '"set-skip_underlyings"' in js
    # full-row fields come from the shared class + css rule
    assert ".set-field.full" in css
    assert "grid-column: 1 / -1" in css


def test_login_page_rejects_injection():
    import trader.web.dashboard as dash

    rendered = dash.LOGIN_HTML('<script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_open_risk_percentage_colored_by_cap():
    # the open-risk text inherits the bar's warning colors (red over the
    # cap, yellow near it) and goes bold when exceeded
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert 'open risk \' + (hidden ? "••••••" : fmtMoney(risk)' in js
    assert "color:' + color" in js
    assert "font-weight:700" in js


def test_positions_table_shows_price_and_return():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "<th class=num>Avg $</th>" in js
    assert "<th class=num>Price $</th>" in js
    assert "<th class=num>Return</th>" in js
    assert "<th class=num>Total Cost $</th>" in js
    # contract cell stacks symbol + strike over the expiry
    assert "p.underlying" in js and "p.expiry" in js
    # avg shows the position total with per-unit in brackets
    assert "avgTotal" in js
    # price shows the quote with the signed market value
    assert "p.current_price" in js and "p.market_value" in js
    # return shows the dollar P&L stacked under the percentage
    assert "p.cost_usd - mv" in js and "plSpan" in js
    # details stack in a sub-line instead of widening the rows
    assert '.subv { display: block;' in dash.DASHBOARD_CSS
    assert "set-ws-positions" in js
    assert "set-ws-values" in js


def test_currency_split_display():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # account value gains a USD equivalent
    assert "usd_value" in js and "USD</span>" in js
    # cash per currency renders on its own card line
    assert "cash_cad" in js and "cash_usd" in js
    # cost shows USD with the CAD amount in brackets
    assert "cost_usd" in js and "cost_cad" in js


def test_long_text_wraps_and_webhooks_are_textareas():
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML + dash.DASHBOARD_CSS
    # alert entries wrap instead of overflowing the panel
    assert "white-space: normal" in html
    assert "word-break: break-word" in html
    assert "white-space: nowrap; color: var(--text)" not in html
    # webhook inputs are full-row wrapping textareas that auto-grow
    js = dash.DASHBOARD_JS
    css = dash.DASHBOARD_CSS
    assert 'textarea id="' in js
    assert "grid-column: 1 / -1" in css
    assert "function autoGrow(" in js


def test_git_status_wording():
    # "not checked yet" appeared twice (as result and as age); pulls say
    # "last auto/manual pull Xm ago" and the age is labeled "checked"
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert '"first check pending"' in js
    assert '"checked " + fmtAge' in js
    assert 'result === "not checked yet"' in js
    assert 's.last_pull.how === "auto" || s.last_pull.how === "manual"' in js
    assert '" · last " + how + "pull "' in js


def test_settings_form_not_clobbered_while_editing():
    # the 5s refresh re-rendered the settings form and wiped edits
    # before the user could save them
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "let settingsDirty = false;" in js
    assert "if (settingsDirty) return;" in js
    # edits mark the form dirty via the central helper
    assert "setSettingsDirty(true);" in js
    # a successful save clears it and re-renders with persisted values
    assert "setSettingsDirty(false);" in js
    assert "load();" in js
    # floating save/revert bar only shows while dirty
    assert "function revertSettings()" in js
    assert 'id="settings-float"' in dash.DASHBOARD_HTML
    assert 'id="settings-revert"' in dash.DASHBOARD_HTML


def test_currency_toggle():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert 'localStorage.getItem("ws_card_currency")' in js
    assert "function flipCardCurrency" in js
    assert "cur-toggle" in dash.DASHBOARD_JS
    # usd mode converts open risk with the derived fx rate
    assert "a.open_risk * fx" in js


def test_dte_badge_colors():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "function dteBadge(" in js
    assert '"#f85149"' in js      # 0dte red
    assert '"#db6d28"' in js      # 1dte orange
    assert '"#d29922"' in js      # 2-7dte yellow
    assert '"#3fb950"' in js      # 8+ dte green
    assert '"dte</span>"' in js
    # expiry sub-line carries the badge
    assert "dteBadge(p.expiry)" in js


def test_positions_table_always_fits_panel():
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML + dash.DASHBOARD_CSS
    # scroll on small screens, adaptive widths on wide ones
    assert "table-layout: auto" in html
    assert "@media (min-width: 641px)" in html
    assert "#positions, #stock-positions { overflow-x: visible; }" in html


def test_badges_do_not_wrap():
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML + dash.DASHBOARD_CSS
    assert "white-space: nowrap" in html
    assert "tag.mini" in html
    # the short and ws tags use the mini variant
    assert 'tag skip mini' in dash.DASHBOARD_JS
    assert 'tag ignored mini' in dash.DASHBOARD_JS


def test_utc_storage_rendered_locally():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # stored UTC gets a Z and renders via the browser clock mechanism
    assert 'if (!/[zZ+]/.test(s.slice(-6))) s += "Z";' in js
    assert "d.getHours()" in js and "d.getMinutes()" in js
    # ws expiry dates are ET trading dates - display date only
    assert 'String(p.expiry || "").slice(0, 10)' in js


def test_dual_timestamps_rendered():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # recent alerts show the alert time and when it was parsed down
    assert "parsed ' + fmtTime(s.received_ts)" in js
    assert "function fmtIso(" in js
    assert "d.getHours()" in js and "d.getMinutes()" in js


def test_stock_kind_cell():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # stocks render their own compact cell (symbol + stock label)
    assert 'p.kind === "stock"' in js
    assert '>stock</span>' in js


def test_stock_currency_display():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # stocks use multiplier 1 (not the 100x option multiplier) and
    # label usd amounts only - cad tickers need no suffix
    assert "isStock ? 1 : 100" in js
    assert 'isStock && p.currency === "USD" ? " usd"' in js


def test_stock_section_and_alloc_bar():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # stock holdings render into their own section
    assert 'id="stock-positions"' in dash.DASHBOARD_HTML
    assert 'rows.filter(r => r.kind === "stock")' in js
    # allocation bar: stocks blue, options purple
    assert "function allocBar" in js
    assert "#4493f8" in js and "#ab7df6" in js


def test_stock_holdings_hidden_by_default():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # holdings section is collapsed until the user opts in
    assert 'localStorage.getItem("ws_show_stocks") === "1"' in js
    assert "toggle-stocks" in dash.DASHBOARD_HTML


def test_hide_value_eye_toggle():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # per-card eye toggle masks value and cash amounts, persisted
    assert 'localStorage.getItem("ws_card_hidden")' in js
    assert "EYE_SVG" in js and "EYE_OFF_SVG" in js
    assert '"••••••"' in js
    assert "toggleCardHidden" in js
    # currency flip is per card too
    assert "flipCardCurrency" in js
    assert 'localStorage.getItem("ws_card_currency")' in js
    # hidden cash keeps the label but drops the amounts; negative
    # (loan) balances show as zero - the loan lives in margin used
    assert "fmtMoney(Math.max(0, a.cash_cad))" in js
    assert "fmtMoney(Math.max(0, a.cash_usd))" in js
    # approx symbol removed from value lines
    assert "≈" not in js


def test_margin_requirement_line():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "margin used" in js
    assert "margin available " in js
    assert "max buying power" in js
    assert 'esc(p.strategy_type || "multi-leg spread")' in js


def test_margin_lines_show_currencies():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert 'fmtMoney(a.margin_used_usd || 0)' in js
    assert ">total margin used<" in js
    assert 'fmtMoney(a.margin_requirement) + " cad"' in js
    assert 'fmtMoney(a.margin_available) + " cad"' in js
    assert 'fmtMoney(a.max_buying_power || 0) + " cad"' in js
    # per-currency loan breakdown follows the total
    assert "margin_used_usd" in js and "margin_used_cad" in js
    # risk carries its display currency
    assert '(showUsd ? "usd" : "cad")' in js


def test_avg_single_value_and_signed_cost():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # avg is one number: per-share for legs, per-contract for spreads
    assert 'Math.abs(p.avg_premium * 100)' in js
    assert "subv\">(\" + fmtSigned(p.avg_premium)" not in js
    # short option rows show the credit as a negative cost
    assert '(p.short ? "-" : "") + fmtMoney(p.cost_usd)' in js
    # short and spread tags coexist
    assert "short position" in js and "multi-leg spread" in js
    assert "cellbadges" in js


def test_portfolio_value_and_tappable_breakdown():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "portfolio value" in js
    assert "toggleMarginBreakdown" in js
    assert 'white-space:pre-line' in js


def test_open_risk_and_breakdown_masked():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert 'open risk \' + (hidden ? "••••••"' in js
    assert 'breakdownText(a.margin_breakdown, hidden)' in js
    assert 'no holdings' in js
    assert 'function breakdownText' in js


def test_margin_usage_bar():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "function marginUsageBar" in js
    assert 'pct >= 80 ? "#f85149" : pct >= 50 ? "#d29922" : "#3fb950"' in js
    assert "marginUsageBar(a);" in js


def test_section_toggles_and_paper_detail():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # alerts and trades sections collapse, persisted
    assert 'localStorage.getItem("ws_alerts_open") !== "0"' in js
    assert 'localStorage.getItem("ws_trades_open") !== "0"' in js
    assert "function toggleAlerts" in js and "function toggleTrades" in js
    # paper card expands with its holdings
    assert "function togglePaper" in js
    assert "data.paper_positions" in js
    assert "ws_paper_open" in js


def test_section_state_applied_on_load():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # persisted hidden state must apply at startup, not only after
    # the first toggle click
    assert "function applySectionVisibility" in js
    assert "applySectionVisibility();" in js


def test_paper_badge_and_holdings_hint():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert 'id="paper-badge"' in dash.DASHBOARD_HTML
    assert "badge.papersim" in js or "papersim" in dash.DASHBOARD_HTML
    assert 'data.paper ? "" : "none"' in js
    assert '"holdings "' in js


def test_paper_open_state_migration():
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    # old single-label values (pre-JSON-array) must not crash the
    # JSON parse
    assert '_po.startsWith("[")' in js
    assert "} catch (e) { paperOpen = []; }" in js


def test_no_duplicate_element_ids():
    """A partial block move once left an orphaned search bar
    with duplicate hs-* ids shadowing the real one - the page
    must never carry the same id twice."""
    import re

    import trader.web.dashboard as dash

    ids = re.findall(r'id="([a-zA-Z0-9_-]+)"', dash.DASHBOARD_HTML)
    assert len(ids) == len(set(ids)), (
        "duplicate ids: "
        + str(sorted({i for i in ids if ids.count(i) > 1}))
    )


def test_history_search_bar_is_single_and_last():
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML
    assert html.count('id="history-search"') == 1
    assert html.count('id="history-results"') == 1
    # it sits below the trade log, at the end of the page body
    assert html.index('id="trades"') < html.index('id="history-search"')
    # no search fragments before the trade log
    assert html.index('id="signals"') < html.index('id="trades"')
    between = html[html.index('id="signals"'):html.index('id="trades"')]
    assert "hs-q" not in between


def test_levels_spy_spot_and_refresh_cadence():
    """/api/spx carries spy's own realtime quote (it trades
    overnight) and the poll rides the positions refresh setting;
    the spy pane shows 'SPY now: x' under its title."""
    import trader.web.server as srv

    src = open(srv.__file__).read()
    assert '"spy": spy' in src
    assert "refresh_seconds" in src
    js = DASHBOARD_JS
    assert "levelsSpy = data.spy" in js
    assert 'levelsSpyStatus = data.spy_status' in js
    # the spy pane labels its quote now (live, incl. the
    # overnight session) or close (market closed) - a missing
    # status (moomoo feed) means live, not closed
    assert '"SPY " + tag + ": <b>"' in js
    assert 'levelsSpyStatus === "CLOSED" ? "close" : "now"' in js
    # the closed index shows its market close, not a live spot
    assert 'tag = levelsStale ? "close" : "now";' in js
    assert "derived): <b>" in js
    assert "data.refresh_seconds" in js


def test_levels_ladder_wide_screen_and_stack():
    """spx stays the left column; wide screens widen the panel,
    narrow screens stack spy under spx."""
    import trader.web.dashboard as dash

    css = dash.DASHBOARD_CSS
    assert "@media (min-width: 1100px)" in css
    assert "min(960px, 94vw)" in css
    assert "@media (max-width: 700px)" in css
    assert "grid-template-columns: 1fr" in css
    js = DASHBOARD_JS
    # spx is sorted into the left/top column regardless of
    # parse order
    assert 'views.sort(function(a, b) {' in js
    assert 'a === "SPX" ? -1' in js


def test_levels_ladder_spot_is_per_ticker():
    """the ladder marker takes an explicit spot - the spy pane
    uses spy's realtime quote, not a conversion of the spx
    global (which goes stale overnight)."""
    js = DASHBOARD_JS
    assert "function buildLevelsLadder(host, ticker, rows, pivot, headerHtml,\n                           spot, tag)" in js
    assert "const nowPrice = spot;" in js
    assert "spot = levelsSpy;" in js


def test_spx_endpoint_reports_market_status():
    """SPX closed shows the market close (stale), spy's
    overnight quote stays live; the poll cadence rides the
    positions refresh setting."""
    import types

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.wealthsimple.positions_refresh_seconds = 45
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class StubAccount:
        def _client(self):
            stub = self

            class FakeWS:
                def get_ticker_id(self, ticker, hint=None):
                    return "sec-" + ticker

                def get_security_quote(self, sec_id):
                    if sec_id.endswith("SPX"):
                        # overnight: the index itself is closed
                        return {
                            "price": "7683.69",
                            "close": "7683.69",
                            "marketStatus": "CLOSED",
                        }
                    # spy keeps trading overnight
                    return {
                        "price": "763.98",
                        "close": "765.61",
                        "marketStatus": "OVERNIGHT",
                    }

            return FakeWS()

    import tempfile
    import time as time_mod

    import trader.web.server as srv
    from trader.store import Store

    class _FreshStore:
        def __enter__(self):
            fd, path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            self._store = Store(path)
            return self._store

        def __exit__(self, *a):
            return False

    import trader.trading.quotes as quotes_mod
    saved = quotes_mod.ACTIVE_QUOTE_PROVIDER
    quotes_mod.ACTIVE_QUOTE_PROVIDER = None
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()
    try:
        with _FreshStore() as store:
            app = create_app(Stub(), store, None, None,
                             StubAccount())
            client = app.test_client()
            data = client.get(
                "/api/spx", headers={"X-Auth-Token": "t"},
            ).get_json()
    finally:
        quotes_mod.ACTIVE_QUOTE_PROVIDER = saved

    assert data["price"] == 7683.69
    assert data["stale"] is True      # closed index -> close tag
    assert data["status"] == "CLOSED"
    assert data["spy"] == 763.98      # spy's own overnight quote
    assert data["spy_status"] == "OVERNIGHT"
    assert data["refresh_seconds"] == 45


def test_spx_endpoint_survives_hung_ws_api(monkeypatch):
    """the ws library sends its requests without a timeout - a
    stalled connection must not pin the request thread until
    waitress runs out of workers (that once wedged the whole
    dashboard). the quote fetch is bounded and the endpoint
    answers with a fallback instead."""
    import tempfile
    import time as time_mod

    import trader.web.server as srv
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class HungAccount:
        def _client(self):
            class FakeWS:
                def get_ticker_id(self, ticker, hint=None):
                    return "sec-" + ticker

                def get_security_quote(self, sec_id):
                    time_mod.sleep(30)   # stalled connection
                    return {}

            return FakeWS()

    monkeypatch.setattr(srv, "_WS_QUOTE_TIMEOUT", 0.3)
    monkeypatch.setattr(srv, "_WS_QUOTE_RETRY", 5.0)
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import trader.trading.quotes as quotes_mod
    saved = quotes_mod.ACTIVE_QUOTE_PROVIDER
    quotes_mod.ACTIVE_QUOTE_PROVIDER = None
    try:
        app = create_app(Stub(), Store(path), None, None,
                         HungAccount())
        client = app.test_client()
        t0 = time_mod.time()
        data = client.get(
            "/api/spx", headers={"X-Auth-Token": "t"},
        ).get_json()
        elapsed = time_mod.time() - t0
    finally:
        quotes_mod.ACTIVE_QUOTE_PROVIDER = saved

    # the bounded fetch gave up well before the 30s stall
    assert elapsed < 5, elapsed
    assert data["spy"] is None
    assert "no spx spot" in (data["error"] or "")
    # the negative cache keeps the next poll fast instead of
    # stacking another hung worker
    t0 = time_mod.time()
    client.get("/api/spx", headers={"X-Auth-Token": "t"})
    assert time_mod.time() - t0 < 1


def test_dashboard_endpoint_survives_hung_ws_api(monkeypatch):
    """/api/dashboard aggregates several ws-touching sections -
    a stalled ws api (the library has no request timeout) must
    degrade each section to its error/empty shape within the
    bound instead of pinning the waitress worker forever (that
    wedge left every dashboard fetch pending)."""
    import tempfile
    import time as time_mod

    import trader.web.server as srv
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class HungAccount:
        def values(self):
            time_mod.sleep(30)
            return {}

        def open_option_positions(self):
            time_mod.sleep(30)
            return {}

        def stock_holdings(self):
            time_mod.sleep(30)
            return {}

    monkeypatch.setattr(srv, "_WS_QUOTE_TIMEOUT", 0.3)
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()
    srv._section_cache.clear()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(Stub(), Store(path), None, None, HungAccount())
    client = app.test_client()

    t0 = time_mod.time()
    data = client.get(
        "/api/dashboard", headers={"X-Auth-Token": "t"},
    ).get_json()
    elapsed = time_mod.time() - t0

    assert elapsed < 5, elapsed
    assert "timed out" in (data["summary"].get("error") or "")
    assert data["positions"] == []
    # the non-ws sections still render
    assert data["me"] is not None or "me" in data
    assert "settings" in data and "signals" in data


def test_dashboard_sections_serve_stale_cache(monkeypatch):
    """stale-while-revalidate: once a section has a good payload,
    a slow or stalled ws api serves the cached one instead of
    degrading - the dashboard keeps rendering through ws outages."""
    import tempfile
    import time as time_mod

    import trader.web.server as srv
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class GoodAccount:
        def values(self):
            return {}

        def open_option_positions(self):
            return {}

        def stock_holdings(self):
            return {}

    class HungAccount:
        def values(self):
            time_mod.sleep(30)
            return {}

        def open_option_positions(self):
            time_mod.sleep(30)
            return {}

        def stock_holdings(self):
            time_mod.sleep(30)
            return {}

    monkeypatch.setattr(srv, "_WS_QUOTE_TIMEOUT", 0.3)
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()
    srv._section_cache.clear()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # a working api populates the section cache
    app = create_app(Stub(), Store(path), None, None, GoodAccount())
    client = app.test_client()
    data = client.get(
        "/api/dashboard", headers={"X-Auth-Token": "t"},
    ).get_json()
    assert "error" not in (data["summary"] or {})
    assert "summary" in srv._section_cache

    # the api stalls: the cached summary still serves
    app2 = create_app(
        Stub(), Store(path), None, None, HungAccount()
    )
    client2 = app2.test_client()
    t0 = time_mod.time()
    data2 = client2.get(
        "/api/dashboard", headers={"X-Auth-Token": "t"},
    ).get_json()
    assert time_mod.time() - t0 < 5
    assert "error" not in (data2["summary"] or {})


def test_ws_http_shim_injects_timeout(monkeypatch):
    """every wealthsimple request must carry a hard timeout -
    the client library sends none, and a stalled connection
    once pinned the whole waitress pool."""
    import trader.ws.ws_http as ws_http
    from wealthsimple_python import client as client_mod

    seen = {}

    def fake_post(url, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return "ok"

    # install() captures requests.post from this module at call
    # time - patch it so the wrapper wraps the recorder
    monkeypatch.setattr(ws_http._requests, "post", fake_post)
    saved = client_mod.requests
    try:
        ws_http._installed = False
        ws_http.install()
        client_mod.requests.post("http://x")
        assert seen["timeout"] == ws_http.WS_HTTP_TIMEOUT
        # an explicit timeout is preserved
        client_mod.requests.post("http://x", timeout=3)
        assert seen["timeout"] == 3
        # idempotent
        ws_http.install()
    finally:
        client_mod.requests = saved
        ws_http._installed = False


def test_spx_endpoint_prefers_moomoo_spy(monkeypatch):
    """moomoo opend is local and fast - the spy ladder spot rides
    it when available and the slow ws quote api is not consulted
    (moomoo carries no market status: the ui treats it as live)."""
    import tempfile
    import types

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class StubProvider:
        _index_ts = 0.0
        _index_error = None

        def index_quote(self, symbol="SPX"):
            return 7683.69

        def stock_quote(self, symbol):
            assert symbol == "SPY"
            return 764.85

    class SentinelAccount:
        """the ws quote api must not be reached when moomoo
        quotes spy"""

        def _client(self):
            class FakeWS:
                def get_ticker_id(self, ticker, hint=None):
                    return "sec-" + ticker

                def get_security_quote(self, sec_id):
                    return {"price": "999.99",
                            "marketStatus": "CLOSED"}

            return FakeWS()

    import trader.trading.quotes as quotes_mod
    import trader.web.server as srv
    saved = quotes_mod.ACTIVE_QUOTE_PROVIDER
    quotes_mod.ACTIVE_QUOTE_PROVIDER = StubProvider()
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()
    try:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(
            Stub(), Store(path), None, None, SentinelAccount()
        )
        client = app.test_client()
        data = client.get(
            "/api/spx", headers={"X-Auth-Token": "t"},
        ).get_json()
    finally:
        quotes_mod.ACTIVE_QUOTE_PROVIDER = saved

    assert data["price"] == 7683.69     # moomoo index quote
    assert data["spy"] == 764.85        # moomoo stock quote
    assert data["spy_status"] is None   # no status -> live tag
    assert data["error"] is None


def test_spx_proxy_only_as_last_resort(monkeypatch):
    """the moomoo spx value on this build is the spy etf x the
    converter ratio - not the index itself. overnight the close
    is wanted, so the ws quote (which also carries the market
    status) wins and the proxy only answers when ws cannot."""
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    class ProxyProvider:
        _index_ts = 0.0
        _index_error = None
        _index_proxy = True

        def index_quote(self, symbol="SPX"):
            return 7686.4          # spy x 10.0391

        def stock_quote(self, symbol):
            return 764.85

    class WSAccount:
        def _client(self):
            class FakeWS:
                def get_ticker_id(self, ticker, hint=None):
                    return "sec-" + ticker

                def get_security_quote(self, sec_id):
                    if sec_id.endswith("SPX"):
                        return {"price": "7683.69",
                                "marketStatus": "CLOSED"}
                    return {"price": "1.00",
                            "marketStatus": "CLOSED"}

            return FakeWS()

    import trader.trading.quotes as quotes_mod
    import trader.web.server as srv
    saved = quotes_mod.ACTIVE_QUOTE_PROVIDER
    quotes_mod.ACTIVE_QUOTE_PROVIDER = ProxyProvider()
    srv._sec_id_cache.clear()
    srv._ws_quote_cache.clear()
    srv._ws_quote_fail_ts.clear()
    try:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(Stub(), Store(path), None, None, WSAccount())
        client = app.test_client()
        data = client.get(
            "/api/spx", headers={"X-Auth-Token": "t"},
        ).get_json()
    finally:
        quotes_mod.ACTIVE_QUOTE_PROVIDER = saved

    # the accurate ws index quote wins over the spy-derived proxy
    assert data["price"] == 7683.69
    assert data["status"] == "CLOSED"
    assert data["stale"] is True
    assert data["spy"] == 764.85


def test_no_eval_or_string_timers_in_shipped_pages():
    """the csp 'eval' devtools warning must never come from our
    page: no eval, no new Function, no string-form setTimeout /
    setInterval anywhere in the shipped html or js (the ones the
    browser reports come from browser extensions, which inject
    their own scripts and run under their own csp)."""
    import re

    import trader.web.dashboard as dash

    js = DASHBOARD_JS
    assert "eval(" not in js
    assert "new Function" not in js
    assert "Function(" not in js
    # string-form timers: setTimeout('...'/'...') / setInterval
    for m in re.finditer(
        r"(setTimeout|setInterval)\s*\(\s*['\"`]", js
    ):
        raise AssertionError(f"string timer in dashboard.js: {m.group(0)}")
    for html in (
        dash.DASHBOARD_HTML,
        dash.LOGIN_HTML() if callable(getattr(dash, "LOGIN_HTML", None))
        else str(getattr(dash, "LOGIN_HTML", "")),
    ):
        assert "eval(" not in html
        assert "new Function" not in html
        # inline event handlers and the single external script
        # are fine - string evaluation is not
        assert "javascript:" not in html

def test_lighthouse_render_and_cls_findings():
    """/static/dashboard.js was render-blocking (750ms of the
    lighthouse critical path) and the empty-to-filled tables
    shifted the page (CLS 0.536). the script is deferred, the
    stylesheet is inlined, both logs are capped scroll boxes, and
    the page carries a main landmark."""
    assert '<script src="/static/dashboard.js" defer></script>' in DASHBOARD_HTML
    # capped blocks: both logs scroll inside a max-height box -
    # short days render compact (no blank space under the last
    # row) and tall days cap out and scroll
    assert "#signals { max-height: min(760px, 92vh)" in DASHBOARD_CSS
    assert "#trades { max-height: min(880px, 92vh)" in DASHBOARD_CSS
    assert "min-height: 380px" not in DASHBOARD_CSS
    assert "overflow-y: auto" in DASHBOARD_CSS
    # the previous session's payload paints on first load: the
    # first data arrival cannot shift anything
    assert "dash_last_payload" in DASHBOARD_JS
    assert "applyDashboard(saved)" in DASHBOARD_JS
    # the header spans reserve their width so late text does not
    # jitter the header row
    assert ".headrow #stops, .headrow #reader" in DASHBOARD_CSS
    assert "min-width: 200px" in DASHBOARD_CSS
    # accessibility: a primary landmark for screen readers
    assert "<main>" in DASHBOARD_HTML and "</main>" in DASHBOARD_HTML
    # accessibility: the page carries a main landmark
    assert DASHBOARD_HTML.count("<main>") == 1
    assert DASHBOARD_HTML.index("<main>") < DASHBOARD_HTML.index(
        'id="accounts"'
    ) < DASHBOARD_HTML.index('id="levelsBackdrop"')


def test_dashboard_inlines_the_stylesheet():
    """the render-blocking css request is gone: the dashboard
    page ships the stylesheet inline in the head, so the
    reserved layout paints before the data arrives."""
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(Stub(), Store(path), None, None, None)
    client = app.test_client()
    r = client.get("/", headers={"X-Auth-Token": "t"})
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "<style>" in html
    assert "height: min(880px, 92vh)" in html
    assert '<link rel="stylesheet"' not in html
    # the composition is cached per process - second load equals
    assert app.test_client().get(
        "/", headers={"X-Auth-Token": "t"}
    ).data == r.data


def test_text_responses_ship_gzipped():
    """lighthouse flagged 'no compression applied' - text
    payloads (html/css/js/json) must ship gzipped when the
    client accepts it, and untouched otherwise."""
    import gzip as gzip_mod
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(Stub(), Store(path), None, None, None)
    client = app.test_client()
    hdr = {"X-Auth-Token": "t", "Accept-Encoding": "gzip"}

    r = client.get("/static/dashboard.js", headers=hdr)
    assert r.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in (r.headers.get("Vary") or "")
    body = gzip_mod.decompress(r.data)
    assert body.startswith(b"/*") or b"function" in body[:2000]

    # a client without gzip support gets plain bytes
    r2 = client.get(
        "/static/dashboard.js", headers={"X-Auth-Token": "t"},
    )
    assert r2.headers.get("Content-Encoding") is None
    assert r2.data == body




def _paper_app(store):
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    return create_app(Stub(), store, None, None, None)


def test_manual_paper_sell_endpoint():
    """the sell closes the position at the live price (the avg
    premium when no quote is up), returns the proceeds to the
    paper cash, realizes the row's pnl and logs the trade - the
    same path an alert sell takes."""
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.trading.parser import parse_alert
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    alert = parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0")
    ck = alert.contract_key()
    store.apply_position(
        "paper", alert, 4, premium=2.0, account="RRSP"
    )
    store.set_paper_equity(1000.0, "RRSP")

    app = create_app(Stub(), store, None, None, None)
    client = app.test_client()
    hdr = {"X-Auth-Token": "t"}
    r = client.post(
        "/api/paper-sell", headers=hdr,
        json={"label": "RRSP", "contract_key": ck},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["sold"] == 4
    assert data["price"] == 2.0     # avg fallback (no live quote)
    assert data["realized"] == 0.0
    assert data["remaining"] == 0
    # the cached dashboard sections were dropped: the next poll
    # shows the position gone instead of a stale pre-sell copy
    import trader.web.server as srv
    assert "paper" not in srv._section_cache
    assert "positions" not in srv._section_cache
    assert store.get_position("paper", ck, "RRSP") == 0
    # proceeds: 4x $2.00 x100 = $800 cad back to the cash
    assert abs(store.paper_equity("RRSP") - 1800.0) < 0.01
    sells = [t for t in store.recent_trades(10)
             if t["mode"] == "paper" and t["action"] == "SELL"
             and "manual SELL" in (t["detail"] or "")]
    assert len(sells_of(store)) == 1
    row = [p for p in store.list_positions("paper", "RRSP")
           if p["contract_key"] == ck]
    assert not row or int(row[0]["qty"]) == 0


def sells_of(store):
    return [t for t in store.recent_trades(20)
            if t.get("action") == "SELL"
            and "manual SELL" in (t.get("detail") or "")]


def test_manual_paper_sell_partial_and_missing():
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.trading.parser import parse_alert
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    app = create_app(Stub(), store, None, None, None)
    client = app.test_client()
    hdr = {"X-Auth-Token": "t", "Content-Type": "application/json"}
    alert = parse_alert("BOUGHT 10/02 DRAM 60c @ 1.6")
    ck = alert.contract_key()
    store.apply_position(
        "paper", alert, 2, premium=1.6, account="default"
    )

    # partial sell on the right account
    r = client.post(
        "/api/paper-sell", headers=hdr,
        json={"label": "default", "contract_key": ck, "qty": 1},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["sold"] == 1
    assert data["remaining"] == 1
    assert store.get_position("paper", ck, "default") == 1

    # unknown contract -> 404
    r2 = client.post(
        "/api/paper-sell", headers=hdr,
        json={"label": "default",
              "contract_key": "NOPE-2026-01-01-1-C"},
    )
    assert r2.status_code == 404


def test_paper_manual_sell_ui():
    """paper positions carry a per-row sell button (admin only)
    with the confirm-modal pattern the other paper actions use."""
    js = DASHBOARD_JS
    assert "function sellPaper(label, key, qty, price)" in js
    assert '"/api/paper-sell"' in js
    assert "contract_key: key" in js
    # the sell button rides the holdings table (admin only)
    assert "Manual paper sell" in js
    # the success modal confirms the fill immediately (the row
    # lingers on the next poll otherwise and looks unfilled)
    assert '"Sold"' in js and "data.realized" in js
    # the sell price lookup is bounded so the post answers fast
    import trader.web.server as srv
    assert "_bounded(_paper_positions_payload, ctx)" in open(srv.__file__).read()


def test_manual_paper_sell_live_price_and_fx():
    """with a ledger present the sell books at the live quote and
    converts the proceeds at the ledger fx; selling again after a
    full close reports the position as gone."""
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.trading.parser import parse_alert
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    from trader.trading.parser import parse_alert

    alert = parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0")
    ck = alert.contract_key()

    class FakeLedger:
        """stands in for the PaperLedger: the holdings quote and
        the fx rate the sell books against"""

        def values(self):
            return {"RRSP": 1000.0}

        def positions(self, label):
            return [{
                "contract_key": ck,
                "price": 3.0, "usd": True,
            }]

        def fx(self):
            return 1.35

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    store.apply_position(
        "paper", alert, 4, premium=2.0, account="RRSP"
    )
    store.set_paper_equity(1000.0, "RRSP")

    executor_stub = types.SimpleNamespace(account=FakeLedger())
    app = create_app(Stub(), store, None, executor=types.SimpleNamespace(
        account=FakeLedger()
    ), account=None)
    client = app.test_client()
    hdr = {"X-Auth-Token": "t"}
    r = client.post(
        "/api/paper-sell", headers=hdr,
        json={"label": "RRSP", "contract_key": ck},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["price"] == 3.0                      # the live quote
    assert data["realized"] == 400.0                 # 4x(2.5-2.0)x100
    # proceeds 4x$3.00x100 = $1000 usd -> cad at the ledger fx
    assert abs(store.paper_equity("RRSP") - (1000 + 1620)) < 0.01
    assert data["remaining"] == 0

    # selling the same contract again: it is already gone
    r2 = client.post(
        "/api/paper-sell", headers=hdr,
        json={"label": "RRSP", "contract_key": ck},
    )
    assert r2.status_code == 404
    assert "no paper position" in r2.get_json()["error"]


def test_paper_reset_invalidates_sections():
    """after a paper reset the dashboard's cached sections are
    dropped - the next poll shows the reset ledger instead of a
    stale pre-reset copy (that lag made the reset button look
    like it did nothing)."""
    import tempfile

    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.store import Store
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    class StubAccount:
        def values(self):
            return {"RRSP": 5000.0}

        def _positions_raw(self):
            return {}

        def _resolve(self):
            return ()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(Stub(), Store(path), None, None, StubAccount())
    client = app.test_client()
    hdr = {"X-Auth-Token": "t"}
    # prime the section cache with a dashboard poll
    client.get("/api/dashboard", headers=hdr)
    assert "paper" in srv_section_cache()
    r = client.post(
        "/api/paper-reset", json={"label": "RRSP"}, headers=hdr,
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "paper" not in srv_section_cache()
    assert "summary" not in srv_section_cache()


def srv_section_cache():
    import trader.web.server as srv

    return srv._section_cache


def test_settings_layout_mirror_and_automation():
    """the mirror real fills fields live inside the automation
    section (one "keeps itself in sync" section) and the
    automation section sits right above the reader."""
    import trader.web.dashboard as dash

    js = DASHBOARD_JS
    # automation appears exactly once and the mirror fields are
    # folded into it - there is no standalone mirror section
    assert js.count('_section("automation"') == 1
    assert '_section("mirror real fills"' not in js
    # automation sits directly above reader
    assert js.index('_section("automation"') < js.index(
        '_section("reader"'
    )
    a_start = js.index('_section("automation"')
    a_end = js.index('_section("reader"')
    # the mirror checkbox/interval ride the automation section
    assert "set-paper-mirror" in js[a_start:a_end]
    assert "set-mirror-interval" in js[a_start:a_end]
    # accounts: the last section in the form
    assert js.index('_section("accounts"') > js.index(
        '_section("discord webhooks'
    )


def test_settings_layout_0dte_control_section():
    """the quick controls and the 0dte day-of-expiry controls
    (auto b2e sell + lotto budget) are folded into the automation
    section; the size tiers (whose per-tier b2e override feeds
    the 0dte controls) sit directly under it."""
    import trader.web.dashboard as dash

    js = DASHBOARD_JS
    # no standalone quick controls / 0dte sections
    assert '_section("quick controls"' not in js
    assert '_section("0dte control"' not in js
    assert js.count('_section("automation"') == 1
    # the quick + 0dte fields live inside the automation section
    a_start = js.index('_section("automation"')
    a_end = js.index('_section("size tiers"')
    for fid in ("set-notify", "set-paper-enabled",
                "set-back_to_entry_enabled", "set-lotto_gain_budget_pct",
                "set-risk_per_trade_pct", "set-paper-mirror",
                "set-au-enabled"):
        assert fid in js[a_start:a_end]
    # size tiers directly under automation, option tiers labelled
    assert a_end < js.index('_section("trading limits"')
    assert "option tiers - risk % cap / min / max contracts" in js


def test_settings_automation_subsections():
    """the automation section groups its fields into titled
    sub-sections: mirror fills, 0dte, global risk cap, quotes
    provider, github code update and account value monitoring."""
    import trader.web.dashboard as dash

    js = DASHBOARD_JS
    # renderSettings declares its accumulator before the first
    # append: a bare `html +=` throws (undeclared variable) and
    # leaves the settings popup blank
    assert 'let html = _section("automation"' in js
    fn = js[js.index("function renderSettings("):js.index(
        "async function saveSettings"
    )]
    assert fn.index("let html =") < fn.index("html +=")
    # the six titled sub-sections, in order
    a_start = js.index('_section("automation"')
    a_end = js.index('_section("size tiers"')
    auto = js[a_start:a_end]
    subs = [
        "_subsection(\"mirror fills\"",
        "_subsection(\"0dte\"",
        "_subsection(\"global risk cap\"",
        "_subsection(\"quotes provider\"",
        "_subsection(\"github code update\"",
        "_subsection(\"account value monitoring\"",
    ]
    assert [auto.index(s) for s in subs] == sorted(
        auto.index(s) for s in subs
    )
    # the fields ride their own sub-section: each field id appears
    # between its sub's title and the next sub's title
    spans = []
    for i, s in enumerate(subs):
        start = auto.index(s)
        end = auto.index(subs[i + 1]) if i + 1 < len(subs) else len(auto)
        spans.append(auto[start:end])
    assert "set-paper-mirror" in spans[0]
    assert "set-mirror-interval" in spans[0]
    assert "set-back_to_entry_enabled" in spans[1]
    assert "set-lotto_gain_budget_pct" in spans[1]
    for fid in ("set-risk_per_trade_pct", "set-max_contracts_per_trade",
                "set-max_open_risk_pct", "set-stop_loss_pct",
                "set-trailing_stop_pct"):
        assert fid in spans[2]
    for fid in ("set-quotes-provider", "set-quotes-moomoo_host",
                "set-quotes-moomoo_port", "set-quotes-enabled"):
        assert fid in spans[3]
    for fid in ("set-au-enabled", "set-au-interval"):
        assert fid in spans[4]
    for fid in ("set-ws-positions", "set-ws-values",
                "set-ws-margin-rate"):
        assert fid in spans[5]


def test_unified_button_and_section_styles():
    """every control shares one visual language: the .btn class
    (neutral dark, #8b949e hover) with .danger and .sm variants,
    the h2 rows use the .h2row flex helper, and no button carries
    a duplicated inline background style."""
    import trader.web.dashboard as dash

    html = dash.DASHBOARD_HTML
    js = dash.DASHBOARD_JS
    css = dash.DASHBOARD_CSS
    # the shared classes exist
    assert ".btn {" in css
    assert ".btn.danger {" in css
    assert ".btn.sm {" in css
    assert ".h2row {" in css
    assert ".mini-toggle.danger {" in css
    # the html uses them: no button carries an inline background
    assert 'style="background:#21262d' not in html
    assert 'style="background:#da3633' not in html
    # the section toggle buttons ride .btn
    for bid in ("toggle-stocks", "toggle-alerts", "toggle-ignored",
                "toggle-trades"):
        assert f'id="{bid}" class="btn"' in html
    # the modal buttons are cancel (neutral) + go (danger)
    assert '<button onclick="closeModal()" class="btn">' in html
    assert 'id="mGo" class="btn danger"' in html
    # the js-rendered controls share the same classes
    assert 'class="btn sm" onclick="historyNav' in js
    assert 'class="mini-toggle danger"' in js
    # the cur-toggle lost its per-instance inline overrides
    assert 'cur-toggle" style=' not in js
    # dead levels ladder toggle styles are gone
    assert ".lv-toggle" not in css


def test_real_card_today_gain_above_risk_bar():
    """the real account card shows today's realized gain above
    the open risk bar, read from the mode-aware ledger number
    (live ledger in live mode, paper ledger otherwise) so the
    card and the lotto budget always agree."""
    import trader.web.dashboard as dash

    js = DASHBOARD_JS
    # the real card reads the mode-aware realized_today, falling
    # back to the paper number for cached payloads
    assert "const todayGain = a.realized_today != null" in js
    assert "a.paper_realized_today" in js
    # the today row sits before the riskbar in the real card
    card = js[js.index("card.innerHTML ="):js.index(
        "function paperAllocBar"
    )]
    today_at = card.index('">today ')
    assert today_at < card.index('<div class="riskbar"')
    assert today_at < card.index("open risk")
