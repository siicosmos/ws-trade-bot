import os
import socket
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"


def result(status, name, detail=""):
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return status != FAIL


def main():
    print("ws-trade-bot diagnostics\n" + "=" * 50)
    exit_code = 0

    result(OK, f"python {sys.version.split()[0]} ({sys.platform})")

    try:
        import flask
        import yaml
        import requests

        result(OK, "core dependencies (flask, yaml, requests)")
    except ImportError as e:
        exit_code = 1
        result(FAIL, "core dependencies", str(e))

    try:
        import wealthsimple_python  # noqa: F401

        result(OK, "wealthsimple-python installed")
    except ImportError:
        result(WARN, "wealthsimple-python not installed",
               "live mode unavailable; notify sizing falls back to paper values")

    config_path = os.path.join(ROOT, "config", "consumer.config.yaml")
    if not os.path.exists(config_path):
        result(FAIL, "consumer config exists",
               "run: copy config/consumer.example.config.yaml "
               "config/consumer.config.yaml")
        print("\ncreate config/consumer.config.yaml first, then rerun")
        sys.exit(1)

    try:
        from core.config import load_config

        cfg = load_config(config_path)
        result(OK, f"consumer config loads (mode={cfg.trading.mode}, "
                   f"port={cfg.pipeline.port})")
        if not cfg.discord.trade_alert_webhook_url:
            result(WARN, "discord.trade_alert_webhook_url not set",
                   "phone alerts will not send")
    except Exception as e:
        exit_code = 1
        result(FAIL, "consumer config loads", f"{type(e).__name__}: {e}")
        sys.exit(1)

    db_path = os.path.join(ROOT, "db", "consumer.trades.db")
    try:
        import sqlite3

        # read-only: a diagnostic must not create or migrate the
        # production db (Store() runs the schema/migration path)
        uri = f"file:{db_path.replace(os.sep, '/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("SELECT COUNT(*) FROM signals").fetchone()
        finally:
            conn.close()
        result(OK, "consumer.trades.db opens read-only (schema present)")
    except sqlite3.OperationalError as e:
        result(WARN, "consumer.trades.db", f"{e} (missing or pre-schema)")
    except Exception as e:
        exit_code = 1
        result(FAIL, "consumer.trades.db opens",
               f"{type(e).__name__}: {e}")

    port = cfg.pipeline.port
    host = "127.0.0.1"

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1)
    already_running = probe.connect_ex((host, port)) == 0
    probe.close()

    if already_running:
        try:
            import requests as rq

            health = rq.get(f"http://{host}:{port}/health", timeout=3).json()
            result(OK, f"pipeline already running on port {port}",
                   f"mode={health.get('mode')} - dashboard: "
                   f"http://{host}:{port}/")
            print("\neverything looks good - open http://127.0.0.1:%d/ "
                  "in your browser" % port)
            sys.exit(0)
        except Exception as e:
            result(WARN, f"something is on port {port} but not the pipeline",
                   str(e))
    else:
        result(OK, f"no stale process on port {port}")

    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.bind(("0.0.0.0", port))
        test_sock.close()
        result(OK, f"port {port} can be bound")
    except OSError as e:
        detail = str(e)
        hint = ""
        if "10013" in detail or "access" in detail.lower():
            hint = (" - likely a Windows/Hyper-V excluded port range; "
                    "change the port in config/<role>.config.yaml "
                    "(e.g. 8081) "
                    "and update reader.info_server_url to match")
        exit_code = 1
        result(FAIL, f"port {port} can be bound", detail + hint)

    print("\nif all checks passed, start the app with "
          "scripts\\start_info.bat or scripts\\start_consumer.bat "
          "and open http://127.0.0.1:%d/ "
          "(use 127.0.0.1, not localhost, if the browser cannot connect)"
          % port)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
