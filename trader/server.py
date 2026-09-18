import hmac
import logging
import os
import secrets
import time
from datetime import timedelta

from flask import Flask, Response, jsonify, redirect, request, session

from .dashboard import DASHBOARD_HTML, LOGIN_HTML
from .pipeline import process_alert
from .store import Store

QUIET_GET_PATHS = (
    "/",
    "/health",
    "/favicon.ico",
    "/api/summary",
    "/api/positions",
    "/api/signals",
    "/api/trades",
    "/api/settings",
    "/api/reader_status",
    "/api/update_status",
)

QUIET_POST_PATHS = (
    "/api/reader_status",
)


QUIET_GET_PREFIXES = (
    "/.well-known/",
)


class QuietPathsFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for path in QUIET_POST_PATHS:
            if f" {path} HTTP" in msg:
                return False
        for path in QUIET_GET_PATHS:
            if f"GET {path} HTTP" in msg:
                return False
        for prefix in QUIET_GET_PREFIXES:
            if f"GET {prefix}" in msg:
                return False
        return True


def install_quiet_filter():
    werkzeug = logging.getLogger("werkzeug")
    if not any(
        isinstance(f, QuietPathsFilter) for f in werkzeug.filters
    ):
        werkzeug.addFilter(QuietPathsFilter())


LOGIN_FAIL_LIMIT = 5
LOCKOUT_SECONDS = 900
_LOGIN_FAILS = {}


def _load_secret_key(config_path):
    if not config_path:
        return secrets.token_hex(32)
    key_file = os.path.join(
        os.path.dirname(os.path.abspath(config_path)), ".session_key"
    )
    try:
        with open(key_file) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w") as f:
            f.write(key)
    except OSError:
        pass
    return key


def create_app(cfg, store: Store, risk, executor, account=None,
                 config_path=None) -> Flask:
    app = Flask(__name__)
    install_quiet_filter()
    app.secret_key = _load_secret_key(config_path)
    app.permanent_session_lifetime = timedelta(days=30)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if getattr(cfg.pipeline, "tls_cert", "") and getattr(
        cfg.pipeline, "tls_key", ""
    ):
        app.config["SESSION_COOKIE_SECURE"] = True
    mode = cfg.trading.mode
    app.reader_state = {
        "channel": None,
        "ok": False,
        "last_seen": None,
    }

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if not token or request.path in ("/health", "/favicon.ico", "/login"):
            return None
        if session.get("auth"):
            return None
        supplied = request.headers.get("X-Auth-Token", "")
        if supplied and hmac.compare_digest(supplied, token):
            return None
        if request.path.startswith("/api/") or request.path == "/alert":
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        token = cfg.pipeline.auth_token
        if not token:
            return redirect("/")
        ip = request.remote_addr or "?"
        now = time.time()
        entry = _LOGIN_FAILS.get(ip)
        if entry and entry.get("locked_until", 0) > now:
            return Response(
                LOGIN_HTML("too many attempts - try again later"),
                403,
                mimetype="text/html",
                headers={"Cache-Control": "no-store"},
            )
        error = None
        if request.method == "POST":
            supplied = request.form.get("password", "")
            if supplied and hmac.compare_digest(supplied, token):
                _LOGIN_FAILS.pop(ip, None)
                session.permanent = True
                session["auth"] = True
                return redirect("/")
            count = (entry or {}).get("count", 0) + 1
            if count >= LOGIN_FAIL_LIMIT:
                _LOGIN_FAILS[ip] = {
                    "count": count,
                    "locked_until": now + LOCKOUT_SECONDS,
                }
                error = "too many attempts - try again later"
            else:
                _LOGIN_FAILS[ip] = {"count": count, "locked_until": 0}
                error = "wrong access token"
            time.sleep(1)
        return Response(
            LOGIN_HTML(error), mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.after_request
    def no_store(resp):
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/")
    def dashboard_page():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    def _real_positions():
        if account is None or not hasattr(
            account, "open_option_positions"
        ):
            return None
        try:
            return account.open_option_positions()
        except Exception:
            return None

    def _real_stocks():
        if account is None or not hasattr(account, "stock_holdings"):
            return None
        try:
            return account.stock_holdings()
        except Exception:
            return None

    def _account_summaries():
        values = {}
        if account is not None:
            try:
                values = account.values()
            except Exception as e:
                return None, str(e)
        t = cfg.trading
        real = _real_positions() or {}
        stocks = _real_stocks() or {}
        shared_fx = None
        for live in real.values():
            if live and live.get("fx"):
                shared_fx = live["fx"]
                break
        if shared_fx is None:
            shared_fx = getattr(account, "_fx_hint", None)
        usd_vals = None
        usd_fn = getattr(account, "usd_values", None)
        if callable(usd_fn):
            try:
                usd_vals = usd_fn() or {}
            except Exception:
                usd_vals = None
        funding = None
        fb_fn = getattr(account, "funding_balances", None)
        if callable(fb_fn):
            try:
                funding = fb_fn() or {}
            except Exception:
                funding = None
        out = []
        for label, value in values.items():
            open_risk = store.open_risk(mode, label)
            fx = shared_fx
            usd_cash = None
            live = real.get(label)
            if live is not None:
                open_risk = sum(
                    (r.get("risk_cad") if r.get("risk_cad") is not None
                     else (r.get("cost_cad") if not r.get("short") else 0))
                    or 0
                    for r in live["positions"]
                )
                if live.get("fx"):
                    fx = live["fx"]
                usd_cash = live.get("usd_cash")
            cash_cad = None
            cash_usd = None
            if funding and funding.get(label):
                for b in funding[label]:
                    if b.get("currency") == "CAD":
                        cash_cad = b["amount"]
                    elif b.get("currency") == "USD":
                        cash_usd = b["amount"]
            if cash_usd is None:
                cash_usd = usd_cash
            if usd_vals and usd_vals.get(label):
                # wealthsimple's own conversion beats our derived fx
                usd_value = round(usd_vals[label], 2)
            elif fx and value:
                usd_value = round(value / fx, 2)
            else:
                usd_value = None
            stale_fn = getattr(account, "stale_age", None)
            value_age = stale_fn(label) if callable(stale_fn) else None
            conv_fx = fx or 1.0
            stock_value = None
            sk = stocks.get(label)
            if sk:
                stock_value = round(sum(
                    (r.get("market_value") or 0)
                    * (conv_fx if r.get("currency") == "USD" else 1)
                    for r in sk
                ), 2)
            option_value = None
            if live is not None:
                option_value = round(sum(
                    abs(r.get("market_value") or 0) * conv_fx
                    for r in live["positions"]
                ), 2)

            def _margin_total(rows):
                total = 0.0
                found = False
                for r in rows:
                    amt = r.get("margin_req_amount")
                    if amt is None:
                        continue
                    found = True
                    if r.get("margin_req_currency") == "USD":
                        total += amt * conv_fx
                    else:
                        total += amt
                return round(total, 2) if found else None

            margin_req = _margin_total(
                (live["positions"] if live is not None else []) + (sk or [])
            )
            out.append(
                {
                    "label": label,
                    "value": value,
                    "value_age": value_age,
                    "usd_value": usd_value,
                    "usd_cash": usd_cash,
                    "cash_cad": cash_cad,
                    "cash_usd": cash_usd,
                    "open_risk": open_risk,
                    "stock_value": stock_value,
                    "option_value": option_value,
                    "margin_requirement": margin_req,
                    "open_risk_pct": (
                        round(open_risk / value * 100, 2)
                        if value and value > 0
                        else None
                    ),
                    "max_open_risk_pct": t.max_open_risk_pct,
                    "risk_per_trade_pct": t.risk_per_trade_pct,
                    "per_trade_budget": (
                        value * (t.risk_per_trade_pct / 100.0)
                        if value and value > 0
                        else None
                    ),
                }
            )
        return out, None

    @app.get("/account")
    def account_view():
        accounts, err = _account_summaries()
        if err:
            return jsonify({"error": err}), 502
        return jsonify(
            {
                "mode": mode,
                "accounts": {a["label"]: a for a in accounts},
                "positions": store.list_positions(mode),
            }
        )

    @app.get("/api/summary")
    def api_summary():
        accounts, err = _account_summaries()
        if err:
            return jsonify({"error": err}), 502
        t = cfg.trading
        last_seen = app.reader_state.get("last_seen")
        reader = dict(app.reader_state)
        reader["desired"] = cfg.reader.channel_marker
        reader["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return jsonify(
            {
                "mode": mode,
                "accounts": accounts,
                "reader": reader,
                "stops": {
                    "stop_loss_pct": t.stop_loss_pct,
                    "trailing_stop_pct": t.trailing_stop_pct,
                    "consecutive_losses": store.loss_streak(mode),
                    "max_consecutive_losses": t.max_consecutive_losses,
                },
            }
        )

    @app.get("/api/positions")
    def api_positions():
        rows = [dict(r) for r in store.list_positions(mode)]
        for r in rows:
            r.setdefault("kind", "option")
        real = _real_positions() or {}
        stocks = _real_stocks() or {}
        fetched = {
            label for label, r in list(real.items()) + list(stocks.items())
            if r is not None
        }
        if fetched:
            # live Wealthsimple positions replace tracked ones for the
            # accounts we could fetch (manual trades included)
            rows = [r for r in rows if r["account"] not in fetched]
            for label, live in real.items():
                if not live:
                    continue
                for r in live["positions"]:
                    row = dict(r)
                    row["account"] = label
                    row["source"] = "ws"
                    row.setdefault("kind", "option")
                    rows.append(row)
            for label, live in stocks.items():
                if not live:
                    continue
                for r in live:
                    row = dict(r)
                    row["account"] = label
                    row["source"] = "ws"
                    rows.append(row)
        return jsonify(rows)

    @app.get("/api/signals")
    def api_signals():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_signals(limit))

    @app.get("/api/trades")
    def api_trades():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_trades(limit))

    @app.get("/api/settings")
    def api_settings_get():
        from .settings import get_settings

        return jsonify(get_settings(cfg))

    @app.post("/api/settings")
    def api_settings_post():
        from .settings import apply_settings

        payload = request.get_json(silent=True) or {}
        applied, errors = apply_settings(cfg, payload, config_path)
        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        return jsonify(
            {
                "status": "ok",
                "applied": applied,
                "restart_required": False,
            }
        )

    @app.post("/api/reader_status")
    def api_reader_status():
        data = request.get_json(silent=True) or {}
        app.reader_state["channel"] = (data.get("channel") or None)
        app.reader_state["ok"] = bool(data.get("ok"))
        app.reader_state["last_seen"] = time.time()
        return jsonify(
            {
                "channel_marker": cfg.reader.channel_marker,
                "poll_interval": cfg.reader.poll_interval,
                "max_items": cfg.reader.max_items,
                "channels": cfg.reader.channels,
            }
        )

    @app.get("/api/reader_status")
    def api_reader_status_get():
        last_seen = app.reader_state.get("last_seen")
        state = dict(app.reader_state)
        state["desired"] = cfg.reader.channel_marker
        state["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return jsonify(state)

    @app.get("/api/update_status")
    def api_update_status():
        updater = getattr(app, "ws_updater", None)
        if updater is None:
            return jsonify({"status": "disabled"})
        return jsonify(
            {
                "status": "active",
                "interval_seconds": (
                    getattr(updater.cfg.auto_update, "interval_seconds", 600)
                ),
                "last_check": updater.last_check,
                "result": updater.last_result,
                "errors": updater.errors,
                "head": (updater.start_head or "")[:8],
                "branch": updater.branch,
                "last_pull": updater.last_pull(),
            }
        )

    @app.post("/alert")
    def alert():
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        author = data.get("author", "")
        try:
            ts = float(data.get("ts")) if data.get("ts") else None
        except (TypeError, ValueError):
            ts = None
        try:
            parsed_ts = (
                float(data.get("parsed_ts"))
                if data.get("parsed_ts") else None
            )
        except (TypeError, ValueError):
            parsed_ts = None
        result = process_alert(
            text, author, cfg, store, risk, executor, account,
            channel=str(data.get("channel") or ""), ts=ts,
            parsed_ts=parsed_ts,
        )
        return jsonify(result)

    return app
