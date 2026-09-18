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
    mode = cfg.trading.mode

    from trader.ws_tokens import load_env_tokens

    load_env_tokens()

    account = None
    executor = None

    if mode == "paper":
        account = PaperAccount(cfg, store)
        executor = PaperExecutor(cfg, store, account)
    elif mode == "live":
        account = WealthsimpleAccount(cfg, store)
        executor = WealthsimpleExecutor(cfg, account)
    else:
        if not cfg.discord.webhook_url:
            print(
                "WARNING: notify mode but discord.webhook_url is not set - "
                "alerts will not reach your phone"
            )
        try:
            account = WealthsimpleAccount(cfg, store)
            account.values()
            stale = None
            stale_fn = getattr(account, "stale_age", None)
            if callable(stale_fn):
                first = next(iter(account.values().keys()), None)
                if first:
                    stale = stale_fn(first)
            if stale:
                print(
                    f"WS values unreachable - using cached values ({stale} old)"
                )
            else:
                print("sizing alerts will use live Wealthsimple account values")
        except Exception:
            account = PaperAccount(cfg, store)
            print(
                "note: Wealthsimple values unavailable and no cache "
                "(run scripts/ws_login.py) - using paper values for sizing alerts"
            )

    risk = RiskEngine(cfg, store, account)

    app = create_app(cfg, store, risk, executor, account)
    print(f"pipeline running in {mode.upper()} mode on {cfg.pipeline.host}:{cfg.pipeline.port}")
    print(f"dashboard: http://127.0.0.1:{cfg.pipeline.port}/")
    app.run(host=cfg.pipeline.host, port=cfg.pipeline.port, threaded=True)


if __name__ == "__main__":
    main()
