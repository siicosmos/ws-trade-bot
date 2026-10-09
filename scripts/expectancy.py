"""Paper expectancy report: judge the alert feed per size tier.

Pairs every closed round trip in the ledger (a position row at
qty 0 with booked realized pnl) and reports, per size tier and
account: trade count, win rate, avg win, avg loss, realized per
trade and realized total. A best-effort slippage section
compares the alert's premium (paper-executed rows) against the
mirrored real fill's price for the same contract/action/day.

Usage:
    python scripts/expectancy.py [--db db/consumer.trades.db] [--mode paper]

Decision rule (see docs/live_readiness_plan.md): a tier with a
negative expectancy over >= 30 closed trades is a candidate for
contracts_max: 0 in that size tier.
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
sys.path.insert(0, ROOT)


def _rows(db, query, params=()):
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, params)]
    finally:
        conn.close()


def expectancy(db, mode):
    closed = _rows(
        db,
        "SELECT account, contract_key, underlying, size, realized "
        "FROM positions WHERE mode = ? AND qty = 0 "
        "AND realized IS NOT NULL AND realized != 0 "
        "ORDER BY account, underlying",
        (mode,),
    )
    open_pos = _rows(
        db,
        "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS qty "
        "FROM positions WHERE mode = ? AND qty > 0",
        (mode,),
    )[0]

    tiers = {}
    for row in closed:
        tier = (row["size"] or "unsized").lower()
        key = (row["account"] or "default", tier)
        t = tiers.setdefault(key, {
            "n": 0, "wins": 0, "sum_win": 0.0, "sum_loss": 0.0,
            "realized": 0.0,
        })
        t["n"] += 1
        t["realized"] += row["realized"]
        if row["realized"] > 0:
            t["wins"] += 1
            t["sum_win"] += row["realized"]
        else:
            t["sum_loss"] += row["realized"]

    print(f"expectancy report - ledger mode={mode!r} ({db})")
    print("=" * 78)
    print(
        f"open positions: {open_pos['n']} ({open_pos['qty']} contracts)"
    )
    print()
    if not tiers:
        print("no closed round trips yet - nothing to report")
        return

    header = (
        f"{'account':<10} {'tier':<8} {'trades':>6} {'win%':>6} "
        f"{'avg win':>10} {'avg loss':>10} {'exp/trade':>11} "
        f"{'realized':>12}"
    )
    print(header)
    print("-" * len(header))
    flags = []
    for (account, tier), t in sorted(tiers.items()):
        win_rate = t["wins"] / t["n"] * 100 if t["n"] else 0.0
        avg_win = t["sum_win"] / t["wins"] if t["wins"] else 0.0
        avg_loss = t["sum_loss"] / (t["n"] - t["wins"]) \
            if t["n"] > t["wins"] else 0.0
        exp = t["realized"] / t["n"]
        flag = ""
        if t["n"] >= 30 and exp < 0:
            flag = "  <-- negative: candidate for contracts_max: 0"
            flags.append((account, tier))
        print(
            f"{account:<10} {tier:<8} {t['n']:>6} {win_rate:>5.0f}% "
            f"{avg_win:>10.2f} {avg_loss:>10.2f} {exp:>11.2f} "
            f"{t['realized']:>12.2f}{flag}"
        )
    if flags:
        print()
        print("negative-expectancy tiers (>= 30 trades): set")
        print("  contracts_max: 0 for these in the size tiers:")
        for account, tier in flags:
            print(f"  - {account}: {tier}")
    print()
    slippage(db, mode)


def slippage(db, mode):
    """Best effort: the alert's premium (paper-executed rows)
    vs the mirrored real fill's price for the same contract,
    action and day. Mirrored rows carry the 'mirrored real fill'
    detail tag."""
    rows = _rows(
        db,
        "SELECT action, ticker, strike, expiry, opt_right, "
        "       date(ts) AS day, price, detail "
        "FROM trades WHERE mode = ? AND status = 'executed' "
        "AND price IS NOT NULL AND strike IS NOT NULL "
        "ORDER BY ts",
        (mode,),
    )
    alerts, fills = {}, {}
    for r in rows:
        key = (
            r["action"], r["ticker"], r["strike"], r["expiry"],
            r["opt_right"], r["day"],
        )
        mirrored = "mirrored real fill" in (r["detail"] or "")
        bucket = fills if mirrored else alerts
        bucket.setdefault(key, []).append(r["price"])

    pairs = []
    for key, prices in alerts.items():
        fill_prices = fills.get(key)
        if fill_prices:
            pairs.append((key, prices[0], fill_prices[0]))
    if not pairs:
        print("slippage: no alert-vs-real-fill pairs found yet")
        return
    print("slippage (alert premium vs mirrored real fill, first of day):")
    print(f"{'action':<6} {'contract':<28} {'alert':>8} {'fill':>8} {'diff':>8}")
    diffs = []
    for key, alert_price, fill_price in pairs:
        diff = fill_price - alert_price
        diffs.append(diff)
        contract = f"{key[1]} {key[2]:g}{key[4] or ''}"
        print(
            f"{key[0]:<6} {contract:<28} {alert_price:>8.2f} "
            f"{fill_price:>8.2f} {diff:>+8.2f}"
        )
    avg = sum(diffs) / len(diffs)
    print(f"\naverage slippage: {avg:+.2f} over {len(diffs)} pair(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        default=os.path.join(ROOT, "db", "consumer.trades.db"),
    )
    ap.add_argument("--mode", default="paper",
                    choices=["paper", "live", "real"])
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"database not found: {args.db}")
        return 1
    expectancy(args.db, args.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())