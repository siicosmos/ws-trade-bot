from flask import Flask, jsonify, request

from .pipeline import process_alert
from .store import Store


def create_app(cfg, store: Store, risk, executor, account=None) -> Flask:
    app = Flask(__name__)
    mode = "paper" if cfg.trading.dry_run else "live"

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
        value = None
        if account is not None:
            try:
                value = account.value()
            except Exception as e:
                return jsonify({"error": str(e)}), 502
        open_risk = store.open_risk(mode)
        t = cfg.trading
        budget = (
            value * (t.risk_per_trade_pct / 100.0) if value is not None else None
        )
        return jsonify(
            {
                "mode": mode,
                "account_value": value,
                "open_risk": open_risk,
                "open_risk_pct": (
                    round(open_risk / value * 100, 2) if value else None
                ),
                "max_open_risk_pct": t.max_open_risk_pct,
                "risk_per_trade_pct": t.risk_per_trade_pct,
                "per_trade_budget": budget,
                "positions": store.list_positions(mode),
            }
        )

    @app.post("/alert")
    def alert():
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        author = data.get("author", "")
        result = process_alert(text, author, cfg, store, risk, executor)
        return jsonify(result)

    return app
