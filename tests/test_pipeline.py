import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.config import TradingConfig
from trader.executor import PaperExecutor, sell_quantity
from trader.parser import parse_alert
from trader.pipeline import process_alert
from trader.risk import RiskEngine
from trader.store import Store


class ConfigStub:
    def __init__(self, trading):
        self.trading = trading
        self.discord = type("D", (), {"webhook_url": ""})()
        self.parser = type("P", (), {"custom_patterns": []})()


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _cfg(**kw):
    return ConfigStub(TradingConfig(**kw))


def test_risk_whitelist():
    store = _fresh_store()
    cfg = _cfg(ticker_whitelist=["SPY"])
    risk = RiskEngine(cfg, store)
    ok, _ = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"))
    assert ok
    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0"))
    assert not ok
    assert "whitelist" in reason


def test_risk_daily_limit_buys_only():
    store = _fresh_store()
    cfg = _cfg(max_trades_per_day=1, cooldown_seconds=0, dedupe_window_minutes=0)
    risk = RiskEngine(cfg, store)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.record_trade("paper", alert.action, alert.ticker, 1, 1.5, alert, "executed", "t")
    ok, reason = risk.evaluate(alert)
    assert not ok
    sell_alert = parse_alert("SOLD 1/4 0DTE SPY 759c @ 2.0")
    ok, reason = risk.evaluate(sell_alert)
    assert ok


def test_risk_dedupe_same_premium():
    store = _fresh_store()
    cfg = _cfg(dedupe_window_minutes=10, cooldown_seconds=0)
    risk = RiskEngine(cfg, store)
    alert = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.0")
    store.record_trade("paper", alert.action, alert.ticker, 1, 2.0, alert, "executed", "t")
    ok, reason = risk.evaluate(alert)
    assert not ok
    other = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.32")
    ok, _ = risk.evaluate(other)
    assert ok


def test_sell_quantity_math():
    assert sell_quantity(4, 0.25) == 1
    assert sell_quantity(3, 0.25) == 1
    assert sell_quantity(10, 2 / 3) == 7
    assert sell_quantity(6, None) == 6
    assert sell_quantity(6, 1.0) == 6
    assert sell_quantity(0, 1.0) == 0


def test_paper_option_buy_sell_cycle():
    store = _fresh_store()
    cfg = _cfg(default_contracts=4, contracts_by_size={"tiny": 1})
    ex = PaperExecutor()

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    res = ex.execute(buy, cfg, store)
    assert res.ok
    assert res.qty == 4
    assert store.get_position("paper", buy.contract_key()) == 4

    sell = parse_alert("SOLD 1/4 0DTE SPY 759c @ 1.95 @everyone +30%")
    res = ex.execute(sell, cfg, store)
    assert res.ok
    assert res.qty == 1
    assert store.get_position("paper", buy.contract_key()) == 3

    out = parse_alert("ALL OUT 0DTE SPY 759c @ 1.5 @everyone out rest BE")
    res = ex.execute(out, cfg, store)
    assert res.ok
    assert res.qty == 3
    assert store.get_position("paper", buy.contract_key()) == 0

    again = parse_alert("ALL OUT 0DTE SPY 759c @ 1.4 @everyone")
    res = ex.execute(again, cfg, store)
    assert not res.ok


def test_paper_size_hint_contracts():
    store = _fresh_store()
    cfg = _cfg(default_contracts=4, contracts_by_size={"tiny": 1, "lotto": 1})
    ex = PaperExecutor()
    buy = parse_alert("BOUGHT 0DTE SPX 7585c @ .7 @everyone HERO or ZERO 🎲")
    res = ex.execute(buy, cfg, store)
    assert res.qty == 4

    buy2 = parse_alert("BOUGHT 09/16 GOOGL 347.5c @ .48 @everyone lotto size 🎲")
    res = ex.execute(buy2, cfg, store)
    assert res.qty == 1


def test_paper_stock_executor():
    store = _fresh_store()
    cfg = _cfg(position_size_cad=1000)
    ex = PaperExecutor()
    result = ex.execute(parse_alert("BUY TSLA @ 250"), cfg, store)
    assert result.ok
    assert result.qty == 4


def test_pipeline_dry_run_option_flow():
    store = _fresh_store()
    cfg = _cfg(dry_run=True, default_contracts=2, cooldown_seconds=0)
    risk = RiskEngine(cfg, store)
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size", "",
        cfg, store, risk, PaperExecutor(),
    )
    assert res["status"] == "executed"
    assert res["alert"]["kind"] == "option"
    assert res["alert"]["underlying"] == "SPY"


def test_pipeline_duplicate_message_ignored():
    store = _fresh_store()
    cfg = _cfg(dry_run=True, cooldown_seconds=0)
    risk = RiskEngine(cfg, store)
    first = process_alert("BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor())
    second = process_alert("BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor())
    assert first["status"] == "executed"
    assert second["status"] == "ignored"
