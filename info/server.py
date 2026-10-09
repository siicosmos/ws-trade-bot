"""The info server: reader ingest + alert feed, no trading.

Wires the store, the push fan-out thread, the auto-updater and
the health watchdog around info.web.create_app. Consumers run
the trading pipeline on their own machines against their own
accounts - this process never touches money.
"""

import os
import sys

from core.ops.loghook import default_log_path, install_log_webhook
from core.ops.updater import (
    INFO_RESTART_FILES, create_updater, pop_exit_marker,
    startup_banner,
)
from core.ops.watchdog import start_health_watchdog
from core.store import Store
from info.fanout import start_fanout_thread
from info.web import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


EXIT_FILE = "pipeline_exit_info.txt"


def main(cfg, args):
    if not cfg.pipeline.auth_token:
        print(
            "REFUSING to start: auth_token is empty - it guards the "
            "reader ingest and the write routes."
        )
        print('generate one with:  python -c "import secrets; '
              'print(secrets.token_urlsafe(24))"')
        print("then set it under info: in info.config.yaml and restart")
        sys.exit(1)

    store = Store(
        args.db,
        retention_days=int(
            getattr(cfg.trading, "history_retention_days", 365)
        ),
    )

    # the .bat restart loop writes the exit code; surface it
    prev_exit = pop_exit_marker(args.db, "info")
    if prev_exit:
        print("previous run: " + prev_exit)

    log_batcher = install_log_webhook(
        cfg.discord.consumer_log_webhook_url,
        log_path=default_log_path("info.log"),
    )

    # the alert source: push every new signal to the registered
    # consumers (pull backfills whatever a push misses)
    start_fanout_thread(
        cfg, store,
        cfg.discord.update_webhook_url,
    )
    print(
        "info server: reader ingest + alert feed "
        f"({len(cfg.consumers)} consumer"
        f"{'s' if len(cfg.consumers) != 1 else ''} registered)"
    )

    banner = startup_banner("info server", ROOT)

    # parity with the consumer app: an unclean previous exit (a
    # crash or a manual kill - NOT a deliberate exit-77 restart,
    # which posted its own notice on the way down) is reported to
    # the update webhook, throttled to one notice a minute
    if prev_exit and "code 77" not in prev_exit:
        import time as _time

        last_notice = store.meta_get("restart_notice_ts") or 0
        try:
            due = _time.time() - float(last_notice) > 60
        except (TypeError, ValueError):
            due = True
        if due:
            store.meta_set("restart_notice_ts", _time.time())
            from core.ops.notify import notify_discord

            notify_discord(
                cfg.discord.update_webhook_url,
                "Info server restarting",
                {"reason": prev_exit, "running": banner},
                ok=True,
            )
            print("restart notice posted to the update webhook")

    def _restart(reason="restart", commits=None):
        # parity with the consumer app: every restart posts a
        # notice to the update webhook (update applies, local code
        # changes). the update restart exits via os._exit, which
        # bypasses atexit - flush the log batcher first so the
        # restart lines reach the webhook
        from core.ops.notify import notify_discord

        fields = {"reason": str(reason)[:1000]}
        if commits:
            fields["commits"] = commits
        notify_discord(
            cfg.discord.update_webhook_url,
            "Info server restarting",
            fields,
            ok=True,
        )
        if log_batcher is not None:
            log_batcher.flush_now()
        os._exit(77)

    updater = create_updater(
        cfg, ROOT,
        cfg.discord.update_webhook_url,
        restart=_restart,
        restart_files=INFO_RESTART_FILES,
    )
    updater.start()
    if cfg.auto_update.enabled:
        print(
            f"auto-update enabled (checking github every "
            f"{cfg.auto_update.interval_seconds}s)"
        )

    app = create_app(
        cfg, store, config_path=os.path.abspath(args.config)
    )
    app.ws_updater = updater
    app.restart_pipeline = _restart
    scheme = (
        "https" if cfg.pipeline.tls_cert and cfg.pipeline.tls_key
        else "http"
    )
    ssl_context = None
    if scheme == "https":
        ssl_context = (cfg.pipeline.tls_cert, cfg.pipeline.tls_key)
        print("dashboard served over HTTPS (self-signed)")
    print(
        f"info server running on {cfg.pipeline.host}:"
        f"{cfg.pipeline.port}"
    )
    print(f"dashboard: {scheme}://127.0.0.1:{cfg.pipeline.port}/")

    # main-thread hang protection: crashes restart via the .bat
    # loop, hangs do not - self-check /health and exit for a
    # clean restart if serving dies
    start_health_watchdog(
        f"{scheme}://127.0.0.1:{cfg.pipeline.port}/health",
        cfg.discord.update_webhook_url,
        verify=scheme != "https",   # self-signed local cert
        store=store,
    )

    # production wsgi server: waitress handles plain http
    # robustly; it has no native tls support, so https (the
    # self-signed cert path) stays on werkzeug
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