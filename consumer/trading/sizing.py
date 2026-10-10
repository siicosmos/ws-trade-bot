"""Position sizing: tier resolution, budget math, and the
per-account sizing preview the dashboard and the pipeline render.

Pure math over the config + account values - the store-backed
risk gates live in risk_gates.py.
"""

from typing import Optional

from consumer.ws.account import account_label, effective_accounts

from .risk_gates import at_cluster_cap, effective_open_risk_cap


def tier_for(alert, cfg):
    tiers = (
        cfg.trading.size_tiers
        if alert is None or alert.kind != "stock"
        else getattr(cfg.trading, "stock_size_tiers", None)
    ) or {}
    if alert and alert.size:
        tier = tiers.get(alert.size)
        if tier:
            return tier
    return None


def effective_risk_pct(acct, cfg, alert=None) -> float:
    tier = tier_for(alert, cfg)
    if tier is not None:
        return float(tier["risk_pct_max"])
    if acct and acct.risk_per_trade_pct is not None:
        return float(acct.risk_per_trade_pct)
    return cfg.trading.risk_per_trade_pct


def effective_contract_cap(acct, cfg) -> int:
    if acct and acct.max_contracts_per_trade is not None:
        return int(acct.max_contracts_per_trade)
    return cfg.trading.max_contracts_per_trade


def tier_plan(alert, cfg, account_value, price, acct=None) -> dict:
    # options size per contract (x100); stocks per share
    mult = 100 if (alert is None or alert.kind != "stock") else 1
    cost = float(price) * mult if price else 0.0
    tier = tier_for(alert, cfg)
    risk_pct = effective_risk_pct(acct, cfg, alert)
    if account_value and account_value > 0:
        budget = float(account_value) * risk_pct / 100.0
    else:
        budget = 0.0
    affordable = int(budget // cost) if cost > 0 and account_value else 0
    cap = effective_contract_cap(acct, cfg)

    tier_min = tier_max = None
    if tier is not None:
        tier_min = int(tier["contracts_min"])
        tier_max = int(tier["contracts_max"])
        qty = min(affordable, tier_max)
        if cap and cap > 0:
            qty = min(qty, cap)
        if qty < tier_min:
            qty = 0
    else:
        qty = affordable
        if cap and cap > 0:
            qty = min(qty, cap)

    return {
        "qty": max(0, qty),
        "affordable": affordable,
        "budget": budget,
        "risk_pct": risk_pct,
        "tier_min": tier_min,
        "tier_max": tier_max,
        "cap": cap,
        "cost": cost,
        "tier": alert.size,
    }


def account_sizing(alert, cfg, account, store=None) -> list:
    price = alert.premium if alert.kind == "option" else (
        alert.entry or alert.premium
    )
    mode = "live" if cfg.trading.mode == "live" else "paper"
    rows = []
    for acct in effective_accounts(cfg):
        label = account_label(acct)
        value = None
        if account is not None:
            try:
                value = account.value(label)
            except Exception:
                value = None

        plan = tier_plan(alert, cfg, value, price, acct)
        contracts = plan["qty"] if (value is not None and price) else None
        warnings = []

        stale_fn = getattr(account, "stale_age", None)
        if value is not None and callable(stale_fn):
            age = stale_fn(label)
            if age:
                warnings.append(
                    f"using cached account value ({age} old)"
                )

        if value is not None and value > 0 and price:
            if plan["qty"] < 1:
                if plan["affordable"] >= 1:
                    warnings.append(
                        f"{plan['risk_pct']:g}% budget "
                        f"${plan['budget']:,.2f} affords "
                        f"{plan['affordable']}, tier minimum is "
                        f"{plan['tier_min'] or 1}"
                    )
                else:
                    warnings.append(
                        f"{plan['risk_pct']:g}% budget "
                        f"${plan['budget']:,.2f} can't cover "
                        f"1 contract at ${plan['cost']:,.0f}"
                    )
            elif plan["affordable"] > plan["qty"]:
                if (
                    plan["cap"]
                    and plan["cap"] > 0
                    and (plan["tier_max"] is None or plan["cap"] < plan["tier_max"])
                ):
                    warnings.append(
                        f"capped at account max of {plan['cap']} "
                        f"(budget could afford {plan['affordable']})"
                    )
                elif (
                    plan["tier_max"] is not None
                    and plan["affordable"] > plan["tier_max"]
                ):
                    tier_name = plan.get("tier") or "size"
                    warnings.append(
                        f"capped at {tier_name} tier max of "
                        f"{plan['tier_max']} "
                        f"(budget could afford {plan['affordable']})"
                    )

        acct_cap = effective_open_risk_cap(acct, cfg)
        if (
            alert.action == "BUY"
            and store is not None
            and value
            and plan["qty"]
            and acct_cap > 0
        ):
            open_risk = store.open_risk(mode, label)
            limit = value * (acct_cap / 100.0)
            if open_risk >= limit:
                warnings.append(
                    f"open risk cap reached "
                    f"(${open_risk:,.0f} of ${limit:,.0f} deployed)"
                )

        if (
            alert.action == "BUY"
            and store is not None
            and value
            and plan["qty"]
            and at_cluster_cap(
                store, mode, label, value, cfg, alert, price
            )
        ):
            clusters = store.open_risk_clusters(mode, label)
            key = (alert.underlying, alert.expiry, alert.right)
            cluster_risk = sum(
                c["risk"] for c in clusters
                if (c["underlying"], c["expiry"], c["right"]) == key
            )
            warnings.append(
                f"cluster cap reached: {alert.underlying} "
                f"{alert.expiry} {alert.right} already at "
                f"${cluster_risk:,.0f} "
                f"({getattr(cfg.trading, 'cluster_cap_pct', 0):g}% "
                f"of account)"
            )

        rows.append(
            {
                "label": label,
                "value": value,
                "risk_pct": plan["risk_pct"],
                "budget": plan["budget"],
                "price": price,
                "contracts": contracts,
                "final_contracts": contracts,
                "actual_risk": (
                    plan["qty"] * plan["cost"]
                    if contracts is not None
                    else None
                ),
                "warnings": warnings,
            }
        )
    return rows


def sell_quantity(held: int, scale: Optional[float]) -> int:
    if held <= 0:
        return 0
    if scale is None or scale >= 1.0:
        return held
    qty = int(held * scale + 0.5)
    return min(held, max(1, qty))


def sizing_multiplier(cfg, store=None, mode=None, provider=None):
    """A 0..1 scalar applied ON TOP of the alert's tier size -
    never a replacement for it (the alert's tier contract stands;
    the scalar only shrinks, never amplifies).

    Two optional components, both default-off in the config:

    - vix_size_scalar: size shrinks as vix rises above its
      long-run average (min(1, 15 / vix)); the vix comes from the
      moomoo provider's index snapshot - unavailable means fail
      open at 1.0 (an etf proxy is refused: its price is
      decoupled from the vix level the formula is calibrated for).
    - kelly_size_scalar: a fractional-kelly fraction from the
      mode's own closed round trips; fewer closed trades than
      kelly_min_trades means fail open at 1.0.

    Returns -1.0 as a VETO sentinel when the kelly component
    finds no edge (full kelly <= 0): the executor skips the
    account instead of executing an uneconomic 1-contract
    minimum. The executor floors positive scalars at 1 contract,
    so a 0 scalar throttles to the minimum rather than vetoing."""
    t = cfg.trading
    mult = 1.0
    # the kelly component first: its veto (-1) skips the vix
    # fetch entirely - no point paying for a regime input the
    # veto discards
    if getattr(t, "kelly_size_scalar", False) and store is not None:
        k = _kelly_fraction(store, mode, t)
        if k is not None:
            if k < 0:
                return -1.0
            mult *= k
    if getattr(t, "vix_size_scalar", False):
        vix = _vix_level(provider)
        if vix:
            mult *= min(1.0, 15.0 / max(float(vix), 1.0))
    return max(0.0, min(1.0, mult))


def _vix_level(provider):
    """The vix regime level from the active moomoo provider -
    None (fail open) when there is none or it cannot answer."""
    if provider is None:
        try:
            from consumer.trading.quotes import ACTIVE_QUOTE_PROVIDER

            provider = ACTIVE_QUOTE_PROVIDER
        except Exception:
            return None
    if provider is None or not hasattr(provider, "vix_quote"):
        return None
    try:
        return provider.vix_quote()
    except Exception:
        return None


def _kelly_fraction(store, mode, t):
    """The fractional-kelly scalar from the mode's closed round
    trips - None (fail open) below kelly_min_trades, -1.0 (veto)
    when full kelly is negative (the book has no edge), else the
    clamped fraction."""
    try:
        stats = store.closed_trade_stats(
            mode, int(getattr(t, "kelly_min_trades", 10) or 10) * 5
        )
    except Exception:
        return None
    n, wins = stats["n"], stats["wins"]
    if n < int(getattr(t, "kelly_min_trades", 10) or 10):
        return None
    p = wins / n
    avg_win, avg_loss = stats["avg_win"], stats["avg_loss"]
    # a book with no losses has avg_loss 0 - no odds to compute,
    # fail open rather than divide (the streak breaker covers it)
    if avg_win <= 0 or avg_loss <= 0:
        return None
    b = avg_win / avg_loss
    full_kelly = (p * b - (1 - p)) / b
    if full_kelly <= 0:
        return -1.0
    frac = float(getattr(t, "kelly_fraction", 0.25) or 0.25)
    return max(0.0, min(1.0, full_kelly * frac))
