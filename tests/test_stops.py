import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from consumer.ws.account import PaperAccount
from core.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig
from consumer.trading.executor import PaperExecutor
from core.parser import parse_alert
from consumer.trading.risk import RiskEngine
from consumer.trading.stops import StopMonitor
from core.store import Store, et_now
import pytest  # noqa: E402


pytestmark = pytest.mark.essential

class ConfigStub:
    def __init__(self, trading, accounts=None, auth_token=""):
        self.trading = trading
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.discord = type("D", (), {"trade_alert_webhook_url": ""})()
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

    far = (et_now().date() + timedelta(days=30)).strftime("%m/%d")
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

    # the flat trail: adaptive off (the stepped ratchet would
    # tighten the trail as the gain grows)
    cfg_t = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                     trailing_stop_pct=20,
                                     adaptive_trail=False))
    monitor_t = StopMonitor(cfg_t, store, None, lambda pos: None)
    assert abs(monitor_t.stop_price(2.0, 3.0) - 2.4) < 1e-9
    assert abs(monitor_t.stop_price(2.0, 2.1) - 1.68) < 1e-9
    assert abs(monitor_t.stop_price(2.0, 2.0) - 1.5) < 1e-9


def test_adaptive_trail_steps():
    """the stepped ratchet: the trail tightens as the gain at the
    peak grows (ride the run-up wide, lock in more near the
    top); a ui-pinned per-position trail still wins."""
    cfg = ConfigStub(TradingConfig(
        mode="paper", stop_loss_pct=25, trailing_stop_pct=10,
        adaptive_trail=True,
        adaptive_trail_steps={"10": 10, "25": 7, "50": 5, "999": 3},
        adaptive_trail_min_pct=3, adaptive_trail_expiry_tighten=0,
    ))
    store = _fresh_store()
    monitor = StopMonitor(cfg, store, None, lambda pos: None)

    # +50% gain at the peak -> the 5% step
    assert abs(monitor.stop_price(2.0, 3.0, pos={}) - 2.85) < 1e-9
    # +12.5% gain -> the 7% step
    assert abs(monitor.stop_price(2.0, 2.25, pos={}) - 2.0925) < 1e-9
    # +5% gain -> the 10% step (the widest)
    assert abs(monitor.stop_price(2.0, 2.10, pos={}) - 1.89) < 1e-9
    # +100% gain -> the 999 ceiling -> 3%
    assert abs(monitor.stop_price(2.0, 4.0, pos={}) - 3.88) < 1e-9
    # a ui-pinned trail wins over the steps
    assert abs(monitor.stop_price(
        2.0, 3.0, pos={"trail_pct": 20}) - 2.4) < 1e-9
    # a pinned 0 = trailing off for that position
    assert abs(monitor.stop_price(
        2.0, 3.0, pos={"trail_pct": 0}) - 1.5) < 1e-9
    # adaptive off = the flat global trail
    cfg_off = ConfigStub(TradingConfig(
        mode="paper", stop_loss_pct=25, trailing_stop_pct=20,
        adaptive_trail=False,
    ))
    monitor_off = StopMonitor(cfg_off, store, None, lambda pos: None)
    assert abs(monitor_off.stop_price(2.0, 3.0, pos={}) - 2.4) < 1e-9


@pytest.mark.minimum
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

    from consumer.trading.quotes import MoomooQuoteProvider

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
    from consumer.trading.executor import lotto_gain_cap

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
    # the pipeline records the blocked attempt (releasing the
    # execution claim) before the next one runs
    store.record_trade(
        "paper", lotto.action, lotto.ticker, 0, None,
        lotto, "skipped", r.detail,
    )
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
    today = et_now().date().isoformat()
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
    old = dict(pos, expiry=(et_now().date() + timedelta(days=7)).isoformat())
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
            from consumer.trading.executor import ExecutionResult

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


def test_per_position_trailing_stop():
    """a per-position trailing % overrides the global trailing
    (which may be off) and ratchets on the position's own peak."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=0,
                                   trailing_stop_pct=0,
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

    # the user sets a 20% trailing stop beside the position
    store.set_position_trail("paper", "default", key, 20)
    rows = store.list_positions("paper")
    assert rows[0]["trail_pct"] == 20

    # the bid rides to 3.0 (+50%) - the peak ratchets, no fire
    quotes[key] = 3.0
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    # the bid falls 20% off the 3.0 peak -> 2.4: sold
    quotes[key] = 2.4
    monitor.check_once()
    assert store.get_position("paper", key) == 0
    sells = [t for t in store.recent_trades(5)
             if t["action"] == "SELL" and "[STOP]" in (t["detail"] or "")]
    assert len(sells) == 1


def test_per_position_trail_0_disables_trailing():
    """trail_pct 0 on the position disables trailing for it even
    when the global trailing stop is on."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                   trailing_stop_pct=20,
                                   risk_per_trade_pct=5,
                                   cooldown_seconds=0))
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0")
    executor.execute(buy, cfg, store)
    key = buy.contract_key()

    store.set_position_trail("paper", "default", key, 0)
    monitor = StopMonitor(cfg, store, executor,
                          lambda pos: 3.0)
    monitor.check_once()   # peak ratchets to 3.0
    # 2.4 = 20% off the peak: the global trailing would fire, the
    # position's own trail_pct = 0 disables it; the fixed -25%
    # stop (1.5) is far below
    monitor.quote_fn = lambda pos: 2.4
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    # a per-position trail takes over the disabled one: 10% off
    # the 3.0 peak = 2.7, and the 2.4 bid is below it
    store.set_position_trail("paper", "default", key, 10)
    monitor.check_once()
    assert store.get_position("paper", key) == 0


def test_paper_stops_run_without_quotes_enabled():
    """quotes.enabled=false must not leave paper positions
    unguarded: run.py falls back to the paper ledger's own price
    map as the monitor's quote source - the global stop and the
    per-size tier stop both fire through it."""
    import types as _types

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper", stop_loss_pct=25,
                                   risk_per_trade_pct=5,
                                   cooldown_seconds=0))
    # a per-size tier stop that is WIDER than the global one -
    # a lotto-sized position must use 70%, not the global 25%
    cfg.trading.size_tiers["lotto"] = {
        "risk_pct_max": 0.5, "contracts_min": 1,
        "contracts_max": 1, "stop_loss_pct": 70.0,
    }
    account = PaperAccount(cfg, store)
    executor = PaperExecutor(cfg, store, account)

    # the ledger prices the contract from a live ws position node
    class FakeWS:
        def _positions_raw(self):
            return {"default": [{
                "quantity": 2,
                "security": {
                    "optionDetails": {
                        "optionType": "CALL",
                        "strikePrice": 759,
                        "expiryDate": "2026-10-06T00:00:00.000-04:00",
                        "underlyingSecurity": {
                            "stock": {"symbol": "SPY"}},
                    },
                    "quoteV2": {"price": "2.0"},
                },
            }]}

    from consumer.ws.account import PaperLedger

    ledger = PaperLedger(cfg, store, FakeWS())
    from consumer.trading.paper import _position_key

    def ledger_quote(pos):
        quotes = ledger._quotes()
        q = (quotes.get(_position_key(pos))
             or quotes.get(pos["contract_key"]))
        return (q or {}).get("price")

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0 medium size")
    assert executor.execute(buy, cfg, store).ok
    key = buy.contract_key()

    monitor = StopMonitor(cfg, store, executor, ledger_quote)
    # bid 2.0: no stop
    monitor.check_once()
    assert store.get_position("paper", key) == 2

    # the position row carries its size keyword -> the tier stop
    # (70% -> 0.6) applies INSTEAD of the global 25% (1.5): at a
    # bid of 1.0 only the global stop would have fired
    rows = store.list_positions("paper")
    assert rows[0]["size"] == "medium"

    # switch the row's size to lotto (tier stop 70%)
    store.set_position_tp("paper", "default", key, None)
    store2_key = key
    conn = __import__("sqlite3").connect(store.path)
    conn.execute(
        "UPDATE positions SET size = 'lotto' WHERE contract_key = ?",
        (store2_key,),
    )
    conn.commit()
    conn.close()

    class FakeQ:
        def __init__(self, price):
            self._p = price

        def __call__(self, pos):
            return self._p

    monitor.quote_fn = FakeQ(1.0)
    monitor.check_once()
    # tier stop 70% -> floor 0.6: 1.0 is above it, no fire (the
    # global 25% stop at 1.5 WOULD have fired here)
    assert store.get_position("paper", key) == 2

    # below the tier floor: fires
    monitor.quote_fn = FakeQ(0.5)
    monitor.check_once()
    assert store.get_position("paper", key) == 0


def test_oversell_clamps_realized_to_held_contracts():
    """a sell larger than the position (duplicate/correction
    alert) clamps qty to 0 and must not book p&l for contracts
    that were never held - the realized number feeds the lotto
    budget and the daily-loss breaker."""
    store = _fresh_store()
    alert = _buy()
    store.apply_position("paper", alert, 5, premium=2.0)

    # sell 10x while holding 5x: only the 5 held contracts close
    store.apply_position("paper", alert, -10, premium=3.0)
    # fully-closed positions drop out of list_positions (qty > 0)
    qty, realized = store._conn.execute(
        "SELECT qty, realized FROM positions WHERE mode = 'paper'"
    ).fetchone()
    assert qty == 0
    # 5 closed contracts x (3.00 - 2.00) x 100 = 500 - not 1000
    assert realized == 500.0
    # the today accumulator (lotto budget / daily-loss breaker)
    # carries the clamped number too
    assert store.realized_today("paper") == 500.0


def test_cooldown_is_mode_scoped():
    """a paper buy must not start the live cooldown (and vice
    versa) - the gates run per mode on the same store."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="live", cooldown_seconds=300,
                                   dedupe_window_minutes=0))
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)

    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.record_trade("paper", "BUY", "SPY", 1, 1.5, alert,
                       "executed", "paper buy")

    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 760c @ 1.5"))
    assert ok, f"paper buy leaked into the live cooldown: {reason}"


def test_us_session_clock():
    """the extended-session clock drives the ladder's spy spot:
    post 16:00-20:00 et, overnight 20:00-04:00 (sun evening on,
    friday night OFF), pre 04:00-09:30."""
    from datetime import datetime, timezone

    from consumer.trading.quotes import us_session

    cases = [
        (datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc),
         "regular", "wed 14:00 et"),
        (datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc),
         "post", "wed 17:00 et"),
        (datetime(2026, 10, 8, 1, 0, tzinfo=timezone.utc),
         "overnight", "wed 21:00 et"),
        (datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
         "pre", "wed 08:00 et"),
        (datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc),
         "post", "fri 17:00 et"),
        (datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc),
         None, "fri 21:00 et - no overnight friday night"),
        (datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc),
         "overnight", "sun 21:00 et"),
        (datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
         None, "sat 08:00 et"),
        (datetime(2026, 10, 9, 7, 59, tzinfo=timezone.utc),
         "overnight", "fri 03:59 et"),
        (datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc),
         "pre", "fri 04:00 et"),
    ]
    for dt, want, label in cases:
        assert q_us_session(dt) == want, label


def q_us_session(dt):
    from consumer.trading.quotes import us_session
    return us_session(dt)


def test_extract_price_follows_the_active_session():
    """the snapshot carries each extended session's own price -
    the active session's price is the live one (the regular
    last_price freezes at the close)."""
    import types

    from consumer.trading.quotes import MoomooQuoteProvider

    def row(**kw):
        return types.SimpleNamespace(get=lambda k, d=None: kw.get(k, d))

    full = dict(last_price=774.45, after_price=775.10,
                overnight_price=776.00)
    assert MoomooQuoteProvider.extract_price(
        row(**full), session="post") == 775.10
    assert MoomooQuoteProvider.extract_price(
        row(**full), session="overnight") == 776.00
    # overnight falls back to the after price when the overnight
    # session has not traded yet
    assert MoomooQuoteProvider.extract_price(
        row(last_price=774.45, after_price=775.10),
        session="overnight") == 775.10
    assert MoomooQuoteProvider.extract_price(
        row(last_price=774.45, pre_price=773.80), session="pre") == 773.80
    # regular / no session: the old bid-then-last behaviour
    assert MoomooQuoteProvider.extract_price(
        row(bid_price=774.40, last_price=774.45),
        session="regular") == 774.40
    assert MoomooQuoteProvider.extract_price(
        row(bid_price=774.40, last_price=774.45)) == 774.40


def test_stock_quote_extended_uses_the_session_price(monkeypatch):
    """regression: stock_quote computed the session but never
    passed it to extract_price - the ladder's spy spot kept
    showing the regular-session bid through the overnight
    session."""
    import types

    from consumer.trading.quotes import MoomooQuoteProvider

    cfg = ConfigStub(TradingConfig(mode="paper"))
    p = MoomooQuoteProvider(cfg)

    class _Row:
        @staticmethod
        def get(key):
            return {
                "code": "US.SPY",
                "bid_price": 774.45,          # the stale regular bid
                "last_price": 773.93,
                "overnight_price": 776.13,    # the live overnight price
                "after_price": 775.10,        # the post-market price
                "pre_price": 773.80,          # the pre-market price
            }.get(key)

    class _Data:
        empty = False

        def __len__(self):
            return 1

        class _Iloc:
            @staticmethod
            def __getitem__(i):
                return _Row()

        iloc = _Iloc()

    class _Ctx:
        @staticmethod
        def get_market_snapshot(codes):
            return 0, _Data()

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(
        p, "_context", lambda: _Ctx(), raising=False
    )
    # pin the session: the test must not depend on the wall clock
    # (the real session at run time could be regular, post,
    # overnight or pre)
    import consumer.trading.quotes as _q

    monkeypatch.setattr(_q, "us_session", lambda now=None: "overnight")
    # extended=True (the ladder): the overnight session's price
    assert p.stock_quote("SPY", extended=True) == 776.13
    p._stock_cache.clear()   # the 5s ttl cache would serve the first
    # extended=False (trading pricing): the regular-session quote
    assert p.stock_quote("SPY") == 774.45
    p._stock_cache.clear()
    # the pre session picks the pre price
    p._stock_cache.clear()
    monkeypatch.setattr(_q, "us_session", lambda now=None: "pre")
    assert p.stock_quote("SPY", extended=True) == 773.80


def test_adaptive_trail_expiry_day_tighten(monkeypatch):
    """on 0dte expiry days the trail tightens through the
    session (-1%/h after 13:00 et, floored at the min); a
    non-0dte position ignores the clock."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import core.store as cs
    import consumer.trading.stops as stops_mod

    cfg = ConfigStub(TradingConfig(
        mode="paper", stop_loss_pct=25, trailing_stop_pct=10,
        adaptive_trail=True,
        adaptive_trail_steps={"10": 10, "25": 7, "50": 5, "999": 3},
        adaptive_trail_min_pct=3, adaptive_trail_expiry_tighten=1.0,
    ))
    store = _fresh_store()
    monitor = StopMonitor(cfg, store, None, lambda pos: None)

    # the fixture's "today" must ride the SAME patched clock the
    # tighten math reads - the real et date marches on and the
    # test would date-bake itself to failure
    monkeypatch.setattr(
        cs, "et_now",
        lambda: datetime(2026, 10, 9, 14, 0,
                         tzinfo=ZoneInfo("America/New_York")),
    )
    today = cs.et_now().date().isoformat()
    pos_today = {"expiry": today}
    pos_0dte_off = {"expiry": "2026-01-01"}   # not expiry day

    # 14:00 et: 1h past the 13:00 cutoff -> -1
    monkeypatch.setattr(
        stops_mod, "et_now",
        lambda: datetime(2026, 10, 9, 14, 0,
                         tzinfo=ZoneInfo("America/New_York")),
    )
    # +50% gain -> the 5% step, tightened to 4%
    assert abs(monitor.stop_price(2.0, 3.0, pos=pos_today) - 2.88) < 1e-9
    # the same gain on a non-expiry day: no tighten
    assert abs(
        monitor.stop_price(2.0, 3.0, pos=pos_0dte_off) - 2.85) < 1e-9

    # 17:30 et: 4.5h past -> -4.5 but the 3% floor holds
    monkeypatch.setattr(
        stops_mod, "et_now",
        lambda: datetime(2026, 10, 9, 17, 30,
                         tzinfo=ZoneInfo("America/New_York")),
    )
    # the 5% step tightened to 0.5 -> floored at 3%
    assert abs(monitor.stop_price(2.0, 3.0, pos=pos_today) - 2.91) < 1e-9
