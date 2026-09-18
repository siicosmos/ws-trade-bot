import re

from trader.dashboard import DASHBOARD_HTML


def _strip_complete_strings(line):
    code = re.sub(r"(?<!:)//.*", "", line)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "", code)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', "", code)
    return code


def test_dashboard_script_strings_terminated():
    scripts = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S)
    assert scripts
    for js in scripts:
        for line in js.split("\n"):
            code = _strip_complete_strings(line)
            assert '"' not in code, f"unterminated string: {line}"
            assert "'" not in code, f"unterminated string: {line}"


def test_clock_has_no_inline_style_override():
    # an inline style once silently beat the #clock rule, freezing it at 12px
    import re
    import trader.dashboard as dash

    m = re.search(r'<span id="clock"[^>]*>', dash.DASHBOARD_HTML)
    assert m and "style" not in m.group(0)
    css = re.search(r"#clock \{(.*?)\}", dash.DASHBOARD_HTML, re.S)
    assert css
    block = css.group(1)
    assert "font-size: 13px" in block
    assert "#7ee0ff" in block
    assert "text-shadow" in block


def test_header_rows_and_mobile_wrap():
    import trader.dashboard as dash

    html = dash.DASHBOARD_HTML
    # row 1: title, mode badge, timebox (age + clock) pinned right
    row = re.search(r'<div class="headrow">(.*?)</div>', html, re.S)
    assert row
    row_html = row.group(1)
    for needle in ("<h1", 'id="mode"', 'id="timebox"', 'id="clock"'):
        assert needle in row_html, needle
    assert 'id="updated"' not in row_html
    assert 'href="/logout"' not in row_html
    # row 2: refreshed label rides the watching line, logout at corner
    assert '<span id="updated">data refreshed</span>' in html
    assert 'id="logout" style="margin-left:auto"' in html
    assert 'id="logout"' in html
    # refresh label renders before the first data cycle
    # the clock never drops to its own line - top-right on all screens
    assert "#timebox { order: 10" not in html


def test_pageshow_rechecks_auth_after_back_button():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                   dash.DASHBOARD_HTML, re.S)[0]
    handler = js[js.index("pageshow"):js.index("pageshow") + 400]
    assert "/api/summary" in handler
    assert "/login" in handler


def test_settings_list_inputs_span_full_row():
    # narrow grid cells clipped the whitelist / skip-underlyings hints
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                   dash.DASHBOARD_HTML, re.S)[0]
    assert '["ticker_whitelist"' in js
    assert "grid-column:1/-1" in js


def test_login_page_rejects_injection():
    import trader.dashboard as dash

    rendered = dash.LOGIN_HTML('<script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_open_risk_percentage_colored_by_cap():
    # the open-risk text inherits the bar's warning colors (red over the
    # cap, yellow near it) and goes bold when exceeded
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    assert "open risk ' + fmtMoney(risk)" in js
    assert "color:' + color" in js
    assert "font-weight:700" in js


def test_positions_table_shows_price_and_return():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
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
    assert '.subv { display: block;' in dash.DASHBOARD_HTML
    assert "set-ws-positions" in js
    assert "set-ws-values" in js


def test_currency_split_display():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    # account value gains a USD equivalent
    assert "usd_value" in js and "USD</span>" in js
    # usd cash replaces the cap text when live data is present
    assert "usd_cash" in js
    # cost shows USD with the CAD amount in brackets
    assert "cost_usd" in js and "cost_cad" in js


def test_long_text_wraps_and_webhooks_are_textareas():
    import trader.dashboard as dash

    html = dash.DASHBOARD_HTML
    # alert entries wrap instead of overflowing the panel
    assert "white-space: normal" in html
    assert "word-break: break-word" in html
    assert "white-space: nowrap; color: var(--text)" not in html
    # webhook inputs are full-row wrapping textareas that auto-grow
    js = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
    assert 'textarea id="' in js
    assert "grid-column:1/-1" in js
    assert "function autoGrow(" in js


def test_git_status_wording():
    # "not checked yet" appeared twice (as result and as age); pulls say
    # "last auto/manual pull Xm ago" and the age is labeled "checked"
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>", dash.DASHBOARD_HTML, re.S)[0]
    assert '"first check pending"' in js
    assert '"checked " + fmtAge' in js
    assert 'result === "not checked yet"' in js
    assert 's.last_pull.how === "auto" || s.last_pull.how === "manual"' in js
    assert '" · last " + how + "pull "' in js


def test_settings_form_not_clobbered_while_editing():
    # the 5s refresh re-rendered the settings form and wiped edits
    # before the user could save them
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>", dash.DASHBOARD_HTML, re.S)[0]
    assert "let settingsDirty = false;" in js
    assert "if (settingsDirty) return;" in js
    # edits mark the form dirty via the central helper
    assert "setSettingsDirty(true);" in js
    # a successful save clears it and re-renders with persisted values
    assert "setSettingsDirty(false);" in js
    assert "await loadSettings();" in js
    # floating save/revert bar only shows while dirty
    assert "function revertSettings()" in js
    assert 'id="settings-float"' in dash.DASHBOARD_HTML
    assert 'id="settings-revert"' in dash.DASHBOARD_HTML


def test_currency_toggle():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    assert 'let valueCurrency = localStorage.getItem' in js
    assert "function toggleValueCurrency()" in js
    assert "cur-toggle" in dash.DASHBOARD_HTML
    # usd mode converts open risk with the derived fx rate
    assert "a.open_risk * fx" in js


def test_dte_badge_colors():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    assert "function dteBadge(" in js
    assert '"#f85149"' in js      # 0dte red
    assert '"#db6d28"' in js      # 1dte orange
    assert '"#d29922"' in js      # 2-7dte yellow
    assert '"#3fb950"' in js      # 8+ dte green
    assert '"dte</span>"' in js
    # expiry sub-line carries the badge
    assert "dteBadge(p.expiry)" in js


def test_positions_table_always_fits_panel():
    import trader.dashboard as dash

    html = dash.DASHBOARD_HTML
    # fixed layout makes the table mathematically unable to exceed
    # its container, and the panels got wider
    assert "table-layout: fixed" in html
    assert 'class="pos"' in html
    assert "max-width: 1400px" in html


def test_badges_do_not_wrap():
    import trader.dashboard as dash

    html = dash.DASHBOARD_HTML
    assert "white-space: nowrap" in html
    assert "tag.mini" in html
    # the short and ws tags use the mini variant
    assert 'tag skip mini' in html
    assert 'tag ignored mini' in html


def test_utc_storage_rendered_locally():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    # stored UTC gets a Z and renders via the browser clock mechanism
    assert 'if (!/[zZ+]/.test(s.slice(-6))) s += "Z";' in js
    assert "d.getHours()" in js and "d.getMinutes()" in js
    # ws expiry dates are ET trading dates - display date only
    assert 'String(p.expiry || "").slice(0, 10)' in js


def test_dual_timestamps_rendered():
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                    dash.DASHBOARD_HTML, re.S)[0]
    # recent alerts show the alert time and when it was parsed down
    assert "parsed ' + fmtTime(s.received_ts)" in js
    assert "function fmtIso(" in js
    assert "d.getHours()" in js and "d.getMinutes()" in js
