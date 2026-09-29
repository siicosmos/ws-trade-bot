import os
import re

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
    assert 'id="settings-btn" onclick="openSettings()"' in html
    # the header buttons share one styled rule
    assert "#settings-btn, #users-btn, #levels-btn {" in css
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
    # overnight session) or close (market closed)
    assert '"SPY " + tag + ": <b>"' in js
    assert 'levelsSpyStatus !== "CLOSED"' in js
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
