"""Store-backed risk gates and position helpers shared by both
executors (paper and live): the open-risk cap, the correlation
cluster cap, the lotto gain budget, and the position lookups the
unparsed-sell / trailing-stop paths need.

Pure sizing math lives in sizing.py; these gates read the store
and the running config.
"""

from typing import Optional


def effective_open_risk_cap(acct, cfg) -> float:
    """The account's open-risk cap: its own override when set
    (a small account may need a much higher share of its own
    value deployed - the $ exposure stays small), the global
    cap otherwise."""
    if acct is not None and getattr(acct, "max_open_risk_pct", None) is not None:
        return float(acct.max_open_risk_pct)
    return float(cfg.trading.max_open_risk_pct)


def at_open_risk_cap(store, mode, label, value, cfg, acct=None) -> bool:
    cap_pct = effective_open_risk_cap(acct, cfg)
    if cap_pct <= 0 or value is None or value <= 0:
        return False
    open_risk = store.open_risk(mode, label)
    limit = value * (cap_pct / 100.0)
    return open_risk >= limit


def at_cluster_cap(store, mode, label, value, cfg, alert=None,
                   price=None) -> bool:
    """Correlation-aware risk: one (underlying, expiry, right)
    cluster is one bet - three same-direction SPX 0dte calls
    count as three positions against the global cap but are a
    single x3 cluster. A buy into an already-capped cluster is
    skipped even when the global cap has room."""
    cap_pct = float(
        getattr(cfg.trading, "cluster_cap_pct", 0) or 0
    )
    if cap_pct <= 0 or value is None or value <= 0:
        return False
    if alert is None or alert.kind != "option":
        return False
    key = (alert.underlying, alert.expiry, alert.right)
    cluster_risk = sum(
        c["risk"] for c in store.open_risk_clusters(mode, label)
        if (c["underlying"], c["expiry"], c["right"]) == key
    )
    limit = value * (cap_pct / 100.0)
    return cluster_risk >= limit


def lotto_gain_cap(store, mode, cfg, alert) -> Optional[float]:
    """The max $ a lotto / profits-only buy may spend today: a
    fraction (default 75%) of what was realized selling today.
    Zero gains means zero lotto budget."""
    if alert.size != "lotto" and not getattr(
        alert, "profits_only", False
    ):
        return None
    frac = float(getattr(cfg.trading, "lotto_gain_budget_pct", 75.0))
    return max(0.0, store.realized_today(mode)) * frac / 100.0


def ledger_quote_price(account, key):
    """The ledger's live quote for a contract (the same pricing
    the dashboard uses); None when unavailable."""
    quotes_fn = getattr(account, "_quotes", None)
    if not callable(quotes_fn):
        return None
    try:
        quote = quotes_fn().get(key)
        if quote and quote.get("price"):
            return float(quote["price"])
    except Exception:
        pass
    return None


def position_row(store, mode, label, key):
    for r in store.list_positions(mode, label):
        if r.get("contract_key") == key:
            return r
    return None


def stop_floor(t, entry, pos):
    """The position's effective stop floor (the tier's or the
    global stop_loss_pct) - the same math the stop monitor
    prices the floor from."""
    stop_pct = float(t.stop_loss_pct)
    size = str((pos or {}).get("size") or "").lower()
    tier = (t.size_tiers or {}).get(size) if size else None
    if tier is not None and tier.get("stop_loss_pct") is not None:
        stop_pct = float(tier["stop_loss_pct"])
    return round(entry * (1 - stop_pct / 100.0), 4) if entry else None