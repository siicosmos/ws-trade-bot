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
