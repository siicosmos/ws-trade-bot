"""Risk upgrades: the 0dte daily cap, the drawdown circuit
breaker, and the self-contained greeks module."""

import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import ConfigStub, TradingConfig, _fresh_store  # noqa: E402
from core.parser import parse_alert  # noqa: E402
from core.store import et_now  # noqa: E402

pytestmark = pytest.mark.essential


# ------------------------------------------------------------ greeks

def test_bs_call_delta_atm_is_about_half():
    from consumer.trading.greeks import bs_greeks

    g = bs_greeks(100.0, 100.0, 30 / 365, 0.05, 0.30, "C")
    assert 0.45 < g["delta"] < 0.60
    assert g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0
    assert g["price"] > 0


def test_put_call_delta_parity():
    from consumer.trading.greeks import bs_greeks

    c = bs_greeks(100.0, 105.0, 30 / 365, 0.05, 0.30, "C")
    p = bs_greeks(100.0, 105.0, 30 / 365, 0.05, 0.30, "P")
    assert abs((c["delta"] - p["delta"]) - 1.0) < 1e-9


def test_expiry_day_greeks_collapse_to_intrinsic_step():
    from consumer.trading.greeks import bs_greeks

    itm = bs_greeks(105.0, 100.0, 0.0, 0.05, 0.30, "C")
    otm = bs_greeks(95.0, 100.0, 0.0, 0.05, 0.30, "C")
    assert itm["delta"] == 1.0 and itm["price"] == 5.0
    assert otm["delta"] == 0.0 and otm["price"] == 0.0
    assert itm["gamma"] == 0.0 and itm["vega"] == 0.0


def test_portfolio_greeks_aggregates_and_skips_unpriced():
    from consumer.trading.greeks import portfolio_greeks

    today = et_now().date()
    exp = (today + timedelta(days=30)).isoformat()
    positions = [
        {"underlying": "SPX", "expiry": exp, "strike": 6000,
         "right": "C", "qty": 2, "contract_key": "k1"},
        {"underlying": "SPY", "qty": 10},   # stock: delta = qty
        {"underlying": "QQQ", "expiry": exp, "strike": 500,
         "right": "P", "qty": 1, "contract_key": "k2"},
    ]
    agg = portfolio_greeks(positions, {"SPX": 6000.0, "SPY": 600.0})
    # SPX call delta x2x100 + SPY stock delta; QQQ unpriced
    assert 0 < agg["delta"] < 201
    assert agg["unpriced"] == 1
    # the stock row contributes exactly its share count
    spot_only = portfolio_greeks(
        [{"underlying": "SPY", "qty": 10}], {"SPY": 600.0})
    assert spot_only["delta"] == 10


# ------------------------------------------------------- 0dte cap

def _alert(expiry=None):
    a = parse_alert("BOUGHT 0DTE SPX 6000c @ 1.5 small")
    if expiry:
        a.expiry = expiry
    return a


def test_zero_dte_exposure_counts_only_todays_expiries():
    store = _fresh_store()
    today = et_now().strftime("%Y-%m-%d")
    tomorrow = (et_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    store.seed_position("paper", "M", "k-today", "SPX", today,
                        6000, "C", 2, 1.5)
    store.seed_position("paper", "M", "k-tmrw", "SPX", tomorrow,
                        6000, "C", 5, 1.5)
    # 2 contracts x 1.5 x 100 - tomorrow's 5x are excluded
    assert store.zero_dte_exposure("paper", "M") == pytest.approx(300.0)


def test_zero_dte_cap_gate():
    from consumer.trading.risk_gates import at_zero_dte_cap

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.zero_dte_cap_pct = 3.0
    today = et_now().strftime("%Y-%m-%d")
    store.seed_position("paper", "M", "k1", "SPX", today,
                        6000, "C", 1, 1.0)
    alert = _alert(today)

    # 100 at risk, cap 3% of 10k = 300: +2 contracts x 1.5 x100
    # = 300 projected -> 400 > 300 blocks
    assert at_zero_dte_cap(store, "paper", "M", 10000.0, cfg,
                           alert, 1.5, 2)
    # +1 contract = 250 total, under the cap
    assert not at_zero_dte_cap(store, "paper", "M", 10000.0, cfg,
                               alert, 1.5, 1)
    # a non-0dte expiry is never gated by this cap
    tomorrow = (et_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert not at_zero_dte_cap(store, "paper", "M", 10000.0, cfg,
                               _alert(tomorrow), 1.5, 50)
    # cap 0 = off
    cfg.trading.zero_dte_cap_pct = 0.0
    assert not at_zero_dte_cap(store, "paper", "M", 10000.0, cfg,
                               alert, 1.5, 100)


def test_zero_dte_cap_gates_the_paper_executor():
    """The executor skips the account at the 0dte cap (the same
    path as the cluster cap)."""
    from consumer.trading.executor import PaperExecutor

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.zero_dte_cap_pct = 1.0
    cfg.trading.size_tiers["small"]["contracts_max"] = 5
    account = __import__(
        "consumer.ws.account", fromlist=["PaperAccount"]
    ).PaperAccount(cfg, store)
    store.set_paper_equity(10000.0, "default")
    today = et_now().strftime("%Y-%m-%d")
    store.seed_position("paper", "default", "k-existing",
                        "SPX", today, 6000, "C", 1, 1.0)
    executor = PaperExecutor(cfg, store, account)
    alert = _alert(today)
    result = executor.execute(alert, cfg, store)
    assert not result.ok
    assert "0dte daily cap" in result.detail


# ------------------------------------------------- drawdown breaker

def test_drawdown_pct_and_lookback():
    store = _fresh_store()
    store.record_equity_peak("paper", "M", 1000.0)
    assert store.drawdown_pct("paper", "M", 900.0) == pytest.approx(10.0)
    assert store.drawdown_pct("paper", "M", 1100.0) == 0.0
    assert store.drawdown_pct("paper", "M", None) == 0.0
    # a peak older than the lookback is ignored
    import json as _json

    with store._write_lock, store._tx:
        store._conn.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (_json.dumps([1000.0, "2026-01-01T00:00:00+00:00"]),
             "equity_peak:paper:M"),
        )
    assert store.drawdown_pct("paper", "M", 900.0) == 0.0


def test_drawdown_breaker_blocks_buys():
    from consumer.trading.risk import RiskEngine

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.max_drawdown_pct = 5.0
    account = type("A", (), {"values": lambda self: {"M": 900.0}})()
    store.record_equity_peak("paper", "M", 1000.0)
    risk = RiskEngine(cfg, store, account)
    ok, reason = risk.evaluate(_alert())
    assert not ok
    assert "drawdown breaker" in reason
    # recovered equity: the gate passes and the peak ratchets
    account.values = lambda: {"M": 1100.0}
    ok, reason = risk.evaluate(_alert())
    assert ok, reason
    assert store.drawdown_pct("paper", "M", 1100.0) == 0.0


# --------------------------------------------- opend-driven upgrades

class _StubProvider:
    """The moomoo provider surface the upgrades consume."""

    def __init__(self, vix=None, bars=None, spots=None):
        self._vix = vix
        self._bars = bars or {}
        self._spots = spots or {}
        self.bar_calls = []

    def vix_quote(self):
        return self._vix

    def daily_bars(self, symbol, n=14):
        self.bar_calls.append((symbol, n))
        return self._bars.get(symbol)

    def stock_quote(self, symbol, ttl=5.0, extended=False):
        return self._spots.get(symbol)


def test_sizing_multiplier_vix_scalar():
    from consumer.trading.sizing import sizing_multiplier

    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.vix_size_scalar = True
    calm = _StubProvider(vix=15.0)
    stressed = _StubProvider(vix=30.0)
    assert sizing_multiplier(cfg, provider=calm) == 1.0
    assert sizing_multiplier(cfg, provider=stressed) == pytest.approx(0.5)
    # no provider / no vix: fail open
    assert sizing_multiplier(cfg, provider=None) == 1.0
    assert sizing_multiplier(cfg, provider=_StubProvider(vix=None)) == 1.0
    # disabled: 1.0 even with data
    cfg.trading.vix_size_scalar = False
    assert sizing_multiplier(cfg, provider=stressed) == 1.0


def test_sizing_multiplier_kelly_from_closed_trades():
    from consumer.trading.sizing import sizing_multiplier

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.kelly_size_scalar = True
    cfg.trading.kelly_min_trades = 5
    cfg.trading.kelly_fraction = 1.0
    # too few closed trips: fail open
    assert sizing_multiplier(cfg, store, "paper") == 1.0
    # 8 winners (+200 each) and 2 losers (-100 each): p=0.8,
    # b=2 -> full kelly = (0.8*2 - 0.2)/2 = 0.7
    for i in range(8):
        store.seed_position("paper", "M", f"w{i}", "SPX",
                            "2026-01-10", 6000, "C", 1, 1.0)
        store._conn.execute(
            "UPDATE positions SET qty = 0, realized = 200, "
            "updated_ts = ? WHERE contract_key = ?",
            (f"2026-01-1{i}T15:00:00", f"w{i}"),
        )
    for i in range(2):
        store.seed_position("paper", "M", f"l{i}", "SPX",
                            "2026-01-10", 6000, "P", 1, 1.0)
        store._conn.execute(
            "UPDATE positions SET qty = 0, realized = -100, "
            "updated_ts = ? WHERE contract_key = ?",
            (f"2026-01-2{i}T15:00:00", f"l{i}"),
        )
    mult = sizing_multiplier(cfg, store, "paper")
    assert mult == pytest.approx(0.7)
    # a negative-expectancy book: full kelly <= 0 -> scalar 0
    # (the executor floors the quantity at 1 contract)
    store.seed_position("paper", "M", "x1", "SPX", "2026-01-10",
                        6000, "C", 1, 1.0)
    store._conn.execute(
        "UPDATE positions SET qty = 0, realized = -2000, "
        "updated_ts = '2026-01-30T15:00:00' WHERE contract_key = 'x1'"
    )
    # p=8/11, avg_win 200, avg_loss 2200/3 -> b < 1 and
    # p*b - (1-p) < 0: full kelly goes negative -> scalar 0
    assert sizing_multiplier(cfg, store, "paper") == 0.0


def test_sizing_multiplier_caps_at_one():
    from consumer.trading.sizing import sizing_multiplier

    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.vix_size_scalar = True
    cfg.trading.kelly_size_scalar = True
    # both components at 1.0 must never amplify
    provider = _StubProvider(vix=10.0)
    assert sizing_multiplier(cfg, provider=provider) == 1.0


def test_atr_stop_price_scales_underlying_atr_by_delta():
    from consumer.trading.stops import atr_stop_price

    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.atr_trailing = True
    cfg.trading.atr_period = 3
    cfg.trading.atr_multiplier = 2.0
    # 4 bars -> 3 true ranges of 2.0 each -> atr 2.0
    bars = [
        {"high": 101.0, "low": 99.0, "close": 100.0},
        {"high": 102.0, "low": 100.0, "close": 101.0},
        {"high": 103.0, "low": 101.0, "close": 102.0},
        {"high": 104.0, "low": 102.0, "close": 103.0},
    ]
    provider = _StubProvider(bars={"SPX": bars})
    today = et_now().strftime("%Y-%m-%d")
    pos = {"underlying": "SPX", "expiry": today, "strike": 6000,
           "right": "C", "qty": 2, "contract_key": "k"}
    # itm strike (spot 103 > strike 102): 0dte delta = 1 ->
    # stop = peak - 2 x 1 x atr(2.0) = 1.0
    pos["strike"] = 102.0
    stop = atr_stop_price(provider, pos, peak=5.0, t=cfg.trading)
    assert stop == pytest.approx(1.0)
    # disabled: None
    cfg.trading.atr_trailing = False
    assert atr_stop_price(provider, pos, 5.0, cfg.trading) is None
    # missing bars: None
    cfg.trading.atr_trailing = True
    assert atr_stop_price(_StubProvider(), pos, 5.0, cfg.trading) is None


def test_stop_price_takes_the_tighter_floor():
    from consumer.trading.stops import StopMonitor

    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.stop_loss_pct = 50.0
    cfg.trading.adaptive_trail = False
    cfg.trading.trailing_stop_pct = 0
    cfg.trading.atr_trailing = True
    cfg.trading.atr_period = 3
    cfg.trading.atr_multiplier = 0.5
    bars = [
        {"high": 101.0, "low": 99.0, "close": 100.0},
        {"high": 102.0, "low": 100.0, "close": 101.0},
        {"high": 103.0, "low": 101.0, "close": 102.0},
        {"high": 104.0, "low": 102.0, "close": 103.0},
    ]
    provider = _StubProvider(bars={"SPX": bars})
    monitor = StopMonitor(cfg, _fresh_store(), None, None,
                          provider=provider)
    today = et_now().strftime("%Y-%m-%d")
    pos = {"underlying": "SPX", "expiry": today, "strike": 102.0,
           "right": "C", "qty": 1, "size": "micro",
           "contract_key": "k"}
    # entry 2.0, stop_loss 50% -> pct floor 1.0; the atr floor
    # = peak - 0.5 x delta(1) x atr(2) - with a high peak it
    # sits above the pct floor and wins
    stop = monitor.stop_price(2.0, 4.5, pos)
    assert stop == pytest.approx(3.5)
    # a low peak: the pct floor wins
    assert monitor.stop_price(2.0, 2.0, pos) == pytest.approx(1.0)


def test_delta_soft_warning_fires_once(monkeypatch):
    from consumer.trading.stops import StopMonitor

    store = _fresh_store()
    today = et_now().strftime("%Y-%m-%d")
    store.seed_position("paper", "M", "k1", "SPX", today,
                        6000, "C", 10, 1.0)
    cfg = ConfigStub(TradingConfig(mode="paper"))
    cfg.trading.delta_cap_pct = 1.0
    # spot 6000 vs strike 6000, 0dte: delta ~1 per contract x10x100
    provider = _StubProvider(spots={"SPX": 6001.0})
    account = type("A", (), {"values": lambda self: {"M": 50000.0}})()
    monitor = StopMonitor(cfg, store, type("T", (), {
        "mode": "paper", "account": account})(),
        None, provider=provider)
    notices = []
    monkeypatch.setattr(
        "consumer.trading.stops.notify_discord",
        lambda *a, **k: notices.append(a),
    )
    monitor._delta_warning()
    assert len(notices) == 1
    # rate-limited: no second notice inside the window
    monitor._delta_warning()
    assert len(notices) == 1
    # cap disabled: silent
    cfg.trading.delta_cap_pct = 0.0
    monitor._delta_warn_ts = 0.0
    monitor._delta_warning()
    assert len(notices) == 1


def test_daily_bars_cached_per_day(monkeypatch):
    """The provider's daily bars cache one pull per underlying
    per calendar day - the history-kline quota is finite."""
    from consumer.trading.quotes import MoomooQuoteProvider

    cfg = type("C", (), {"quotes": type("Q", (), {
        "moomoo_host": "127.0.0.1", "moomoo_port": 11111})()})()
    provider = MoomooQuoteProvider(cfg)

    class _Row:
        def __init__(self, h, l, c):
            self._r = {"high": h, "low": l, "close": c}

        def get(self, key):
            return self._r.get(key)

    class _Data:
        empty = False

        def __init__(self, rows):
            self._rows = [_Row(*r) for r in rows]

        def __len__(self):
            return len(self._rows)

        @property
        def iloc(self):
            return self._rows

    calls = []

    class _Ctx:
        def request_history_kline(self, code, **kw):
            calls.append(code)
            return 0, _Data([
                (100.0 + i, 99.0 + i, 100.0 + i) for i in range(20)
            ])

    provider._ctx = _Ctx()
    bars = provider.daily_bars("SPX", 14)
    assert bars and len(bars) == 15
    provider.daily_bars("SPX", 14)
    assert len(calls) == 1   # same day: served from cache
    provider.daily_bars("SPY", 14)
    assert len(calls) == 2
