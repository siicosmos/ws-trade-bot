import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.account import PaperAccount
from trader.config import TradingConfig
from trader.executor import PaperExecutor, contracts_for, sell_quantity
from trader.parser import parse_alert
from trader.pipeline import process_alert
from trader.risk import RiskEngine
from trader.store import Store


class ConfigStub:
    def __init__(self, trading):
        self.trading = trading
        self.discord = type("D", (), {"webhook_url": ""})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = type("W", (), {"account_id": "", "exchange_hint": ""})()


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _setup(**trading_kw):
    trading_kw.setdefault("mode", "paper")
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(**trading_kw))
    account = PaperAccount(cfg, store)
    return cfg, store, account, RiskEngine(cfg, store, account)


def test_risk_whitelist():
    cfg, store, account, risk = _setup(ticker_whitelist=["SPY"])
    ok, _ = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"))
    assert ok
    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0"))
    assert not ok
    assert "whitelist" in reason


def test_risk_daily_limit_buys_only():
    cfg, store, account, risk = _setup(
        max_trades_per_day=1, cooldown_seconds=0, dedupe_window_minutes=0
    )
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.record_trade("paper", alert.action, alert.ticker, 1, 1.5, alert, "executed", "t")
    ok, reason = risk.evaluate(alert)
    assert not ok
    sell_alert = parse_alert("SOLD 1/4 0DTE SPY 759c @ 2.0")
    ok, reason = risk.evaluate(sell_alert)
    assert ok


def test_risk_dedupe_same_premium():
    cfg, store, account, risk = _setup(dedupe_window_minutes=10, cooldown_seconds=0)
    alert = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.0")
    store.record_trade("paper", alert.action, alert.ticker, 1, 2.0, alert, "executed", "t")
    ok, reason = risk.evaluate(alert)
    assert not ok
    other = parse_alert("SOLD 1/4 0DTE SPX 7650c @ 2.32")
    ok, _ = risk.evaluate(other)
    assert ok


def test_open_risk_cap_blocks_new_buys():
    cfg, store, account, risk = _setup(
        paper_account_value=1000, max_open_risk_pct=30, cooldown_seconds=0,
        dedupe_window_minutes=0,
    )
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.apply_position("paper", alert, 2, premium=1.5)
    open_risk = store.open_risk("paper")
    assert open_risk == 300
    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 759c @ 1.6"))
    assert not ok
    assert "open risk" in reason


def test_contracts_for_sizing():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5
    )
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), cfg, 10000, 1.5) == 3
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ .65"), cfg, 10000, 0.65) == 7
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 99"), cfg, 10000, 99.0) == 0
    cfg2, _, _, _ = _setup(
        paper_account_value=10000, risk_per_trade_pct=5,
        size_risk_multiplier={"tiny": 0.5},
    )
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone tiny size"), cfg2, 10000, 1.5) == 1
    cfg3, _, _, _ = _setup(max_contracts_per_trade=2)
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), cfg3, 100000, 0.5) == 2


def test_sell_quantity_math():
    assert sell_quantity(4, 0.25) == 1
    assert sell_quantity(3, 0.25) == 1
    assert sell_quantity(10, 2 / 3) == 7
    assert sell_quantity(6, None) == 6
    assert sell_quantity(6, 1.0) == 6
    assert sell_quantity(0, 1.0) == 0


def test_paper_option_cycle_with_equity():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5, cooldown_seconds=0
    )
    ex = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone")
    res = ex.execute(buy, cfg, store)
    assert res.ok
    assert res.qty == 3
    assert store.get_position("paper", buy.contract_key()) == 3
    assert store.paper_equity() == 10000 - 3 * 150

    sell = parse_alert("SOLD 1/4 0DTE SPY 759c @ 1.95 @everyone +30%")
    res = ex.execute(sell, cfg, store)
    assert res.ok
    assert res.qty == 1
    assert store.paper_equity() == 10000 - 450 + 195

    out = parse_alert("ALL OUT 0DTE SPY 759c @ 1.5 @everyone out rest BE")
    res = ex.execute(out, cfg, store)
    assert res.ok
    assert res.qty == 2
    assert store.get_position("paper", buy.contract_key()) == 0


def test_paper_budget_too_small_skips():
    cfg, store, account, risk = _setup(
        paper_account_value=500, risk_per_trade_pct=5
    )
    ex = PaperExecutor(cfg, store, account)
    buy = parse_alert("BOUGHT 0DTE SPX 7650c @ 20.0 @everyone")
    res = ex.execute(buy, cfg, store)
    assert not res.ok
    assert store.get_position("paper", buy.contract_key()) == 0


def test_pipeline_dry_run_option_flow():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5, cooldown_seconds=0
    )
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size", "",
        cfg, store, risk, PaperExecutor(cfg, store, account),
    )
    assert res["status"] == "executed"
    assert res["alert"]["kind"] == "option"
    assert res["alert"]["underlying"] == "SPY"


def test_pipeline_duplicate_message_ignored():
    cfg, store, account, risk = _setup(cooldown_seconds=0, position_size_cad=1000)
    first = process_alert(
        "BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor(cfg, store, account)
    )
    second = process_alert(
        "BUY TSLA @ 250", "", cfg, store, risk, PaperExecutor(cfg, store, account)
    )
    assert first["status"] == "executed"
    assert second["status"] == "ignored"


def test_account_endpoint():
    from trader.server import create_app

    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5
    )
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    store.apply_position("paper", alert, 2, premium=1.5)
    app = create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    client = app.test_client()
    resp = client.get("/account")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["account_value"] == 10000
    assert data["open_risk"] == 300
    assert data["open_risk_pct"] == 3.0
    assert data["per_trade_budget"] == 500
    assert len(data["positions"]) == 1
    assert data["positions"][0]["qty"] == 2


def test_notify_mode_forwards_without_trading():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size", "",
        cfg, store, risk, None,
    )
    assert res["status"] == "notified"
    assert res["alert"]["kind"] == "option"
    assert store.trades_today("paper") == 0
    assert store.open_risk("paper") == 0
    assert store.list_positions("paper") == []


def test_notify_mode_sell_alert():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "SOLD 1/4 0DTE SPX 7650c @ 2.0 @everyone +30%", "",
        cfg, store, risk, None,
    )
    assert res["status"] == "notified"
    assert res["alert"]["action"] == "SELL"
    assert res["alert"]["scale"] == 0.25


def test_notify_mode_ignores_non_signals():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert("executed 2.15 ^", "", cfg, store, risk, None)
    assert res["status"] == "ignored"


def test_notify_mode_dedupes_repeat_messages():
    cfg, store, account, risk = _setup(mode="notify")
    res1 = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None
    )
    res2 = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None
    )
    assert res1["status"] == "notified"
    assert res2["status"] == "ignored"
