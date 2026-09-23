import hmac
import logging
import os
import secrets
import time
from datetime import timedelta

from flask import Flask, Response, jsonify, redirect, request, session

from ..ws.account_types import REGISTERED_ACCOUNT_TYPES
from .dashboard import LOGIN_HTML
from ..pipeline import process_alert
from ..store import Store

QUIET_GET_PATHS = (
    "/",
    "/health",
    "/favicon.ico",
    "/api/summary",
    "/api/positions",
    "/api/signals",
    "/api/trades",
    "/api/settings",
    "/api/reader_status",
    "/api/update_status",
)

QUIET_POST_PATHS = (
    "/api/reader_status",
)


QUIET_GET_PREFIXES = (
    "/.well-known/",
)


class QuietPathsFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for path in QUIET_POST_PATHS:
            if f" {path} HTTP" in msg:
                return False
        for path in QUIET_GET_PATHS:
            if f"GET {path} HTTP" in msg:
                return False
        for prefix in QUIET_GET_PREFIXES:
            if f"GET {prefix}" in msg:
                return False
        return True


def install_quiet_filter():
    werkzeug = logging.getLogger("werkzeug")
    if not any(
        isinstance(f, QuietPathsFilter) for f in werkzeug.filters
    ):
        werkzeug.addFilter(QuietPathsFilter())


LOGIN_FAIL_LIMIT = 5
LOCKOUT_SECONDS = 900
_LOGIN_FAILS = {}


def _load_secret_key(config_path):
    if not config_path:
        return secrets.token_hex(32)
    key_file = os.path.join(
        os.path.dirname(os.path.abspath(config_path)), ".session_key"
    )
    try:
        with open(key_file) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w") as f:
            f.write(key)
    except OSError:
        pass
    return key


def _paper_card_metrics(
    ledger, store, cfg, label, value, registered, conv_fx
):
    """Margin-style card metrics for a paper account, computed
    with the same model as the real cards: long options at 100%,
    spreads at full wing width, stocks at their margin rate,
    margin used being negative ledger cash."""
    cash = store.paper_equity(label)
    rows = []
    pos_fn = getattr(ledger, "positions", None)
    if callable(pos_fn):
        try:
            rows = pos_fn(label) or []
        except Exception:
            rows = []
    by_key = {r["contract_key"]: r for r in rows}
    stock_value = round(sum(
        (r.get("value") or 0)
        for r in rows if r.get("kind") == "stock"
    ), 2)
    option_value = round(sum(
        abs(r.get("value") or 0)
        for r in rows if r.get("kind") == "option"
    ), 2)
    pos_cash = max(cash or 0.0, 0.0)
    alloc_base = round(
        stock_value + option_value + pos_cash, 2
    ) or None

    out = {
        "paper_cash": cash,
        "paper_stock_value": stock_value or None,
        "paper_option_value": option_value or None,
        "paper_alloc_base": alloc_base,
    }

    # open risk from the paper trade log
    try:
        open_risk = store.open_risk("paper", label)
    except Exception:
        open_risk = 0.0
    out["paper_open_risk"] = open_risk
    out["paper_open_risk_pct"] = (
        round(open_risk / value * 100, 2)
        if value and value > 0 else 0.0
    )

    if registered or not value:
        out.update(
            {
                "paper_margin_requirement": None,
                "paper_margin_breakdown": [],
                "paper_margin_used": None,
                "paper_margin_used_usd": None,
                "paper_margin_used_cad": None,
                "paper_margin_available": None,
                "paper_max_buying_power": None,
                "paper_portfolio_value": None,
            }
        )
        return out

    default_rate = getattr(
        cfg.wealthsimple, "stock_margin_rate", 0.30
    )
    overrides = getattr(
        cfg.wealthsimple, "margin_rate_overrides", {}
    ) or {}

    from ..trading.margin import (
        Holding, compute_requirement, resolve_rate,
    )

    holdings = []
    for r in rows:
        if r.get("kind") != "stock":
            continue
        is_usd = bool(r.get("usd"))
        holdings.append(
            Holding(
                symbol=str(r.get("underlying") or ""),
                kind="stock",
                currency="usd" if is_usd else "cad",
                # ledger values are cad - carry the native figure
                native_value=(
                    (r.get("value") or 0) / conv_fx
                    if is_usd and conv_fx
                    else (r.get("value") or 0)
                ),
            )
        )

    # option legs grouped per underlying+expiry, classified into
    # structures so spreads charge their wing width
    groups = {}
    for pos in store.list_positions("paper", label):
        if not pos.get("right"):
            continue
        key = (pos.get("underlying"), pos.get("expiry"))
        groups.setdefault(key, []).append(pos)
    from ..trading.strategies import classify_legs

    for (sym, _expiry), legs in groups.items():
        shaped = []
        for leg in legs:
            try:
                qty = float(leg["qty"] or 0)
            except (TypeError, ValueError):
                continue
            if not qty:
                continue
            shaped.append(
                {
                    "strike": leg.get("strike"),
                    "right": leg.get("right"),
                    "short": qty < 0,
                    "qty": abs(qty),
                }
            )
        if not shaped:
            continue
        info = classify_legs(shaped)
        # usd underlyings charge their width in cad
        usd = any(
            (by_key.get(l.get("contract_key")) or {})
            .get("usd")
            for l in legs
        )
        cur = "usd" if usd else "cad"
        if info and info.get("kind") in (
            "vertical", "butterfly", "condor",
            "iron fly", "ratio",
        ):
            holdings.append(
                Holding(
                    symbol=sym,
                    kind="spread",
                    currency=cur,
                    structure=info.get("name"),
                    qty=info.get("qty") or 0,
                    width=info.get("width") or 0,
                )
            )
        else:
            # long (or unpaired) legs carry their full value
            for leg in legs:
                row = by_key.get(leg.get("contract_key"))
                if row is None:
                    continue
                is_usd = bool(row.get("usd"))
                holdings.append(
                    Holding(
                        symbol=leg.get("contract_key") or "?",
                        kind="long",
                        currency=cur,
                        native_value=(
                            abs(row.get("value") or 0) / conv_fx
                            if is_usd and conv_fx
                            else abs(row.get("value") or 0)
                        ),
                    )
                )

    margin_req, parts = compute_requirement(
        holdings, conv_fx,
        lambda h: resolve_rate(
            h.symbol, None, None, overrides, default_rate
        ),
    )
    used = max(0.0, -(cash or 0.0))
    # currency split of the paper loan, mirroring the real
    # account: usd holdings are what usd borrowing backs
    usd_rows_value = sum(
        abs(r.get("value") or 0)
        for r in rows if r.get("usd")
    )
    usd_loan_cad = min(used, usd_rows_value)
    margin_available = round(value - margin_req, 2)
    out.update(
        {
            "paper_margin_requirement": margin_req,
            "paper_margin_breakdown": parts,
            "paper_margin_used": round(used, 2),
            "paper_margin_used_usd": (
                round(usd_loan_cad / conv_fx, 2)
                if conv_fx else 0.0
            ),
            "paper_margin_used_cad": round(
                used - usd_loan_cad, 2
            ),
            "paper_margin_available": margin_available,
            "paper_max_buying_power": (
                round(margin_available / default_rate, 2)
                if margin_available and margin_available > 0
                else 0.0
            ),
            "paper_portfolio_value": round(value + used, 2),
        }
    )
    return out


def create_app(cfg, store: Store, risk, executor, account=None,
                 config_path=None) -> Flask:
    app = Flask(__name__)
    install_quiet_filter()
    app.secret_key = _load_secret_key(config_path)
    app.permanent_session_lifetime = timedelta(days=30)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if getattr(cfg.pipeline, "tls_cert", "") and getattr(
        cfg.pipeline, "tls_key", ""
    ):
        app.config["SESSION_COOKIE_SECURE"] = True
    mode = cfg.trading.mode
    app.reader_state = {
        "channel": None,
        "ok": False,
        "last_seen": None,
    }

    # the 5s dashboard polls otherwise flood pipeline.log with
    # access lines for every api call
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if not token or request.path in ("/health", "/favicon.ico", "/login"):
            return None
        if session.get("auth"):
            return None
        supplied = request.headers.get("X-Auth-Token", "")
        if supplied and hmac.compare_digest(supplied, token):
            return None
        if request.path.startswith("/api/") or request.path == "/alert":
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        token = cfg.pipeline.auth_token
        if not token:
            return redirect("/")
        ip = request.remote_addr or "?"
        now = time.time()
        entry = _LOGIN_FAILS.get(ip)
        if entry and entry.get("locked_until", 0) > now:
            return Response(
                LOGIN_HTML("too many attempts - try again later"),
                403,
                mimetype="text/html",
                headers={"Cache-Control": "no-store"},
            )
        error = None
        if request.method == "POST":
            supplied = request.form.get("password", "")
            if supplied and hmac.compare_digest(supplied, token):
                _LOGIN_FAILS.pop(ip, None)
                session.permanent = True
                session["auth"] = True
                return redirect("/")
            count = (entry or {}).get("count", 0) + 1
            if count >= LOGIN_FAIL_LIMIT:
                _LOGIN_FAILS[ip] = {
                    "count": count,
                    "locked_until": now + LOCKOUT_SECONDS,
                }
                error = "too many attempts - try again later"
            else:
                _LOGIN_FAILS[ip] = {"count": count, "locked_until": 0}
                error = "wrong access token"
            time.sleep(1)
        return Response(
            LOGIN_HTML(error), mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.after_request
    def no_store(resp):
        # unconditional: flask's static handler sets no-cache,
        # and the page must never serve stale after an update
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def dashboard_page():
        return app.send_static_file("dashboard.html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    def _real_positions():
        if account is None or not hasattr(
            account, "open_option_positions"
        ):
            return None
        try:
            return account.open_option_positions()
        except Exception:
            return None

    def _real_stocks():
        if account is None or not hasattr(account, "stock_holdings"):
            return None
        try:
            return account.stock_holdings()
        except Exception:
            return None

    _summary_cache = {
        "ts": 0.0, "accounts": None, "ver": -1,
    }
    app._summary_cache = _summary_cache

    def _account_summaries():
        # the dashboard polls every few seconds - reuse the
        # computed summaries between position/value refreshes
        if (
            _summary_cache["accounts"] is not None
            and _summary_cache["ver"] == store.data_version()
            and time.time() - _summary_cache["ts"] < 2.5
        ):
            return _summary_cache["accounts"], None
        values = {}
        if account is not None:
            try:
                values = account.values()
            except Exception as e:
                return None, str(e)
        t = cfg.trading
        real = _real_positions() or {}
        stocks = _real_stocks() or {}
        shared_fx = None
        for live in real.values():
            if live and live.get("fx"):
                shared_fx = live["fx"]
                break
        if shared_fx is None:
            shared_fx = getattr(account, "_fx_hint", None)
        usd_vals = None
        usd_fn = getattr(account, "usd_values", None)
        if callable(usd_fn):
            try:
                usd_vals = usd_fn() or {}
            except Exception:
                usd_vals = None
        funding = None
        fb_fn = getattr(account, "funding_balances", None)
        if callable(fb_fn):
            try:
                funding = fb_fn() or {}
            except Exception:
                funding = None
        # paper ledger values when paper trading runs alongside
        paper_values = {}
        paper_initials = {}
        _paper_ledger = None
        if getattr(
            getattr(cfg, "paper", None), "enabled", False
        ):
            ledger = getattr(executor, "account", None)
            vals_fn = getattr(ledger, "values", None)
            if callable(vals_fn):
                try:
                    paper_values = vals_fn() or {}
                except Exception:
                    paper_values = {}
            _paper_ledger = ledger
            for lbl in (paper_values or {}):
                init = store.meta_get(f"paper_initial:{lbl}")
                if init is not None:
                    try:
                        paper_initials[lbl] = float(init)
                    except (TypeError, ValueError):
                        pass

        out = []
        for label, value in values.items():
            open_risk = store.open_risk(mode, label)
            fx = shared_fx
            usd_cash = None
            live = real.get(label)
            if live is not None:
                open_risk = sum(
                    (r.get("risk_cad") if r.get("risk_cad") is not None
                     else (r.get("cost_cad") if not r.get("short") else 0))
                    or 0
                    for r in live["positions"]
                )
                if live.get("fx"):
                    fx = live["fx"]
                usd_cash = live.get("usd_cash")
            cash_cad = None
            cash_usd = None
            if funding and funding.get(label):
                for b in funding[label]:
                    if b.get("currency") == "CAD":
                        cash_cad = b["amount"]
                    elif b.get("currency") == "USD":
                        cash_usd = b["amount"]
            if cash_usd is None:
                cash_usd = usd_cash
            if usd_vals and usd_vals.get(label):
                # wealthsimple's own conversion beats our derived fx
                usd_value = round(usd_vals[label], 2)
            elif fx and value:
                usd_value = round(value / fx, 2)
            else:
                usd_value = None
            stale_fn = getattr(account, "stale_age", None)
            value_age = stale_fn(label) if callable(stale_fn) else None
            conv_fx = fx or 1.0
            stock_value = None
            sk = stocks.get(label)
            if sk:
                stock_value = round(sum(
                    (r.get("market_value") or 0)
                    * (conv_fx if r.get("currency") == "USD" else 1)
                    for r in sk
                ), 2)
            option_value = None
            if live is not None:
                option_value = round(sum(
                    abs(r.get("market_value") or 0) * conv_fx
                    for r in live["positions"]
                ), 2)

            # allocation base: gross assets, independent of how the
            # loan is reported - holdings plus positive cash only
            alloc_base = None
            if (stock_value or 0) or (option_value or 0):
                pos_cash = max(cash_cad or 0.0, 0.0)
                if cash_usd and cash_usd > 0:
                    pos_cash += cash_usd * conv_fx
                alloc_base = round(
                    (stock_value or 0) + (option_value or 0)
                    + pos_cash, 2
                )

            def _registered_plan():
                type_map_fn = getattr(account, "account_type_map", None)
                resolve_fn = getattr(account, "_resolve", None)
                if callable(type_map_fn) and callable(resolve_fn):
                    try:
                        type_map = type_map_fn() or {}
                        ids = {
                            lbl: aid for lbl, aid in resolve_fn()
                        }
                        acct_type = str(
                            type_map.get(ids.get(label) or "") or ""
                        ).upper()
                        if acct_type in REGISTERED_ACCOUNT_TYPES:
                            return True
                    except Exception:
                        pass
                label_upper = str(label or "").upper()
                return any(
                    t in label_upper for t in REGISTERED_ACCOUNT_TYPES
                )

            margin_req = None
            margin_used = None
            margin_available = None
            max_buying_power = None
            used_cad_raw = 0.0
            used_usd_raw = 0.0
            req_parts = []
            portfolio_value = None
            if not _registered_plan() and value:
                # computed per the WS margin page: requirement is
                # the maintenance rate over holdings (rate per
                # symbol, long options get no loan value, shorts
                # count their defined risk), margin used is the
                # negative trading balance, availability is equity
                # minus what is committed
                default_rate = getattr(
                    cfg.wealthsimple, "stock_margin_rate", 0.30
                )
                overrides = getattr(
                    cfg.wealthsimple, "margin_rate_overrides", {}
                ) or {}
                rate_fn = getattr(account, "security_margin_rate", None)
                from ..trading.margin import (
                    Holding, compute_requirement, resolve_rate,
                )

                holdings = []
                for r in (sk or []):
                    holdings.append(
                        Holding(
                            symbol=str(r.get("underlying") or ""),
                            kind="stock",
                            currency=(
                                "usd"
                                if r.get("currency") == "USD"
                                else "cad"
                            ),
                            native_value=r.get("market_value") or 0,
                            security_id=r.get("security_id"),
                        )
                    )

                def _rate_for(h):
                    return resolve_rate(
                        h.symbol, h.security_id, rate_fn,
                        overrides, default_rate,
                    )

                def _opt_cur(r):
                    return (
                        "usd"
                        if r.get("cost_usd") is not None
                        else "cad"
                    )

                if live is not None:
                    for r in live["positions"]:
                        cur = _opt_cur(r)
                        cur_fx = conv_fx if cur == "usd" else 1.0
                        if r.get("spread"):
                            # WS charges spreads the full width
                            # without netting the premium
                            width = None
                            try:
                                s1, s2 = str(
                                    r.get("strike") or ""
                                ).split("/")
                                width = abs(float(s2) - float(s1))
                            except (ValueError, AttributeError):
                                width = None
                            amount = None
                            if not width:
                                # no derivable width - fall back
                                # to defined risk / full value
                                if r.get("short"):
                                    amount = (
                                        (r.get("risk_cad") or 0)
                                        + abs(
                                            r.get("cost_cad") or 0
                                        )
                                    ) / cur_fx
                                else:
                                    amount = abs(
                                        r.get("market_value") or 0
                                    )
                            holdings.append(
                                Holding(
                                    symbol=r.get("underlying") or "?",
                                    kind="spread",
                                    currency=cur,
                                    structure=r.get("strategy_type"),
                                    qty=r.get("qty") or 0,
                                    width=width,
                                    amount_native=amount,
                                )
                            )
                        elif r.get("short"):
                            holdings.append(
                                Holding(
                                    symbol=r.get("underlying") or "?",
                                    kind="short",
                                    currency=cur,
                                    risk=(r.get("risk_cad") or 0)
                                    / cur_fx,
                                )
                            )
                        else:
                            holdings.append(
                                Holding(
                                    symbol=r.get("underlying") or "?",
                                    kind="long",
                                    currency=cur,
                                    native_value=abs(
                                        r.get("market_value") or 0
                                    ),
                                )
                            )
                margin_req, req_parts = compute_requirement(
                    holdings, conv_fx, _rate_for
                )
                used = 0.0
                used_cad_raw = 0.0
                used_usd_raw = 0.0
                if funding and funding.get(label):
                    for b in funding[label]:
                        amt = b.get("amount")
                        if amt is not None and amt < 0:
                            if b.get("currency") == "USD":
                                used_usd_raw += -amt
                                used += -amt * conv_fx
                            else:
                                used_cad_raw += -amt
                                used += -amt
                margin_used = round(used, 2)
                # NLV already nets the loan as negative cash, so
                # availability is simply equity minus requirement
                margin_available = round(value - margin_req, 2)
                max_buying_power = (
                    round(margin_available / default_rate, 2)
                    if margin_available and margin_available > 0
                    else 0.0
                )
                # gross holdings: equity plus the loan against them
                portfolio_value = round(value + margin_used, 2)
            out.append(
                {
                    "label": label,
                    "value": value,
                    "value_age": value_age,
                    "usd_value": usd_value,
                    "usd_cash": usd_cash,
                    "cash_cad": cash_cad,
                    "cash_usd": cash_usd,
                    "open_risk": open_risk,
                    "stock_value": stock_value,
                    "option_value": option_value,
                    "alloc_base": alloc_base,
                    "margin_requirement": margin_req,
                    "margin_breakdown": req_parts,
                    "paper_value": paper_values.get(label),
                    "paper_initial": paper_initials.get(label),
                    # prefer wealthsimple's own conversion for
                    # the real account (works with no open
                    # positions, unlike the positions-derived fx
                    # which falls back to 1.0 and makes the flip a
                    # no-op); positions fx is the fallback
                    "paper_usd_value": (
                        round(
                            paper_values[label] * usd_value / value, 2
                        )
                        if label in paper_values
                        and paper_values.get(label) is not None
                        and usd_value and value
                        else (
                            round(
                                paper_values[label] / conv_fx, 2
                            )
                            if label in paper_values
                            and paper_values.get(label) is not None
                            and conv_fx and conv_fx > 1.0
                            else None
                        )
                    ),
                    "paper_pnl": (
                        # `or 0.0` also normalizes -0.0, which
                        # otherwise renders as "+$-0.00"
                        round(
                            paper_values[label]
                            - paper_initials[label], 2
                        ) or 0.0
                        if label in paper_values
                        and label in paper_initials else None
                    ),
                    **(
                        _paper_card_metrics(
                            _paper_ledger, store, cfg, label,
                            paper_values[label],
                            _registered_plan(), conv_fx,
                        )
                        if label in paper_values
                        and paper_values.get(label) is not None
                        and _paper_ledger is not None
                        else {}
                    ),
                    "margin_used": margin_used,
                    "margin_used_cad": round(used_cad_raw, 2)
                    if used_cad_raw else 0.0,
                    "margin_used_usd": round(used_usd_raw, 2)
                    if used_usd_raw else 0.0,
                    "margin_available": margin_available,
                    "max_buying_power": max_buying_power,
                    "portfolio_value": portfolio_value,
                    "open_risk_pct": (
                        round(open_risk / value * 100, 2)
                        if value and value > 0
                        else None
                    ),
                    "max_open_risk_pct": t.max_open_risk_pct,
                    "risk_per_trade_pct": t.risk_per_trade_pct,
                    "per_trade_budget": (
                        value * (t.risk_per_trade_pct / 100.0)
                        if value and value > 0
                        else None
                    ),
                }
            )
        _summary_cache["ts"] = time.time()
        _summary_cache["ver"] = store.data_version()
        _summary_cache["accounts"] = out
        return out, None

    def _summary_payload():
        accounts, err = _account_summaries()
        if err:
            return {"error": err}
        t = cfg.trading
        last_seen = app.reader_state.get("last_seen")
        reader = dict(app.reader_state)
        reader["desired"] = cfg.reader.channel_marker
        reader["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return {
            "mode": mode,
            "paper": bool(
                getattr(
                    getattr(cfg, "paper", None), "enabled", False
                ) or mode == "paper",
            ),
            "accounts": accounts,
            "reader": reader,
            "stops": {
                "stop_loss_pct": t.stop_loss_pct,
                "trailing_stop_pct": t.trailing_stop_pct,
                "consecutive_losses": store.loss_streak(mode),
                "max_consecutive_losses": t.max_consecutive_losses,
            },
        }

    @app.get("/api/summary")
    def api_summary():
        payload = _summary_payload()
        if "error" in payload:
            return jsonify(payload), 502
        return jsonify(payload)

    @app.post("/api/paper-reset")
    def api_paper_reset():
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ) and mode != "paper":
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
            from ..ws.account import seed_paper_accounts

            seeded = seed_paper_accounts(cfg, store, account) or []
        _summary_cache["ts"] = 0.0
        return jsonify({
            "status": "ok", "label": label,
            "reseeded": label in seeded,
        })

    def _paper_positions_payload():
        if not getattr(
            getattr(cfg, "paper", None), "enabled", False
        ):
            return {}
        ledger = getattr(executor, "account", None)
        positions_fn = getattr(ledger, "positions", None)
        if not callable(positions_fn):
            return {}
        out = {}
        try:
            for label in (ledger.values() or {}):
                out[label] = positions_fn(label)
        except Exception as e:
            return {"error": str(e)}
        return out

    @app.get("/api/paper-positions")
    def api_paper_positions():
        payload = _paper_positions_payload()
        if isinstance(payload, dict) and "error" in payload:
            return jsonify(payload), 500
        return jsonify(payload)

    def _positions_payload():
        rows = [dict(r) for r in store.list_positions(mode)]
        for r in rows:
            r.setdefault("kind", "option")
        real = _real_positions() or {}
        stocks = _real_stocks() or {}
        fetched = {
            label for label, r in list(real.items()) + list(stocks.items())
            if r is not None
        }
        if fetched:
            # live Wealthsimple positions replace tracked ones for the
            # accounts we could fetch (manual trades included)
            rows = [r for r in rows if r["account"] not in fetched]
            for label, live in real.items():
                if not live:
                    continue
                for r in live["positions"]:
                    row = dict(r)
                    row["account"] = label
                    row["source"] = "ws"
                    row.setdefault("kind", "option")
                    rows.append(row)
            for label, live in stocks.items():
                if not live:
                    continue
                for r in live:
                    row = dict(r)
                    row["account"] = label
                    row["source"] = "ws"
                    rows.append(row)
        return rows

    @app.get("/api/positions")
    def api_positions():
        return jsonify(_positions_payload())

    @app.get("/api/signals")
    def api_signals():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_signals(limit))

    @app.get("/api/trades")
    def api_trades():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_trades(limit))

    @app.get("/api/settings")
    def api_settings_get():
        from ..settings import get_settings

        return jsonify(get_settings(cfg))

    @app.get("/api/dashboard")
    def api_dashboard():
        """Everything the dashboard polls, in one round trip."""
        summary = _summary_payload()
        if "error" in summary:
            return jsonify(summary), 502
        from ..settings import get_settings

        return jsonify(
            {
                "summary": summary,
                "paper_positions": _paper_positions_payload(),
                "positions": _positions_payload(),
                "signals": store.recent_signals(50),
                "trades": store.recent_trades(50),
                "settings": get_settings(cfg),
                "update_status": _update_status_payload(),
            }
        )

    @app.post("/api/settings")
    def api_settings_post():
        from ..settings import apply_settings

        payload = request.get_json(silent=True) or {}
        applied, errors = apply_settings(cfg, payload, config_path)
        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        # settings feed the summaries - drop the cache at once
        _summary_cache["ts"] = 0.0
        return jsonify(
            {
                "status": "ok",
                "applied": applied,
                "restart_required": False,
            }
        )

    @app.post("/api/reader_status")
    def api_reader_status():
        data = request.get_json(silent=True) or {}
        app.reader_state["channel"] = (data.get("channel") or None)
        app.reader_state["ok"] = bool(data.get("ok"))
        app.reader_state["last_seen"] = time.time()
        return jsonify(
            {
                "channel_marker": cfg.reader.channel_marker,
                "poll_interval": cfg.reader.poll_interval,
                "max_items": cfg.reader.max_items,
                "channels": cfg.reader.channels,
            }
        )

    @app.get("/api/reader_status")
    def api_reader_status_get():
        last_seen = app.reader_state.get("last_seen")
        state = dict(app.reader_state)
        state["desired"] = cfg.reader.channel_marker
        state["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return jsonify(state)

    def _update_status_payload():
        updater = getattr(app, "ws_updater", None)
        if updater is None:
            return {"status": "disabled"}
        return {
            "status": "active",
            "interval_seconds": (
                getattr(updater.cfg.auto_update, "interval_seconds", 600)
            ),
            "last_check": updater.last_check,
            "result": updater.last_result,
            "errors": updater.errors,
            "head": (updater.start_head or "")[:8],
            "branch": updater.branch,
            "last_pull": updater.last_pull(),
        }

    @app.get("/api/update_status")
    def api_update_status():
        return jsonify(_update_status_payload())

    @app.post("/alert")
    def alert():
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
