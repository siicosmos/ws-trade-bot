import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.config import TradingConfig
from trader.executor import PaperExecutor
from trader.parser import parse_alert
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


def test_risk_whitelist():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(ticker_whitelist=["TSLA"]))
    risk = RiskEngine(cfg, store)
    ok, reason = risk.evaluate(parse_alert("BUY TSLA"))
    assert ok
    ok, reason = risk.evaluate(parse_alert("BUY GME"))
    assert not ok
    assert "whitelist" in reason


def test_risk_daily_limit():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(max_trades_per_day=1))
    risk = RiskEngine(cfg, store)
    alert = parse_alert("BUY TSLA")
    store.record_trade("paper", alert.action, alert.ticker, 1, 100, alert, "executed", "test")
    ok, reason = risk.evaluate(alert)
    assert not ok
    assert "daily" in reason


def test_risk_dedupe():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(dedupe_window_minutes=10, cooldown_seconds=0))
    risk = RiskEngine(cfg, store)
    alert = parse_alert("BUY TSLA")
    store.record_trade("paper", alert.action, alert.ticker, 1, 100, alert, "executed", "test")
    ok, reason = risk.evaluate(alert)
    assert not ok
    assert "duplicate" in reason


def test_paper_executor():
    cfg = ConfigStub(TradingConfig(position_size_cad=1000))
    ex = PaperExecutor()
    result = ex.execute(parse_alert("BUY TSLA @ 250"), cfg)
    assert result.ok
    assert result.qty == 4


def test_pipeline_dry_run():
    from trader.pipeline import process_alert

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(dry_run=True))
    risk = RiskEngine(cfg, store)
    res = process_alert("BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor())
    assert res["status"] == "executed"
    assert res["alert"]["ticker"] == "TSLA"
    assert store.trades_today("paper") == 1


def test_pipeline_duplicate_message_ignored():
    from trader.pipeline import process_alert

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(dry_run=True))
    risk = RiskEngine(cfg, store)
    first = process_alert("BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor())
    second = process_alert("BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor())
    assert first["status"] == "executed"
    assert second["status"] == "ignored"
    assert store.trades_today("paper") == 1
