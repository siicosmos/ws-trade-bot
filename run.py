import argparse
import os
import sys

from trader.ws.account import PaperAccount, WealthsimpleAccount
from trader.config import load_config
from trader.pipeline import process_alert
from trader.trading.executor import PaperExecutor, WealthsimpleExecutor
from trader.trading.quotes import make_quote_provider
from trader.trading.risk import RiskEngine
from trader.web.server import create_app
from trader.store import Store
from trader.ops.updater import AutoUpdater

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="trades.db")
    args = ap.parse_args()

    # a hard restart can leave the previous instance holding
    # the port - clear it before we bind. the config path
    # scopes the match: the info server and the consumer app
    # share run.py but must never kill each other
    from trader.ops.processes import terminate_stale_instances

    terminate_stale_instances(
        os.path.abspath(__file__),
        config_path=os.path.abspath(args.config),
    )

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
    store = Store(
        args.db,
        retention_days=int(
            getattr(cfg.trading, "history_retention_days", 365)
        ),
    )
    mode = cfg.trading.mode
    role = getattr(cfg.pipeline, "role", "consumer")

    from trader.ops.loghook import install_log_webhook

    # the .bat restart loop writes the exit code; surface it.
    # per-role files: config_info.yaml -> info_exit.txt,
    # config_consumer.yaml -> consumer_exit.txt, config.yaml ->
    # pipeline_exit.txt (the roles share a repo and would
    # otherwise read each other's last words)
    stem = os.path.splitext(os.path.basename(args.config))[0]
    role_suffix = (
        stem[len("config_"):]
        if stem.startswith("config_") and len(stem) > len("config_")
        else ""
    )
    exit_name = (
        f"{role_suffix}_exit.txt" if role_suffix else "pipeline_exit.txt"
    )
    exit_file = os.path.join(ROOT, exit_name)
    try:
        with open(exit_file) as f:
            print("previous run: " + f.read().strip())
        os.remove(exit_file)
    except OSError:
        pass

    log_batcher = install_log_webhook(
        cfg.discord.pipeline_log_webhook_url,
        log_path=os.path.join(
            os.path.dirname(os.path.abspath(args.db)),
            "pipeline.log",
        ),
    )

    from trader.ws.ws_tokens import load_env_tokens

    load_env_tokens()

    account = None
    executor = None
    paper_ledger = None
    risk = None

    if role == "info":
        # the alert source server: reader ingest + feed only -
        # no trading wiring (no executors, stops, mirror,
        # quotes). Consumers run the trading pipeline on their
        # own machines against their own accounts.
        from trader.ops.fanout import start_fanout_thread

        start_fanout_thread(
            cfg, store,
            cfg.discord.update_webhook_url
            or cfg.discord.webhook_url,
        )
        print(
            "info server: reader ingest + alert feed "
            f"({len(cfg.consumers)} consumer"
            f"{'s' if len(cfg.consumers) != 1 else ''} registered)"
        )
    elif mode == "live":
        account = WealthsimpleAccount(cfg, store)
        executor = WealthsimpleExecutor(cfg, account)
        # the mirror thread always runs in live mode: it books
        # every actual fill into the real account's ledger (the
        # dashboard's today gain reads it) and reconciles the
        # live ledger's estimated bookings against the fills
        from trader.trading.mirror import start_mirror_thread

        start_mirror_thread(
            cfg, store, account, None,
            cfg.paper.mirror_interval_seconds,
            cfg.discord.update_webhook_url
            or cfg.discord.webhook_url,
        )
        print(
            "mirroring real fills into the ledgers "
            f"every {cfg.paper.mirror_interval_seconds}s"
        )
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
            from trader.ws.account import PaperLedger, seed_paper_accounts

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
                from trader.trading.mirror import start_mirror_thread

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

    if role != "info":
        risk = RiskEngine(cfg, store, account)

        # the alert feed: pull alerts from the info server and
        # run them through the same process_alert as the /alert
        # route (push delivery needs no code here - the route is
        # already up). the atomic signal claim makes dual
        # delivery idempotent
        if getattr(cfg, "feed", None) and cfg.feed.url:
            from trader.ops.feedclient import start_feed_client

            def _on_feed_alert(text, author, ts, channel):
                process_alert(
                    text, author, cfg, store, risk, executor,
                    account, channel=channel, ts=ts,
                )

            start_feed_client(
                cfg, store, _on_feed_alert,
                cfg.discord.update_webhook_url
                or cfg.discord.webhook_url,
            )
            print(f"feed client: pulling alerts from {cfg.feed.url}")

        # the stop monitor runs wherever positions are executed: live,
        # paper-only, or paper alongside notify (it watches the paper
        # ledger through the PaperExecutor). It also fires the
        # per-position tp / trailing guards - so it starts whenever
        # positions execute, even with the global stops at 0
        paper_alongside = (
            mode == "notify"
            and executor is not None
            and getattr(getattr(cfg, "paper", None), "enabled", False)
        )
        if mode in ("paper", "live") or paper_alongside:
            from trader.trading.stops import StopMonitor

            quote_fn = make_quote_provider(cfg, account)
            quote_src = None
            if quote_fn is not None:
                quote_src = (
                    "moomoo" if cfg.quotes.provider == "moomoo"
                    else "ws quotes"
                )
            if quote_fn is None:
                # quotes disabled does not mean unguarded: the paper
                # ledger already prices its positions (live ws nodes,
                # ws chains, moomoo) - that map drives the monitor;
                # live mode falls back to the ws chains directly, an
                # executing mode must never run without protection
                if paper_ledger is not None:
                    from trader.trading.paper import _position_key

                    def ledger_quote(pos):
                        try:
                            quotes = paper_ledger._quotes()
                        except Exception:
                            return None
                        q = (
                            quotes.get(_position_key(pos))
                            or quotes.get(pos["contract_key"])
                        )
                        return (q or {}).get("price")

                    quote_fn = ledger_quote
                    quote_src = "ledger-priced quotes"
                elif mode == "live":
                    from trader.trading.quotes import (
                        make_ws_quote_provider,
                    )

                    quote_fn = make_ws_quote_provider(cfg, account)
                    quote_src = "ws option chains"
            if quote_fn is not None:
                monitor = StopMonitor(
                    cfg, store, executor, quote_fn,
                    cfg.discord.webhook_url,
                )
                monitor.start()
                trail = (
                    f", trailing {cfg.trading.trailing_stop_pct}%"
                    if cfg.trading.trailing_stop_pct > 0
                    else ""
                )
                print(
                    f"stop monitor active ({quote_src}): "
                    f"{cfg.trading.stop_loss_pct}% stop{trail}, "
                    f"checked every {cfg.trading.stop_check_seconds}s"
                )
            elif mode == "live":
                print(
                    "WARNING: live mode without live quotes - the stop "
                    "monitor is OFF, open positions have no automated "
                    "protection (enable quotes in the settings)"
                )

    from trader.ops.updater import startup_banner

    startup_banner("pipeline", ROOT)

    def _restart_pipeline():
        # the update restart exits via os._exit, which bypasses
        # atexit - flush the log batcher so the restart lines
        # reach the webhook
        if log_batcher is not None:
            log_batcher.flush_now()
        os._exit(77)

    updater = AutoUpdater(
        cfg, ROOT,
        cfg.discord.update_webhook_url or cfg.discord.webhook_url,
        restart=_restart_pipeline,
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
    # the mode slider restarts the app after persisting (the
    # executors/threads wire by mode at startup)
    app.restart_pipeline = _restart_pipeline
    scheme = "https" if cfg.pipeline.tls_cert and cfg.pipeline.tls_key else "http"
    ssl_context = None
    if scheme == "https":
        ssl_context = (cfg.pipeline.tls_cert, cfg.pipeline.tls_key)
        print("dashboard served over HTTPS (self-signed)")
    print(f"pipeline running in {role.upper()} role"
          + (f" ({mode.upper()} mode)" if role == "consumer" else "")
          + f" on {cfg.pipeline.host}:{cfg.pipeline.port}")
    print(f"dashboard: {scheme}://127.0.0.1:{cfg.pipeline.port}/")

    # main-thread hang protection: crashes restart via the .bat
    # loop, hangs do not - self-check /health and exit for a
    # clean restart if serving dies
    from trader.ops.watchdog import start_health_watchdog

    start_health_watchdog(
        f"{scheme}://127.0.0.1:{cfg.pipeline.port}/health",
        cfg.discord.update_webhook_url
        or cfg.discord.webhook_url,
        verify=scheme != "https",   # self-signed local cert
        store=store,
    )

    # production wsgi server (plan #8): waitress handles plain
    # http robustly. it has no native tls support, so https
    # (the self-signed cert path) stays on werkzeug - serving
    # plain http under an https config would break the health
    # watchdog and the reader's https posts.
    if pick_wsgi(ssl_context, waitress_available()):
        from waitress import serve as waitress_serve

        print("serving with waitress (production wsgi)")
        waitress_serve(
            app,
            host=cfg.pipeline.host, port=cfg.pipeline.port,
            threads=16,
        )
        return

    if ssl_context is not None:
        print("serving https via werkzeug (waitress has no tls)")
    app.run(
        host=cfg.pipeline.host, port=cfg.pipeline.port, threaded=True,
        ssl_context=ssl_context,
    )


def waitress_available():
    try:
        import waitress  # noqa: F401

        return True
    except ImportError:
        return False


def pick_wsgi(ssl_context, waitress_ok):
    """waitress for plain http; tls stays on werkzeug (waitress
    has no native ssl support)."""
    return bool(waitress_ok and ssl_context is None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
