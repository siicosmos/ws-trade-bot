import gzip
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta

from flask import Flask, Response, g, jsonify, request, session

from consumer.ws.account_types import REGISTERED_ACCOUNT_TYPES
from consumer.dashboard import (
    LOGIN_HTML, dashboard_css, dashboard_html,
)
from consumer.pipeline import process_alert
from core.store import Store
from core.web_common import (
    install_auth, install_gzip, install_quiet_filter, load_secret_key,
)
from consumer.trading.margin import Holding, compute_requirement, resolve_rate

def _paper_card_metrics(
    ledger, store, cfg, label, value, registered, conv_fx
):
    """Margin-style card metrics for a paper account, computed
    with the same model as the real cards: long options at 100%,
    spreads at full wing width, stocks at their margin rate,
    margin used being negative ledger cash."""
    # two cash pools (the adjust editor): the cad ledger cash
    # and the usd pool - the margin math nets them at the live fx
    cash_cad = store.paper_equity(label)
    cash_usd = store.paper_cash_usd(label) or 0.0
    cash = (
        cash_cad + cash_usd * conv_fx
        if cash_cad is not None else None
    )
    rows = []
    pos_fn = getattr(ledger, "positions", None)
    if callable(pos_fn):
        try:
            rows = pos_fn(label) or []
        except Exception:
            rows = []
    by_key = {r["contract_key"]: r for r in rows}
    stock_value = round(sum(
        (r.get("value_cad") if r.get("value_cad") is not None
         else (r.get("value") or 0))
        for r in rows if r.get("kind") == "stock"
    ), 2)
    option_value = round(sum(
        (r.get("value_cad") if r.get("value_cad") is not None
         else abs(r.get("value") or 0))
        for r in rows if r.get("kind") == "option"
    ), 2)
    pos_cash = max(cash or 0.0, 0.0)
    alloc_base = round(
        stock_value + option_value + pos_cash, 2
    ) or None

    out = {
        # the cad pool - the card renders the usd pool next to it
        # from paper_cash_usd
        "paper_cash": cash_cad,
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
                # ledger values stay in the quote currency (usd
                # rows quote usd) - carry the native figure and
                # let compute_requirement convert (dividing here
                # cancelled the fx and understated usd margin)
                native_value=abs(r.get("value") or 0),
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
    from consumer.trading.strategies import classify_legs

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
        if (
            info and info.get("kind") in (
                "vertical", "butterfly", "condor",
                "iron fly", "ratio",
            )
            and info.get("debit") is False
        ):
            # ws's maintenance rule: SOLD (credit) spreads carry
            # the full wing width - the same rule as the real
            # account. a long/debit structure charges its legs
            # (the debit paid), not the wing
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
                        native_value=abs(row.get("value") or 0),
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
    # the feed client's liveness (alerts pulled from the info
    # server) - the dashboard's status line shows this when set
    feed_state: dict = field(default_factory=lambda: {
        "last_seen": None, "ok": None, "cursor": None,
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


# the ws quote api never times out (the library's requests
# calls carry no timeout) - a stalled connection would pin a
# waitress worker until the pool drains and even /health stops
# answering. every ws quote is therefore bounded by a worker
# thread and cached so the polling endpoints stay cheap
_WS_QUOTE_TIMEOUT = 15.0
_WS_QUOTE_TTL = 10.0
_WS_QUOTE_RETRY = 30.0
# the dashboard's ws-touching sections cache briefly and serve
# stale when a refresh does not finish in time - the ws api can
# run several seconds per round trip on a bad network, and the
# page must not stall on it
_SECTION_TTL = 15.0
_section_cache = {}


def _section_ttl(cfg):
    """The stale-while-revalidate window rides the settings: a
    short positions/values refresh must not sit behind a fixed
    15s cache - cap the ttl at half the shortest refresh so the
    dashboard keeps up with what the settings promise."""
    ws = getattr(cfg, "wealthsimple", None)
    try:
        positions = float(
            getattr(ws, "positions_refresh_seconds", 30)
        )
        values = float(
            getattr(ws, "values_refresh_seconds", 60)
        )
    except (TypeError, ValueError):
        return _SECTION_TTL
    return min(_SECTION_TTL, max(2.5, min(positions / 2, values / 2)))


_sec_id_cache = {}
_ws_quote_cache = {}
_ws_quote_fail_ts = {}


def _ws_quote_fetch(account, ticker):
    """The unbounded network part: resolve the security id (a
    search call, cached per ticker) and fetch its quote."""
    sec_id = _sec_id_cache.get(ticker)
    if not sec_id:
        try:
            ws = account._client()
            sec_id = ws.get_ticker_id(ticker, None)
        except Exception:
            return None
        if not sec_id:
            return None
        _sec_id_cache[ticker] = sec_id
    try:
        quote = account._client().get_security_quote(sec_id) or {}
    except Exception:
        return None
    price = (
        quote.get("price")
        or quote.get("lastPrice")
        or quote.get("ask")
        or quote.get("bid")
    )
    if not price:
        return None
    try:
        return (
            float(price),
            str(quote.get("marketStatus") or "").upper(),
        )
    except (TypeError, ValueError):
        return None


def _ws_stock_quote(account, ticker):
    """(price, marketStatus) for a ticker from the ws quote api,
    bounded and cached.

    spy trades overnight and post-market, so its quote stays
    live when the index snapshot goes stale; the status lets the
    ui tell a live spot from the market close. Returns None on
    timeout or failure - the ladder falls back to the derived
    value instead of stalling the request thread."""
    now = time.time()
    hit = _ws_quote_cache.get(ticker)
    if hit and now - hit[0] < _WS_QUOTE_TTL:
        return hit[1]
    failed = _ws_quote_fail_ts.get(ticker)
    if failed and now - failed < _WS_QUOTE_RETRY:
        return None
    box = {}

    def _work():
        try:
            box["q"] = _ws_quote_fetch(account, ticker)
        except Exception:
            pass

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(_WS_QUOTE_TIMEOUT)
    q = box.get("q")
    if q:
        _ws_quote_cache[ticker] = (now, q)
    else:
        # the call may still be running in the background -
        # back off so the next poll does not stack another one
        _ws_quote_fail_ts[ticker] = now
    return q


def _bounded(fn, *args, **kwargs):
    """Run fn in a worker thread bounded by _WS_QUOTE_TIMEOUT.

    The ws client's requests carry no timeout, so a stalled
    connection would otherwise pin a waitress worker until the
    pool drains - the dashboard once wedged exactly like that
    (every fetch pending, /health unreachable). Returns None
    when the call does not finish in time; callers degrade to
    their cached/error shapes instead."""
    box = {}

    def _work():
        try:
            box["r"] = fn(*args, **kwargs)
        except Exception:
            pass

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(_WS_QUOTE_TIMEOUT)
    return box.get("r")


def _registered_plan(account, label, acct=None):
    """Non-margin check: an explicit config type wins, then the
    WS API's unifiedAccountType, then label keywords."""
    from consumer.ws.account_types import account_is_non_margin

    if acct is not None and str(
        getattr(acct, "type", "") or ""
    ).strip().lower() in ("margin", "non_margin"):
        return account_is_non_margin(acct)

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
                # ws's maintenance rule: sold spreads carry the
                # FULL wing width (verified against ws's margin
                # page: stocks at 30% + width x 100 x qty = the
                # reported total). risk_cad (the netted max loss)
                # stays a display metric, not the requirement.
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
                    # no derivable width - fall back to the
                    # full width implied by risk + credit
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
    # utilization: the loan against available - ws's own bar
    # divides borrowing by borrowing + available
    margin_used = round(used, 2)
    # ws's available: equity minus the requirement PLUS the
    # current market value of the short structures (the credit
    # side already collected stays spendable) - verified
    # against ws's margin page to the cent
    short_mv_cad = 0.0
    if live is not None:
        for r in live["positions"]:
            if not r.get("short"):
                continue
            mv = r.get("market_value")
            if mv:
                short_mv_cad += abs(float(mv)) * conv_fx
    margin_available = round(
        value - margin_req + short_mv_cad, 2
    )
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


def _acct_by_label(cfg, label):
    from consumer.ws.ws_common import effective_accounts as _ea
    from consumer.ws.ws_common import account_label as _al

    for acct in _ea(cfg):
        if _al(acct) == label:
            return acct
    return None


def _effective_open_risk_cap(cfg, label):
    """The account's own open-risk cap override when set (a
    small account may deploy a high share of its own value),
    the global cap otherwise."""
    from consumer.trading.executor import effective_open_risk_cap

    return effective_open_risk_cap(_acct_by_label(cfg, label), cfg)


def _position_parts_key(row):
    """The alert-format contract key built from a position row's
    parts - ws-sourced rows carry their own display key, the
    ledger (and every alert) keys the plain date."""
    expiry = str(row.get("expiry") or "")[:10]
    strike = row.get("strike")
    right = row.get("right")
    if not expiry or strike is None or not right:
        return None
    try:
        return (
            f"{row['underlying']}-{expiry}"
            f"-{float(strike):g}-{right}"
        )
    except (TypeError, ValueError):
        return None


def _match_store_position(store, mode, label, contract_key,
                          payload_rows=None):
    """A store ledger row for a position: exact contract_key
    first, then a parts match against the payload rows (ws rows
    use their own display key). Returns the store row or None."""
    rows = store.list_positions(mode, label)
    for r in rows:
        if r["contract_key"] == contract_key:
            return r
    for r in (payload_rows or []):
        if r.get("account") != label:
            continue
        if r.get("contract_key") != contract_key:
            continue
        parts_key = _position_parts_key(r)
        if not parts_key:
            continue
        for r2 in rows:
            if r2["contract_key"] == parts_key:
                return r2
        # no ledger row yet: book the ws holding into the ledger
        # so the monitor can watch guards on it
        if r.get("source") == "ws" and not r.get("spread"):
            store.seed_position(
                mode, label, parts_key,
                r.get("underlying") or "",
                str(r.get("expiry") or "")[:10],
                float(r.get("strike") or 0), r.get("right"),
                int(r.get("qty") or 0),
                r.get("avg_premium"),
            )
            return next(
                (x for x in store.list_positions(mode, label)
                 if x["contract_key"] == parts_key),
                None,
            )
    return None


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

    registered = _registered_plan(
        account, label, _acct_by_label(ctx.cfg, label)
    )
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
        # correlation view: the largest (underlying, expiry,
        # right) cluster's risk - the ui flags one bet dominating
        # the open-risk cap
        "open_risk_cluster": (
            max(
                (c["risk"] for c in store.open_risk_clusters(
                    ctx.mode, label
                )),
                default=0.0,
            )
        ),
        "cluster_cap_pct": float(
            getattr(snap["t"], "cluster_cap_pct", 0) or 0
        ),
        "stock_value": stock_value,
        "option_value": option_value,
        "alloc_base": alloc_base,
        # today's realized sell gains minus sell losses, as the
        # paper ledger books them for this account (mirrored real
        # fills included) - the lotto budget rides the same number
        "paper_realized_today": round(
            store.realized_today("paper", label), 2
        ),
        # the real card's today line reads the real account's own
        # ledger: actual fills (bot + manual) at actual prices,
        # booked by the mirror thread per real account - it must
        # never repeat the paper simulation's number
        "realized_today": round(
            store.realized_today("real", label), 2
        ),
        "paper_value": paper_values.get(label),
        "paper_initial": paper_initials.get(label),
        # the adjust editor's cash pools (the ledger's cad cash
        # + the usd pool the editor manages)
        "paper_cash_cad": store.paper_equity(label),
        "paper_cash_usd": store.paper_cash_usd(label),
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
        "max_open_risk_pct": _effective_open_risk_cap(cfg, label),
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
        # only a paper ledger carries positions() - a live
        # account's values() would paint phantom paper cards
        vals_fn = (
            getattr(ledger, "values", None)
            if callable(getattr(ledger, "positions", None))
            else None
        )
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


def _dashboard_sections(ctx):
    """summary / paper / positions in one shot.

    The dashboard polls every few seconds while each section's
    ws fetches can take tens of seconds on a bad network - so
    the refreshes run concurrently, bounded, and anything not
    finished in time serves the last good payload (stale-while-
    revalidate) or its error/empty shape on a cold start."""
    sections = (
        ("summary", _summary_payload, ctx,
         {"error": "summary refresh timed out - "
          "the ws api is not answering"}),
        ("paper", _paper_positions_payload, ctx,
         {"error": "paper pricing timed out - "
          "the ws api is not answering"}),
        ("positions", _positions_payload, ctx, []),
    )
    now = time.time()
    ttl = _section_ttl(ctx.cfg)
    results = {}
    pending = []
    for key, fn, arg, fallback in sections:
        hit = _section_cache.get(key)
        if hit and now - hit[0] < ttl:
            results[key] = hit[1]
        else:
            pending.append((key, fn, arg, fallback))
    boxes = {}
    workers = []
    for key, fn, arg, fallback in pending:
        box = {}
        boxes[key] = box

        def _work(fn=fn, arg=arg, box=box):
            try:
                box["r"] = fn(arg)
            except Exception:
                pass

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        workers.append(worker)
    # the sections run concurrently: the whole batch is capped at
    # one bound, not the sum of them
    deadline = time.time() + _WS_QUOTE_TIMEOUT
    for worker in workers:
        worker.join(max(0.0, deadline - time.time()))
    for key, fn, arg, fallback in pending:
        box = boxes[key]
        payload = box.get("r")
        if payload is not None:
            _section_cache[key] = (time.time(), payload)
            results[key] = payload
        else:
            hit = _section_cache.get(key)
            results[key] = hit[1] if hit else fallback
    return results["summary"], results["paper"], results["positions"]


def _summary_payload(ctx):
    accounts, err = _account_summaries(ctx)
    if err:
        return {"error": err}
    cfg = ctx.cfg
    store = ctx.store
    mode = ctx.mode
    t = cfg.trading
    feed_seen = (ctx.feed_state or {}).get("last_seen")
    if feed_seen:
        # alerts arrive from the info server's feed - the status
        # line reports the feed, not a (absent) local reader
        reader = {
            "channel": "the alert feed",
            "ok": bool((ctx.feed_state or {}).get("ok", True)),
            "error": (ctx.feed_state or {}).get("error"),
            "last_seen": feed_seen,
            "desired": None,
            "age_seconds": round(time.time() - feed_seen, 1),
        }
    else:
        last_seen = ctx.reader_state.get("last_seen")
        reader = dict(ctx.reader_state)
        reader["desired"] = (
            cfg.reader.channels[0] if cfg.reader.channels else ""
        )
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
        # the live trail distance (the stepped ratchet or the
        # pinned trail) - the dashboard shows it beside the stop
        if r.get("right"):
            try:
                from consumer.trading.stops import adaptive_trail_pct
                r["eff_trail"] = adaptive_trail_pct(
                    ctx.cfg.trading, r,
                    r.get("peak_bid") or 0,
                    r.get("avg_premium") or 0,
                )
            except Exception:
                r["eff_trail"] = None
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
