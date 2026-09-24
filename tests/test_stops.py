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
