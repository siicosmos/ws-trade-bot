import argparse

from trader.account import PaperAccount, WealthsimpleAccount
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

    if cfg.trading.dry_run:
        account = PaperAccount(cfg, store)
        executor = PaperExecutor(cfg, store, account)
        mode = "PAPER"
    else:
        account = WealthsimpleAccount(cfg)
        executor = WealthsimpleExecutor(cfg, account)
        mode = "LIVE"

    risk = RiskEngine(cfg, store, account)

    app = create_app(cfg, store, risk, executor, account)
    print(f"pipeline running in {mode} mode on {cfg.pipeline.host}:{cfg.pipeline.port}")
    app.run(host=cfg.pipeline.host, port=cfg.pipeline.port, threaded=True)


if __name__ == "__main__":
    main()
