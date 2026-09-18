import logging
import time

from flask import Flask, Response, jsonify, request

from .dashboard import DASHBOARD_HTML
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


def create_app(cfg, store: Store, risk, executor, account=None,
                 config_path=None) -> Flask:
    app = Flask(__name__)
    install_quiet_filter()
    mode = cfg.trading.mode
    app.reader_state = {
        "channel": None,
        "ok": False,
        "last_seen": None,
    }

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if not token or request.path in ("/health", "/favicon.ico"):
            return None
        if request.headers.get("X-Auth-Token") == token:
            return None
        auth = request.authorization
        if auth and auth.password == token:
            return None
        return Response(
            "unauthorized", 401,
            {"WWW-Authenticate": 'Basic realm="ws-trade-bot"'},
        )

    @app.get("/")
    def dashboard_page():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "mode": mode})

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    def _account_summaries():
        values = {}
        if account is not None:
            try:
                values = account.values()
            except Exception as e:
                return None, str(e)
        t = cfg.trading
        out = []
        for label, value in values.items():
            open_risk = store.open_risk(mode, label)
            stale_fn = getattr(account, "stale_age", None)
            value_age = stale_fn(label) if callable(stale_fn) else None
            out.append(
                {
                    "label": label,
                    "value": value,
                    "value_age": value_age,
                    "open_risk": open_risk,
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
        return jsonify(store.list_positions(mode))

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
        result = process_alert(
            text, author, cfg, store, risk, executor, account
        )
        return jsonify(result)

    return app
