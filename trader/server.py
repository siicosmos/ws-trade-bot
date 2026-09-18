from flask import Flask, jsonify, request

from .pipeline import process_alert
from .store import Store


def create_app(cfg, store: Store, risk, executor, account=None) -> Flask:
    app = Flask(__name__)
    mode = cfg.trading.mode

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "mode": mode,
            }
        )

    @app.get("/account")
    def account_view():
        values = {}
        if account is not None:
            try:
                values = account.values()
            except Exception as e:
                return jsonify({"error": str(e)}), 502
        t = cfg.trading
        accounts_out = {}
        for label, value in values.items():
            open_risk = store.open_risk(mode, label)
            accounts_out[label] = {
                "value": value,
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
        return jsonify(
            {
                "mode": mode,
                "accounts": accounts_out,
                "positions": store.list_positions(mode),
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
