"""The info server's web app: alert ingest, the consumer feed,
reader heartbeat, and the lean admin dashboard.

No trading lives here - the executors, ledgers, positions and
quotes are consumer-app territory. This app is the alert
source: the reader POSTs alerts, signals are recorded, and
consumers receive them via push (fan-out) and the long-poll
feed.

No login: the dashboard is open (read-only data - signals,
levels, consumer health), so a session cookie here can never
fight with the consumer app's cookie on the same host. The
write routes are token-guarded (X-Auth-Token, the reader's
pipeline token): POST /alert - a fake alert would make every
consumer app trade - and the levels editor + settings POSTs - a
fake levels text reaches every consumer's ladder and informs
trades just the same. The dashboard prompts once for the token
and keeps it in localStorage (no cookie).
"""

import hmac
import os
import logging
import time

from flask import Flask, Response, jsonify, request

from core.store import Store
from core.web_common import install_gzip, install_quiet_filter
from .dashboard import info_css, info_html
from .ingest import ingest_alert


def create_app(cfg, store: Store, config_path=None) -> Flask:
    app = Flask(__name__)
    install_quiet_filter()
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    # the dashboard assets change with every release - always
    # revalidate (see the consumer app for the same note)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    # the reader's heartbeat state (dashboard reader line).
    # reader settings live in the reader's own yaml - read the
    # first allowed channel from there for the "waiting for"
    # display (best effort; the reader and this app share the box)
    app.reader_state = {"channel": None, "ok": False, "last_seen": None}
    app.reader_desired = ""
    try:
        import yaml as _yaml

        rpath = os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
            "reader", "reader.config.yaml",
        )
        if os.path.exists(rpath):
            with open(rpath, encoding="utf-8") as f:
                rcfg = _yaml.safe_load(f) or {}
            flat = rcfg.get("reader") or rcfg
            channels = [
                str(c).strip().lower()
                for c in flat.get("channels") or []
                if str(c).strip()
            ]
            app.reader_desired = channels[0] if channels else ""
    except Exception:
        pass

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def _reader_token_ok():
        """The shared write guard: the reader's pipeline token
        (dashboard password)."""
        token = cfg.pipeline.auth_token
        supplied = request.headers.get("X-Auth-Token", "")
        return bool(token) and hmac.compare_digest(supplied, token)

    def _write_guard():
        """The other write routes guard the same way: a fake
        levels text reaches every consumer's ladder, and a fake
        settings POST redirects this app's log webhooks or
        disables its auto-update."""
        if not _reader_token_ok():
            return jsonify({"error": "unauthorized"}), 401
        return None

    install_gzip(app)

    _page_cache = {"html": ""}

    @app.get("/")
    def info_page():
        # the stylesheet is inlined into the head (same pattern
        # as the consumer dashboard); the html re-reads when the
        # file changed on disk so an update never serves an old
        # page with new js
        current = info_html().replace(
            '<link rel="stylesheet" href="/static/dashboard.css">',
            "<style>\n" + info_css() + "\n</style>",
            1,
        )
        if _page_cache["html"] != current:
            _page_cache["html"] = current
        resp = app.response_class(current, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/static/dashboard.css")
    def shared_css():
        # the site-wide stylesheet lives in core/static (one
        # design language for both apps)
        from flask import send_from_directory

        return send_from_directory(
            os.path.join(
                os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "core", "static",
            ),
            "dashboard.css", mimetype="text/css",
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.post("/alert")
    def alert():
        """Reader ingest: parse + dedupe + record - no execution.
        Token-guarded: a fake alert would make every consumer
        app trade."""
        if not _reader_token_ok():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        author = data.get("author", "")
        try:
            ts = float(data.get("ts")) if data.get("ts") else None
        except (TypeError, ValueError):
            ts = None
        try:
            parsed_ts = (
                float(data.get("parsed_ts"))
                if data.get("parsed_ts") else None
            )
        except (TypeError, ValueError):
            parsed_ts = None
        return jsonify(ingest_alert(
            text, author, cfg, store,
            channel=str(data.get("channel") or ""), ts=ts,
            parsed_ts=parsed_ts,
        ))

    # --------------------------------------------------------
    # the alert feed for consumer apps
    # --------------------------------------------------------
    def _consumer_by_token(token):
        for c in getattr(cfg, "consumers", None) or []:
            if c.token and hmac.compare_digest(c.token, token):
                return c
        return None

    @app.get("/api/feed")
    def api_feed():
        """Consumer feed: alerts recorded after the ?since= cursor
        (signal rowid), the current SPX levels text, and a
        long-poll wait so consumers get new alerts near-instantly.
        Auth: X-Auth-Token must match a consumers[] entry.
        Without since=: head-only - just the current cursor (a
        fresh consumer starts there; replaying history would
        execute old alerts)."""
        consumer = _consumer_by_token(
            request.headers.get("X-Auth-Token", "")
        )
        if consumer is None:
            return jsonify({"error": "invalid consumer token"}), 401
        head = store.max_signal_rowid()
        raw_since = request.args.get("since", None)
        if raw_since is None:
            from .fanout import record_seen

            record_seen(consumer.label, head)
            return jsonify({
                "alerts": [],
                "levels": store.meta_get("spx_levels_text") or "",
                "cursor": head,
            })
        try:
            since = int(raw_since)
        except (TypeError, ValueError):
            since = 0
        try:
            wait = min(
                max(float(request.args.get("wait", 0) or 0), 0.0), 25.0
            )
        except (TypeError, ValueError):
            wait = 0.0
        deadline = time.time() + wait
        rows = store.signals_since(since)
        while not rows and time.time() < deadline:
            time.sleep(0.3)
            rows = store.signals_since(since)
        from core.parser import parse_alert

        alerts = []
        for r in rows:
            parsed = None
            if r["parsed"]:
                try:
                    a = parse_alert(
                        r["text"], cfg.parser.custom_patterns
                    )
                    parsed = a.to_dict() if a else None
                except Exception:
                    parsed = None
            alerts.append({
                "id": r["id"],
                "ts": r["ts"],
                "author": r["author"],
                "text": r["text"],
                "correction": bool(r["correction"]),
                "channel": r["channel"],
                "alert": parsed,
            })
        cursor = alerts[-1]["id"] if alerts else since
        from .fanout import record_seen

        record_seen(consumer.label, cursor)
        return jsonify({
            "alerts": alerts,
            "levels": store.meta_get("spx_levels_text") or "",
            "cursor": cursor,
        })

    @app.get("/api/feed-status")
    def api_feed_status():
        """Info dashboard: per-consumer push/pull health."""
        from .fanout import snapshot

        state = snapshot()
        by_label = {
            c["label"]: c for c in state["consumers"]
        }
        now = time.time()
        consumers = []
        for c in getattr(cfg, "consumers", None) or []:
            s = dict(by_label.get(c.label, {}))
            s["label"] = c.label
            s["push_url"] = c.push_url
            age = (
                round(now - s["last_seen"], 1)
                if s.get("last_seen") else None
            )
            s["last_seen_age"] = age
            s["alive"] = age is not None and age < 30
            consumers.append(s)
        return jsonify({
            "consumers": consumers,
            "signals_recorded": store.max_signal_rowid(),
        })

    # --------------------------------------------------------
    # reader heartbeat: the reader posts channel/ok. Reader
    # settings live in the reader's own yaml (reader/reader.config.yaml)
    # - the heartbeat no longer pushes settings, it would
    # override the yaml on every poll
    # --------------------------------------------------------
    @app.post("/api/reader_status")
    def api_reader_status():
        data = request.get_json(silent=True) or {}
        app.reader_state["channel"] = (data.get("channel") or None)
        app.reader_state["ok"] = bool(data.get("ok"))
        app.reader_state["last_seen"] = time.time()
        return jsonify({})

    @app.get("/api/reader_status")
    def api_reader_status_get():
        last_seen = app.reader_state.get("last_seen")
        state = dict(app.reader_state)
        state["desired"] = app.reader_desired
        state["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return jsonify(state)

    # --------------------------------------------------------
    # spx levels: the info server is the source of truth
    # --------------------------------------------------------
    @app.get("/api/levels")
    def api_levels():
        return jsonify({
            "text": store.meta_get("spx_levels_text"),
            "editable": True,
        })

    @app.post("/api/spx-levels")
    def api_spx_levels():
        """Save the pasted levels text - consumers receive it via
        the feed and render it read-only on their ladder."""
        denied = _write_guard()
        if denied is not None:
            return denied
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "")[:8000]
        store.meta_set("spx_levels_text", text)
        return jsonify({"status": "ok"})

    @app.get("/api/update_status")
    def api_update_status():
        from core.ops.updater import update_status_payload

        return jsonify(update_status_payload(app))

    # --------------------------------------------------------
    # settings: auto-update + this app's webhooks (the reader's
    # settings live in the reader's own yaml, the consumer's in
    # the consumer app)
    # --------------------------------------------------------
    @app.get("/api/settings")
    def api_settings_get():
        return jsonify({
            "auto_update": {
                "enabled": cfg.auto_update.enabled,
                "interval_seconds": cfg.auto_update.interval_seconds,
            },
            "discord": {
                "consumer_log_webhook_url": (
                    cfg.discord.consumer_log_webhook_url
                ),
                "update_webhook_url": cfg.discord.update_webhook_url,
            },
        })

    @app.post("/api/settings")
    def api_settings_post():
        denied = _write_guard()
        if denied is not None:
            return denied
        data = request.get_json(silent=True) or {}
        errors = []
        applied = {}

        au = data.get("auto_update") or {}
        if "enabled" in au:
            cfg.auto_update.enabled = bool(au["enabled"])
            applied["auto_update.enabled"] = cfg.auto_update.enabled
        if "interval_seconds" in au:
            try:
                value = int(au["interval_seconds"])
            except (TypeError, ValueError):
                value = -1
            if not (30 <= value <= 86400):
                errors.append(
                    "auto_update.interval_seconds: must be 30-86400"
                )
            else:
                cfg.auto_update.interval_seconds = value
                applied["auto_update.interval_seconds"] = value

        dc = data.get("discord") or {}
        for field in (
            "consumer_log_webhook_url",
            "update_webhook_url",
        ):
            if field in dc:
                url = str(dc[field]).strip()
                if url and not url.startswith("https://"):
                    errors.append(
                        f"discord.{field}: must be an https URL"
                    )
                else:
                    setattr(cfg.discord, field, url)
                    applied[f"discord.{field}"] = url

        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        if applied and config_path:
            try:
                with open(config_path, encoding="utf-8") as f:
                    import yaml as _yaml

                    raw = _yaml.safe_load(f) or {}
                if applied.get("auto_update.enabled") is not None:
                    raw.setdefault("auto_update", {})["enabled"] = (
                        cfg.auto_update.enabled
                    )
                if "auto_update.interval_seconds" in applied:
                    raw.setdefault(
                        "auto_update", {}
                    )["interval_seconds"] = (
                        cfg.auto_update.interval_seconds
                    )
                d = raw.setdefault("discord", {})
                for field in ("consumer_log_webhook_url",
                              "update_webhook_url"):
                    if f"discord.{field}" in applied:
                        d[field] = getattr(cfg.discord, field)
                from core.config import dump_yaml_config

                dump_yaml_config(raw, config_path)
            except OSError as e:
                return jsonify(
                    {"status": "error",
                     "errors": [f"could not write config: {e}"]}
                ), 500
        return jsonify({"status": "ok", "applied": applied})

    @app.get("/api/signals")
    def api_signals():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_signals(limit))

    return app