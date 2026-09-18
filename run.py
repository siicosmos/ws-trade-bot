import argparse

from trader.config import load_config
from trader.executor import PaperExecutor, WealthsimpleExecutor
from trader.risk import RiskEngine
from trader.server import create_app
from trader.store import Store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="trades.db")
    args = ap.parse_args()

    cfg = load_config(args.config)
    store = Store(args.db)
    risk = RiskEngine(cfg, store)

    if cfg.trading.dry_run:
        executor = PaperExecutor()
        mode = "PAPER"
    else:
        executor = WealthsimpleExecutor(cfg)
        mode = "LIVE"

    app = create_app(cfg, store, risk, executor)
    print(f"pipeline running in {mode} mode on {cfg.pipeline.host}:{cfg.pipeline.port}")
    app.run(host=cfg.pipeline.host, port=cfg.pipeline.port, threaded=True)


if __name__ == "__main__":
    main()
