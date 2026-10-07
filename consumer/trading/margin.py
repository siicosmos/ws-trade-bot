"""One margin model, one source of truth.

Computes the maintenance requirement and its breakdown from a
normalized list of holdings. Both the real account cards and
the paper ledger cards build Holdings and call in here, so the
two paths can never drift apart.

Currency model: every holding carries its native amount and
listing currency; the requirement is accumulated in CAD via the
passed fx. Long options are charged at 100% of their value,
spreads at their full strike width (WS does not net the credit),
shorts at their defined risk, stocks at their margin rate.
"""


class Holding:
    """One margin-relevant holding, normalized.

    kind: "stock" | "spread" | "short" | "long"
    currency: "usd" | "cad" - the holding's listing currency

    stock  - symbol, native_value, security_id (for rate lookup)
    spread - symbol, structure, qty, width (strike width in
             points), amount_native (when the width cannot be
             derived, the charge to use instead)
    short  - symbol, risk (defined risk in native currency)
    long   - symbol, native_value
    """

    def __init__(
        self, symbol, kind, currency="cad", native_value=None,
        security_id=None, structure=None, qty=None, width=None,
        amount_native=None, risk=None,
    ):
        self.symbol = symbol
        self.kind = kind
        self.currency = (
            "usd" if str(currency).lower() == "usd" else "cad"
        )
        self.native_value = native_value
        self.security_id = security_id
        self.structure = structure
        self.qty = qty
        self.width = width
        self.amount_native = amount_native
        self.risk = risk


def resolve_rate(
    symbol, security_id=None, rate_fn=None,
    overrides=None, default_rate=0.30,
):
    """Margin rate for a stock: the per-security rate when a
    resolver is available (real holdings know their security
    id), the configured overrides next, then the default."""
    if callable(rate_fn) and security_id:
        try:
            rate = rate_fn(security_id)
            if rate is not None:
                return float(rate)
        except Exception:
            pass
    return float((overrides or {}).get(symbol, default_rate))


def compute_requirement(holdings, fx=1.0, rate_for=None):
    """Returns (requirement_cad, breakdown_lines).

    rate_for: callable(Holding) -> float, for stock rates. Pass
    a resolver bound to the account's per-security rates and the
    configured overrides.
    """
    req = 0.0
    parts = []
    for h in holdings:
        cur_fx = fx if h.currency == "usd" else 1.0
        if h.kind == "stock":
            rate = (
                rate_for(h) if callable(rate_for)
                else 0.30
            )
            charge = (h.native_value or 0) * rate
            req += charge * cur_fx
            parts.append(
                f"{h.symbol} {h.native_value:.2f} x {rate:.0%} "
                f"= {charge:.2f} {h.currency}"
            )
        elif h.kind == "spread":
            if h.amount_native is not None:
                charge = h.amount_native
            else:
                charge = (h.width or 0) * 100 * (h.qty or 0)
            req += charge * cur_fx
            width_txt = (
                f"width {h.width:g} = " if h.width else "= "
            )
            parts.append(
                f"{h.symbol} {h.structure or 'spread'} "
                f"{(h.qty or 0):g}x {width_txt}{charge:.2f} "
                f"{h.currency}"
            )
        elif h.kind == "short":
            req += (h.risk or 0) * cur_fx
            parts.append(
                f"{h.symbol} short risk {(h.risk or 0):.2f} "
                f"{h.currency}"
            )
        elif h.kind == "long":
            charge = abs(h.native_value or 0)
            req += charge * cur_fx
            parts.append(
                f"{h.symbol} option {charge:.2f} x 100% "
                f"{h.currency}"
            )
    return round(req, 2), parts
