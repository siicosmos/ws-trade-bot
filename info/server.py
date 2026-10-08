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
    INFO_RESTART_FILES, create_updater, startup_banner,
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

    # the .bat restart loop writes the exit code; surface it.
    # the file lives next to the db - each role folder reports
    # its own last words
    exit_file = os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "pipeline_exit.txt"
    )
    try:
        with open(exit_file, encoding="utf-8") as f:
            print("previous run: " + f.read().strip())
        os.remove(exit_file)
    except OSError:
        pass

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

    startup_banner("info server", ROOT)

    def _restart():
        # the update restart exits via os._exit, which bypasses
        # atexit - flush the log batcher so the restart lines
        # reach the webhook
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