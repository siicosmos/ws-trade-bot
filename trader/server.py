from flask import Flask, Response, jsonify, request

from .dashboard import DASHBOARD_HTML
from .pipeline import process_alert
from .store import Store


def create_app(cfg, store: Store, risk, executor, account=None) -> Flask:
    app = Flask(__name__)
    mode = cfg.trading.mode

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if not token or request.path == "/health":
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
        return jsonify({"mode": mode, "accounts": accounts})

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
