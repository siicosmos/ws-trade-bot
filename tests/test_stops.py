import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.ws.account import PaperAccount
from trader.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig
from trader.trading.executor import PaperExecutor
from trader.trading.parser import parse_alert
from trader.trading.risk import RiskEngine
from trader.trading.stops import StopMonitor
from trader.store import Store


class ConfigStub:
    def __init__(self, trading, accounts=None, auth_token=""):
        self.trading = trading
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.discord = type("D", (), {"webhook_url": ""})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])
        self.reader = ReaderConfig()


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _buy(text="BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size"):
    return parse_alert(text)


def test_realized_pnl_on_scale_out():
    store = _fresh_store()
    alert = _buy()
    store.apply_position("paper", alert, 5, premium=1.5)
    store.apply_position("paper", alert, -1, premium=1.95)
    rows = store.list_positions("paper")
    assert rows[0]["realized"] == 1 * (1.95 - 1.5) * 100

    store.apply_position("paper", alert, -4, premium=1.0)
    rows = store.list_positions("paper")
    assert rows == []
    total = 45 - 4 * 50
    assert store.loss_streak("paper") == (1 if total < 0 else 0)


def test_loss_streak_resets_on_winning_close():
    store = _fresh_store()
    a1 = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    a2 = parse_alert("BOUGHT 0DTE SPY 760c @ 1.5")

    store.apply_position("paper", a1, 1, premium=1.5)
    store.apply_position("paper", a1, -1, premium=1.0)
    assert store.loss_streak("paper") == 1

    store.apply_position("paper", a2, 1, premium=1.5)
    store.apply_position("paper", a2, -1, premium=2.0)
    assert store.loss_streak("paper") == 0


def test_loss_streak_breaker_blocks_buys():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", max_consecutive_losses=2,
                                   cooldown_seconds=0, dedupe_window_minutes=0))
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)

    a1 = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    a2 = parse_alert("BOUGHT 0DTE SPY 760c @ 1.5")
    for a in (a1, a2):
        store.apply_position("paper", a, 1, premium=1.5)
        store.apply_position("paper", a, -1, premium=1.0)
    assert store.loss_streak("paper") == 2

    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 761c @ 1.5"))
    assert not ok
    assert "loss-streak breaker" in reason

    ok, _ = risk.evaluate(parse_alert("SOLD 1/4 0DTE SPY 759c @ 2.0"))
    assert ok


def test_min_dte_gate():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", min_dte_days=7,
                                   cooldown_seconds=0, dedupe_window_minutes=0))
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)

    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"))
    assert not ok
    assert "min_dte_days" in reason

    far = (date.today() + timedelta(days=30)).strftime("%m/%d")
    ok, _ = risk.evaluate(
        parse_alert(f"BOUGHT {far} SPY 759c @ 1.5 @everyone small size")
    )
    assert ok

    cfg_none = ConfigStub(TradingConfig(mode="paper", cooldown_seconds=0,
                                        dedupe_window_minutes=0))
    risk_none = RiskEngine(cfg_none, store, PaperAccount(cfg_none, store))
    ok, _ = risk_none.evaluate(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"))
    assert ok


def test_stop_price_fixed_and_trailing():
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25))
    store = _fresh_store()
    monitor = StopMonitor(cfg, store, None, lambda pos: None)
    assert abs(monitor.stop_price(2.0, 2.0) - 1.5) < 1e-9

    cfg_t = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                     trailing_stop_pct=20))
    monitor_t = StopMonitor(cfg_t, store, None, lambda pos: None)
    assert abs(monitor_t.stop_price(2.0, 3.0) - 2.4) < 1e-9
    assert abs(monitor_t.stop_price(2.0, 2.1) - 1.68) < 1e-9
    assert abs(monitor_t.stop_price(2.0, 2.0) - 1.5) < 1e-9


def test_stop_monitor_fires_on_drop():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                   risk_per_trade_pct=5, cooldown_seconds=0))
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0 @everyone medium size")
    res = executor.execute(buy, cfg, store)
    assert res.ok
    assert res.qty == 2

    quotes = {}
    monitor = StopMonitor(cfg, store, executor,
                          lambda pos: quotes.get(pos["contract_key"]))

    quotes[buy.contract_key()] = 2.05
    monitor.check_once()
    assert store.get_position("paper", buy.contract_key()) == 2

    quotes[buy.contract_key()] = 1.4
    monitor.check_once()
    assert store.get_position("paper", buy.contract_key()) == 0

    trades = store.recent_trades(10)
    stop_rows = [t for t in trades if t["detail"].startswith("[STOP]")]
    assert len(stop_rows) == 1
    assert stop_rows[0]["status"] == "executed"
    assert "SELL 2x" in stop_rows[0]["detail"]

    positions = store.list_positions("paper")
    assert positions == []


def test_stop_monitor_records_peak_for_trailing():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                   trailing_stop_pct=30,
                                   risk_per_trade_pct=5, cooldown_seconds=0))
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 @everyone medium size")
    executor.execute(buy, cfg, store)
    key = buy.contract_key()

    quotes = {key: 2.0}
    monitor = StopMonitor(cfg, store, executor, lambda pos: quotes[key])
    monitor.check_once()
    assert store.get_position("paper", key) == 5
    rows = {p["contract_key"]: p for p in store.list_positions("paper")}
    assert rows[key]["peak_bid"] == 2.0

    quotes[key] = 1.3
    monitor.check_once()
    assert store.get_position("paper", key) == 0


def test_moomoo_provider_reconnects_after_opend_drop():
    """OpenD can restart while the pipeline runs - the cached
    context must be dropped so the next poll reconnects."""
    import sys
    import types

    # swap in a stub module even when the real moomoo-api
    # package is installed - and restore it afterwards
    real = sys.modules.get("moomoo")
    stub = types.ModuleType("moomoo")

    from trader.trading.quotes import MoomooQuoteProvider

    made = []

    class _Row:
        @staticmethod
        def get(key):
            return {"bid_price": 1.5}.get(key)

    class _Data:
        empty = False

        class _Iloc:
            @staticmethod
            def __getitem__(i):
                return _Row()

        iloc = _Iloc()

    calls = {"n": 0}

    class _Ctx:
        def __init__(self, host, port):
            made.append(self)

        def get_market_snapshot(self, codes):
            calls["n"] += 1
            if calls["n"] == 1:   # the very first call ever dies
                raise OSError("connection reset by OpenD")
            return 0, _Data()

        def close(self):
            pass

    stub.OpenQuoteContext = _Ctx
    sys.modules["moomoo"] = stub
    try:
        cfg = types.SimpleNamespace(
            quotes=types.SimpleNamespace(
                moomoo_host="127.0.0.1", moomoo_port=11111
            )
        )
        pos = {
            "underlying": "SPY", "expiry": "2026-09-18",
            "strike": 759.0, "right": "C",
        }
        p = MoomooQuoteProvider(cfg)
        assert p.quote(pos) is None      # first call: connection dies
        assert p._ctx is None           # context dropped for reconnect
        assert p.quote(pos) == 1.5       # second call: reconnected
        assert len(made) == 2
    finally:
        if real is not None:
            sys.modules["moomoo"] = real
        else:
            sys.modules.pop("moomoo", None)




def test_realized_today_accumulates_on_sells():
    """sells book their realized pnl into a per-day counter -
    lotto / profits-only alerts spend against it."""
    store = _fresh_store()
    assert store.realized_today("paper") == 0.0
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.apply_position("paper", buy, 5, premium=1.5, account="default")
    # sell 2 at 2.5: +2x(2.5-1.5)x100 = +200
    store.apply_position(
        "paper", parse_alert("SOLD 0DTE SPY 759c @ 2.5"), -2,
        premium=2.5, account="default",
    )
    assert abs(store.realized_today("paper") - 200.0) < 0.01
    # a losing sell subtracts: -1x(1.0-1.5)x100 = -50
    store.apply_position(
        "paper", parse_alert("SOLD 0DTE SPY 759c @ 1.0"), -1,
        premium=1.0, account="default",
    )
    assert abs(store.realized_today("paper") - 150.0) < 0.01




def test_lotto_gain_cap_and_parser_qualifiers():
    """hero-or-zero parses as lotto, profits-only parses as a
    profits_only alert, and both spend at most the configured
    fraction of today's realized gains (zero gains -> zero
    budget)."""
    from trader.trading.executor import lotto_gain_cap

    cfg = ConfigStub(TradingConfig(mode="paper"))
    store = _fresh_store()
    store = _fresh_store()
    lotto = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 hero or zero")
    assert lotto.size == "lotto"
    po = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 PROFITS ONLY")
    assert po.size != "lotto"
    assert getattr(po, "profits_only") is True

    # no realized gain -> zero lotto budget
    assert lotto_gain_cap(store, "paper", cfg, lotto) == 0.0
    # a realized gain lands: cap = gain x 75%
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.apply_position("paper", buy, 5, premium=1.5, account="default")
    store.apply_position("paper", parse_alert("SOLD 0DTE SPY 759c @ 2.5"),
                         -2, premium=2.5, account="default")
    assert abs(store.realized_today("paper") - 200.0) < 0.01
    assert abs(lotto_gain_cap(store, "paper", cfg, lotto) - 150.0) < 0.01
    # non-lotto alerts are not capped at all
    assert lotto_gain_cap(store, "paper", cfg, buy) is None


def test_lotto_paper_buy_needs_realized_gain():
    """the paper executor skips lotto alerts with no realized
    gain today and buys against the gain when there is one."""
    cfg = ConfigStub(TradingConfig(mode="paper"))
    t = cfg.trading
    store = _fresh_store()
    account = PaperAccount(t, store)
    store.set_paper_equity(100000.0, "default")
    executor = PaperExecutor(t, store, account)
    lotto = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0 hero or zero")

    r = executor.execute(lotto, cfg, store)
    assert not r.ok
    assert "lotto budget" in r.detail

    # book a realized gain by selling into strength
    store.apply_position("paper", parse_alert("BOUGHT 0DTE SPY 759c @ 1.0"),
                         5, premium=1.5, account="default")
    store.apply_position("paper", parse_alert("SOLD 0DTE SPY 759c @ 2.5"),
                         -2, premium=2.5, account="default")
    assert abs(store.realized_today("paper") - 200.0) < 0.01
    r2 = executor.execute(lotto, cfg, store)
    assert r2.ok, r2.detail


def test_per_size_stop_loss():
    """the stop comes from the position's size tier when it has
    its own stop_loss_pct - a lotto tolerates -50%, medium -25%."""
    monitor = StopMonitor(
        ConfigStub(TradingConfig(mode="paper")), _fresh_store(), None,
        lambda pos: None,
    )
    lotto_pos = {"avg_premium": 2.0, "size": "lotto", "right": "C"}
    # lotto: 2.0 x (1 - 50%)
    assert abs(monitor.stop_price(2.0, 2.0, lotto_pos) - 1.0) < 1e-9
    # medium rides the 25% tier stop
    assert abs(monitor.stop_price(2.0, 2.0, {"size": "medium"}) - 1.5) < 1e-9
    # unsized positions fall back to the global stop
    assert abs(monitor.stop_price(2.0, 2.0, None) - 1.5) < 1e-9


def test_back_to_entry_sells_0dte_gain_gone():
    """a 0dte option that had a gain and gave it back to its
    entry is sold before it expires worthless - other days and
    positions without a prior gain are left alone."""
    cfg = ConfigStub(TradingConfig(mode="paper"))
    monitor = StopMonitor(cfg, _fresh_store(), None, lambda pos: None)
    today = date.today().isoformat()
    pos = {
        "contract_key": "SPY-2026-10-02-759-C", "right": "C",
        "avg_premium": 1.5, "expiry": today, "size": "lotto",
    }
    # had a gain (peak 3.0), price back at entry -> hit
    assert monitor._back_to_entry_hit(pos, 1.0, 3.0, 1.0) is True
    # still above entry -> hold
    assert monitor._back_to_entry_hit(pos, 1.0, 3.0, 1.2) is False
    # never had a gain -> nothing to protect
    assert monitor._back_to_entry_hit(pos, 1.0, 1.0, 0.9) is False
    # not expiring today -> leave it
    old = dict(pos, expiry=(date.today() + timedelta(days=7)).isoformat())
    assert monitor._back_to_entry_hit(old, 1.0, 3.0, 1.0) is False
    # the global kill switch
    cfg.trading.back_to_entry_enabled = False
    assert monitor._back_to_entry_hit(pos, 1.0, 3.0, 1.0) is False


def test_per_size_stop_fires_for_lotto_width():
    """a lotto position stops at -50% while the global 25% would
    have fired earlier - check_once uses the position's tier."""
    cfg = ConfigStub(TradingConfig(mode="paper"))
    store = _fresh_store()
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0 hero or zero")
    store.apply_position("paper", alert, 1, premium=2.0,
                         account="default")
    class _FakeTrader:
        mode = "paper"

        def execute(self, alert, cfg, store):
            from trader.trading.executor import ExecutionResult

            return ExecutionResult(
                True, f"SELL 1x @ {alert.premium}", qty=1
            )

    monitor = StopMonitor(cfg, store, _FakeTrader(), lambda pos: 1.1)
    monitor.check_once()
    assert not [t for t in store.recent_trades(10)
                if "[STOP]" in (t["detail"] or "")]
    monitor = StopMonitor(cfg, store, _FakeTrader(), lambda pos: 1.0)
    monitor.check_once()
    # bid 1.0 = -50% of entry: the lotto stop fires here
    trades = [t for t in store.recent_trades(20)
              if "[STOP]" in (t["detail"] or "")]
    assert trades, "lotto stop should have fired"


def test_realized_today_per_account():
    """the daily realized counter is keyed per account (the
    account cards show each label's own today) on top of the
    mode aggregate the lotto budget reads."""
    store = _fresh_store()
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.apply_position("paper", buy, 5, premium=1.5, account="RRSP")
    store.apply_position("paper", parse_alert("SOLD 0DTE SPY 759c @ 2.5"),
                         -2, premium=2.5, account="RRSP")
    store.apply_position("paper", parse_alert("BOUGHT 0DTE SPY 769c @ 1.0"),
                         2, premium=1.0, account="default")
    store.apply_position("paper", parse_alert("SOLD 0DTE SPY 759c @ 2.0"),
                         -1, premium=1.0, account="default")
    # RRSP booked +2x(2.5-1.5)x100 = +200
    assert abs(store.realized_today("paper", "RRSP") - 200.0) < 0.01
    # the mode aggregate covers every account (lotto budget)
    assert abs(store.realized_today("paper") - 200.0) < 0.01


def test_per_position_tp_fires_at_gain_target():
    """a position with its own tp target sells the whole rest of
    the position when the bid reaches entry x (1 + tp%) - even
    with the global stops off (the ALL OUT alert is optional)."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=0,
                                   risk_per_trade_pct=5,
                                   cooldown_seconds=0))
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0")
    executor.execute(buy, cfg, store)
    key = buy.contract_key()

    quotes = {key: 2.0}
    monitor = StopMonitor(cfg, store, executor,
                          lambda pos: quotes.get(pos["contract_key"]))
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    # the user sets a 25% take-profit beside the position
    store.set_position_tp("paper", "default", key, 25)
    rows = store.list_positions("paper")
    assert rows[0]["tp_gain_pct"] == 25

    # 2.0 bid is below the target - nothing fires
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    # 2.6 = +30% >= 25% - the rest is sold
    quotes[key] = 2.6
    monitor.check_once()
    assert store.get_position("paper", key) == 0
    sells = [t for t in store.recent_trades(5)
             if t["action"] == "SELL" and "[TP]" in (t["detail"] or "")]
    assert len(sells) == 1

    # clearing the target works
    store.set_position_tp("paper", "default", key, None)
    assert store.list_positions("paper") == []


def test_tp_runs_when_global_stop_off():
    """with stop_loss_pct = 0 the stop check is inert (a 0% stop
    would fire at entry) but the per-position tp still fires."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=0,
                                   risk_per_trade_pct=5,
                                   cooldown_seconds=0))
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0")
    executor.execute(buy, cfg, store)
    key = buy.contract_key()

    monitor = StopMonitor(cfg, store, executor,
                          lambda pos: 1.0)
    # -50%: no stop fired (stops off), no tp (gain negative)
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    store.set_position_tp("paper", "default", key, 20)
    monitor.check_once()
    # 1.0 is a loss, not a tp hit - the position stays
    assert store.get_position("paper", key) == 2

    monitor.quote_fn = lambda pos: 2.6   # +30% >= 25% target
    monitor.check_once()
    assert store.get_position("paper", key) == 0
