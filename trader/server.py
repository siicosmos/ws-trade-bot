from flask import Flask, jsonify, request

from .pipeline import process_alert
from .store import Store


def create_app(cfg, store: Store, risk, executor) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "mode": "paper" if cfg.trading.dry_run else "live",
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
