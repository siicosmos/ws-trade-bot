"""Real-fills ledger (mode="real") booking via the mirror.

The real account card's today gain reads this ledger: actual
fills at actual prices, per real account. Sells book against
the real ledger's cost basis (seeded from the live holdings),
so a real loss shows negative instead of the paper number.
"""
import os
import tempfile
import types

from trader.store import Store
from trader.trading.mirror import mirror_real_trades


def _option_node(action, qty, premium, cid, strike=105,
                 symbol="AAOI"):
    return {
        "canonicalId": cid,
        "status": "settled",
        "type": action,
        "assetQuantity": qty,
        "assetSymbol": symbol,
        "strikePrice": strike,
        "contractType": "CALL",
        "expiryDate": "2026-10-02T00:00:00.000+00:00",
        "amount": qty * (premium or 0) * 100,
        "occurredAt": "2026-09-30T14:30:00.000+00:00",
        "currency": "USD",
    }


class StubWs:
    def __init__(self, edges, positions=None, label="RRSP"):
        self._edges = edges
        self._positions = positions or {}
        self._label = label

    def _resolve(self):
        return [(self._label, f"account-{self._label}")]

    def _client(self):
        edges = self._edges
        return types.SimpleNamespace(
            get_activities=lambda **kw: {"edges": edges}
        )

    def _positions_raw(self):
        return self._positions


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def test_mirror_books_real_fills_into_real_ledger():
    """buy 2 @ 2.0 then sell 1 @ 2.5: the real ledger books the
    actual fills and realizes +50 - the real card's today gain."""
    ws = StubWs([
        {"node": _option_node("BUY", 2, 2.0, "c-buy")},
        {"node": _option_node("SELL", 1, 2.5, "c-sell")},
    ])
    store = _store()
    mirror_real_trades({}, store, ws, None)
    assert abs(store.realized_today("real", "RRSP") - 50.0) < 0.01
    assert abs(store.realized_today("real", "RRSP") - 50.0) < 0.01
    assert store.get_position(
        "real", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 1


def test_mirror_real_sell_without_real_holding_is_skipped():
    """a real sell whose contract the real ledger never held has
    no honest cost basis - the fill is ignored rather than
    crediting the full proceeds as a gain."""
    ws = StubWs([{"node": _option_node("SELL", 1, 2.5, "c-sell")}])
    store = _store()
    mirror_real_trades({}, store, ws, None)
    assert store.realized_today("real", "RRSP") == 0.0
    assert store.get_position(
        "real", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 0


def test_real_seed_gives_sells_honest_basis():
    """the real ledger seeds from the live holdings (their real
    average prices): a sell of a pre-mirroring position books
    realized pnl against that basis, not the full proceeds."""

    class Seeded(StubWs):
        def __init__(self, edges):
            super().__init__(
                edges,
                positions={"MARGIN": [{
                    "security": {
                        "stock": {"symbol": "AAOI"},
                        "optionDetails": {
                            "underlyingSecurity": {"stock": {
                                "symbol": "AAOI"}},
                            "optionType": "CALL",
                            "strikePrice": 105,
                            "expiryDate": "2026-10-02T00:00:00",
                        }},
                    "quantity": 2,
                    "averagePrice": {
                        "amount": 2.0, "currency": "USD"},
                }]},
                label="MARGIN",
            )

    ws = Seeded([{"node": _option_node("SELL", 1, 1.5, "c-s1")}])
    store = _store()
    mirror_real_trades({}, store, ws, None)
    # sold 1 of the seeded 2 @ 1.5 against a 2.0 basis -> -50
    assert abs(store.realized_today("real", "MARGIN") + 50.0) < 0.01


def _est_shim(action="BUY", key="AAOI-2026-10-02-105-C"):
    return types.SimpleNamespace(
        kind="option", action=action, ticker="AAOI",
        underlying="AAOI", expiry="2026-10-02", strike=105,
        right="C", contract_key=lambda: key,
        dedupe_key=lambda: "est",
    )


def _pending(store, action, qty, est, pre_qty=0, pre_avg=None,
             label="RRSP", key="AAOI-2026-10-02-105-C"):
    store.record_pending_order(
        "live", label, "ord-1", "option", key, "AAOI",
        "2026-10-02", 105, "C", action, qty, est,
        pre_qty=pre_qty, pre_avg=pre_avg,
    )
    return store.open_pending_orders("live", label)[0]


def test_reconcile_full_fill_reprices_sell():
    """booked 2 sold @ 2.0 against a 1.0 basis (est realized
    +200), filled 2 @ 2.5: realized corrects to +300 (truth) and
    the order settles filled."""
    from trader.trading.mirror import reconcile_pending_fill

    store = _store()
    store.apply_position(
        "live", _est_shim("BUY"), 2, premium=1.0, account="RRSP"
    )
    row = _pending(store, "SELL", 2, 2.0, pre_qty=2, pre_avg=1.0)
    store.apply_position("live", _est_shim("SELL"), -2, premium=2.0,
                         account="RRSP")
    assert abs(store.realized_today("live", "RRSP") - 200.0) < 0.01
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "SELL", 2, 2.5
    )
    # truth: 2x(2.5-1.0)x100 = +300
    assert abs(store.realized_today("live", "RRSP") - 300.0) < 0.01
    assert store.get_position(
        "live", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 0
    assert store.open_pending_orders("live", "RRSP") == []


def test_partial_fill_restores_exact_basis():
    """booked 2 sold @ 2.0 (pre 2 @ 1.0), filled only 1 @ 2.5:
    qty restores to 1 and realized is the truth (1x(2.5-1.0)x100
    = +150), not the estimate's +200."""
    from trader.trading.mirror import reconcile_pending_fill

    store = _store()
    store.apply_position(
        "live", _est_shim("BUY"), 2, premium=1.0, account="RRSP"
    )
    _pending(store, "SELL", 2, 2.0, pre_qty=2, pre_avg=1.0)
    store.apply_position("live", _est_shim("SELL"), -2, premium=2.0,
                         account="RRSP")
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "SELL", 1, 2.5
    )
    assert store.get_position(
        "live", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 1
    assert abs(store.realized_today("live", "RRSP") - 150.0) < 0.01


def test_sweep_reverses_unfilled_estimate_exactly():
    """a buy estimate that never filled restores the pre-order
    position exactly: qty and the average premium both."""
    from trader.trading.mirror import sweep_pending_orders
    import sqlite3

    store = _store()
    store.apply_position(
        "live", _est_shim("BUY"), 2, premium=1.0, account="RRSP"
    )
    row = _pending(store, "BUY", 2, 2.0, pre_qty=2, pre_avg=1.0)
    store.apply_position("live", _est_shim("BUY"), 2, premium=2.0,
                         account="RRSP")
    # the estimate blended the avg to (2x1.0 + 2x2.0)/4 = 1.5
    assert abs(store.list_positions("live", "RRSP")[0][
        "avg_premium"] - 1.5) < 0.01
    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE pending_orders SET placed_ts = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", row["id"]),
    )
    conn.commit()
    conn.close()
    sweep_pending_orders(store, "RRSP")
    pos = store.list_positions("live", "RRSP")[0]
    assert pos["qty"] == 2
    assert abs(pos["avg_premium"] - 1.0) < 0.01
    assert store.open_pending_orders("live", "RRSP") == []
