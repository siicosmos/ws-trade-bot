"""Option strategy classification from position legs.

Takes the net legs of one underlying+expiry group and names the
structure: verticals (call/put debit/credit), butterflies (long/
short, plain and broken wing), iron condors and iron butterflies
(long/short, plain and broken wing), and ratio spreads. Also
derives each structure's max-loss width for risk display.
"""


def _close(a, b):
    return abs(a - b) < 1e-9


def _net_legs(legs):
    nets = {}
    qty_hint = 0.0
    for leg in legs:
        try:
            strike = round(float(leg.get("strike")), 4)
            qty = abs(float(leg.get("qty") or 0))
        except (TypeError, ValueError):
            return None, 0.0
        right = str(leg.get("right") or "").upper()[:1] or "?"
        signed = -qty if leg.get("short") else qty
        nets[(right, strike)] = nets.get((right, strike), 0) + signed
        qty_hint = max(qty_hint, qty)
    return nets, qty_hint


def _vertical(strikes, side):
    """Two same-type strikes, equal opposite quantities."""
    (s1, q1), (s2, q2) = strikes
    if abs(q1) != abs(q2) or q1 * q2 > 0:
        return None
    if side == "call":
        debit = q1 > 0            # long lower call = bull call debit
    else:
        debit = q2 > 0            # long higher put = bear put debit
    return {
        "name": f"{side} {'debit' if debit else 'credit'} spread",
        "kind": "vertical",
        "qty": abs(q1),
        "strikes": [s1, s2],
        "width": s2 - s1,
        "debit": debit,
    }


def _butterfly(strikes, side):
    """Three strikes in a 1:2:1 wing-body-wing ratio."""
    (s1, q1), (s2, q2), (s3, q3) = strikes
    base = abs(q1)
    if not base or abs(q2) != 2 * base or abs(q3) != base:
        return None
    if q1 * q3 <= 0 or q1 * q2 >= 0:
        return None
    long_fly = q1 > 0
    w1, w2 = s2 - s1, s3 - s2
    broken = not _close(w1, w2)
    name = ("long " if long_fly else "short ") + side + (
        " broken wing butterfly" if broken else " butterfly"
    )
    return {
        "name": name,
        "kind": "butterfly",
        "qty": base,
        "strikes": [s1, s2, s3],
        "width": max(w1, w2),
        "debit": long_fly,
    }


def _classify_side(strikes, side):
    if len(strikes) == 2:
        vertical = _vertical(strikes, side)
        if vertical:
            return vertical
        (s1, q1), (s2, q2) = strikes
        return {
            "name": f"{side} ratio spread",
            "kind": "ratio",
            "qty": max(abs(q1), abs(q2)),
            "strikes": [s1, s2],
            "width": s2 - s1,
            "debit": None,
        }
    if len(strikes) == 3:
        fly = _butterfly(strikes, side)
        if fly:
            return fly
    return None


def _classify_four(calls, puts, qty):
    """Call vertical + put vertical: condor or iron butterfly."""
    c = _vertical(calls, "call")
    p = _vertical(puts, "put")
    if not c or not p:
        return None
    body_shared = _close(c["strikes"][0], p["strikes"][1])
    widths = (c["width"], p["width"])
    broken = not _close(*widths)
    # selling both verticals = short structure (collect credit);
    # buying both = long
    short_struct = not c["debit"]
    if short_struct != (not p["debit"]):
        return None          # mixed directions - not a condor
    direction = "short" if short_struct else "long"
    if body_shared:
        name = f"{direction} iron butterfly"
        kind = "iron_fly"
    else:
        name = (
            f"{direction} broken wing iron condor" if broken
            else f"{direction} iron condor"
        )
        kind = "condor"
    return {
        "name": name,
        "kind": kind,
        "qty": min(c["qty"], p["qty"]),
        "strikes": sorted(
            p["strikes"] + c["strikes"]
        ),
        "width": max(widths),
        "debit": not short_struct,
    }


def classify_legs(legs):
    """Classify a group of same-expiry legs.

    legs: [{strike, right ('C'/'P'), short (bool), qty (>0)}].
    Returns a dict with name / kind / qty / strikes / width /
    debit, or None when the structure is not recognized.
    """
    nets, qty_hint = _net_legs(legs)
    if nets is None or not qty_hint:
        return None
    calls = sorted(
        (s, q) for (r, s), q in nets.items() if r == "C" and q
    )
    puts = sorted(
        (s, q) for (r, s), q in nets.items() if r == "P" and q
    )
    if not calls and not puts:
        return None
    if not puts:
        return _classify_side(calls, "call")
    if not calls:
        return _classify_side(puts, "put")
    if len(calls) == 2 and len(puts) == 2:
        return _classify_four(calls, puts, qty_hint)
    return None
