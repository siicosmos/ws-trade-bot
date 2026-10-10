"""Dashboard payload builders: account summaries, the
summary/paper/positions sections, and the bounded ws-quote
machinery they run on.

The builders are module-level functions over PipelineContext
(web_context); create_app's routes stay thin closures that call
them (usually through _bounded).
"""

import threading
import time

from .web_cards import (
    _acct_by_label, _effective_open_risk_cap, _margin_metrics,
    _paper_card_metrics, _registered_plan,
)
from consumer.trading.greeks import portfolio_greeks


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

    # portfolio greeks for the card's delta line: live rows when
    # the ws fetch worked, ledger rows otherwise, plus the stock
    # holdings (delta one per share). spots ride the bounded
    # quote cache - an underlying we cannot quote lands in
    # "unpriced" instead of fabricating a greek
    greek_rows = []
    if live is not None:
        greek_rows += live["positions"]
    else:
        greek_rows += store.list_positions(ctx.mode, label)
    greek_rows += (sk or [])
    spots = {}
    if account is not None:
        for u in sorted({
            r.get("underlying") for r in greek_rows
            if r.get("underlying")
        }):
            q = _ws_stock_quote(account, u)
            if q and q[0]:
                spots[u] = q[0]
    greeks = portfolio_greeks(greek_rows, spots)

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
        "delta": greeks["delta"],
        "gamma": greeks["gamma"],
        "vega": greeks["vega"],
        "theta": greeks["theta"],
        "unpriced_greeks": greeks["unpriced"],
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
