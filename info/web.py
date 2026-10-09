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
    app._last_reject_log_write = 0.0
    app._last_reject_log_feed = 0.0
    app.reader_desired = ""
    try:
        import yaml as _yaml

        rpath = os.path.join(
            os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
            "config", "reader.config.yaml",
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
        return bool(token) and hmac.compare_digest(
            supplied.encode("utf-8", "ignore"),
            token.encode("utf-8", "ignore"),
        )

    def _write_guard():
        """The other write routes guard the same way: a fake
        levels text reaches every consumer's ladder, and a fake
        settings POST redirects this app's log webhooks or
        disables its auto-update."""
        if not _reader_token_ok():
            # a rejected write (token mismatch) used to be fully
            # silent - werkzeug is quieted and the caller gives no
            # detail. rate-limited so a scanner cannot flood the log
            now = time.time()
            if now - app._last_reject_log_write > 60:
                app._last_reject_log_write = now
                logging.getLogger("info.write-guard").warning(
                    "write request rejected (bad or missing "
                    "X-Auth-Token) on %s %s - check that the "
                    "caller's token matches info.auth_token",
                    request.method, request.path,
                )
            return jsonify({"error": "unauthorized"}), 401
        return None

    install_gzip(app)

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
            now = time.time()
            if now - app._last_reject_log_write > 60:
                app._last_reject_log_write = now
                logging.getLogger("info.write-guard").warning(
                    "alert ingest rejected (bad or missing "
                    "X-Auth-Token) - check that the reader's "
                    "auth_token matches"
                )
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
            if c.token and hmac.compare_digest(
                c.token.encode("utf-8", "ignore"),
                token.encode("utf-8", "ignore"),
            ):
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
            # a rejected pull used to be fully silent - the
            # consumer's table dot just went hollow with no reason
            # logged anywhere. rate-limited like the write guard
            now = time.time()
            if now - app._last_reject_log_feed > 60:
                app._last_reject_log_feed = now
                logging.getLogger("info.feed-guard").warning(
                    "feed request rejected (bad or missing "
                    "X-Auth-Token) - the consumer's feed.token must "
                    "match a consumers[] entry on this server"
                )
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
            # an unparsable cursor must not fall back to 0 - that
            # would replay the entire alert history to the consumer
            return jsonify({"error": "invalid since"}), 400
        try:
            wait = min(
                max(float(request.args.get("wait", 0) or 0), 0.0), 25.0
            )
        except (TypeError, ValueError):
            wait = 0.0
        deadline = time.time() + wait
        rows = store.signals_since(since)
        from .fanout import record_seen
        from .ingest import FEED_WAKE

        if not rows and wait:
            # the wait can run 25s - refresh the seen-cursor up
            # front so a healthy long-poller's dot stays green
            record_seen(consumer.label, store.max_signal_rowid())
        while not rows and time.time() < deadline:
            with FEED_WAKE:
                # woken by ingest on every recorded signal; the
                # 1s slice is a safety net against a missed wake
                FEED_WAKE.wait(timeout=min(deadline - time.time(), 1.0))
            # the query rides OUTSIDE the lock: ingest's notify
            # must not queue behind every poller's db read
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
    # settings live in the reader's own yaml (config/reader.config.yaml)
    # - the heartbeat no longer pushes settings, it would
    # override the yaml on every poll
    # --------------------------------------------------------
    @app.post("/api/reader_status")
    def api_reader_status():
        # a fake heartbeat would mask a dead reader on the
        # dashboard - the same guard as the other write routes
        denied = _write_guard()
        if denied is not None:
            return denied
        data = request.get_json(silent=True) or {}
        app.reader_state["channel"] = (data.get("channel") or None)
        app.reader_state["ok"] = bool(data.get("ok"))
        app.reader_state["error"] = data.get("error")
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
        # the dashboard is open-read, but the webhook urls are
        # bearer credentials - only a request carrying the valid
        # write token (the settings editor sends it) sees the real
        # values; everyone else gets a placeholder
        trusted = _reader_token_ok()
        mask = "••••••••"

        def webhook(value):
            return value if trusted else (mask if value else "")

        return jsonify({
            "auto_update": {
                "enabled": cfg.auto_update.enabled,
                "interval_seconds": cfg.auto_update.interval_seconds,
            },
            "discord": {
                "consumer_log_webhook_url": webhook(
                    cfg.discord.consumer_log_webhook_url
                ),
                "update_webhook_url": webhook(
                    cfg.discord.update_webhook_url
                ),
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

        # a masked placeholder round-tripping from a client that
        # loaded the settings without the token means "unchanged"
        mask = "••••••••"

        # validate everything first, then apply - the old order
        # mutated cfg before the validation result was known, so a
        # 400 left the in-memory config diverging from disk
        pending = {}

        au = data.get("auto_update") or {}
        if "enabled" in au:
            pending["auto_update.enabled"] = bool(au["enabled"])
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
                pending["auto_update.interval_seconds"] = value

        dc = data.get("discord") or {}
        for field in (
            "consumer_log_webhook_url",
            "update_webhook_url",
        ):
            if field in dc:
                url = str(dc[field]).strip()
                if url == mask:
                    continue   # masked round-trip: leave as-is
                if url and not url.startswith("https://"):
                    errors.append(
                        f"discord.{field}: must be an https URL"
                    )
                else:
                    pending[f"discord.{field}"] = url

        if errors:
            return jsonify({"status": "error", "errors": errors}), 400

        # snapshot for the rollback: a failed disk write must
        # not leave the running process diverging from what a
        # restart would load
        snapshot = {
            key: getattr(
                cfg.auto_update if key.startswith("auto_update.")
                else cfg.discord,
                key.split(".", 1)[1],
            )
            for key in pending
        }
        for key, value in pending.items():
            section, field = key.split(".", 1)
            if section == "auto_update":
                setattr(cfg.auto_update, field, value)
            else:
                setattr(cfg.discord, field, value)
            applied[key] = value
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
                for key, value in snapshot.items():
                    section, field = key.split(".", 1)
                    if section == "auto_update":
                        setattr(cfg.auto_update, field, value)
                    else:
                        setattr(cfg.discord, field, value)
                return jsonify(
                    {"status": "error",
                     "errors": [f"could not write config: {e}"]}
                ), 500
        return jsonify({"status": "ok", "applied": applied})

    @app.get("/api/signals")
    def api_signals():
        limit = min(max(
            request.args.get("limit", default=50, type=int) or 50,
            1), 200)
        return jsonify(store.recent_signals(limit))

    return app