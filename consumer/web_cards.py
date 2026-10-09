"""Account-card math: margin metrics for live and paper
accounts, registered-plan detection, and the account/config
lookups the cards need.

The shared margin model lives in trading/margin.py; this module
adapts the dashboard's payloads to it.
"""

from consumer.trading.margin import (
    Holding, compute_requirement, resolve_rate,
)
from consumer.ws.account_types import REGISTERED_ACCOUNT_TYPES


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
    from consumer.trading.risk_gates import effective_open_risk_cap

    return effective_open_risk_cap(_acct_by_label(cfg, label), cfg)
