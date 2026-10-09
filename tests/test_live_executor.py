"""Live-executor safety tests: every WealthsimpleExecutor path
driven against a fake ws client, so the code that touches real
money is exercised exactly like the paper paths have been.

The fake client mimics the surface the executor touches
(security/option-chain resolution, quotes, order placement,
position queries) and records every order for assertions.
"""
import os
import tempfile
import types
from datetime import date

from core.config import TradingConfig, WealthsimpleConfig
from core.store import Store
from consumer.trading.executor import WealthsimpleExecutor
from core.parser import parse_alert
from consumer.ws.account import WSAccountConfig

# 0DTE alerts parse to today's date - the fake chain serves it
TODAY = date.today().isoformat()


def _chain_entry(opt_id, strike, ask, bid):
    return {
        "id": opt_id,
        "optionDetails": {
            "strikePrice": strike,
            "optionType": "CALL",
        },
        "quote": {"ask": ask, "bid": bid},
    }


class FakeWS:
    """The executor's client surface, recording every order."""

    def __init__(self, ask=1.5, bid=1.2, fail_orders=False):
        self.ask = ask
        self.bid = bid
        self.fail_orders = fail_orders
        self.orders = []

    def get_ticker_id(self, ticker, hint=None):
        return f"sec-{ticker.lower()}"

    def search_securities(self, ticker, security_group_ids=None):
        return [{"stock": {"symbol": ticker}, "id": f"sec-{ticker.lower()}"}]

    def get_option_expiry_dates(self, sec_id):
        return [TODAY]

    def get_option_chain(self, sec_id, expiry, opt_type):
        return [_chain_entry("opt-1", 759, self.ask, self.bid)]

    def _order(self, name, qty, price):
        if self.fail_orders:
            raise RuntimeError("ws order rejected")
        self.orders.append((name, qty, price))
        return {"orderId": f"order-{len(self.orders)}"}

    def buy_option(self, account_id, opt_id, qty, limit):
        return self._order("buy_option", qty, limit)

    def sell_option(self, account_id, opt_id, qty, limit):
        return self._order("sell_option", qty, limit)

    def get_security_quote(self, sec_id):
        return {"ask": self.ask, "bid": self.bid, "price": self.ask}

    def market_buy(self, account_id, sec_id, qty):
        return self._order("market_buy", qty, self.ask)

    def limit_buy(self, account_id, sec_id, qty, price):
        return self._order("limit_buy", qty, price)

    def market_sell(self, account_id, sec_id, qty):
        return self._order("market_sell", qty, self.bid)

    def limit_sell(self, account_id, sec_id, qty, price):
        return self._order("limit_sell", qty, price)

    def get_positions(self, account_ids=None):
        return list(self._holdings)

    _holdings: list = []


class StubAccount:
    """Just .value/.values - what the executor and risk read."""

    def __init__(self, values):
        self._values = values

    def value(self, label="default"):
        return self._values.get(label)

    def values(self):
        return dict(self._values)


def _live_cfg(**over):
    trading = TradingConfig(mode="live", **over)
    trading.size_tiers["medium"] = {
        "risk_pct_max": 5.0, "contracts_min": 1,
        "contracts_max": 4, "stop_loss_pct": 25.0,
    }
    cfg = types.SimpleNamespace(
        trading=trading,
        wealthsimple=WealthsimpleConfig(accounts=[
            WSAccountConfig(account_id="acc-1", label="RRSP",
                            enabled=True),
        ]),
        paper=None,
    )
    return cfg


def _executor(cfg, store, account):
    ex = WealthsimpleExecutor(cfg, account)
    ex._ws = None   # never let it build the real client
    return ex


def _patch_client(monkeypatch, ws):
    # _client() builds the real WealthsimpleV2 client - pin the
    # fake in its place
    def fake_client(self):
        return ws

    monkeypatch.setattr(
        WealthsimpleExecutor, "_client", fake_client, raising=False,
    )


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _account_values():
    return {"RRSP": 10000.0}


def test_live_option_buy_sizes_books_and_pends(monkeypatch):
    """a live option buy sizes off the tier, places the order at
    the quote ask, books the position and records a pending order
    with the pre-order snapshot."""
    ws = FakeWS(ask=1.5, bid=1.2)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    res = ex.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 medium size"),
        cfg, store,
    )
    assert res.ok
    # the live limit is clamped to the alert's quoted premium
    # (+ max_slippage_pct): the 1.5 ask is 25% above the quoted
    # 1.2, so the order bids 1.22 instead of chasing the ask.
    # budget 5% x 10000 = 500 -> 4 contracts @ 1.22 (tier max 4)
    assert ws.orders == [("buy_option", 4, 1.22)]
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 4
    pending = store.open_pending_orders("live", "RRSP")
    assert len(pending) == 1
    assert pending[0]["qty"] == 4
    assert pending[0]["est_price"] == 1.22
    assert pending[0]["pre_qty"] == 0
    assert pending[0]["status"] == "open"


def test_live_option_sell_flattens_and_realizes(monkeypatch):
    """a live sell sells the held quantity at the bid, books the
    realized pnl and records the pending order."""
    ws = FakeWS(ask=1.5, bid=2.0)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPY 759c @ 1.0"), 4,
        premium=1.0, account="RRSP",
    )
    res = ex.execute(
        parse_alert("SOLD 0DTE SPY 759c @ 2.0"), cfg, store,
    )
    assert res.ok
    assert ws.orders == [("sell_option", 4, 2.0)]
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 0
    # 4x(2.0-1.0)x100 = +400 realized
    assert abs(store.realized_today("live", "RRSP") - 400.0) < 0.01
    pending = store.open_pending_orders("live", "RRSP")
    assert pending[0]["action"] == "SELL"
    assert pending[0]["pre_qty"] == 4


def test_live_buy_skips_at_open_risk_cap(monkeypatch):
    """a live buy is skipped - no order placed, nothing booked -
    once the open-risk cap is reached."""
    ws = FakeWS(ask=1.5, bid=1.2)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg(max_open_risk_pct=10.0)
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    # deploy 1500 of risk (10% cap of 10000): 15 contracts @ 1.0
    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0"), 15,
        premium=1.0, account="RRSP",
    )
    res = ex.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 medium size"),
        cfg, store,
    )
    assert res.ok is False or res.qty == 0
    assert ws.orders == []
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 0


def test_live_lotto_buy_needs_realized_gains(monkeypatch):
    """a live lotto buy with zero realized gains today is
    skipped - the lotto budget gate holds in live mode."""
    ws = FakeWS(ask=1.5, bid=1.2)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    res = ex.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 lotto size"),
        cfg, store,
    )
    assert ws.orders == []
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 0


def test_live_stock_buy_respects_tier_and_open_risk(monkeypatch):
    """a live stock buy sizes from the stock tier percent and is
    skipped at the open-risk cap (the guard the live path was
    missing)."""
    ws = FakeWS(ask=100.0, bid=99.0)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    cfg.trading.stock_size_tiers = {"medium": 5.0}
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    res = ex.execute(
        parse_alert("BOUGHT LLYX shares @ 100.0 medium size"),
        cfg, store,
    )
    assert res.ok
    # 5% x 10000 = 500 -> 5 shares @ 100
    assert ws.orders == [("limit_buy", 5, 100.5)] or ws.orders == [
        ("market_buy", 5, 100.0)
    ]

    # now trip the cap: the ledger's open risk (what
    # _at_open_risk_cap reads) is filled with option positions -
    # 10 contracts @ 1.0 = 1000 of risk vs the 500 (5%) cap
    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0"), 10,
        premium=1.0, account="RRSP",
    )
    cfg2 = _live_cfg(max_open_risk_pct=5.0)
    cfg2.trading.stock_size_tiers = {"medium": 5.0}
    ex2 = _executor(cfg2, store, account)
    res2 = ex2.execute(
        parse_alert("BOUGHT LLYX shares @ 100.0 medium size"),
        cfg2, store,
    )
    assert res2.qty == 0
    assert "open risk cap" in res2.detail
    # and no second order was placed
    assert len(ws.orders) == 1


def test_live_stock_sell_only_if_held(monkeypatch):
    """a live stock sell refuses when ws shows no holdings and
    sell_only_if_held is on."""
    ws = FakeWS(ask=100.0, bid=99.0)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg(sell_only_if_held=True)
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    res = ex.execute(
        parse_alert("SOLD LLYX shares @ 99.0"), cfg, store,
    )
    assert res.qty == 0
    assert ws.orders == []
    assert "no position to sell" in res.detail


def test_live_order_failure_leaves_ledger_clean(monkeypatch):
    """a rejected ws order records the error, places nothing and
    books nothing - the ledger never carries a phantom fill."""
    ws = FakeWS(ask=1.5, bid=1.2, fail_orders=True)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    try:
        ex.execute(
            parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 medium size"),
            cfg, store,
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 0
    assert store.open_pending_orders("live", "RRSP") == []


def test_live_b2e_and_stop_sells_execute_live(monkeypatch):
    """the stop monitor's exits ride the same live executor: a
    stop-fire sells the held contracts at the bid and books the
    realized loss."""
    ws = FakeWS(ask=0.6, bid=0.5)
    _patch_client(monkeypatch, ws)
    cfg = _live_cfg()
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 lotto size"),
        2, premium=1.0, account="RRSP",
    )
    from consumer.trading.stops import StopMonitor

    monitor = StopMonitor(cfg, store, ex, lambda pos: 0.5, "")
    pos = store.list_positions("live", "RRSP")[0]
    monitor.check_once()
    # stop: 1.0 x (1 - 50% lotto) = 0.5 -> bid 0.5 hits it
    assert ws.orders and ws.orders[0][0] == "sell_option"
    assert store.get_position(
        "live", f"SPY-{TODAY}-759-C", "RRSP"
    ) == 0
    trades = [t for t in _trade_rows(store) if t[0] == "live"]
    assert any("STOP" in (t[2] or "") for t in trades)


def _trade_rows(store):
    rows = []
    conn = store._conn
    for r in conn.execute(
        "SELECT mode, action, detail FROM trades ORDER BY id"
    ):
        rows.append((r[0], r[1], r[2]))
    return rows


def test_kill_switch_blocks_buys_allows_sells():
    """the runtime kill switch: paused blocks every new BUY,
    exits stay takeable."""
    from consumer.trading.risk import RiskEngine

    cfg = _live_cfg(trading_paused=True)
    store = _store()
    account = StubAccount(_account_values())
    risk = RiskEngine(cfg, store, account)
    ok, reason = risk.evaluate(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.2")
    )
    assert not ok
    assert "kill switch" in reason
    ok, _ = risk.evaluate(parse_alert("SOLD 0DTE SPY 759c @ 2.0"))
    assert ok

def test_live_option_buy_cluster_cap(monkeypatch):
    """correlation-aware risk: same-direction SPX calls are one
    bet - a buy into the capped cluster is skipped while the
    global cap still has room, and a different cluster trades."""
    ws = FakeWS(ask=1.0, bid=0.9)
    _patch_client(monkeypatch, ws)
    # cluster cap 5% of 10000 = 500; the global cap stays wide
    # (30% = 3000) so only the cluster check can block
    cfg = _live_cfg(
        max_open_risk_pct=30.0, cluster_cap_pct=5.0,
        cooldown_seconds=0, dedupe_window_minutes=0,
    )
    store = _store()
    account = StubAccount(_account_values())
    ex = _executor(cfg, store, account)

    # first SPX call: 4 contracts @ 1.0 ask = 400 cluster risk
    res1 = ex.execute(
        parse_alert("BOUGHT 0DTE SPX 759c @ 1.0 medium size"),
        cfg, store,
    )
    assert res1.ok and res1.qty == 4

    # a second SPX buy fills the cluster to 800 (>= the 500 cap)
    res2 = ex.execute(
        parse_alert("BOUGHT 0DTE SPX 759c @ 1.0 medium size"),
        cfg, store,
    )
    assert res2.ok and res2.qty == 4
    assert len(ws.orders) == 2

    # a third SPX buy lands in an already-capped cluster even
    # though the global cap (3000) has plenty of room
    res3 = ex.execute(
        parse_alert("BOUGHT 0DTE SPX 759c @ 1.0 medium size"),
        cfg, store,
    )
    assert res3.qty == 0
    assert "cluster cap" in res3.detail
    assert len(ws.orders) == 2

    # a SPY call is a different cluster - it trades
    res4 = ex.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 medium size"),
        cfg, store,
    )
    assert res4.ok and res4.qty == 4
    assert len(ws.orders) == 3



def test_open_risk_cap_per_account(monkeypatch):
    """a small account can carry its own higher open-risk cap
    while the other accounts keep the global one - the $ risk
    stays small either way."""
    ws = FakeWS(ask=1.0, bid=0.9)
    _patch_client(monkeypatch, ws)

    def _cfg():
        return types.SimpleNamespace(
            trading=TradingConfig(
                mode="live", max_open_risk_pct=30.0,
                cooldown_seconds=0, dedupe_window_minutes=0,
            ),
            wealthsimple=WealthsimpleConfig(accounts=[
                WSAccountConfig(account_id="acc-1", label="Margin",
                                max_open_risk_pct=80.0, enabled=True),
                WSAccountConfig(account_id="acc-2", label="RRSP",
                                enabled=True),
            ]),
            paper=None,
        )

    cfg = _cfg()
    cfg.trading.size_tiers["medium"] = {
        "risk_pct_max": 5.0, "contracts_min": 1,
        "contracts_max": 10, "stop_loss_pct": 25.0,
    }
    store = _store()
    account = StubAccount({"Margin": 2000.0, "RRSP": 20000.0})
    ex = _executor(cfg, store, account)

    # ledger state: margin carries 700 risk (cap 1600), rrsp sits
    # exactly at its global 30% cap (6000 of 20000)
    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPY 759c @ 1.0"), 7,
        premium=1.0, account="Margin",
    )
    store.apply_position(
        "live", parse_alert("BOUGHT 0DTE SPY 759c @ 1.0"), 60,
        premium=1.0, account="RRSP",
    )

    res = ex.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 medium size"),
        cfg, store,
    )
    # margin: its own 80% cap (1600 of 2000) still has room for
    # the 5% budget's 1 contract; rrsp is at its global cap
    assert res.ok
    assert res.breakdown["Margin"].startswith("1x @ 1.0")
    assert "open risk cap reached" in res.breakdown["RRSP"]
    assert len(ws.orders) == 1
    assert ws.orders[0][0] == "buy_option"

    # without the override the margin account would be capped by
    # the global 30% too (700 >= 600): nothing trades
    cfg2 = _cfg()
    cfg2.trading.size_tiers["medium"] = {
        "risk_pct_max": 5.0, "contracts_min": 1,
        "contracts_max": 10, "stop_loss_pct": 25.0,
    }
    for acct in cfg2.wealthsimple.accounts:
        acct.max_open_risk_pct = None
    ex2 = _executor(cfg2, store, account)
    res2 = ex2.execute(
        parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 medium size"),
        cfg2, store,
    )
    assert res2.qty == 0
    assert "open risk cap reached" in res2.breakdown["Margin"]
    assert "open risk cap reached" in res2.breakdown["RRSP"]
    assert len(ws.orders) == 1


def test_execute_serializes_concurrent_calls(monkeypatch):
    """the stop monitor, the feed client, flask threads and the
    mirror all call execute() - interleaved get_position -> order
    -> apply_position would double-sell the same live position,
    so the whole path must hold one lock."""
    import threading
    import time as _time

    cfg = _live_cfg()
    store = _store()
    ex = _executor(cfg, store, None)

    ws = FakeWS()
    _patch_client(monkeypatch, ws)

    active = []
    overlap = []

    def slow_option(alert, cfg2, store2):
        active.append(1)
        _time.sleep(0.05)
        if len(active) > 1:
            overlap.append(len(active))
        active.pop()
        return ExecutionResult(True, "ok")

    monkeypatch.setattr(ex, "_execute_option", slow_option)

    alert = parse_alert("BOUGHT 0DTE SPX 6000c @ 1.50 tiny size")
    threads = [
        threading.Thread(target=ex.execute, args=(alert, cfg, store))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlap, f"execute() ran concurrently: {overlap}"


def test_idless_accounts_inherit_fallback_once():
    """the id-less fallback resolves to ONE real account - handing
    it to every id-less row would place the same order twice on
    the same account."""
    cfg = _live_cfg()
    cfg.wealthsimple.accounts = [
        WSAccountConfig(account_id="", label="A", enabled=True),
        WSAccountConfig(account_id="", label="B", enabled=True),
        WSAccountConfig(account_id="acc-real", label="C",
                        enabled=True),
    ]
    ex = WealthsimpleExecutor(cfg, None)

    class StubWS:
        def get_accounts(self):
            return [{"id": "acc-first", "status": "ACTIVE"}]

    pairs = ex._account_ids(StubWS())
    # config order wins: row A (id-less) inherits the fallback
    # first; row C's explicit id resolves to the SAME account and
    # is skipped as a duplicate - one alert, one order, one account
    assert [(p[0], p[1]) for p in pairs] == [("A", "acc-real")]


def test_idless_single_row_inherits_fallback():
    # the legit single-account case: one row, id left blank - it
    # inherits the resolved account and trades as before
    cfg = _live_cfg()
    cfg.wealthsimple.accounts = [
        WSAccountConfig(account_id="", label="Only", enabled=True),
    ]
    ex = WealthsimpleExecutor(cfg, None)

    class StubWS:
        def get_accounts(self):
            return [{"id": "acc-first", "status": "ACTIVE"}]

    pairs = ex._account_ids(StubWS())
    assert [(p[0], p[1]) for p in pairs] == [("Only", "acc-first")]


def test_option_limit_clamped_to_alert_premium():
    """a stale/garbage chain quote must not be chased at any
    price: the live limit stays within max_slippage_pct of the
    alert's quoted premium."""
    import types as _types

    cfg = _live_cfg()
    cfg.trading.max_slippage_pct = 2
    ex = WealthsimpleExecutor(cfg, None)

    # buy: an ask way above the quoted premium is capped
    assert ex._clamped_limit(3.00, 1.50, "BUY", cfg) == 1.53
    # a better ask is taken as-is
    assert ex._clamped_limit(1.40, 1.50, "BUY", cfg) == 1.4
    # sell: a bid way below the quoted premium is floored
    assert ex._clamped_limit(0.50, 1.50, "SELL", cfg) == 1.47
    # a better bid is taken as-is
    assert ex._clamped_limit(1.60, 1.50, "SELL", cfg) == 1.6
    # no quoted premium -> no clamp possible, raw quote passes
    assert ex._clamped_limit(3.00, None, "BUY", cfg) == 3.0
    assert ex._clamped_limit(None, 1.50, "BUY", cfg) is None
