import argparse
import os
import sys

from trader.account import PaperAccount, WealthsimpleAccount
from trader.config import load_config
from trader.executor import PaperExecutor, WealthsimpleExecutor
from trader.quotes import make_quote_provider
from trader.risk import RiskEngine
from trader.server import create_app
from trader.store import Store
from trader.updater import AutoUpdater

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="trades.db")
    args = ap.parse_args()

    # a hard restart can leave the previous instance holding
    # the port - clear it before we bind
    from trader.processes import terminate_stale_instances

    terminate_stale_instances(os.path.abspath(__file__))

    cfg = load_config(args.config)
    if (
        not cfg.pipeline.auth_token
        and cfg.pipeline.host not in ("127.0.0.1", "localhost", "::1")
    ):
        print(
            "REFUSING to start: pipeline.auth_token is empty while binding "
            f"to {cfg.pipeline.host} (reachable by other machines)."
        )
        print('generate one with:  python -c "import secrets; '
              'print(secrets.token_urlsafe(24))"')
        print("then set it under pipeline: in config.yaml and restart")
        sys.exit(1)
    store = Store(args.db)
    mode = cfg.trading.mode

    from trader.loghook import install_log_webhook

    install_log_webhook(
        cfg.discord.pipeline_log_webhook_url,
        log_path=os.path.join(
            os.path.dirname(os.path.abspath(args.db)),
            "pipeline.log",
        ),
    )

    from trader.ws_tokens import load_env_tokens

    load_env_tokens()

    account = None
    executor = None
    paper_ledger = None

    if mode == "live":
        account = WealthsimpleAccount(cfg, store)
        executor = WealthsimpleExecutor(cfg, account)
    else:
        # notify and paper share one pipeline: parse, record and
        # (notify only) push alerts to the phone. paper mode is
        # the quiet variant - simulated execution without the
        # notifications
        if mode == "notify" and not cfg.discord.webhook_url:
            print(
                "WARNING: notify mode but discord.webhook_url is not set - "
                "alerts will not reach your phone"
            )
        account = WealthsimpleAccount(cfg, store)
        if mode == "paper" or cfg.paper.enabled:
            from trader.account import PaperLedger, seed_paper_accounts

            seeded = seed_paper_accounts(cfg, store, account)
            if seeded:
                print(
                    "paper trading: seeded " + ", ".join(seeded)
                    + " from live accounts"
                )
            paper_ledger = PaperLedger(cfg, store, account)
            executor = PaperExecutor(cfg, store, paper_ledger)
            print(
                "paper trading "
                + ("(quiet mode)" if mode == "paper"
                   else "enabled alongside notify")
            )
            if cfg.paper.mirror:
                from trader.mirror import start_mirror_thread

                start_mirror_thread(
                    cfg, store, account, paper_ledger,
                    cfg.paper.mirror_interval_seconds,
                    cfg.discord.update_webhook_url
                    or cfg.discord.webhook_url,
                )
                print(
                    "mirroring real trades into the paper ledger "
                    f"every {cfg.paper.mirror_interval_seconds}s"
                )
        try:
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

    if mode in ("paper", "live") and cfg.trading.stop_loss_pct > 0:
        from trader.stops import StopMonitor

        quote_fn = make_quote_provider(cfg, account)
        if quote_fn is not None:
            monitor = StopMonitor(
                cfg, store, executor, quote_fn, cfg.discord.webhook_url
            )
            monitor.start()
            trail = (
                f", trailing {cfg.trading.trailing_stop_pct}%"
                if cfg.trading.trailing_stop_pct > 0
                else ""
            )
            print(
                f"stop monitor active: {cfg.trading.stop_loss_pct}% stop{trail}, "
                f"checked every {cfg.trading.stop_check_seconds}s"
            )

    from trader.updater import startup_banner

    startup_banner("pipeline", ROOT)

    updater = AutoUpdater(
        cfg, ROOT,
        cfg.discord.update_webhook_url or cfg.discord.webhook_url,
    )
    updater.start()
    if cfg.auto_update.enabled:
        print(
            f"auto-update enabled (checking github every "
            f"{cfg.auto_update.interval_seconds}s)"
        )

    app = create_app(
        cfg, store, risk, executor, account, config_path=os.path.abspath(
            args.config
        )
    )
    app.ws_updater = updater
    scheme = "https" if cfg.pipeline.tls_cert and cfg.pipeline.tls_key else "http"
    ssl_context = None
    if scheme == "https":
        ssl_context = (cfg.pipeline.tls_cert, cfg.pipeline.tls_key)
        print("dashboard served over HTTPS (self-signed)")
    print(f"pipeline running in {mode.upper()} mode on {cfg.pipeline.host}:{cfg.pipeline.port}")
    print(f"dashboard: {scheme}://127.0.0.1:{cfg.pipeline.port}/")
    app.run(
        host=cfg.pipeline.host, port=cfg.pipeline.port, threaded=True,
        ssl_context=ssl_context,
    )


if __name__ == "__main__":
    main()
