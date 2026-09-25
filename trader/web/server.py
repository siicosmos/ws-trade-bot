import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta

from flask import Flask, Response, g, jsonify, redirect, request, session

from ..ws.account_types import REGISTERED_ACCOUNT_TYPES
from .dashboard import LOGIN_HTML
from ..pipeline import process_alert
from ..store import Store
from ..trading.margin import Holding, compute_requirement, resolve_rate

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


def _purge_login_fails(now):
    """Drop fail entries not touched for a day - the dict must
    not grow without bound under sustained attacks. (A zero
    locked_until means 'not locked yet', not 'long expired'.)"""
    stale = [
        ip for ip, e in _LOGIN_FAILS.items()
        if now - e.get("ts", 0) > 86400
    ]
    for ip in stale:
        _LOGIN_FAILS.pop(ip, None)


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
        # the session signing key is only for this user
        os.chmod(key_file, 0o600)
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


@dataclass
class PipelineContext:
    """Explicit dependency bundle for the dashboard payloads.

    The heavy builders below are module-level functions over
    this context (second-review refactor): testable without
    building the whole app. Routes in create_app stay thin
    closures over it.
    """

    cfg: object
    store: Store
    risk: object
    executor: object
    account: object = None
    mode: str = ""
    config_path: str = None
    reader_state: dict = field(default_factory=lambda: {
        "channel": None, "ok": False, "last_seen": None,
    })
    summary_cache: dict = field(default_factory=lambda: {
        "ts": 0.0, "accounts": None, "ver": -1,
    })


def _real_positions(ctx):
    account = ctx.account
    if account is None or not hasattr(
        account, "open_option_positions"
    ):
        return None
    try:
        return account.open_option_positions()
    except Exception:
        return None


def _real_stocks(ctx):
    account = ctx.account
    if account is None or not hasattr(account, "stock_holdings"):
        return None
    try:
        return account.stock_holdings()
    except Exception:
        return None


def _registered_plan(account, label):
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


def _margin_metrics(ctx, label, value, live, sk, conv_fx,
                    funding):
    """Margin numbers for one non-registered account with a
    value: builds normalized Holdings and calls the shared
    margin model (plan #1)."""
    cfg = ctx.cfg
    default_rate = getattr(
        cfg.wealthsimple, "stock_margin_rate", 0.30
    )
    overrides = getattr(
        cfg.wealthsimple, "margin_rate_overrides", {}
    ) or {}
    rate_fn = getattr(ctx.account, "security_margin_rate", None)

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

    if live is not None:
        for r in live["positions"]:
            cur = (
                "usd"
                if r.get("cost_usd") is not None
                else "cad"
            )
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
    return {
        "margin_requirement": margin_req,
        "margin_breakdown": req_parts,
        "margin_used": margin_used,
        "margin_used_cad": round(used_cad_raw, 2)
        if used_cad_raw else 0.0,
        "margin_used_usd": round(used_usd_raw, 2)
        if used_usd_raw else 0.0,
        "margin_available": margin_available,
        "max_buying_power": max_buying_power,
        "portfolio_value": portfolio_value,
    }


def _account_summary(ctx, snap, label, value):
    """One account's dashboard row (second-review depth fix):
    registered-plan detection, funding parsing, margin
    assembly and the paper merge, split one per concern."""
    cfg = ctx.cfg
    store = ctx.store
    account = ctx.account
    real, stocks = snap["real"], snap["stocks"]
    funding = snap["funding"]
    paper_values = snap["paper_values"]
    paper_initials = snap["paper_initials"]
    paper_ledger = snap["paper_ledger"]
    t = snap["t"]

    open_risk = store.open_risk(ctx.mode, label)
    fx = snap["shared_fx"]
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
    usd_vals = snap["usd_vals"]
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

    registered = _registered_plan(account, label)
    # computed per the WS margin page: requirement is the
    # maintenance rate over holdings (rate per symbol, long
    # options get no loan value, shorts count their defined
    # risk), margin used is the negative trading balance,
    # availability is equity minus what is committed
    margin = {
        "margin_requirement": None,
        "margin_breakdown": [],
        "margin_used": None,
        "margin_used_cad": 0.0,
        "margin_used_usd": 0.0,
        "margin_available": None,
        "max_buying_power": None,
        "portfolio_value": None,
    }
    if not registered and value:
        margin = _margin_metrics(
            ctx, label, value, live, sk, conv_fx, funding
        )

    return {
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
                paper_ledger, store, cfg, label,
                paper_values[label],
                registered, conv_fx,
            )
            if label in paper_values
            and paper_values.get(label) is not None
            and paper_ledger is not None
            else {}
        ),
        **margin,
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


def _account_summaries(ctx):
    """The dashboard polls every few seconds - reuse the
    computed summaries between position/value refreshes."""
    cache = ctx.summary_cache
    if (
        cache["accounts"] is not None
        and cache["ver"] == ctx.store.data_version()
        and time.time() - cache["ts"] < 2.5
    ):
        return cache["accounts"], None
    cfg = ctx.cfg
    store = ctx.store
    account = ctx.account
    values = {}
    if account is not None:
        try:
            values = account.values()
        except Exception as e:
            return None, str(e)
    real = _real_positions(ctx) or {}
    stocks = _real_stocks(ctx) or {}
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
    paper_ledger = None
    if getattr(
        getattr(cfg, "paper", None), "enabled", False
    ):
        ledger = getattr(ctx.executor, "account", None)
        vals_fn = getattr(ledger, "values", None)
        if callable(vals_fn):
            try:
                paper_values = vals_fn() or {}
            except Exception:
                paper_values = {}
        paper_ledger = ledger
        for lbl in (paper_values or {}):
            init = store.meta_get(f"paper_initial:{lbl}")
            if init is not None:
                try:
                    paper_initials[lbl] = float(init)
                except (TypeError, ValueError):
                    pass

    snap = {
        "t": cfg.trading,
        "real": real,
        "stocks": stocks,
        "shared_fx": shared_fx,
        "usd_vals": usd_vals,
        "funding": funding,
        "paper_values": paper_values,
        "paper_initials": paper_initials,
        "paper_ledger": paper_ledger,
    }
    out = [
        _account_summary(ctx, snap, label, value)
        for label, value in values.items()
    ]
    cache["ts"] = time.time()
    cache["ver"] = store.data_version()
    cache["accounts"] = out
    return out, None


def _summary_payload(ctx):
    accounts, err = _account_summaries(ctx)
    if err:
        return {"error": err}
    cfg = ctx.cfg
    store = ctx.store
    mode = ctx.mode
    t = cfg.trading
    last_seen = ctx.reader_state.get("last_seen")
    reader = dict(ctx.reader_state)
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


def _paper_positions_payload(ctx):
    if not getattr(
        getattr(ctx.cfg, "paper", None), "enabled", False
    ):
        return {}
    ledger = getattr(ctx.executor, "account", None)
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


def _positions_payload(ctx):
    store = ctx.store
    mode = ctx.mode
    rows = [dict(r) for r in store.list_positions(mode)]
    for r in rows:
        r.setdefault("kind", "option")
    real = _real_positions(ctx) or {}
    stocks = _real_stocks(ctx) or {}
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


def _update_status_payload(app):
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
    ctx = PipelineContext(
        cfg=cfg, store=store, risk=risk, executor=executor,
        account=account, mode=cfg.trading.mode,
        config_path=config_path,
    )
    # compat aliases (tests and the reader poke these directly)
    app.reader_state = ctx.reader_state
    app._summary_cache = ctx.summary_cache

    # first boot: the access token becomes the admin password so
    # the existing workflow keeps working
    if store.user_count() == 0 and cfg.pipeline.auth_token:
        store.create_user("admin", cfg.pipeline.auth_token, "admin")

    # the 5s dashboard polls otherwise flood pipeline.log with
    # access lines for every api call
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if request.path in ("/health", "/favicon.ico", "/login"):
            return None
        if not token and store.user_count() == 0:
            # legacy install (no token, no users): open access,
            # exactly as before user accounts existed; /login
            # remains reachable to bootstrap the first admin
            return None
        if session.get("auth"):
            return None
        supplied = request.headers.get("X-Auth-Token", "")
        if supplied and token and hmac.compare_digest(supplied, token):
            g.admin = True
            return None
        if request.path.startswith("/api/") or request.path == "/alert":
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        token = cfg.pipeline.auth_token
        first = store.user_count() == 0
        ip = request.remote_addr or "?"
        now = time.time()
        _purge_login_fails(now)
        entry = _LOGIN_FAILS.get(ip)
        if entry and entry.get("locked_until", 0) > now:
            return Response(
                LOGIN_HTML("too many attempts - try again later",
                           first=first),
                403,
                mimetype="text/html",
                headers={"Cache-Control": "no-store"},
            )
        error = None
        if request.method == "POST":
            username = str(request.form.get("username") or "").strip()
            supplied = request.form.get("password", "")

            def _fail(label):
                nonlocal error
                count = (entry or {}).get("count", 0) + 1
                if count >= LOGIN_FAIL_LIMIT:
                    _LOGIN_FAILS[ip] = {
                        "count": count,
                        "locked_until": now + LOCKOUT_SECONDS,
                        "ts": now,
                    }
                    error = "too many attempts - try again later"
                else:
                    _LOGIN_FAILS[ip] = {
                        "count": count, "locked_until": 0, "ts": now,
                    }
                    error = label
                time.sleep(1)

            if first:
                # claim the first admin account
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", username):
                    _fail("pick a username (letters, digits, - _)")
                elif len(supplied) < 6:
                    _fail("password must be at least 6 characters")
                else:
                    store.create_user(username, supplied, "admin")
                    _LOGIN_FAILS.pop(ip, None)
                    session.permanent = True
                    session["auth"] = True
                    session["user"] = {
                        "username": username, "role": "admin",
                    }
                    return redirect("/")
            elif not username and token and hmac.compare_digest(
                supplied, token
            ):
                # legacy: the bare access token still opens an
                # owner session (scripts, old bookmarks)
                _LOGIN_FAILS.pop(ip, None)
                session.permanent = True
                session["auth"] = True
                return redirect("/")
            else:
                user = store.verify_user(username, supplied)
                if user is not None:
                    _LOGIN_FAILS.pop(ip, None)
                    session.permanent = True
                    session["auth"] = True
                    session["user"] = user
                    return redirect("/")
                _fail("wrong username or password")
        return Response(
            LOGIN_HTML(error, first=first), mimetype="text/html",
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

    def _me():
        return session.get("user")

    def _is_admin() -> bool:
        # a legacy open install (no token, no users yet) is the
        # owner's single-user setup - full access until an
        # admin account is claimed
        if not cfg.pipeline.auth_token and store.user_count() == 0:
            return True
        if getattr(g, "admin", False):
            return True
        user = session.get("user")
        if user:
            return user.get("role") == "admin"
        # legacy sessions (pre-users) were the owner's
        return bool(session.get("auth"))

    def _require_admin():
        if not _is_admin():
            return jsonify({"error": "admin required"}), 403
        return None

    @app.get("/api/summary")
    def api_summary():
        payload = _summary_payload(ctx)
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
            from ..ws.account import seed_paper_accounts

            seeded = seed_paper_accounts(cfg, store, account) or []
        ctx.summary_cache["ts"] = 0.0
        return jsonify({
            "status": "ok", "label": label,
            "reseeded": label in seeded,
        })

    @app.get("/api/paper-positions")
    def api_paper_positions():
        payload = _paper_positions_payload(ctx)
        if isinstance(payload, dict) and "error" in payload:
            return jsonify(payload), 500
        return jsonify(payload)

    @app.get("/api/positions")
    def api_positions():
        return jsonify(_positions_payload(ctx))

    @app.get("/api/signals")
    def api_signals():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_signals(limit))

    @app.get("/api/trades")
    def api_trades():
        limit = request.args.get("limit", default=50, type=int)
        return jsonify(store.recent_trades(limit))

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
        from ..settings import get_settings

        return jsonify(get_settings(cfg))

    @app.get("/api/dashboard")
    def api_dashboard():
        """Everything the dashboard polls, in one round trip.
        A summary failure degrades to its error marker - the
        rest of the payload still renders."""
        from ..settings import get_settings

        return jsonify(
            {
                "summary": _summary_payload(ctx),
                "paper_positions": _paper_positions_payload(ctx),
                "positions": _positions_payload(ctx),
                "signals": store.recent_signals(50),
                "trades": store.recent_trades(50),
                "settings": get_settings(cfg),
                "update_status": _update_status_payload(app),
                "me": _me(),
            }
        )

    @app.post("/api/settings")
    def api_settings_post():
        denied = _require_admin()
        if denied:
            return denied
        from ..settings import apply_settings

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

    @app.post("/api/reader_status")
    def api_reader_status():
        data = request.get_json(silent=True) or {}
        ctx.reader_state["channel"] = (data.get("channel") or None)
        ctx.reader_state["ok"] = bool(data.get("ok"))
        ctx.reader_state["last_seen"] = time.time()
        return jsonify(
            {
                "channel_marker": cfg.reader.channel_marker,
                "poll_interval": cfg.reader.poll_interval,
                "max_items": cfg.reader.max_items,
                "channels": cfg.reader.channels,
                "channel_servers": getattr(
                    cfg.reader, "channel_servers", {}
                ),
                "auto_switch": bool(
                    getattr(
                        cfg.reader, "auto_switch_channel", True
                    )
                ),
                "discord_reopen_seconds": getattr(
                    cfg.reader, "discord_reopen_seconds", 15
                ),
                "discord_restart_seconds": int(
                    getattr(
                        cfg.reader, "discord_restart_seconds", 90
                    )
                ),
            }
        )

    @app.get("/api/reader_status")
    def api_reader_status_get():
        last_seen = ctx.reader_state.get("last_seen")
        state = dict(ctx.reader_state)
        state["desired"] = cfg.reader.channel_marker
        state["age_seconds"] = (
            round(time.time() - last_seen, 1) if last_seen else None
        )
        return jsonify(state)

    @app.get("/api/update_status")
    def api_update_status():
        return jsonify(_update_status_payload(app))

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
            # legacy token sessions act as the owner
            me = {"username": "admin", "role": "admin"} \
                if _is_admin() else None
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
            if target != me.get("username"):
                if not admin:
                    return jsonify(
                        {"error": "admin required"}
                    ), 403
            if target != me.get("username") or admin:
                if not admin:
                    return jsonify({"error": "admin required"}), 403
                if not store.get_user(target):
                    return jsonify({"error": "unknown user"}), 404
                if not store.update_password(target, new_password):
                    return jsonify({"error": "update failed"}), 400
                return jsonify({"status": "ok"})
            # self change requires the current password
            current = str(data.get("current_password") or "")
            user = store.verify_user(me.get("username"), current)
            if user is None:
                return jsonify(
                    {"error": "current password is wrong"}
                ), 403
            store.update_password(target, new_password)
            return jsonify({"status": "ok"})

        return jsonify({"error": "unknown action"}), 400

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
