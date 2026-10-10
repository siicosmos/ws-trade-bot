"""Portfolio Greeks for the dashboard and the risk gates.

Self-contained Black-Scholes math (math.erf, no third-party
pricing library) - the bot's requirements are pinned and a
pricing dependency for four numbers is not worth the supply
chain. The IV input is a flat approximation (no surface is
tracked), which is fine for a risk LIMIT and for display, not
for marking.

Conventions: T in years, r continuous, sigma annualized, right
"C"/"P". Positions are the store's list_positions rows (option
rows carry right/strike/expiry; stock rows have right None and
contribute delta = qty).
"""

import math
from datetime import date, datetime, timedelta

from core.store import et_now


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_greeks(S, K, T, r=0.05, sigma=0.30, right="C"):
    """Black-Scholes greeks for one option unit (per share).

    At or past expiry (T <= 0) the greeks collapse to the
    intrinsic step: delta 1/-1 in the money, 0 out of the money,
    gamma/vega/theta 0 - the expiry-day limit case (a 0dte
    position's delta is binary, not smooth)."""
    out = {
        "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
        "price": 0.0,
    }
    if S is None or K is None or S <= 0 or K <= 0:
        return out
    call = (right or "C").upper().startswith("C")
    if T <= 0:
        if call:
            out["delta"] = 1.0 if S > K else 0.0
            out["price"] = max(0.0, S - K)
        else:
            out["delta"] = -1.0 if S < K else 0.0
            out["price"] = max(0.0, K - S)
        return out
    try:
        sigma = float(sigma)
        r = float(r)
    except (TypeError, ValueError):
        return out
    if sigma <= 0:
        # deterministic forward: intrinsic discounted
        if call:
            out["delta"] = 1.0 if S > K else 0.0
        else:
            out["delta"] = -1.0 if S < K else 0.0
        return out
    sq_t = math.sqrt(T)
    d1 = (
        math.log(S / K) + (r + 0.5 * sigma * sigma) * T
    ) / (sigma * sq_t)
    d2 = d1 - sigma * sq_t
    nd1, nd2 = _norm_cdf(d1), _norm_cdf(d2)
    disc = math.exp(-r * T)
    if call:
        out["delta"] = nd1
        out["price"] = S * nd1 - K * disc * nd2
    else:
        out["delta"] = nd1 - 1.0
        out["price"] = K * disc * (1 - nd2) - S * (1 - nd1)
    out["gamma"] = _norm_pdf(d1) / (S * sigma * sq_t)
    # vega per 1.00 vol point (the 1/100 convention: a 1-point
    # move is 0.01 in sigma)
    out["vega"] = S * _norm_pdf(d1) * sq_t / 100.0
    # theta per day (the standard /365 convention)
    if call:
        theta = (
            -S * _norm_pdf(d1) * sigma / (2 * sq_t)
            - r * K * disc * nd2
        )
    else:
        theta = (
            -S * _norm_pdf(d1) * sigma / (2 * sq_t)
            + r * K * disc * (1 - nd2)
        )
    out["theta"] = theta / 365.0
    return out


def days_to_expiry(expiry, today=None):
    """Calendar days from today (ET) to the expiry string - a
    0dte position is 0, a past-dated row clamps at 0."""
    if not expiry:
        return 0.0
    try:
        exp = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return 0.0
    today = today or et_now().date()
    return max(0.0, float((exp - today).days))


def portfolio_greeks(positions, spots, r=0.05, default_iv=0.30,
                     iv_by_key=None, today=None):
    """Aggregate greeks over list_positions rows.

    spots: {underlying: spot} - rows without a spot are skipped
    (no price, no greek). Option rows contribute per-contract
    greeks x qty x 100; stock rows (right None) contribute
    delta = qty (shares) and nothing else. Returns the aggregate
    dict plus the count of rows that could not be priced."""
    agg = {
        "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
        "unpriced": 0,
    }
    iv_by_key = iv_by_key or {}
    for p in positions or []:
        qty = int(p.get("qty") or 0)
        if not qty:
            continue
        S = spots.get(p.get("underlying"))
        if S is None:
            agg["unpriced"] += 1
            continue
        if not p.get("right"):
            # stock: delta one per share
            agg["delta"] += qty
            continue
        T = days_to_expiry(p.get("expiry"), today) / 365.0
        g = bs_greeks(
            S, p.get("strike"), T, r,
            iv_by_key.get(p.get("contract_key"), default_iv),
            p.get("right"),
        )
        mult = qty * 100
        agg["delta"] += g["delta"] * mult
        agg["gamma"] += g["gamma"] * mult
        agg["vega"] += g["vega"] * mult
        agg["theta"] += g["theta"] * mult
    for k in ("delta", "gamma", "vega", "theta"):
        agg[k] = round(agg[k], 2)
    return agg