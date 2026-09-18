import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.account import PaperAccount
from trader.config import TradingConfig, WealthsimpleConfig, WSAccountConfig
from trader.executor import (
    PaperExecutor, account_sizing, contracts_for, sell_quantity, tier_plan,
)
from trader.parser import parse_alert
from trader.pipeline import process_alert
from trader.risk import RiskEngine
from trader.store import Store


class ConfigStub:
    def __init__(self, trading, accounts=None, auth_token=""):
        self.trading = trading
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.discord = type("D", (), {"webhook_url": ""})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])


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


def test_contracts_for_sizing():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5
    )
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), cfg, 10000, 1.5) == 3
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ .65"), cfg, 10000, 0.65) == 7
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 99"), cfg, 10000, 99.0) == 0
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size"), cfg, 10000, 1.5) == 1
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone lotto size"), cfg, 10000, 1.5) == 0
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size"), cfg, 10000, 1.5) == 3
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone large size"), cfg, 10000, 1.5) == 6
    cfg3, _, _, _ = _setup(max_contracts_per_trade=2)
    assert contracts_for(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), cfg3, 100000, 0.5) == 2


def test_tier_plan_capping():
    cfg, store, account, risk = _setup()
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    plan = tier_plan(alert, cfg, 50000, 1.5)
    assert plan["affordable"] == 6
    assert plan["qty"] == 2
    assert plan["tier_min"] == 1
    assert plan["tier_max"] == 2

    plan = tier_plan(alert, cfg, 2000, 1.5)
    assert plan["affordable"] == 0
    assert plan["qty"] == 0


def test_tier_plan_min_contract_gate():
    cfg = ConfigStub(TradingConfig(mode="paper", size_tiers={
        "small": {"risk_pct_max": 2.0, "contracts_min": 4, "contracts_max": 5},
    }))
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    plan = tier_plan(alert, cfg, 10000, 1.5)
    assert plan["affordable"] == 1
    assert plan["qty"] == 0

    plan = tier_plan(alert, cfg, 100000, 1.5)
    assert plan["affordable"] == 13
    assert plan["qty"] == 5


def test_contracts_for_per_account_overrides():
    cfg, store, account, risk = _setup(risk_per_trade_pct=5)
    big = WSAccountConfig(account_id="rrsp", label="RRSP", risk_per_trade_pct=3)
    small = WSAccountConfig(account_id="pers", label="Personal", risk_per_trade_pct=10)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    assert contracts_for(alert, cfg, 50000, 1.5, big) == 10
    assert contracts_for(alert, cfg, 2000, 1.5, small) == 1
    sized = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    assert contracts_for(sized, cfg, 50000, 1.5, big) == 2
    capped = WSAccountConfig(account_id="x", label="x", max_contracts_per_trade=2)
    assert contracts_for(alert, cfg, 50000, 1.5, capped) == 2


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


def test_multi_account_paper_execution():
    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP", paper_value=50000),
        WSAccountConfig(account_id="pers", label="Personal", paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="paper", risk_per_trade_pct=5, cooldown_seconds=0),
        accounts=accounts,
    )
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    ex = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
    res = ex.execute(buy, cfg, store)
    assert res.ok
    assert res.breakdown["RRSP"] == "5x @ 1.5 (tier max 5, could afford 16)"
    assert res.breakdown["Personal"] == "0 (budget $100 < $150/contract)"
    assert res.qty == 5
    assert store.get_position("paper", buy.contract_key(), "RRSP") == 5
    assert store.get_position("paper", buy.contract_key(), "Personal") == 0
    assert store.paper_equity("RRSP") == 50000 - 5 * 150
    assert store.paper_equity("Personal") == 2000

    sell = parse_alert("SOLD 1/4 0DTE SPY 759c @ 1.95 @everyone")
    res = ex.execute(sell, cfg, store)
    assert res.ok
    assert res.breakdown["RRSP"] == "1/5x @ 1.95"
    assert res.breakdown["Personal"] == "no position"
    assert store.get_position("paper", buy.contract_key(), "RRSP") == 4
    assert store.get_position("paper", buy.contract_key(), "Personal") == 0


def test_multi_account_open_risk_cap_skips_only_that_account():
    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP", paper_value=50000),
        WSAccountConfig(account_id="pers", label="Personal", paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="paper", risk_per_trade_pct=5,
                      max_open_risk_pct=30, cooldown_seconds=0),
        accounts=accounts,
    )
    account = PaperAccount(cfg, store)
    ex = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone big size")
    res = ex.execute(buy, cfg, store)
    assert res.ok
    assert res.breakdown["Personal"] == "1x @ 1.5"
    assert res.breakdown["RRSP"] == "10x @ 1.5 (tier max 10, could afford 33)"

    tiny = parse_alert("BOUGHT 0DTE SPY 800c @ .5 @everyone")
    tiny.underlying = "SPY"
    store.apply_position("paper", tiny, 12, premium=0.5, account="Personal")
    assert store.open_risk("paper", "Personal") == 750
    assert 750 >= 2000 * 0.30

    res = ex.execute(buy, cfg, store)
    assert "skipped (open risk cap reached" in res.breakdown["Personal"]
    assert "x @ 1.5" in res.breakdown["RRSP"]


def test_account_sizing_rows():
    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP", paper_value=50000),
        WSAccountConfig(account_id="pers", label="Personal", paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify", risk_per_trade_pct=5),
                     accounts=accounts)
    account = PaperAccount(cfg, store)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    rows = account_sizing(alert, cfg, account, store)
    by_label = {r["label"]: r for r in rows}
    assert by_label["RRSP"]["value"] == 50000
    assert by_label["RRSP"]["risk_pct"] == 2.0
    assert by_label["RRSP"]["contracts"] == 2
    assert by_label["RRSP"]["actual_risk"] == 300
    assert by_label["RRSP"]["warnings"] == [
        "capped at tier max 2 (budget could afford 6)"
    ]
    assert by_label["Personal"]["risk_pct"] == 2.0
    assert by_label["Personal"]["contracts"] == 0
    assert by_label["Personal"]["warnings"] == [
        "budget $40 below $150 per-contract cost"
    ]


def test_account_sizing_tier_min_warning():
    cfg = ConfigStub(TradingConfig(mode="notify", size_tiers={
        "small": {"risk_pct_max": 2.0, "contracts_min": 4, "contracts_max": 5},
    }))
    store = _fresh_store()
    account = PaperAccount(cfg, store)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    rows = account_sizing(alert, cfg, account, store)
    row = rows[0]
    assert row["contracts"] == 0
    assert row["warnings"] == [
        "budget $200 affords 1, tier minimum is 4"
    ]


def test_pipeline_dry_run_option_flow():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5, cooldown_seconds=0
    )
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size", "",
        cfg, store, risk, PaperExecutor(cfg, store, account), account,
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


def test_notify_mode_forwards_without_trading():
    cfg, store, account, risk = _setup(mode="notify", paper_account_value=10000)
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size", "",
        cfg, store, risk, None, account,
    )
    assert res["status"] == "notified"
    assert res["alert"]["kind"] == "option"
    assert store.trades_today("paper") == 0
    assert store.open_risk("paper") == 0
    assert store.list_positions("paper") == []
    assert res["sizing"][0]["label"] == "default"
    assert res["sizing"][0]["risk_pct"] == 2.0
    assert res["sizing"][0]["contracts"] == 1


def test_notify_mode_sell_no_sizing():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "SOLD 1/4 0DTE SPX 7650c @ 2.0 @everyone +30%", "",
        cfg, store, risk, None, account,
    )
    assert res["status"] == "notified"
    assert res["alert"]["action"] == "SELL"
    assert res["sizing"] == []


def test_notify_mode_ignores_non_signals():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert("executed 2.15 ^", "", cfg, store, risk, None, account)
    assert res["status"] == "ignored"


def test_notify_mode_dedupes_repeat_messages():
    cfg, store, account, risk = _setup(mode="notify")
    res1 = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None, account
    )
    res2 = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None, account
    )
    assert res1["status"] == "notified"
    assert res2["status"] == "ignored"


def _make_app(auth_token="", mode="paper"):
    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP", paper_value=50000),
        WSAccountConfig(account_id="pers", label="Personal", paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode=mode, risk_per_trade_pct=5),
                     accounts=accounts, auth_token=auth_token)
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    app = __import__("trader.server", fromlist=["create_app"]).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    return app, store, account


def test_dashboard_and_api_endpoints():
    from trader.parser import parse_alert as pa

    app, store, account = _make_app()
    alert = pa("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    store.apply_position("paper", alert, 2, premium=1.5, account="Personal")
    store.record_trade("paper", "BUY", "SPY", 2, 1.5, alert, "executed", "test")
    store.record_signal("k1", "", "BOUGHT 0DTE SPY 759c @ 1.5", True)

    client = app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b"WS Trade Bot" in page.data

    summary = client.get("/api/summary").get_json()
    labels = {a["label"] for a in summary["accounts"]}
    assert labels == {"RRSP", "Personal"}

    positions = client.get("/api/positions").get_json()
    assert len(positions) == 1
    assert positions[0]["account"] == "Personal"

    signals = client.get("/api/signals").get_json()
    assert len(signals) == 1
    assert signals[0]["parsed"] == 1

    trades = client.get("/api/trades").get_json()
    assert len(trades) == 1
    assert trades[0]["action"] == "BUY"


def test_auth_guard():
    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()

    assert client.get("/").status_code == 401
    assert client.get("/api/summary").status_code == 401
    assert client.get("/health").status_code == 200

    ok = client.get("/api/summary", headers={"X-Auth-Token": "s3cret"})
    assert ok.status_code == 200

    import base64
    basic = base64.b64encode(b"user:s3cret").decode()
    ok = client.get(
        "/api/summary", headers={"Authorization": f"Basic {basic}"}
    )
    assert ok.status_code == 200

    bad = client.get("/api/summary", headers={"X-Auth-Token": "wrong"})
    assert bad.status_code == 401


def test_auth_guard_alert_endpoint():
    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()

    blocked = client.post(
        "/alert", json={"text": "BOUGHT 0DTE SPY 759c @ 1.5"}
    )
    assert blocked.status_code == 401

    ok = client.post(
        "/alert", json={"text": "BOUGHT 0DTE SPY 759c @ 1.5"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert ok.status_code == 200


def test_persist_env_tokens():
    import trader.ws_tokens as wt

    fd, path = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    old = os.environ.get("WS_ACCESS_TOKEN"), os.environ.get("WS_REFRESH_TOKEN")
    try:
        os.environ["WS_ACCESS_TOKEN"] = "acc1"
        os.environ["WS_REFRESH_TOKEN"] = "ref1"
        assert wt.persist_env_tokens(path=path) is True
        with open(path) as f:
            content = f.read()
        assert "export WS_ACCESS_TOKEN=acc1" in content
        assert "export WS_REFRESH_TOKEN=ref1" in content
        assert os.stat(path).st_mode & 0o777 == 0o600

        assert wt.persist_env_tokens(path=path) is False

        os.environ["WS_ACCESS_TOKEN"] = "acc2"
        assert wt.persist_env_tokens(path=path) is True
        with open(path) as f:
            assert "acc2" in f.read()
    finally:
        if old[0] is None:
            os.environ.pop("WS_ACCESS_TOKEN", None)
        else:
            os.environ["WS_ACCESS_TOKEN"] = old[0]
        if old[1] is None:
            os.environ.pop("WS_REFRESH_TOKEN", None)
        else:
            os.environ["WS_REFRESH_TOKEN"] = old[1]
        os.unlink(path)


def test_cached_value_roundtrip():
    from datetime import datetime, timedelta, timezone

    store = _fresh_store()
    assert store.get_cached_value("RRSP") is None
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.set_cached_value("RRSP", 50000.0, ts)
    cached = store.get_cached_value("RRSP")
    assert cached["value"] == 50000.0
    assert cached["ts"] == ts


def test_ws_account_falls_back_to_cached_values():
    from datetime import datetime, timedelta, timezone
    from trader.account import WealthsimpleAccount

    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP"),
        WSAccountConfig(account_id="pers", label="Personal"),
    ]
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), accounts=accounts)
    ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store.set_cached_value("RRSP", 51234.0, ts)
    store.set_cached_value("Personal", 1987.0, ts)

    acc = WealthsimpleAccount(cfg, store)

    def boom():
        raise RuntimeError("no tokens")

    acc._client = boom
    values = acc.values()
    assert values["RRSP"] == 51234.0
    assert values["Personal"] == 1987.0
    assert acc.stale_age("RRSP") == "3h"
    assert acc.stale_age("Personal") == "3h"


def test_ws_account_raises_without_cache():
    from trader.account import WealthsimpleAccount

    cfg = ConfigStub(TradingConfig(mode="notify"))
    store = _fresh_store()
    acc = WealthsimpleAccount(cfg, store)

    def boom():
        raise RuntimeError("no tokens")

    acc._client = boom
    try:
        acc.values()
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_account_sizing_shows_stale_warning():
    from datetime import datetime, timedelta, timezone
    from trader.account import WealthsimpleAccount

    accounts = [WSAccountConfig(account_id="rrsp", label="RRSP")]
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), accounts=accounts)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    store.set_cached_value("RRSP", 50000.0, ts)

    acc = WealthsimpleAccount(cfg, store)

    def boom():
        raise RuntimeError("no tokens")

    acc._client = boom
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    rows = account_sizing(alert, cfg, acc, store)
    assert rows[0]["value"] == 50000.0
    assert rows[0]["contracts"] == 2
    assert any("cached account value" in w for w in rows[0]["warnings"])
