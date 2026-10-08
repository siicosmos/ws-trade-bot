"""Real-fills ledger (mode="real") booking via the mirror.

The real account card's today gain reads this ledger: actual
fills at actual prices, per real account. Sells book against
the real ledger's cost basis (seeded from the live holdings),
so a real loss shows negative instead of the paper number.
"""
import os
import tempfile
import types

from core.store import Store
from consumer.trading.mirror import mirror_real_trades
import pytest  # noqa: E402


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


pytestmark = pytest.mark.essential

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


@pytest.mark.minimum
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
    from consumer.trading.mirror import reconcile_pending_fill

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
    from consumer.trading.mirror import reconcile_pending_fill

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
    from consumer.trading.mirror import (
        sweep_pending_orders, reconcile_pending_fill
    )
    import sqlite3
    import types as _types

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
    cfg = _types.SimpleNamespace(trading=_types.SimpleNamespace(
        partial_fill_cancel_pct=0
    ))
    ws = types.SimpleNamespace(_client=lambda: types.SimpleNamespace())
    sweep_pending_orders(cfg, store, ws, "RRSP")
    pos = store.list_positions("live", "RRSP")[0]
    assert pos["qty"] == 2
    assert abs(pos["avg_premium"] - 1.0) < 0.01
    assert store.open_pending_orders("live", "RRSP") == []


def test_sweep_keeps_partially_filled_booking():
    """an order that partially filled before the ttl keeps the
    filled part booked exactly (the account really holds it) -
    only the unfilled remainder is reversed."""
    from consumer.trading.mirror import (
        sweep_pending_orders, reconcile_pending_fill
    )
    import sqlite3
    import types as _types

    store = _store()
    # pre-order: nothing held; estimated booking 2x @ 2.0
    row = _pending(store, "BUY", 2, 2.0, pre_qty=0, pre_avg=None)
    store.apply_position(
        "live", _est_shim("BUY"), 2, premium=2.0, account="RRSP"
    )
    # one contract actually filled @ 2.2
    from consumer.trading.mirror import reconcile_pending_fill

    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 1, 2.2
    )
    # the estimate is corrected to the actual holding: 1 filled,
    # the rest of the order still open
    assert store.get_position(
        "live", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 1
    # the order stays open - a later fill of the remainder
    # still reconciles
    assert len(store.open_pending_orders("live", "RRSP")) == 1

    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE pending_orders SET placed_ts = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", row["id"]),
    )
    conn.commit()
    conn.close()
    cfg = _types.SimpleNamespace(trading=_types.SimpleNamespace(
        partial_fill_cancel_pct=0
    ))
    ws = types.SimpleNamespace(_client=lambda: types.SimpleNamespace())
    sweep_pending_orders(cfg, store, ws, "RRSP")
    # the ledger holds exactly what filled: 1x @ 2.2
    pos = store.list_positions("live", "RRSP")[0]
    assert pos["qty"] == 1
    assert abs(pos["avg_premium"] - 2.2) < 0.01
    assert store.open_pending_orders("live", "RRSP") == []


def test_partial_fill_accumulates_and_settles():
    """fills of one order accumulate: 1 of 2, then the rest -
    each correction re-books from the pre snapshot with the
    blended price, and the order settles only when complete."""
    from consumer.trading.mirror import reconcile_pending_fill

    store = _store()
    row = _pending(store, "BUY", 2, 2.0, pre_qty=0, pre_avg=None)
    store.apply_position(
        "live", _est_shim("BUY"), 2, premium=2.0, account="RRSP"
    )
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 1, 2.2
    )
    # the order stays open - the remainder may still fill
    open_rows = store.open_pending_orders("live", "RRSP")
    assert len(open_rows) == 1
    assert open_rows[0]["filled_qty"] == 1
    assert abs(open_rows[0]["filled_price"] - 2.2) < 0.01
    # a later fill of the remainder reconciles the same order
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 1, 2.4
    )
    assert store.get_position(
        "live", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 2
    pos = store.list_positions("live", "RRSP")[0]
    # blended fill price: (1x2.2 + 1x2.4) / 2 = 2.3
    assert abs(pos["avg_premium"] - 2.3) < 0.01
    assert store.open_pending_orders("live", "RRSP") == []


def _chain_client(price, cancelled=None):
    """a ws client stub: the option chain quotes `price`, the
    cancel records itself (and raises like ws does on an
    already-closed order when told to fail)."""
    state = {"cancelled": cancelled if cancelled is not None else []}

    def cancel_order(order_id):
        state["cancelled"].append(order_id)
        return {}

    return types.SimpleNamespace(
        get_ticker_id=lambda ticker, hint: "sec1",
        get_option_expiry_dates=lambda sec_id: ["2026-10-02"],
        get_option_chain=lambda sec_id, expiry, opt_type: [{
            "id": "opt1",
            "strikePrice": {"amount": "105"},
            "quote": {"bid": str(price)},
        }],
        cancel_order=cancel_order,
        get_positions=lambda account_ids=None: [],
        search_securities=lambda ticker, **kw: [
            {"id": "sec1", "stock": {"symbol": ticker}}
        ],
    ), state


def _shock_account(cfg, price, cancelled=None):
    client, state = _chain_client(price, cancelled)
    return types.SimpleNamespace(
        cfg=cfg, _client=lambda: client
    ), state




def _shock_ws(cfg, price, cancelled=None):
    """(account stub, cancel-state) - the client quotes `price`
    on the chain and records cancels."""
    client, state = _chain_client(price, cancelled)
    account = types.SimpleNamespace(cfg=cfg, _client=lambda: client)
    return account, state


def test_shock_cancel_keeps_filled_part():
    """a partially-filled buy whose market price ran >= the
    cancel pct away from the estimate: the remainder is
    cancelled and the filled part stays as the position -
    future alerts (an ALL OUT) trade against what is held."""
    from consumer.trading.mirror import (
        sweep_pending_orders, reconcile_pending_fill
    )
    import sqlite3
    import types as _types

    store = _store()
    row = _pending(store, "BUY", 5, 2.0, pre_qty=0, pre_avg=None)
    store.apply_position(
        "live", _est_shim("BUY"), 5, premium=2.0, account="RRSP"
    )
    # 2 of 5 filled @ 2.0, then the market hikes 20% (2.0 -> 2.4)
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 2, 2.0
    )
    assert store.get_position(
        "live", "AAOI-2026-10-02-105-C", "RRSP"
    ) == 2

    cfg = _types.SimpleNamespace(
        trading=_types.SimpleNamespace(
            partial_fill_cancel_pct=10.0
        ),
        wealthsimple=_types.SimpleNamespace(exchange_hint=""),
    )
    account, state = _shock_ws(cfg, 2.4, [])
    sweep_pending_orders(cfg, store, account, "RRSP", "")
    # the filled 2 stay as the position at their fill price
    pos = store.list_positions("live", "RRSP")[0]
    assert pos["qty"] == 2
    assert abs(pos["avg_premium"] - 2.0) < 0.01
    # the order settled as partial-cancelled, cancel reached ws
    assert store.open_pending_orders("live", "RRSP") == []
    conn = sqlite3.connect(store.path)
    statuses = [
        r[0] for r in conn.execute(
            "SELECT status FROM pending_orders"
        ).fetchall()
    ]
    conn.close()
    assert statuses == ["partial_cancelled"]
    # the cancel went out to the broker with the order id
    assert state["cancelled"] == ["ord-1"]


def test_no_shock_leaves_partial_order_open():
    """a partial fill whose market price stayed near the
    estimate is untouched by the sweep until the ttl."""
    from consumer.trading.mirror import (
        sweep_pending_orders, reconcile_pending_fill
    )
    import types as _types

    store = _store()
    row = _pending(store, "BUY", 5, 2.0, pre_qty=0, pre_avg=None)
    store.apply_position(
        "live", _est_shim("BUY"), 5, premium=2.0, account="RRSP"
    )
    assert reconcile_pending_fill(
        store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 2, 2.0
    )
    cfg = _types.SimpleNamespace(
        trading=_types.SimpleNamespace(
            partial_fill_cancel_pct=10.0
        ),
        wealthsimple=_types.SimpleNamespace(exchange_hint=""),
    )
    account, state = _shock_ws(cfg, 2.05, [])
    sweep_pending_orders(cfg, store, account, "RRSP", "")
    rows = store.open_pending_orders("live", "RRSP")
    assert len(rows) == 1
    assert rows[0]["filled_qty"] == 2


def test_slippage_notice_on_reconcile():
    """a fill landing beyond max_slippage_pct from the order's
    estimate posts a notice with both prices."""
    from consumer.trading.mirror import reconcile_pending_fill

    posted = []
    import core.ops.notify as notify

    orig = notify.notify_discord

    def spy(url, title, fields, ok=True, color=None):
        posted.append((title, dict(fields)))

    notify.notify_discord = spy
    try:
        store = _store()
        row = _pending(store, "BUY", 2, 2.0, pre_qty=0, pre_avg=None)
        store.apply_position(
            "live", _est_shim("BUY"), 2, premium=2.0,
            account="RRSP"
        )
        # filled 15% above the estimate (limit 2%)
        assert reconcile_pending_fill(
            store, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 2,
            2.3, max_slippage_pct=2.0, webhook_url="http://hook",
        )
        # a small slip stays silent
        store2 = _store()
        _pending(store2, "BUY", 2, 2.0, pre_qty=0, pre_avg=None)
        assert reconcile_pending_fill(
            store2, "RRSP", "AAOI-2026-10-02-105-C", "BUY", 2,
            2.01, max_slippage_pct=2.0, webhook_url="http://hook",
        )
    finally:
        notify.notify_discord = orig
    assert len(posted) == 1
    title, fields = posted[0]
    assert "SLIPPAGE" in title
    assert fields["estimated"] == "2"
    assert fields["filled"] == "2.3"
    assert "15" in fields["slippage"]
