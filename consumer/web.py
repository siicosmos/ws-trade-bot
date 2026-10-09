"""The consumer dashboard's flask app.

create_app wires the session auth, the routes and the payload
builders - the heavy lifting lives in web_payloads (payloads +
bounded ws fetches), web_cards (margin/card math) and
web_context (the dependency bundle).
"""

import logging
import os
import re
import threading
import time
from datetime import timedelta

from flask import Flask, Response, g, jsonify, request, session

from consumer.dashboard import (
    LOGIN_HTML, dashboard_css, dashboard_html,
)
from consumer.pipeline import process_alert
from core.store import Store
from core.web_common import (
    install_auth, install_gzip, install_quiet_filter, load_secret_key,
)
from .web_context import PipelineContext
from .web_payloads import (
    _bounded, _dashboard_sections, _match_store_position,
    _paper_positions_payload, _positions_payload, _section_cache,
    _sec_id_cache, _summary_payload, _ws_quote_cache,
    _ws_quote_fail_ts, _ws_stock_quote,
)


# re-exported for the tests that import it from here
from .web_cards import _paper_card_metrics  # noqa: F401


def create_app(cfg, store: Store, risk, executor, account=None,
               config_path=None) -> Flask:
    app = Flask(__name__)
    install_quiet_filter()
    app.secret_key = load_secret_key(config_path)
    # cookies ignore ports: two apps on the same host (two
    # consumers, or a consumer next to anything else) must not
    # share a cookie name or they log each other out
    app.config["SESSION_COOKIE_NAME"] = "ws_session_" + str(
        getattr(cfg.pipeline, "port", 8080)
    )
    # a stolen cookie on a trading dashboard must not stay valid
    # for a month - 12h covers a trading day and re-login is one
    # token away
    app.permanent_session_lifetime = timedelta(hours=12)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    # the dashboard assets change with every release - the flask
    # default (hours of browser caching) serves stale js after an
    # update; these files are tiny and local/tailnet, always
    # revalidate
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # explicit: the session cookie is the browser's only
    # credential - javascript must never see it
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    if getattr(cfg.pipeline, "tls_cert", "") and getattr(
        cfg.pipeline, "tls_key", ""
    ):
        app.config["SESSION_COOKIE_SECURE"] = True
    ctx = PipelineContext(
        cfg=cfg, store=store, risk=risk, executor=executor,
        account=account, mode=cfg.trading.mode,
        config_path=config_path,
    )
    # compat aliases (tests poke these directly)
    app.reader_state = ctx.reader_state
    app.feed_state = ctx.feed_state
    app._summary_cache = ctx.summary_cache

    # first boot: the access token becomes the admin password -
    # the token is the bootstrap (it is mandatory, see the
    # refusal check in app.main)
    if store.user_count() == 0 and cfg.pipeline.auth_token:
        store.create_user("admin", cfg.pipeline.auth_token, "admin")

    # the 5s dashboard polls otherwise flood consumer.log with
    # access lines for every api call
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    install_auth(app, cfg, store, LOGIN_HTML)
    install_gzip(app)

    _dashboard_cache = {"html": ""}

    @app.get("/")
    def dashboard_page():
        # the stylesheet is inlined into the head: one request
        # fewer on the critical path and the layout (the reserved
        # trade-log block especially) is painted before the js
        # pulls data - the load-time layout shift shrinks to the
        # first-paint frame. re-read when the file changed on
        # disk so a code update never serves an old page with
        # new js
        html = _dashboard_cache["html"]
        current = dashboard_html().replace(
            '<link rel="stylesheet" href="/static/dashboard.css">',
            "<style>\n" + dashboard_css() + "\n</style>",
            1,
        )
        if html != current:
            _dashboard_cache["html"] = current
        resp = app.response_class(current, mimetype="text/html")
        # the page carries the versioned script url - a cached
        # page would keep stale js alive across updates
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

    def _me():
        return session.get("user")

    def _is_admin() -> bool:
        if getattr(g, "admin", False):
            # the machine token (reader, scripts) acts as the owner
            return True
        user = session.get("user")
        return bool(user) and user.get("role") == "admin"

    def _require_admin():
        if not _is_admin():
            return jsonify({"error": "admin required"}), 403
        return None

    @app.get("/api/summary")
    def api_summary():
        payload = _bounded(_summary_payload, ctx)
        if payload is None:
            payload = {
                "error": "summary refresh timed out - "
                "the ws api is not answering",
            }
        if "error" in payload:
            return jsonify(payload), 502
        return jsonify(payload)

    @app.post("/api/paper-reset")
    def api_paper_reset():
        denied = _require_admin()
        if denied:
            return denied
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ) and ctx.mode != "paper":
            return jsonify(
                {"error": "paper trading is not active"}
            ), 400
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label") or "").strip()
        if not label:
            return jsonify({"error": "label required"}), 400
        store.reset_paper_account(label)
        seeded = []
        if hasattr(account, "_positions_raw"):
            from consumer.ws.account import seed_paper_accounts

            seeded = seed_paper_accounts(cfg, store, account) or []
        ctx.summary_cache["ts"] = 0.0
        # the dashboard's stale-while-revalidate cache would
        # otherwise keep serving the pre-reset positions for up
        # to 15s - drop them so the next poll shows the reset
        _section_cache.pop("paper", None)
        _section_cache.pop("positions", None)
        _section_cache.pop("summary", None)
        return jsonify({
            "status": "ok", "label": label,
            "reseeded": label in seeded,
        })

    @app.post("/api/paper-adjust")
    def api_paper_adjust():
        """The paper account's adjust editor: set the cash
        pools (CAD + USD) and replace the holdings (edit qty /
        avg, remove, add new rows)."""
        denied = _require_admin()
        if denied:
            return denied
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ) and ctx.mode != "paper":
            return jsonify(
                {"error": "paper trading is not active"}
            ), 400
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label") or "").strip()
        if not label:
            return jsonify({"error": "label required"}), 400

        errors = []
        # validate first, apply last - the cash edits used to land
        # before the holdings validation ran, so a holdings error
        # returned 400 with the cash already changed
        pending_cad = None
        pending_usd = None
        if payload.get("cash_cad") is not None:
            try:
                pending_cad = max(0.0, round(float(payload["cash_cad"]), 2))
            except (TypeError, ValueError):
                errors.append("cash_cad: not a number")
        if payload.get("cash_usd") is not None:
            try:
                pending_usd = max(0.0, round(float(payload["cash_usd"]), 2))
            except (TypeError, ValueError):
                errors.append("cash_usd: not a number")

        holdings = payload.get("holdings")
        if holdings is not None:
            if not isinstance(holdings, list):
                errors.append("holdings: expected a list")
            else:
                rows = []
                for i, h in enumerate(holdings):
                    if not isinstance(h, dict):
                        errors.append(f"holdings[{i}]: expected a mapping")
                        continue
                    underlying = str(
                        h.get("underlying") or ""
                    ).strip().upper()[:20]
                    qty = h.get("qty")
                    avg = h.get("avg") or 0.0
                    try:
                        qty = int(float(qty))
                    except (TypeError, ValueError):
                        errors.append(
                            f"holdings[{i}].qty: not a number"
                        )
                        continue
                    try:
                        avg = round(float(avg), 6)
                    except (TypeError, ValueError):
                        errors.append(
                            f"holdings[{i}].avg: not a number"
                        )
                        continue
                    right = str(h.get("right") or "").strip().upper()[:1]
                    if right not in ("C", "P"):
                        # stock holding: the key is the symbol
                        if not underlying:
                            errors.append(
                                f"holdings[{i}]: underlying required"
                            )
                            continue
                        rows.append(
                            (underlying, underlying, None, None,
                             None, qty, avg)
                        )
                        continue
                    expiry = str(h.get("expiry") or "").strip()[:10]
                    try:
                        strike = float(h.get("strike"))
                    except (TypeError, ValueError):
                        errors.append(
                            f"holdings[{i}].strike: not a number"
                        )
                        continue
                    if not underlying or not expiry or qty <= 0:
                        errors.append(
                            f"holdings[{i}]: underlying, expiry "
                            "and a positive qty are required"
                        )
                        continue
                    rows.append((
                        f"{underlying}-{expiry}-{strike:g}-{right}",
                        underlying, expiry, strike, right, qty, avg,
                    ))
                if not errors:
                    store.set_paper_holdings(label, rows)

        if not errors:
            if pending_cad is not None:
                store.set_paper_equity(pending_cad, label)
            if pending_usd is not None:
                store.set_paper_cash_usd(pending_usd, label)

        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        ctx.summary_cache["ts"] = 0.0
        _section_cache.pop("paper", None)
        _section_cache.pop("summary", None)
        ledger = getattr(executor, "account", None)
        return jsonify({
            "status": "ok", "label": label,
            "value": (
                round(ledger.value(label), 2)
                if ledger is not None and hasattr(ledger, "value")
                else None
            ),
        })

    @app.get("/api/paper-positions")
    def api_paper_positions():
        payload = _bounded(_paper_positions_payload, ctx)
        if payload is None:
            payload = {
                "error": "paper pricing timed out - "
                "the ws api is not answering",
            }
        if isinstance(payload, dict) and "error" in payload:
            return jsonify(payload), 500
        return jsonify(payload)

    @app.post("/api/position-sell")
    def api_position_sell():
        """Manual close of a tracked LIVE position from the open
        positions table: places a REAL sell order at the current
        bid (the same path a stop-monitor exit takes). Sells the
        whole position. Paper rows keep using /api/paper-sell."""
        denied = _require_admin()
        if denied:
            return denied
        if ctx.mode != "live":
            return jsonify(
                {"error": "live manual sell needs live mode"}
            ), 400
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label") or "").strip()
        contract_key = str(payload.get("contract_key") or "").strip()
        if not label or not contract_key:
            return jsonify(
                {"error": "label and contract_key required"}
            ), 400
        row = _match_store_position(
            store, "live", label, contract_key,
            _bounded(_positions_payload, ctx),
        )
        if row is None:
            return jsonify(
                {"error": f"no live position {contract_key!r} "
                          f"on {label!r}"}
            ), 404
        key = row["contract_key"]
        if int(row.get("qty") or 0) < 1:
            return jsonify({"error": "position is empty"}), 400

        # a real sell order at the current bid
        from core.parser import Alert

        alert = Alert(
            action="SELL",
            ticker=row["underlying"],
            kind="option",
            underlying=row["underlying"],
            expiry=row["expiry"],
            strike=row["strike"],
            right=row["right"],
            raw=f"[MANUAL] dashboard sell of {key}",
        )
        executor = ctx.executor
        if executor is None or not hasattr(executor, "execute"):
            return jsonify({"error": "no executor configured"}), 400
        try:
            result = executor.execute(alert, cfg, store)
        except Exception as e:
            store.record_trade(
                "live", "SELL", row["underlying"], 0, None,
                alert, "error", f"[MANUAL] failed: {e}",
                message_key=key,
            )
            return jsonify({"error": str(e)}), 500
        store.record_trade(
            "live", "SELL", row["underlying"], result.qty,
            result.price, alert,
            "executed" if result.ok else "skipped",
            f"[MANUAL] {result.detail}", message_key=key,
        )
        _section_cache.pop("positions", None)
        _section_cache.pop("summary", None)
        return jsonify({
            "status": "ok" if result.ok else "skipped",
            "sold": result.qty,
            "price": result.price,
            "detail": result.detail,
        })

    @app.post("/api/position-tp")
    def api_position_tp():
        """Per-position sell guards: take-profit (sell the whole
        remaining position when its gain vs the entry premium
        reaches this percent) and a per-position trailing stop
        (sell once the bid falls this % off its peak). The ALL
        OUT alert is not guaranteed to arrive. Set beside each
        position row; a null clears that guard."""
        denied = _require_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or ctx.mode or "paper")
        if mode not in ("paper", "live"):
            return jsonify({"error": "mode must be paper or live"}), 400
        label = str(payload.get("label") or "").strip()
        contract_key = str(payload.get("contract_key") or "").strip()
        if not label or not contract_key:
            return jsonify(
                {"error": "label and contract_key required"}
            ), 400

        def _pct(value, name):
            if value in (None, ""):
                return None
            try:
                pct = float(value)
            except (TypeError, ValueError):
                return False
            if not (0 <= pct <= 10000):
                return False
            return round(pct, 2)

        tp = _pct(payload.get("tp_gain_pct"), "tp_gain_pct")
        trail = _pct(payload.get("trail_pct"), "trail_pct")
        if tp is False or trail is False:
            return jsonify(
                {"error": "tp_gain_pct / trail_pct must be numbers "
                          "between 0 and 10000"}
            ), 400
        row = _match_store_position(
            store, mode, label, contract_key,
            _bounded(_positions_payload, ctx),
        )
        if row is None:
            return jsonify(
                {"error": f"no {mode} position "
                          f"{contract_key!r} on {label!r}"}
            ), 404
        contract_key = row["contract_key"]
        if "tp_gain_pct" in payload:
            store.set_position_tp(mode, label, contract_key, tp)
        if "trail_pct" in payload:
            store.set_position_trail(mode, label, contract_key, trail)
        # the dashboard's stale-while-revalidate cache would
        # otherwise keep serving the pre-update rows
        _section_cache.pop("paper", None)
        _section_cache.pop("positions", None)
        fresh = next(
            (r for r in store.list_positions(mode, label)
             if r["contract_key"] == contract_key),
            {},
        )
        return jsonify({
            "status": "ok",
            "contract_key": contract_key,
            "tp_gain_pct": fresh.get("tp_gain_pct"),
            "trail_pct": fresh.get("trail_pct"),
        })

    @app.post("/api/paper-sell")
    def api_paper_sell():
        """Manual close of a paper position at its live price.

        The proceeds return to the paper cash (options x100 at
        the fx rate, stocks x1) exactly like an executed paper
        sell; the position row realizes its pnl and the loss
        streak updates through the same path an alert sell takes.
        Sells the whole position unless a qty is given."""
        denied = _require_admin()
        if denied:
            return denied
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ) and ctx.mode != "paper":
            return jsonify(
                {"error": "paper trading is not active"}
            ), 400
        payload = request.get_json(silent=True) or {}
        label = str(payload.get("label") or "").strip()
        contract_key = str(payload.get("contract_key") or "").strip()
        if not label or not contract_key:
            return jsonify(
                {"error": "label and contract_key required"}
            ), 400
        rows = store.list_positions("paper", label)
        row = next(
            (r for r in rows if r["contract_key"] == contract_key),
            None,
        )
        if row is None:
            return jsonify(
                {"error": f"no paper position {contract_key!r} "
                 f"on {label!r}"}
            ), 404
        held = int(row["qty"])
        if held < 1:
            return jsonify({"error": "position is empty"}), 400
        qty = max(1, min(int(payload.get("qty") or 0) or held, held))
        # the live price the holdings table shows; the avg premium
        # is the fallback when no quote is available
        price = None
        row_usd = None
        try:
            # bounded: the paper pricing can reach the slow ws
            # api - the sell must answer fast (the avg premium
            # is the fallback when the bound misses)
            live = _bounded(_paper_positions_payload, ctx) or {}
            for r in live.get(label) or []:
                if r.get("contract_key") == contract_key:
                    if r.get("price"):
                        price = float(r["price"])
                    # the listing's currency rides the priced row
                    # (the store's position row has no usd column)
                    row_usd = bool(r.get("usd"))
                    break
        except Exception:
            price = None
        if not price:
            price = float(row["avg_premium"] or 0.0)
        if not price:
            return jsonify(
                {"error": "no price for this contract - "
                 "cannot sell without a quote"}
            ), 409
        from core.parser import Alert

        is_option = bool(row["right"])
        alert = Alert(
            action="SELL",
            ticker=row["underlying"],
            kind="option" if is_option else "stock",
            underlying=row["underlying"] or "",
            expiry=row["expiry"],
            strike=row["strike"],
            right=row["right"],
            premium=price,
        )
        if alert.contract_key() != contract_key:
            return jsonify(
                {"error": f"contract fields do not match "
                 f"{contract_key!r}"}
            ), 400
        mult = 100 if is_option else 1
        avg = float(row["avg_premium"] or 0.0)
        # the proceeds return to the paper cash the same way an
        # executed sell books them: options and usd-listed stocks
        # convert at the ledger's fx, cad stocks book raw
        fx = 1.0
        ledger = getattr(ctx.executor, "account", None)
        if ledger is not None:
            try:
                fx = ledger.fx() or 1.0
            except Exception:
                fx = 1.0
        usd = row_usd if row_usd is not None else is_option
        fx_eff = fx if (is_option or usd) else 1.0
        realized = (
            round(qty * (price - avg) * mult * fx_eff, 2)
            if avg else 0.0
        )
        store.apply_position(
            "paper", alert, -qty, premium=price, account=label,
            fx=fx_eff,
        )
        credit = qty * price * (100 if is_option else 1)
        store.adjust_paper_equity(credit * fx_eff, label)
        remaining = store.get_position("paper", contract_key, label)
        detail = (
            f"[PAPER] manual SELL {qty}/{held}x "
            f"{contract_key} @ {price} · realized "
            f"{realized:+.2f} · {remaining}x left"
        )
        store.record_trade(
            "paper", "SELL", row["underlying"], qty, price, alert,
            "executed", detail,
            message_key=f"manual-{int(time.time() * 1000)}",
        )
        ctx.summary_cache["ts"] = 0.0
        # drop the cached sections - the next dashboard poll must
        # show the position gone, not a 15s-stale pre-sell copy
        # (that lag once made the sell look like it did nothing)
        _section_cache.pop("paper", None)
        _section_cache.pop("positions", None)
        return jsonify({
            "status": "ok", "label": label,
            "contract_key": contract_key,
            "sold": qty, "price": price,
            "realized": round(realized, 2),
            "remaining": remaining,
        })

    @app.get("/api/positions")
    def api_positions():
        return jsonify(_positions_payload(ctx))

    @app.get("/api/signals")
    def api_signals():
        limit = min(max(
            request.args.get("limit", default=50, type=int) or 50,
            1), 200)
        return jsonify(store.recent_signals(limit))

    @app.get("/api/trades")
    def api_trades():
        limit = min(max(
            request.args.get("limit", default=50, type=int) or 50,
            1), 200)
        return jsonify(store.recent_trades(limit))

    @app.get("/api/spx")
    def api_spx():
        """Realtime SPX/SPY spot for the levels ladder (from the
        moomoo feed when configured, the ws quote api otherwise).
        SPY trades overnight and post-market, so its own quote
        keeps the spy ladder live when the index is closed; the
        closed index shows its market close instead of a spot."""
        from consumer.trading.quotes import ACTIVE_QUOTE_PROVIDER

        provider = ACTIVE_QUOTE_PROVIDER
        price = None
        age = None
        error = None
        status = None
        proxy_spx = None
        if provider is not None and hasattr(provider, "index_quote"):
            try:
                q = provider.index_quote("SPX")
                if q and getattr(provider, "_index_proxy", False):
                    # the moomoo value is the spy etf x the
                    # converter ratio, not the index itself -
                    # overnight the close is wanted, so keep the
                    # proxy only as a last resort behind the ws
                    # quote (which also carries the market status)
                    proxy_spx = q
                elif q:
                    price = q
                    age = max(0, int(time.time() - provider._index_ts))
                    error = getattr(provider, "_index_error", None)
                else:
                    error = getattr(provider, "_index_error", None)
            except Exception as e:
                price = None
                error = str(e)
        if not price:
            # ws fallback: a live quote straight from the ws api
            # (the index when listed, else the spy etf x ratio)
            try:
                spx_q = _ws_stock_quote(account, "SPX")
                if spx_q and spx_q[0]:
                    price = round(spx_q[0], 2)
                    status = spx_q[1]
                    error = None
                    age = None
                else:
                    spy_q = _ws_stock_quote(account, "SPY")
                    if spy_q and spy_q[0]:
                        # the index is only reachable through the
                        # etf here - it is closed whenever that
                        # path is in use
                        price = round(spy_q[0] * 10.0391, 2)
                        status = "CLOSED"
                        error = None
                        age = None
            except Exception:
                pass
        if not price:
            # last resort: option positions carry the underlying's
            # own quote (the spx index spot)
            try:
                live = {}
                if account is not None and hasattr(
                    account, "open_option_positions"
                ):
                    live = account.open_option_positions() or {}
                for row in live.values():
                    if not row:
                        continue
                    for r in row["positions"]:
                        u = (r.get("underlying") or "").upper()
                        up = r.get("underlying_price")
                        if u == "SPX" and up:
                            price = up
                            error = None
                            age = None
                            break
                    if price:
                        break
            except Exception:
                pass
        if not price:
            error = error or "no spx spot from moomoo or ws"
        if not price and proxy_spx:
            # ws could not answer at all - the spy-derived proxy
            # still points at the right level
            price = proxy_spx
            error = None
        # spy's own realtime quote: moomoo first (local opend,
        # live through the overnight session, no ws round trips),
        # the bounded ws quote as fallback. the moomoo quote
        # carries no market status - the ui treats it as live
        spy = None
        spy_status = None
        if provider is not None and hasattr(provider, "stock_quote"):
            try:
                # extended=True: the ladder's spy spot follows the
                # active extended session (post / overnight) instead
                # of freezing at the regular close
                spy = provider.stock_quote("SPY", extended=True)
            except Exception:
                spy = None
        if not spy:
            try:
                spy_q = _ws_stock_quote(account, "SPY")
            except Exception:
                spy_q = None
            spy = spy_q[0] if spy_q else None
            spy_status = spy_q[1] if spy_q else None
        # stale = the market is not trading: the close shows
        # marked as 'close' instead of 'now' (the moomoo feed
        # reports an age, ws reports the market status)
        stale = bool(age and age > 120) or status == "CLOSED"
        # the saved levels text rides along: it is the
        # cross-device source of truth for the ladder - on a
        # consumer it is owned by the info server's feed sync
        # (feedclient writes it into the store)
        return jsonify({
            "price": price, "age": age, "error": error,
            "stale": stale, "status": status,
            "spy": spy, "spy_status": spy_status,
            "refresh_seconds": getattr(
                getattr(cfg, "wealthsimple", None),
                "positions_refresh_seconds", 30,
            ),
            "text": store.meta_get("spx_levels_text"),
            "ts": time.time(),
        })

    @app.get("/api/history")
    def api_history():
        kind = request.args.get("kind", "trades")
        if kind not in ("trades", "signals", "both"):
            kind = "trades"
        limit = min(max(
            request.args.get("limit", default=50, type=int) or 50, 1), 200)
        offset = max(
            request.args.get("offset", default=0, type=int) or 0, 0)
        rows, total = store.search_history(
            kind=kind,
            ticker=request.args.get("ticker", "").strip() or None,
            action=request.args.get("action", "").strip() or None,
            status=request.args.get("status", "").strip() or None,
            mode=request.args.get("mode", "").strip() or None,
            since=request.args.get("since", "").strip() or None,
            until=request.args.get("until", "").strip() or None,
            q=request.args.get("q", "").strip() or None,
            limit=limit,
            offset=offset,
        )
        return jsonify({"kind": kind, "total": total, "limit": limit,
                        "offset": offset, "rows": rows})

    @app.get("/api/settings")
    def api_settings_get():
        from consumer.settings import get_settings

        # webhook urls are credentials - viewers get a masked view
        return jsonify(get_settings(cfg, mask_secrets=not _is_admin()))

    @app.get("/api/dashboard")
    def api_dashboard():
        """Everything the dashboard polls, in one round trip.
        Each ws-touching section refreshes concurrently, bounded
        and cached - a slow or stalled ws api serves the last
        good payload instead of pinning the request thread."""
        from consumer.settings import get_settings
        from core.ops.updater import update_status_payload

        summary, paper, positions = _dashboard_sections(ctx)
        return jsonify(
            {
                "summary": summary,
                "paper_positions": paper,
                "positions": positions,
                "signals": store.recent_signals(50),
                "trades": store.recent_trades(50),
                "settings": get_settings(cfg, mask_secrets=not _is_admin()),
                "update_status": update_status_payload(app),
                "me": _me(),
            }
        )

    @app.post("/api/settings")
    def api_settings_post():
        denied = _require_admin()
        if denied:
            return denied
        from consumer.settings import apply_settings

        payload = request.get_json(silent=True) or {}
        applied, errors = apply_settings(cfg, payload, config_path)
        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        # settings feed the summaries - drop the cache at once
        ctx.summary_cache["ts"] = 0.0
        return jsonify(
            {
                "status": "ok",
                "applied": applied,
                "restart_required": False,
            }
        )

    @app.post("/api/mode")
    def api_mode_post():
        """The mode slider: persist trading.mode and restart this
        app (the executors/threads wire by mode at startup)."""
        denied = _require_admin()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in ("notify", "paper", "live"):
            return jsonify(
                {"status": "error",
                 "errors": ["mode: must be notify, paper or live"]}
            ), 400
        if mode == cfg.trading.mode:
            return jsonify({
                "status": "ok", "mode": mode, "restarting": False,
            })
        from consumer.settings import set_mode

        if not set_mode(cfg, mode, config_path):
            return jsonify(
                {"status": "error",
                 "errors": ["could not write the config file"]}
            ), 500
        restart = callable(getattr(app, "restart_pipeline", None))
        if restart:
            # respond first, then exit - the .bat loop restarts
            # with the new mode
            def _late_exit():
                time.sleep(1.5)
                app.restart_pipeline(f"mode switched to {mode}")

            threading.Thread(target=_late_exit, daemon=True).start()
        return jsonify({
            "status": "ok", "mode": mode, "restarting": restart,
        })

    @app.get("/api/update_status")
    def api_update_status():
        from core.ops.updater import update_status_payload

        return jsonify(update_status_payload(app))

    @app.post("/api/paper-resize")
    def api_paper_resize():
        """Bring past paper stock positions up to the tier
        sizing (older trades were sized with the flat dollar
        budget). The original alert's size keyword is recovered
        from the signal text; unsized alerts use the medium
        tier."""
        denied = _require_admin()
        if denied:
            return denied
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ) and ctx.mode != "paper":
            return jsonify(
                {"error": "paper trading is not active"}
            ), 400
        ledger = getattr(executor, "account", None)
        if ledger is None or not hasattr(ledger, "values"):
            return jsonify({"error": "no paper ledger"}), 400
        from consumer.trading.mirror import MirrorShim
        from core.parser import parse_alert

        tiers = getattr(
            cfg.trading, "stock_size_tiers", None
        ) or {}
        target_label = str(
            (request.get_json(silent=True) or {}).get("label") or ""
        ).strip()
        values = ledger.values() or {}
        if target_label and target_label in values:
            values = {target_label: values[target_label]}
        adjusted = []
        for label, value in values.items():
            if not value or value <= 0:
                continue
            for pos in store.list_positions("paper", label):
                if pos.get("right"):
                    continue   # options only
                held = pos.get("qty") or 0
                avg = pos.get("avg_premium") or 0
                if held < 1 or not avg:
                    continue
                # the original alert's size keyword. the trade
                # rows are per-ALERT (one aggregate row across the
                # accounts), so the lookup matches the newest paper
                # BUY for the ticker - an account filter could
                # never match and every position silently resized
                # to the medium tier
                trade_key = None
                for t in store.recent_trades(200):
                    if (
                        t.get("mode") == "paper"
                        and t.get("ticker") == pos.get("underlying")
                        and t.get("action") == "BUY"
                    ):
                        trade_key = t.get("message_key")
                        break
                size = None
                text = store.signal_text(trade_key) if trade_key else None
                if text:
                    alert = parse_alert(text)
                    if alert is not None:
                        size = alert.size
                tier = (size or "medium").lower()
                pct = tiers.get(tier) or tiers.get("medium")
                if pct is None:
                    continue
                intended = int(
                    float(value) * (pct / 100.0) / avg
                )
                delta = int(intended - held)
                if delta == 0:
                    continue
                shim = MirrorShim(
                    action="BUY" if delta > 0 else "SELL",
                    kind="stock",
                    underlying=pos.get("underlying"),
                    expiry=None, strike=None, right=None,
                    premium=avg, entry=avg,
                    stop_loss=None, take_profit=None,
                    ts=str(time.time()),
                )
                shim.dedupe_key = (
                    lambda k=label, u=pos.get("underlying"):
                    f"RESIZE|{k}|{u}|{time.time()}"
                )
                store.apply_position(
                    "paper", shim, delta, premium=avg, account=label
                )
                store.adjust_paper_equity(
                    -delta * avg, label
                )
                store.record_trade(
                    "paper", "BUY" if delta > 0 else "SELL",
                    pos.get("underlying"), abs(delta), avg,
                    shim, "executed",
                    f"resized to {tier} tier: {held} -> {intended} "
                    f"shares", trade_key,
                )
                adjusted.append(
                    f"{label}: {pos.get('underlying')} "
                    f"{held} -> {intended}"
                )
        ctx.summary_cache["ts"] = 0.0
        return jsonify({
            "status": "ok", "adjusted": adjusted,
        })

    @app.get("/api/users")
    def api_users_get():
        denied = _require_admin()
        if denied:
            return denied
        return jsonify(store.list_users())

    @app.post("/api/users")
    def api_users_post():
        me = _me()
        if me is None:
            return jsonify({"error": "unauthorized"}), 401
        admin = me.get("role") == "admin"
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "")
        target = str(data.get("username") or "").strip()

        if action == "create":
            if not admin:
                return jsonify({"error": "admin required"}), 403
            username = target
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", username):
                return jsonify({"error": "bad username"}), 400
            password = str(data.get("password") or "")
            if len(password) < 6:
                return jsonify(
                    {"error": "password must be at least 6 characters"}
                ), 400
            role = str(data.get("role") or "viewer")
            if not store.create_user(username, password, role):
                return jsonify(
                    {"error": "username taken or bad role"}
                ), 400
            return jsonify({"status": "ok"})

        if action == "delete":
            if not admin:
                return jsonify({"error": "admin required"}), 403
            if target == me.get("username"):
                return jsonify({"error": "cannot delete yourself"}), 400
            admins = [u for u in store.list_users()
                      if u["role"] == "admin"]
            if len(admins) == 1 and admins[0]["username"] == target:
                return jsonify(
                    {"error": "cannot delete the last admin"}
                ), 400
            if not store.delete_user(target):
                return jsonify({"error": "unknown user"}), 404
            return jsonify({"status": "ok"})

        if action == "set_password":
            new_password = str(data.get("password") or "")
            if len(new_password) < 6:
                return jsonify(
                    {"error": "password must be at least 6 characters"}
                ), 400
            if target == me.get("username"):
                # self change requires the current password (an
                # admin too - a hijacked session must not silently
                # take over the account)
                current = str(data.get("current_password") or "")
                user = store.verify_user(me.get("username"), current)
                if user is None:
                    return jsonify(
                        {"error": "current password is wrong"}
                    ), 403
                store.update_password(target, new_password)
                return jsonify({"status": "ok"})
            # someone else's password: admin only
            if not admin:
                return jsonify({"error": "admin required"}), 403
            if not store.get_user(target):
                return jsonify({"error": "unknown user"}), 404
            if not store.update_password(target, new_password):
                return jsonify({"error": "update failed"}), 400
            return jsonify({"status": "ok"})

        return jsonify({"error": "unknown action"}), 400

    @app.post("/alert")
    def alert():
        # alerts execute real orders in live mode - viewers (and
        # any authenticated-but-not-owner session) must not post
        # them; the machine token (the info server's push) and
        # admin sessions pass
        if not _is_admin():
            return jsonify({"error": "admin required"}), 403
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
        result = process_alert(
            text, author, cfg, store, risk, executor, account,
            channel=str(data.get("channel") or ""), ts=ts,
            parsed_ts=parsed_ts,
        )
        return jsonify(result)

    return app
