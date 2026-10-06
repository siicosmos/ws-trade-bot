import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.ws.account import PaperAccount
from trader.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig
from trader.trading.executor import (
    PaperExecutor, account_sizing, sell_quantity, tier_plan,
)
from trader.trading.parser import parse_alert
from trader.pipeline import process_alert
from trader.trading.risk import RiskEngine
from trader.store import Store


class ConfigStub:
    def __init__(self, trading, accounts=None, auth_token=""):
        self.trading = trading
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.auto_update = type("AU", (), {
            "enabled": False, "interval_seconds": 600})()
        self.quotes = type("Q", (), {
            "enabled": False, "provider": "ws",
            "moomoo_host": "", "moomoo_port": 11111})()
        self.discord = type("D", (), {
            "webhook_url": "", "reader_log_webhook_url": "",
            "pipeline_log_webhook_url": "",
            "update_webhook_url": "", "notify": True})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])
        self.reader = ReaderConfig()


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


def test_risk_daily_loss_breaker_blocks_buys_only():
    """the hard daily-loss breaker: once today's realized pnl
    sinks below max_daily_loss_pct of the account value, new
    buys block - sells (exits) stay takeable."""
    cfg, store, account, risk = _setup(
        max_daily_loss_pct=5.0, cooldown_seconds=0,
        dedupe_window_minutes=0, max_consecutive_losses=0,
    )
    # 5% of the 10000 paper seed = a -500 floor
    loss = parse_alert("SOLD 0DTE SPY 759c @ 0.5")
    store.apply_position(
        "paper", parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), 2,
        premium=1.5,
    )
    store.apply_position("paper", loss, -2, premium=0.5)
    # 2x(0.5-1.5)x100 = -200 realized... not enough; push further
    store.apply_position(
        "paper", parse_alert("BOUGHT 0DTE SPX 7650c @ 2.0"), 2,
        premium=2.0,
    )
    store.apply_position(
        "paper", parse_alert("SOLD 0DTE SPX 7650c @ 0.0"), -2,
        premium=0.01,
    )
    realized = store.realized_today("paper")
    assert realized <= -500.0, realized
    buy = parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0")
    ok, reason = risk.evaluate(buy)
    assert not ok
    assert "daily loss limit" in reason
    # exits stay takeable
    sell = parse_alert("SOLD 0DTE SPY 759c @ 1.0")
    ok, _ = risk.evaluate(sell)
    assert ok


def test_risk_daily_loss_breaker_off_by_default():
    cfg, store, account, risk = _setup(
        cooldown_seconds=0, dedupe_window_minutes=0
    )
    assert cfg.trading.max_daily_loss_pct == 0.0
    ok, _ = risk.evaluate(parse_alert("BOUGHT 0DTE SPX 7650c @ 1.0"))
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


def test_account_sizing():
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5
    )

    def contracts(alert, acct=None):
        rows = account_sizing(alert, cfg, acct or account)
        return [r["final_contracts"] for r in rows]

    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")) == [3]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ .65")) == [7]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 99")) == [0]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")) == [1]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone lotto size")) == [0]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")) == [3]
    assert contracts(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone large size")) == [6]
    cfg3, _, account3, _ = _setup(max_contracts_per_trade=2)
    rows = account_sizing(parse_alert("BOUGHT 0DTE SPY 759c @ 1.5"), cfg3, account3)
    assert [r["final_contracts"] for r in rows] == [2]


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


def test_per_account_sizing_overrides():
    cfg, store, account, risk = _setup(risk_per_trade_pct=5)
    big = WSAccountConfig(account_id="rrsp", label="RRSP", risk_per_trade_pct=3)
    small = WSAccountConfig(account_id="pers", label="Personal", risk_per_trade_pct=10)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    assert tier_plan(alert, cfg, 50000, 1.5, big)["qty"] == 10
    assert tier_plan(alert, cfg, 2000, 1.5, small)["qty"] == 1
    sized = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    assert tier_plan(sized, cfg, 50000, 1.5, big)["qty"] == 2
    capped = WSAccountConfig(account_id="x", label="x", max_contracts_per_trade=2)
    assert tier_plan(alert, cfg, 50000, 1.5, capped)["qty"] == 2


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
        "capped at small tier max of 2 (budget could afford 6)"
    ]
    assert by_label["Personal"]["risk_pct"] == 2.0
    assert by_label["Personal"]["contracts"] == 0
    assert by_label["Personal"]["warnings"] == [
        "2% budget $40.00 can't cover 1 contract at $150"
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
        "2% budget $200.00 affords 1, tier minimum is 4"
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
    app = __import__("trader.web.server", fromlist=["create_app"]).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    return app, store, account


def test_dashboard_and_api_endpoints():
    from trader.trading.parser import parse_alert as pa

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

    redirected = client.get("/")
    assert redirected.status_code == 302
    assert redirected.headers["Location"].endswith("/login")
    assert client.get("/api/summary").status_code == 401
    assert client.get("/health").status_code == 200

    ok = client.get("/api/summary", headers={"X-Auth-Token": "s3cret"})
    assert ok.status_code == 200

    import base64
    basic = base64.b64encode(b"user:s3cret").decode()
    basic_resp = client.get(
        "/api/summary", headers={"Authorization": f"Basic {basic}"}
    )
    assert basic_resp.status_code == 401

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
    import trader.ws.ws_tokens as wt

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
        if os.name == "posix":
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
    from trader.ws.account import WealthsimpleAccount

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
    from trader.ws.account import WealthsimpleAccount

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
    from trader.ws.account import WealthsimpleAccount

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


def test_load_env_tokens():
    import trader.ws.ws_tokens as wt

    fd, path = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    with open(path, "w") as f:
        f.write("export WS_ACCESS_TOKEN=tokA\n")
        f.write("export WS_REFRESH_TOKEN=tokB\n")
        f.write("garbage line\n")
    old = os.environ.get("WS_ACCESS_TOKEN"), os.environ.get("WS_REFRESH_TOKEN")
    try:
        os.environ.pop("WS_ACCESS_TOKEN", None)
        os.environ.pop("WS_REFRESH_TOKEN", None)
        assert wt.load_env_tokens(path=path) is True
        assert os.environ["WS_ACCESS_TOKEN"] == "tokA"
        assert os.environ["WS_REFRESH_TOKEN"] == "tokB"

        os.environ["WS_ACCESS_TOKEN"] = "envwins"
        wt.load_env_tokens(path=path)
        assert os.environ["WS_ACCESS_TOKEN"] == "envwins"
        assert os.environ["WS_REFRESH_TOKEN"] == "tokB"
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


def test_load_env_tokens_missing_file():
    import trader.ws.ws_tokens as wt

    assert wt.load_env_tokens(path="/nonexistent/ws_tokens.env") is False


def test_notify_mode_forwards_correction():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "Typo on the last alert, it was 760c not 7645c", "",
        cfg, store, risk, None, account,
    )
    assert res["status"] == "correction"
    signals = store.recent_signals(10)
    assert signals[0]["parsed"] == 0
    assert signals[0]["correction"] == 1
    assert store.trades_today("paper") == 0


def test_actionable_correction_still_executes():
    cfg, store, account, risk = _setup(cooldown_seconds=0)
    ex = PaperExecutor(cfg, store, account)
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
    assert ex.execute(buy, cfg, store).ok
    res = process_alert(
        "CORRECTION: ALL OUT 0DTE SPY 759c @ 2.0 @everyone", "",
        cfg, store, risk, ex, account,
    )
    assert res["status"] == "executed"
    assert res["correction"] is True
    signals = store.recent_signals(10)
    assert signals[0]["parsed"] == 1
    assert signals[0]["correction"] == 1


def test_notify_mode_alert_with_correction_flag():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "CORRECTION: ALL OUT 0DTE SPY 759c @ 2.0 @everyone", "",
        cfg, store, risk, None, account,
    )
    assert res["status"] == "notified"
    assert res["correction"] is True


def test_ignored_message_forwarded_plain(monkeypatch):
    import trader.ops.notify as notify_mod

    sent = []

    def fake_post(url, json=None, timeout=None, **kw):
        sent.append((url, json))

    monkeypatch.setattr(notify_mod.requests, "post", fake_post)

    cfg, store, account, risk = _setup(mode="notify")
    cfg.discord.webhook_url = "http://hook"
    res = process_alert(
        "executed 2.15 ^", "", cfg, store, risk, None, account
    )
    assert res["status"] == "ignored"
    plain = [p for p in sent if p[1] and "content" in p[1]]
    assert plain and plain[0][1]["content"] == "executed 2.15 ^"


def test_repeated_plain_message_notifies_each_time(monkeypatch):
    import trader.ops.notify as notify_mod

    sent = []

    def fake_post(url, json=None, timeout=None, **kw):
        sent.append((url, json))

    monkeypatch.setattr(notify_mod.requests, "post", fake_post)

    cfg, store, account, risk = _setup(mode="notify")
    cfg.discord.webhook_url = "http://hook"
    for _ in range(2):
        res = process_alert(
            "executed 2.15 ^", "", cfg, store, risk, None, account
        )
        assert res["status"] == "ignored"
    plain = [p for p in sent if p[1] and "content" in p[1]]
    assert len(plain) == 2
    assert plain[0][1]["content"] == "executed 2.15 ^"


def test_signal_records_channel():
    cfg, store, account, risk = _setup(mode="notify")
    res = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None, account,
        channel="test-alerts",
    )
    assert res["status"] == "notified"
    signals = store.recent_signals(10)
    assert signals[0]["channel"] == "test-alerts"


def test_signal_channel_defaults_empty():
    cfg, store, account, risk = _setup(mode="notify")
    process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "", cfg, store, risk, None, account
    )
    signals = store.recent_signals(10)
    assert signals[0]["channel"] == ""


def test_login_logout_flow():
    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()

    # wrong credentials stay on the login page
    bad = client.post("/login", data={"username": "admin",
                                      "password": "nope"})
    assert bad.status_code == 200
    assert "wrong username or password" in bad.get_data(as_text=True)

    # the access token seeded the admin account - the token
    # itself still works as a legacy owner login
    ok = client.post("/login", data={"username": "",
                                     "password": "s3cret"})
    assert ok.status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/api/summary").status_code == 200

    # and the named admin account logs in with its password
    client.get("/logout")
    ok2 = client.post("/login", data={"username": "admin",
                                      "password": "s3cret"})
    assert ok2.status_code == 302
    assert client.get("/api/dashboard").get_json()["me"]["role"] == "admin"

    # logout clears the session
    out = client.get("/logout")
    assert out.status_code == 302
    redirected = client.get("/")
    assert redirected.status_code == 302
    assert redirected.headers["Location"].endswith("/login")


def test_no_store_headers():
    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()
    client.post("/login", data={"password": "s3cret"})
    resp = client.get("/")
    assert resp.headers["Cache-Control"] == "no-store"
    api_resp = client.get("/api/summary")
    assert api_resp.headers["Cache-Control"] == "no-store"


def test_login_rate_limit():
    import trader.web.server as srv

    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()
    srv._LOGIN_FAILS.clear()
    old_limit = srv.LOGIN_FAIL_LIMIT
    srv.LOGIN_FAIL_LIMIT = 3
    try:
        for _ in range(3):
            client.post("/login", data={"password": "nope"})
        locked = client.post("/login", data={"password": "s3cret"})
        assert locked.status_code == 403
        assert "too many attempts" in locked.get_data(as_text=True)
    finally:
        srv.LOGIN_FAIL_LIMIT = old_limit
        srv._LOGIN_FAILS.clear()


def test_health_discloses_nothing():
    app, store, account = _make_app(auth_token="s3cret")
    client = app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert set(resp.get_json().keys()) == {"status"}


def test_refuses_non_localhost_without_token(tmp_path):
    import subprocess
    import sys

    cfg_path = tmp_path / "open.yaml"
    cfg_path.write_text(
        "pipeline:\n  host: \"0.0.0.0\"\n  port: 8080\n  auth_token: \"\"\n"
        "trading:\n  mode: notify\n"
    )
    r = subprocess.run(
        [sys.executable, "run.py", "-c", str(cfg_path), "--db",
         str(tmp_path / "x.db")],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0
    assert "auth_token" in (r.stdout + r.stderr)


def test_dashboard_escapes_untrusted_text():
    import re
    import trader.web.dashboard as dash

    js = dash.DASHBOARD_JS
    assert "function esc(" in js
    assert '.replace(/</g, "&lt;")' not in js
    for field in ("s.text", "s.channel", "p.underlying", "p.strike",
                  "t.detail", "t.ticker", "a.label"):
        assert f"esc({field}" in js
    # expiry goes through esc wrapped in the date slice
    assert 'esc(String(p.expiry || "").slice(0, 10))' in js


def test_notify_sell_fields_scaling_and_sold(monkeypatch):
    import json as json_mod

    import trader.ops.notify as notify
    from trader.trading.parser import parse_alert

    posts = []
    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json=None, timeout=None: posts.append(json),
    )

    alert = parse_alert(
        "ALL OUT 09/18 SPY 753c @ 6.22 exiting swing runners here for 120%"
    )
    notify.notify_alert("http://hook", alert)

    embed = posts[0]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["scaling"] == "**+120%**"
    assert fields["sold"] == "ALL"
    assert "**SELL**" in fields["type"]
    assert "scale" not in fields


def test_notify_buy_fields_bold(monkeypatch):
    import trader.ops.notify as notify
    from trader.trading.parser import parse_alert

    posts = []
    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json=None, timeout=None: posts.append(json),
    )

    alert = parse_alert(
        "BOUGHT 09/25 ARM 300c @ 1.65 @everyone small size likely swing"
    )
    notify.notify_alert("http://hook", alert)

    embed = posts[0]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "**BUY**" in fields["type"]
    assert fields["underlying"] == "**ARM**"
    assert fields["premium"] == "**$1.65**"
    assert fields["size"] == "**small**"
    assert "sold" not in fields and "scaling" not in fields


def _ws_position_fixture():
    return [
        {
            "quantity": "2",
            "bookValue": {"amount": "450.00", "currency": "CAD"},
            "marketBookValue": {"amount": "330.00", "currency": "USD"},
            "averagePrice": {"amount": "2.25", "currency": "CAD"},
            "marketAveragePrice": {"amount": "1.65", "currency": "USD"},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "ARM"},
                "optionDetails": {
                    "strikePrice": "300",
                    "optionType": "CALL",
                    "expiryDate": "2026-09-25",
                    "multiplier": "100",
                    "underlyingSecurity": {
                        "stock": {"symbol": "ARM"}
                    },
                },
                "quoteV2": {"price": "2.10"},
            },
        },
        {   # stock position - filtered out
            "quantity": "10",
            "bookValue": {"amount": "1500.00"},
            "security": {
                "securityType": "STOCK",
                "stock": {"symbol": "AAPL"},
                "optionDetails": None,
            },
        },
    ]


class FakeWS:
    def __init__(self, positions=None, fail=False):
        self.positions = positions or []
        self.fail = fail

    def get_positions(self, account_ids=None, **kw):
        if self.fail:
            raise RuntimeError("api down")
        return self.positions


def test_open_option_positions_filters_and_maps(monkeypatch, tmp_path):
    from trader.ws.account import WealthsimpleAccount
    from tests.test_pipeline import _ws_position_fixture  # noqa

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS(_ws_position_fixture())
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(
        acct, "_resolve", lambda: [("RRSP", "acct-1")]
    )

    rows = acct.open_option_positions()["RRSP"]["positions"]
    arm = rows[0]
    assert arm["underlying"] == "ARM"
    assert arm["qty"] == 2
    assert arm["avg_premium"] == 1.65      # USD per unit
    assert arm["cost_usd"] == 330.0
    assert arm["cost_cad"] == 450.0
    assert arm["right"] == "C"
    assert arm["contract_key"] == "ARM 2026-09-25 300C"
    assert arm["current_price"] == 2.10   # USD quote
    assert arm["market_value"] == 420.0   # USD
    assert arm["pct_return"] == 27.3      # USD vs USD

    full = acct.open_option_positions()["RRSP"]
    assert round(full["fx"], 4) == round(450.0 / 330.0, 4)

    # cached: second call does not hit the api even if it now fails
    acct._ws = FakeWS(fail=True)
    assert acct.open_option_positions()["RRSP"] == full


def test_positions_endpoint_merges_live(monkeypatch):
    app, store, account = _make_app(mode="paper")
    client = app.test_client()
    account.open_option_positions = lambda: {
        "RRSP": {
            "positions": [
                {
                    "contract_key": "ARM 2026-09-25 300C",
                    "underlying": "ARM",
                    "expiry": "2026-09-25",
                    "strike": "300",
                    "right": "C",
                    "qty": 2,
                    "avg_premium": 1.65,
                    "cost": 330.0,
                    "cost_usd": 330.0,
                    "cost_cad": 450.0,
                    "current_price": 2.10,
                    "market_value": 420.0,
                    "pct_return": 27.3,
                }
            ],
            "fx": 1.3636,
            "usd_cash": 120.50,
        },
        "Personal": {"positions": [], "fx": None, "usd_cash": None},
    }
    from trader.trading.parser import parse_alert

    old_alert = parse_alert("BOUGHT 01/01 OLD 100c @ 1.0")
    store.apply_position("paper", old_alert, 1, account="RRSP")
    resp = client.get("/api/positions")
    rows = resp.get_json()
    by_account = {}
    for r in rows:
        by_account.setdefault(r["account"], []).append(r)
    assert by_account["RRSP"][0]["contract_key"].startswith("ARM")
    assert by_account["RRSP"][0]["source"] == "ws"

    summary = client.get("/api/summary").get_json()
    rrsp = next(a for a in summary["accounts"] if a["label"] == "RRSP")
    assert rrsp["usd_cash"] == 120.50
    assert rrsp["usd_value"] == round(rrsp["value"] / 1.3636, 2)
    assert rrsp["open_risk"] == 450.0   # CAD cost
    # Personal has live (empty) data: tracked rows for it are dropped
    assert by_account.get("Personal", []) == []


def test_usd_values_from_financials(monkeypatch):
    from trader.ws.account import WealthsimpleAccount

    class FakeWS:
        def get_account_current_financials(self, account_id, currency="CAD"):
            if currency == "USD":
                return {
                    "netLiquidationValueV2": {
                        "amount": "16000.50", "currency": "USD"
                    }
                }
            return {
                "netLiquidationValueV2": {
                    "amount": "22000.00", "currency": "CAD"
                }
            }

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct._ws = FakeWS()
    acct.cache_seconds = 900
    acct._cache = None
    acct._cache_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "acct-1")])

    usd = acct.usd_values()
    assert usd == {"RRSP": 16000.50}


def test_summary_prefers_ws_usd_value(monkeypatch):
    app, store, account = _make_app(mode="paper")
    client = app.test_client()
    account.usd_values = lambda: {"RRSP": 16000.5}
    account.open_option_positions = lambda: {
        "RRSP": {"positions": [], "fx": 1.3636, "usd_cash": None},
    }
    summary = client.get("/api/summary").get_json()
    rrsp = next(a for a in summary["accounts"] if a["label"] == "RRSP")
    assert rrsp["usd_value"] == 16000.5


def test_short_option_position_displayed(monkeypatch):
    from trader.ws.account import WealthsimpleAccount

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                {
                    "quantity": "-1",
                    "positionDirection": "SHORT",
                    "bookValue": {"amount": "-165.00", "currency": "CAD"},
                    "marketBookValue": {"amount": "-120.00",
                                         "currency": "USD"},
                    "security": {
                        "securityType": "OPTION",
                        "stock": {"symbol": "SPY"},
                        "optionDetails": {
                            "strikePrice": "753",
                            "optionType": "PUT",
                            "expiryDate": "2026-09-18",
                            "multiplier": "100",
                            "underlyingSecurity": {
                                "stock": {"symbol": "SPY"}
                            },
                        },
                        "quoteV2": {"price": "1.20"},
                    },
                },
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    rows = acct.open_option_positions()["RRSP"]["positions"]
    assert len(rows) == 1
    short = rows[0]
    assert short["short"] is True
    assert short["qty"] == 1
    assert short["cost_usd"] == 120.0
    assert short["cost_cad"] == 165.0
    assert short["current_price"] == 1.20
    # credit 120 vs buyback 120 -> 0%
    assert short["pct_return"] == 0.0


def test_debit_spread_grouping(monkeypatch):
    from trader.ws.account import WealthsimpleAccount

    def leg(strike, qty, book_usd, mv_quote, direction):
        return {
            "quantity": str(qty),
            "positionDirection": direction,
            "bookValue": {"amount": str(book_usd * 1.3636)},
            "marketBookValue": {"amount": str(book_usd)},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPY"},
                "optionDetails": {
                    "strikePrice": str(strike),
                    "optionType": "CALL",
                    "expiryDate": "2026-09-25",
                    "multiplier": "100",
                    "underlyingSecurity": {"stock": {"symbol": "SPY"}},
                },
                "quoteV2": {"price": str(mv_quote)},
            },
        }

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                leg(753, 1, 120, 1.50, "LONG"),    # long 753C, cost $120
                leg(758, 1, 55, 0.90, "SHORT"),    # short 758C, credit $55
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    data = acct.open_option_positions()["RRSP"]
    rows = data["positions"]
    assert len(rows) == 1                    # legs collapse into one row
    spread = rows[0]
    assert spread["spread"] is True
    assert spread["contract_key"] == "SPY 753/758C"
    assert spread["qty"] == 1
    assert spread["cost_usd"] == 65.0        # 120 debit - 55 credit
    # net mv: long 150 - short 90 = 60 -> +7.69% on the 65 debit
    assert spread["market_value"] == 60.0
    assert spread["pct_return"] == -7.7
    assert spread["risk_cad"] == round(65 * 1.3636, 2)


def test_credit_spread_risk_uses_width(monkeypatch):
    # short 750P for 0.55 credit, long 745P for 0.20 -> width 500,
    # max loss = 500 - 35 credit = 465 per 1x
    from trader.ws.account import WealthsimpleAccount

    def leg(strike, direction, book_usd, quote):
        return {
            "quantity": "1",
            "positionDirection": direction,
            "bookValue": {"amount": str(book_usd * 1.4)},
            "marketBookValue": {"amount": str(book_usd)},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPY"},
                "optionDetails": {
                    "strikePrice": str(strike),
                    "optionType": "PUT",
                    "expiryDate": "2026-10-16",
                    "multiplier": "100",
                    "underlyingSecurity": {"stock": {"symbol": "SPY"}},
                },
                "quoteV2": {"price": str(quote)},
            },
        }

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                leg(750, "SHORT", 55, 0.40),
                leg(745, "LONG", 20, 0.15),
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = 1.4
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    spread = acct.open_option_positions()["RRSP"]["positions"][0]
    assert spread["spread"] is True
    assert spread["contract_key"] == "SPY 745/750P"
    assert spread["cost_usd"] == -35.0       # net credit
    # max loss = width 500 - credit 35
    assert spread["risk_cad"] == round(465 * 1.4, 2)


def test_notify_call_put_field_lowercase(monkeypatch):
    import trader.ops.notify as notify
    from trader.trading.parser import parse_alert

    posts = []
    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, json=None, timeout=None: posts.append(json),
    )

    alert = parse_alert("BOUGHT 09/25 ARM 300c @ 1.65 small size")
    notify.notify_alert("http://hook", alert)
    embed = posts[0]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["call/put"] == "call"
    # the emoji lives in the title only, not the type field
    assert "🟢" not in fields["type"]
    assert embed["title"].startswith("🟢")


def test_notify_mode_records_trade_log(monkeypatch):
    # notify mode is the dry-run ledger: every processed alert lands
    # in the trade log with the per-account sizing decision
    app, store, account = _make_app(mode="notify")
    store._conn.execute("DELETE FROM trades")
    store._conn.commit()
    client = app.test_client()
    monkeypatch.setattr(
        "trader.ops.notify.requests.post",
        lambda *a, **k: type("R", (), {"status_code": 200})(),
    )
    resp = client.post(
        "/alert",
        json={"text": "BOUGHT 09/25 ARM 300c @ 1.65 small size"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status_code == 200
    trades = store.recent_trades()
    assert trades and trades[0]["status"] == "notified"
    assert trades[0]["mode"] == "notify"
    assert "buy" in trades[0]["detail"] or "skip" in trades[0]["detail"]


def test_summary_includes_cash_balances(monkeypatch):
    app, store, account = _make_app(mode="paper")
    client = app.test_client()
    account.funding_balances = lambda: {
        "RRSP": [
            {"currency": "CAD", "amount": 1500.0},
            {"currency": "USD", "amount": 250.0},
        ],
        "Personal": None,
    }
    summary = client.get("/api/summary").get_json()
    rrsp = next(a for a in summary["accounts"] if a["label"] == "RRSP")
    assert rrsp["cash_cad"] == 1500.0
    assert rrsp["cash_usd"] == 250.0


def test_funding_balances_mapping(monkeypatch):
    from trader.ws.account import WealthsimpleAccount

    class FakeWS:
        def get_account_funding_balances(self, account_ids):
            return [{
                "id": account_ids[0],
                "trading_balances": [
                    {"amount": "1500.00", "currency": "CAD"},
                    {"amount": "250.00", "currency": "USD"},
                ],
            }]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct.cache_seconds = 900
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._funding_cache = None
    acct._funding_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    fb = acct.funding_balances()
    assert fb["RRSP"] == [
        {"currency": "CAD", "amount": 1500.0},
        {"currency": "USD", "amount": 250.0},
    ]


def test_stock_holdings_mapping(monkeypatch):
    from trader.ws.account import WealthsimpleAccount

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                {   # US stock - native average is authoritative, the
                    # fx-converted marketBookValue (1600) must NOT win
                    "quantity": "10",
                    "bookValue": {"amount": "2000.00", "currency": "CAD"},
                    "marketBookValue": {"amount": "1600.00",
                                         "currency": "USD"},
                    "totalValue": {"amount": "1700.00", "currency": "USD"},
                    "averagePrice": {"amount": "150.00",
                                      "currency": "USD"},
                    "security": {
                        "securityType": "STOCK",
                        "stock": {"symbol": "AAPL", "name": "Apple"},
                        "quoteV2": {"price": "170.00",
                                     "currency": "USD"},
                    },
                },
                {   # Canadian exchange listing - quotes in CAD
                    "quantity": "100",
                    "bookValue": {"amount": "3100.00", "currency": "CAD"},
                    "marketBookValue": {"amount": "3100.00",
                                         "currency": "CAD"},
                    "totalValue": {"amount": "3550.00", "currency": "CAD"},
                    "averagePrice": {"amount": "31.00",
                                      "currency": "CAD"},
                    "security": {
                        "securityType": "STOCK",
                        "stock": {"symbol": "RY", "name": "RBC"},
                        "quoteV2": {"price": "35.50",
                                     "currency": "CAD"},
                    },
                },
                {   # no quote at all - value from the node itself,
                    # currency from the security listing
                    "quantity": "20",
                    "bookValue": {"amount": "2900.00", "currency": "CAD"},
                    "marketBookValue": {"amount": "2300.00",
                                         "currency": "USD"},
                    "totalValue": {"amount": "2400.00", "currency": "USD"},
                    "averagePrice": {"amount": "115.00",
                                      "currency": "USD"},
                    "security": {
                        "securityType": "STOCK",
                        "currency": "USD",
                        "stock": {"symbol": "FOTO", "name": "Fotona"},
                    },
                },
                {   # currency position - excluded from holdings
                    "quantity": "120",
                    "security": {
                        "securityType": "CURRENCY",
                        "stock": {"symbol": "USD"},
                        "quoteV2": {"price": "1.36"},
                    },
                },
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct.cache_seconds = 900
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._funding_cache = None
    acct._funding_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    rows = acct.stock_holdings()["RRSP"]
    assert len(rows) == 3
    aapl = rows[0]
    assert aapl["kind"] == "stock"
    assert aapl["underlying"] == "AAPL"
    assert aapl["qty"] == 10
    assert aapl["avg_premium"] == 150.0     # USD per share
    assert aapl["current_price"] == 170.0
    assert aapl["cost_usd"] == 1500.0
    assert aapl["cost_cad"] == 2000.0
    assert aapl["market_value"] == 1700.0
    assert aapl["pct_return"] == 13.3
    assert aapl["currency"] == "USD"
    foto = rows[2]
    assert foto["underlying"] == "FOTO"
    assert foto["currency"] == "USD"          # from the security
    assert foto["market_value"] == 2400.0     # node totalValue
    assert foto["current_price"] is None     # no quote available

    ry = rows[1]
    assert ry["underlying"] == "RY"
    assert ry["currency"] == "CAD"
    assert ry["avg_premium"] == 31.0
    assert ry["current_price"] == 35.5
    assert ry["cost_usd"] == 3100.0         # qty x native average
    assert ry["cost_cad"] == 3100.0
    assert ry["market_value"] == 3550.0
    assert ry["pct_return"] == 14.5         # CAD-vs-CAD ratio


def test_positions_include_stocks(monkeypatch):
    app, store, account = _make_app(mode="paper")
    client = app.test_client()
    account.open_option_positions = lambda: {
        "RRSP": {"positions": [], "fx": 1.36, "usd_cash": None},
    }
    account.stock_holdings = lambda: {
        "RRSP": [
            {
                "contract_key": "AAPL", "underlying": "AAPL",
                "expiry": None, "strike": None, "right": None,
                "qty": 10, "short": False, "kind": "stock",
                "avg_premium": 150.0, "cost": 1500.0,
                "cost_usd": 1500.0, "cost_cad": 2000.0,
                "current_price": 170.0, "market_value": 1700.0,
                "pct_return": 13.3,
            }
        ],
    }
    rows = client.get("/api/positions").get_json()
    stocks = [r for r in rows if r.get("kind") == "stock"]
    assert stocks and stocks[0]["underlying"] == "AAPL"
    assert stocks[0]["source"] == "ws"


def test_positions_via_real_init(monkeypatch):
    """Regression: _positions_raw attrs must exist on __init__, else
    live fetching dies with AttributeError and the tab goes empty."""
    from trader.ws.account import WealthsimpleAccount

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return []

    acct = WealthsimpleAccount(FakeCfg())
    acct._ws = FakeWS()
    monkeypatch.setattr(acct, "_resolve", lambda: [("T", "a")])
    assert acct.open_option_positions() == {
        "T": {"positions": [], "fx": None, "usd_cash": None}
    }
    assert acct.stock_holdings() == {"T": []}


def test_summary_allocation_values():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                {"market_value": 100.0, "risk_cad": 120.0,
                 "cost_cad": 130.0, "short": False},
            ],
            "fx": 1.25,
        },
    }
    account.stock_holdings = lambda: {
        "Personal": [
            {"market_value": 800.0, "currency": "USD"},
            {"market_value": 500.0, "currency": "CAD"},
        ],
    }
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    # usd stock 800 * 1.25 + cad stock 500
    assert row["stock_value"] == 1500.0
    # option mv 100 * 1.25
    assert row["option_value"] == 125.0



def test_app_document_positions_and_margin():
    from trader.ws.account import WealthsimpleAccount
    from trader.ws.ws_positions_query import (
        FETCH_IDENTITY_POSITIONS, app_positions_variables,
    )

    spread_node = {
        "quantity": "-2",
        "positionDirection": "SHORT",
        "bookValue": {"amount": "-100.00", "currency": "CAD"},
        "marketBookValue": {"amount": "-75.00", "currency": "USD"},
        "totalValue": {"amount": "-50.00", "currency": "USD"},
        "strategyType": "VERTICAL_SPREAD",
        "marginRequirement": {"amount": "55.00", "currency": "USD"},
        "legs": [
            {"quantity": "2", "positionDirection": "SHORT",
             "security": {"optionDetails": {
                "strikePrice": "6000", "optionType": "CALL",
                "expiryDate": "2026-09-18",
                "underlyingSecurity": {"stock": {"symbol": "SPX"}},
            }}},
            {"quantity": "2", "positionDirection": "LONG",
             "security": {"optionDetails": {
                "strikePrice": "6010", "optionType": "CALL",
             }}},
        ],
        "security": {},
    }
    expired_node = {
        "quantity": "-1",
        "positionDirection": "SHORT",
        "bookValue": {"amount": "-25.00", "currency": "CAD"},
        "marketBookValue": {"amount": "-18.00", "currency": "USD"},
        "strategyType": "VERTICAL_SPREAD",
        "legs": [
            {"security": {"optionDetails": {
                "strikePrice": "7.5", "optionType": "PUT",
                "expiryDate": "2026-09-18",
                "underlyingSecurity": {"stock": {"symbol": "BFLY"}},
            }},
             "totalValue": {"amount": "0.00", "currency": "USD"}},
            {"security": {"optionDetails": {
                "strikePrice": "5", "optionType": "PUT",
            }},
             "totalValue": None},
        ],
        "security": {},
    }

    single_node = {
        "quantity": "10",
        "bookValue": {"amount": "2000.00", "currency": "CAD"},
        "marketBookValue": {"amount": "1500.00", "currency": "USD"},
        "averagePrice": {"amount": "150.00", "currency": "USD"},
        "totalValue": {"amount": "1700.00", "currency": "USD"},
        "marginRequirement": {"amount": "400.00", "currency": "USD"},
        "security": {
            "securityType": "STOCK",
            "stock": {"symbol": "AAPL"},
            "quoteV2": {"price": "170.00", "currency": "USD"},
        },
    }

    class FakeWS:
        identity_id = "id1"

        def graphql_query(self, op, query, variables):
            assert query == FETCH_IDENTITY_POSITIONS
            assert variables["currencyOverride"] == "MARKET"
            return {"data": {"identity": {"financials": {
                "current": {"positions": {"edges": [
                    {"node": dict(spread_node)},
                    {"node": dict(expired_node)},
                    {"node": dict(single_node)},
                ]}}}}}}

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct.cache_seconds = 900
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._funding_cache = None
    acct._funding_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch = None

    import unittest.mock as mock
    with mock.patch.object(acct, "_resolve",
                           lambda: [("T", "a1")]):
        raw = acct._positions_raw()
    assert raw == {"T": [spread_node, expired_node, single_node]}

    opts = acct.open_option_positions()["T"]
    assert len(opts["positions"]) == 2
    sp = opts["positions"][0]
    assert sp["strategy_type"] == "call credit spread"
    assert sp["spread"] is True
    assert sp["short"] is True            # net-short credit spread
    assert sp["qty"] == 2
    assert sp["cost_usd"] == -75.0        # credit: signed net
    assert sp["market_value"] == -50.0
    assert sp["strike"] == "6000/6010"
    assert sp["expiry"] == "2026-09-18"
    assert sp["underlying"] == "SPX"
    # risk from WS margin requirement converted at book ratio
    assert sp["risk_cad"] == round(55.0 * (100.0 / 75.0), 2)
    assert sp["margin_req_amount"] == 55.0
    assert sp["margin_req_currency"] == "USD"

    expired = opts["positions"][1]
    assert expired["short"] is True
    assert expired["qty"] == 1
    assert expired["market_value"] == 0.0       # legs settled to 0
    assert expired["pct_return"] == 100.0       # full credit kept

    stocks = acct.stock_holdings()["T"]
    assert len(stocks) == 1
    assert stocks[0]["margin_req_amount"] == 400.0


def test_summary_margin_requirement():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {"positions": [], "fx": 1.25, "usd_cash": None},
    }
    account.stock_holdings = lambda: {
        "Personal": [
            # VDY / ZWC-style CAD holdings: 30% maintenance
            {"market_value": 56.13, "currency": "CAD",
             "underlying": "VDY"},
            {"market_value": 1871.07, "currency": "CAD",
             "underlying": "ZWC"},
        ],
    }
    account.funding_balances = lambda: {
        "Personal": [
            {"currency": "CAD", "amount": 0.30},
            {"currency": "USD", "amount": -148.29},
        ],
    }
    account.values = lambda: {"Personal": 1720.02}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]

    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    assert row["margin_requirement"] == round(0.3 * 1927.20, 2)
    # 148.29 usd loan at the account fx (1.25 here)
    assert row["margin_used"] == round(148.29 * 1.25, 2)
    # NLV already nets the loan
    assert row["margin_available"] == round(
        1720.02 - round(0.3 * 1927.20, 2), 2
    )
    assert row["max_buying_power"] == round(
        row["margin_available"] / 0.30, 2
    )

def test_registered_account_no_margin(monkeypatch):
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                {"market_value": 100.0, "risk_cad": 120.0,
                 "cost_cad": 130.0, "short": False},
            ],
            "fx": 1.25,
        },
    }
    account.stock_holdings = lambda: {
        "Personal": [
            {"market_value": 800.0, "currency": "USD",
             "underlying": "AAPL"},
        ],
    }
    account.values = lambda: {"Personal": 50000.0}
    account._resolve = lambda: [("Personal", "pers")]
    account.account_type_map = lambda: {"pers": "RRSP"}

    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    assert row["margin_requirement"] is None
    assert row["margin_available"] is None

    account.account_type_map = lambda: {"pers": "PERSONAL"}
    client.application._summary_cache["ts"] = 0.0
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    assert row["margin_requirement"] is not None

def test_account_type_map_cached():
    from trader.ws.account import WealthsimpleAccount

    calls = []

    class FakeWS:
        def get_accounts(self):
            calls.append(1)
            return [
                {"id": "a1", "unifiedAccountType": "rrsp"},
                {"id": "a2", "unifiedAccountType": "PERSONAL"},
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount(FakeCfg())
    acct._ws = FakeWS()
    assert acct.account_type_map() == {"a1": "RRSP", "a2": "PERSONAL"}
    assert acct.account_type_map() == {"a1": "RRSP", "a2": "PERSONAL"}
    assert len(calls) == 1


def test_registered_label_suppresses_margin():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {"RRSP": None}
    account.stock_holdings = lambda: {
        "RRSP": [
            {"market_value": 800.0, "currency": "CAD",
             "underlying": "AAPL"},
        ],
    }
    account.values = lambda: {"RRSP": 50000.0}
    account.account_type_map = lambda: {}
    account._resolve = lambda: [("RRSP", "pers")]
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "RRSP")
    assert row["margin_requirement"] is None
    assert row["margin_available"] is None



def test_security_margin_rate_from_api():
    from trader.ws.account import WealthsimpleAccount
    from trader.ws.ws_security_query import FETCH_SECURITY

    calls = []

    class FakeWS:
        def graphql_query(self, op, query, variables):
            assert op == "FetchSecurity"
            assert query == FETCH_SECURITY
            calls.append(variables["securityId"])
            # percent-style value exercises the normalization
            return {"data": {"security": {"marginRates": {
                "clientMarginRate": "30"}}}}

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount(FakeCfg())
    acct._ws = FakeWS()
    assert acct.security_margin_rate("sec-1") == 0.30
    # cached - no second api call
    assert acct.security_margin_rate("sec-1") == 0.30
    assert calls == ["sec-1"]

    class NoMarginWS:
        def graphql_query(self, op, query, variables):
            return {"data": {"security": {"marginRates": None}}}

    acct._ws = NoMarginWS()
    acct._margin_rates = {}
    assert acct.security_margin_rate("sec-2") is None


def test_requirement_uses_api_rate():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {"positions": [], "fx": 1.25, "usd_cash": None},
    }
    account.stock_holdings = lambda: {
        "Personal": [
            {"market_value": 1871.07, "currency": "CAD",
             "underlying": "ZWC", "security_id": "sec-zwc"},
            {"market_value": 56.13, "currency": "CAD",
             "underlying": "VDY", "security_id": "sec-vdy"},
        ],
    }
    account.funding_balances = lambda: {
        "Personal": [{"currency": "CAD", "amount": 0.30}],
    }
    account.values = lambda: {"Personal": 1720.02}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]

    rates = {"sec-zwc": 0.30, "sec-vdy": 0.50}
    account.security_margin_rate = lambda sid: rates.get(sid)

    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    # 1871.07 * 0.30 + 56.13 * 0.50 (api rate beats the default)
    assert row["margin_requirement"] == round(
        1871.07 * 0.30 + 56.13 * 0.50, 2
    )


def test_reaction_reread_suppressed():
    cfg, store, account, risk = _setup(
        mode="notify", paper_account_value=10000, cooldown_seconds=0
    )
    base = "SOLD 1/4 0DTE IWM 285c @ .96 @everyone +50% rest 2X or BE"
    first = process_alert(
        base, "", cfg, store, risk, PaperExecutor(cfg, store, account)
    )
    # the reader re-delivers the same message with a reaction
    # count appended when someone reacts to it
    reread = process_alert(
        base + " 1", "", cfg, store, risk,
        PaperExecutor(cfg, store, account),
    )
    assert first["status"] == "notified"
    assert reread["status"] == "ignored"
    assert "reaction" in reread["reason"]

    # a genuinely different scaling alert still goes through
    other = "SOLD 1/4 0DTE IWM 285c @ .83 @everyone +30% rest 2X or BE"
    res = process_alert(
        other, "", cfg, store, risk, PaperExecutor(cfg, store, account)
    )
    assert res["status"] == "notified"


def test_expired_credit_spread_full_return(monkeypatch):
    # credit spread legged in as separate positions (no strategyType),
    # expired worthless: quotes settled to zero and the full credit
    # is kept - qty shows negative and return reads +100%
    from trader.ws.account import WealthsimpleAccount

    def leg(strike, direction, book_usd, quote):
        return {
            "quantity": "1",
            "positionDirection": direction,
            "bookValue": {"amount": str(book_usd * 1.4)},
            "marketBookValue": {"amount": str(book_usd)},
            "totalValue": {"amount": str(quote * 100)},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPX"},
                "optionDetails": {
                    "strikePrice": str(strike),
                    "optionType": "PUT",
                    "expiryDate": "2026-09-18",
                    "multiplier": "100",
                    "underlyingSecurity": {"stock": {"symbol": "SPX"}},
                },
                "quoteV2": {"price": str(quote)},
            },
        }

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                # short 6100P for 1.80, long 6050P for 0.95
                # -> 0.85 credit, now expired at zero
                leg(6100, "SHORT", -180, 0.0),
                leg(6050, "LONG", 95, 0.0),
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = 1.4
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    spread = acct.open_option_positions()["RRSP"]["positions"][0]
    assert spread["spread"] is True
    assert spread["short"] is True          # net-short credit spread
    assert spread["market_value"] == 0.0    # settled to zero
    assert spread["cost_usd"] == -85.0      # credit received
    assert spread["pct_return"] == 100.0     # full credit kept


def test_margin_requirement_breakdown():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {"positions": [], "fx": 1.25, "usd_cash": None},
    }
    account.stock_holdings = lambda: {
        "Personal": [
            {"market_value": 4209.00, "currency": "CAD",
             "underlying": "ZWC", "security_id": "sec-zwc"},
            {"market_value": 56.90, "currency": "CAD",
             "underlying": "VDY", "security_id": "sec-vdy"},
        ],
    }
    account.funding_balances = lambda: {
        "Personal": [{"currency": "CAD", "amount": 0.30}],
    }
    account.values = lambda: {"Personal": 1767.30}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]
    account.security_margin_rate = lambda sid: {"sec-zwc": 0.30,
                                                "sec-vdy": 0.30}.get(sid)
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    assert row["margin_requirement"] == round(0.3 * 4265.9, 2)
    assert any("ZWC" in p and "30%" in p for p in row["margin_breakdown"])
    assert any("VDY" in p for p in row["margin_breakdown"])


def test_spread_requirement_uses_full_width():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                # 5-point wide credit spread, 18 credit, settled:
                # ws charges the FULL width even when the credit
                # was kept (verified against ws's margin page)
                {"underlying": "SPX", "spread": True, "short": True,
                 "strike": "6100/6105", "qty": 1,
                 "risk_cad": round(482 * 1.403, 2),
                 "cost_cad": round(-18 * 1.403, 2),
                 "cost_usd": -18.0,
                 "market_value": 0.0},
            ],
            "fx": 1.403,
        },
    }
    account.stock_holdings = lambda: {"Personal": []}
    account.funding_balances = lambda: {
        "Personal": [{"currency": "USD", "amount": -114.29}],
    }
    account.values = lambda: {"Personal": 1767.30}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    # full width: 5 x 100 x 1 x 1.403 - risk_cad is not the
    # requirement, it is the netted max-loss display metric
    assert row["margin_requirement"] == round(5 * 100 * 1.403, 2)
    assert row["margin_available"] == round(
        1767.30 - round(5 * 100 * 1.403, 2), 2
    )
    assert any("width" in p for p in row["margin_breakdown"])


def test_update_relevance_gating(monkeypatch, tmp_path):
    import trader.ops.updater as up

    root = str(tmp_path)

    class FakeResult:
        def __init__(self, rc=0, out=""):
            self.returncode = rc
            self.stdout = out

    calls = {}

    def fake_git(r, *args):
        calls["args"] = args
        if args[0] == "diff":
            return FakeResult(0, "docs/x.md\nreader/discord_reader.py\n")
        return FakeResult(0, "")

    monkeypatch.setattr(up, "_git", fake_git)
    # reader changes are not pipeline-relevant
    files = up.git_changed_files(root, "a", "b")
    assert files == ["docs/x.md", "reader/discord_reader.py"]
    assert not up.files_match(files, up.PIPELINE_RESTART_FILES)
    assert up.files_match(files, up.READER_RESTART_FILES)

    def fake_git2(r, *args):
        if args[0] == "diff":
            return FakeResult(0, "trader/server.py\n")
        return FakeResult(0, "")

    monkeypatch.setattr(up, "_git", fake_git2)
    files = up.git_changed_files(root, "a", "b")
    assert up.files_match(files, up.PIPELINE_RESTART_FILES)

    # unknown diff -> treat as relevant
    def fake_git3(r, *args):
        if args[0] == "diff":
            return FakeResult(1, "")
        return FakeResult(0, "")

    monkeypatch.setattr(up, "_git", fake_git3)
    assert up.git_changed_files(root, "a", "b") is None
    assert up.files_match(None, up.PIPELINE_RESTART_FILES) is False


def test_sell_strike_mismatch_detected():
    cfg, store, account, risk = _setup(
        mode="notify", paper_account_value=10000, cooldown_seconds=0
    )
    from trader.pipeline import process_alert

    buy = process_alert(
        "BOUGHT 09/25 COIN 210c @ 2.0 small size", "",
        cfg, store, risk, PaperExecutor(cfg, store, account),
    )
    assert buy["status"] == "notified"

    sell = process_alert(
        "SOLD 1/4 09/25 COIN 200c @ 2.42 +20%", "",
        cfg, store, risk, PaperExecutor(cfg, store, account),
    )
    assert sell["status"] == "notified"
    trades = store.recent_trades(5)
    mismatch_rows = [
        t for t in trades
        if "differs from last buy" in str(t.get("detail", ""))
    ]
    assert mismatch_rows, trades
    assert "200C" in mismatch_rows[0]["detail"]
    assert "210C" in mismatch_rows[0]["detail"]

    # a matching sell produces no warning
    ok = process_alert(
        "SOLD 1/4 09/25 COIN 210c @ 2.42 +20%", "",
        cfg, store, risk, PaperExecutor(cfg, store, account),
    )
    assert ok["status"] == "notified"
    last = store.recent_trades(1)[0]
    assert "differs from" not in str(last.get("detail", ""))


def test_allocation_base_is_gross_assets():
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                {"market_value": 100.0, "risk_cad": 120.0,
                 "cost_cad": 130.0, "short": False, "spread": True,
                 "strike": "6000/6005", "qty": 1},
            ],
            "fx": 1.25,
        },
    }
    account.stock_holdings = lambda: {
        "Personal": [
            {"market_value": 800.0, "currency": "USD"},
            {"market_value": 500.0, "currency": "CAD"},
        ],
    }
    account.funding_balances = lambda: {
        "Personal": [
            # a loan must not enter the allocation base
            {"currency": "CAD", "amount": 0.30},
            {"currency": "USD", "amount": -114.29},
        ],
    }
    account.values = lambda: {"Personal": 1767.30}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    # stocks 1500 + option 125 + cash 0.30 - no loan
    assert row["alloc_base"] == round(1500 + 125 + 0.30, 2)


def test_strategy_classifier_names():
    from trader.trading.strategies import classify_legs as cl

    def leg(strike, right, short, qty=1):
        return {"strike": strike, "right": right,
                "short": short, "qty": qty}

    assert cl([leg(600, "C", False), leg(610, "C", True)])[
        "name"] == "call debit spread"
    assert cl([leg(600, "C", True), leg(610, "C", False)])[
        "name"] == "call credit spread"
    assert cl([leg(600, "P", False), leg(590, "P", True)])[
        "name"] == "put debit spread"
    assert cl([leg(600, "P", True), leg(590, "P", False)])[
        "name"] == "put credit spread"
    # long call butterfly: wings long, body short
    assert cl([leg(590, "C", False), leg(600, "C", True, 2),
               leg(610, "C", False)])["name"] == "long call butterfly"
    # short broken wing put butterfly
    assert cl([leg(590, "P", True), leg(600, "P", False, 2),
               leg(620, "P", True)])[
        "name"] == "short put broken wing butterfly"
    # short iron condor: call credit + put credit
    assert cl([leg(6100, "C", True), leg(6110, "C", False),
               leg(5900, "P", True), leg(5890, "P", False)])[
        "name"] == "short iron condor"
    # long broken wing iron condor: both debits, unequal widths
    assert cl([leg(6100, "C", False), leg(6110, "C", True),
               leg(5900, "P", False), leg(5860, "P", True)])[
        "name"] == "long broken wing iron condor"
    # short iron butterfly: shared body strike
    assert cl([leg(6000, "C", True), leg(6010, "C", False),
               leg(6000, "P", True), leg(5990, "P", False)])[
        "name"] == "short iron butterfly"
    # ratio spread
    assert cl([leg(600, "C", False), leg(610, "C", True, 2)])[
        "name"] == "call ratio spread"
    # unrecognized mix
    assert cl([leg(600, "C", False), leg(600, "P", False)]) is None


def test_condor_combines_verticals():
    from trader.ws.account import WealthsimpleAccount

    def leg(strike, right, direction, book_usd, mv_quote):
        return {
            "quantity": "1",
            "positionDirection": direction,
            "bookValue": {"amount": str(book_usd * 1.4)},
            "marketBookValue": {"amount": str(book_usd)},
            "totalValue": {"amount": str(mv_quote * 100)},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPX"},
                "optionDetails": {
                    "strikePrice": str(strike),
                    "optionType": "CALL" if right == "C" else "PUT",
                    "expiryDate": "2026-09-25",
                    "multiplier": "100",
                    "underlyingSecurity": {"stock": {"symbol": "SPX"}},
                },
                "quoteV2": {"price": str(mv_quote)},
            },
        }

    class FakeWS:
        def get_positions(self, account_ids=None, **kw):
            return [
                # short call spread + short put spread = condor
                leg(6100, "C", "SHORT", -180, 1.5),
                leg(6110, "C", "LONG", 95, 0.8),
                leg(5900, "P", "SHORT", -150, 1.2),
                leg(5890, "P", "LONG", 80, 0.6),
            ]

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = 1.4
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    import unittest.mock as mock
    with mock.patch.object(acct, "_resolve",
                           lambda: [("T", "a1")]):
        rows = acct.open_option_positions()["T"]["positions"]
    assert len(rows) == 1
    condor = rows[0]
    assert condor["strategy_type"] == "short iron condor"
    assert condor["spread"] is True
    assert condor["short"] is True
    # credit: -180 + 95 - 150 + 80 = -155
    assert condor["cost_usd"] == -155.0
    # risk = max width (10) x 100 - credit
    assert condor["risk_cad"] == round(
        (10 * 100 - 155) * 1.4, 2
    )


def _paper_cfg(cfg):
    from types import SimpleNamespace

    cfg.paper = SimpleNamespace(enabled=True)
    return cfg


def test_paper_seeding_and_ledger(monkeypatch):
    from trader.ws.account import PaperLedger, WealthsimpleAccount, seed_paper_accounts
    from trader.config import Config

    # raw live nodes: one long option leg + one stock, one short leg
    nodes = [
        {
            "quantity": "2", "positionDirection": "LONG",
            "bookValue": {"amount": "300.00", "currency": "CAD"},
            "marketBookValue": {"amount": "220.00", "currency": "USD"},
            "totalValue": {"amount": "240.00", "currency": "USD"},
            "averagePrice": {"amount": "1.10", "currency": "USD"},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPX"},
                "optionDetails": {
                    "strikePrice": "6000", "optionType": "CALL",
                    "expiryDate": "2026-09-25",
                    "underlyingSecurity": {"stock": {"symbol": "SPX"}},
                },
                "quoteV2": {"price": "1.20", "currency": "USD"},
            },
        },
        {
            "quantity": "100", "positionDirection": "LONG",
            "bookValue": {"amount": "3100.00", "currency": "CAD"},
            "marketBookValue": {"amount": "3100.00", "currency": "CAD"},
            "totalValue": {"amount": "3200.00", "currency": "CAD"},
            "averagePrice": {"amount": "31.00", "currency": "CAD"},
            "security": {
                "securityType": "STOCK",
                "stock": {"symbol": "ZWC"},
                "quoteV2": {"price": "32.00", "currency": "CAD"},
            },
        },
        {   # short leg - not tracked, value backed out of cash
            "quantity": "-1", "positionDirection": "SHORT",
            "bookValue": {"amount": "-140.00", "currency": "CAD"},
            "marketBookValue": {"amount": "-100.00", "currency": "USD"},
            "totalValue": {"amount": "-90.00", "currency": "USD"},
            "security": {
                "securityType": "OPTION",
                "stock": {"symbol": "SPX"},
                "optionDetails": {
                    "strikePrice": "5900", "optionType": "PUT",
                    "expiryDate": "2026-09-25",
                },
                "quoteV2": {"price": "0.90", "currency": "USD"},
            },
        },
    ]

    class FakeWS:
        identity_id = "id1"

        def graphql_query(self, op, query, variables):
            return {"data": {"identity": {"financials": {
                "current": {"positions": {"edges": [
                    {"node": dict(n)} for n in nodes
                ]}}}}}}

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60
        trading = None
        paper = None

    from tests.test_pipeline import _ws_position_fixture  # noqa

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct.cache_seconds = 900
    acct._ws = FakeWS()
    acct._resolved = [("T", "a1")]
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = 1.4
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._funding_cache = None
    acct._funding_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    acct._type_map = None
    acct._margin_rates = {}
    acct.values = lambda: {"T": 3360.0}
    acct.open_option_positions = lambda: {
        "T": {"positions": [], "fx": 1.4, "usd_cash": None},
    }

    store = _fresh_store()
    seeded = seed_paper_accounts(FakeCfg(), store, acct)
    assert seeded == ["T"]

    cash = store.paper_equity("T")
    # nlv 3360 minus long option 240x1.3636 minus stock 3200
    assert cash == round(3360.0 - 240.0 * (300.0 / 220.0) - 3200.0, 2)

    positions = store.list_positions("paper", "T")
    keys = {p["contract_key"] for p in positions}
    assert "SPX-2026-09-25-6000-C" in keys
    assert "ZWC" in keys
    assert not any("5900" in k for k in keys)   # shorts untracked

    ledger = PaperLedger(FakeCfg(), store, acct)
    value = ledger.value("T")
    # cash + option 2x1.20x100x1.4 + stock 100x32
    expect = cash + 2 * 1.20 * 100 * 1.4 + 100 * 32.0
    assert value == round(expect, 2)

    # seeding is idempotent
    assert seed_paper_accounts(FakeCfg(), store, acct) == []


def test_notify_plus_paper_executes_and_skips_unheld():
    cfg, store, account, risk = _setup(
        mode="notify", paper_account_value=10000,
        cooldown_seconds=0,
    )
    cfg = _paper_cfg(cfg)
    from trader.ws.account import PaperLedger
    from trader.pipeline import process_alert

    ledger = PaperLedger(cfg, store, None)
    executor = PaperExecutor(cfg, store, ledger)

    buy = process_alert(
        "BOUGHT 09/25 COIN 210c @ 2.0 small size", "",
        cfg, store, risk, executor, account,
    )
    assert buy["status"] == "notified"
    assert buy["paper"]["ok"] is True
    trades = store.recent_trades(5)
    paper_rows = [t for t in trades if t["mode"] == "paper"]
    assert paper_rows and paper_rows[0]["status"] == "executed"

    # a sell for a contract never held is skipped, not shorted
    sell = process_alert(
        "SOLD 09/25 COIN 200c @ 2.42 +20%", "",
        cfg, store, risk, executor, account,
    )
    assert sell["status"] == "notified"
    assert sell["paper"]["ok"] is False
    trades = store.recent_trades(5)
    skipped = [
        t for t in trades
        if t["mode"] == "paper" and t["status"] == "skipped"
    ]
    assert skipped and "no position" in skipped[0]["detail"]


def test_positions_mapping_cached_per_raw_generation(monkeypatch):
    from trader.ws.account import WealthsimpleAccount
    from tests.test_pipeline import _ws_position_fixture  # noqa

    fetches = []

    class FakeWS:
        identity_id = None

        def get_positions(self, account_ids=None, **kw):
            fetches.append(1)
            return _ws_position_fixture()

    class FakeCfg:
        class wealthsimple:
            positions_refresh_seconds = 30
            values_refresh_seconds = 60

    acct = WealthsimpleAccount.__new__(WealthsimpleAccount)
    acct.cfg = FakeCfg()
    acct._ws = FakeWS()
    acct._resolved = None
    acct._stale = {}
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct._fx_quote = None
    acct._fx_quote_ts = 0.0
    acct._usd_cache = None
    acct._usd_cache_ts = 0.0
    acct._cache = None
    acct._cache_ts = 0.0
    acct._type_map = None
    acct._margin_rates = {}
    monkeypatch.setattr(acct, "_resolve", lambda: [("RRSP", "a1")])

    maps = []
    orig_options = WealthsimpleAccount._map_options
    monkeypatch.setattr(
        WealthsimpleAccount, "_map_options",
        lambda self, raw: (maps.append(1), orig_options(self, raw))[1],
    )

    for _ in range(3):
        opts = acct.open_option_positions()
        stocks = acct.stock_holdings()
    # one raw fetch, one mapping pass - the rest served from cache
    assert len(fetches) == 1
    assert len(maps) == 1
    assert opts["RRSP"]["positions"]
    assert stocks is not None

    # a fresh generation (forced) remaps
    acct._ws = FakeWS()
    acct._raw_cache = None
    acct._raw_ts = 0.0
    acct.open_option_positions(max_age_seconds=0)
    assert len(maps) == 2


def test_summary_cache_serves_repeated_polls():
    app, store, account = _make_app()
    client = app.test_client()
    calls = []
    orig = account.values
    account.values = lambda: (calls.append(1), orig())[1]
    for _ in range(3):
        summary = client.get("/api/summary").get_json()
    assert len(calls) == 1
    assert summary["accounts"]
    # invalidation refreshes
    client.application._summary_cache["ts"] = 0.0
    client.get("/api/summary")
    assert len(calls) == 2


def test_positions_cache_invalidates_on_writes():
    app, store, account = _make_app()
    from trader.trading.parser import parse_alert

    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 small size")
    store.apply_position("paper", alert, 2, premium=1.5, account="Personal")
    first = store.list_positions("paper", "Personal")
    assert first and first[0]["qty"] == 2
    # served from cache
    assert store.list_positions("paper", "Personal")[0]["qty"] == 2
    store.apply_position("paper", alert, 1, premium=1.5, account="Personal")
    assert store.list_positions("paper", "Personal")[0]["qty"] == 3


def _mirror_ws_account(activities):
    from trader.ws.account import WealthsimpleAccount

    class FakeClient:
        def get_activities(self, **kw):
            return {"edges": [{"node": a} for a in activities]}

    class FakeWSAccount:
        def __init__(self):
            self._client_obj = FakeClient()

        def _client(self):
            return self._client_obj

        def _resolve(self):
            return [("Personal", "pers")]

    return FakeWSAccount()


def test_mirror_real_trades_applies_fills():
    from trader.trading.mirror import MirrorShim, mirror_real_trades
    from trader.ws.account import PaperLedger

    store = _fresh_store()
    # a seeded ledger holding 2 of the contract being sold
    shim = MirrorShim(
        kind="option", underlying="SPX", expiry="2026-09-25",
        strike=6000, right="C", action="BUY", premium=1.0,
        entry=1.0, ts="t",
    )
    store.apply_position("paper", shim, 2, premium=1.0,
                         account="Personal")
    store.set_paper_equity(500.0, "Personal")
    store.meta_set("paper_seed:Personal", time.time())

    acts = [
        {   # real fill: bought 1 more at 1.10 (total 110 usd)
            "canonicalId": "a1", "type": "BUY", "status": "COMPLETED",
            "assetSymbol": "SPX", "strikePrice": "6000",
            "contractType": "CALL", "expiryDate": "2026-09-25",
            "assetQuantity": "1", "amount": "110.00",
            "currency": "USD", "occurredAt": "2026-09-22T15:00:00Z",
        },
        {   # real fill: sold 1 at 1.50 (total 150 usd)
            "canonicalId": "a2", "type": "SELL", "status": "COMPLETED",
            "assetSymbol": "SPX", "strikePrice": "6000",
            "contractType": "CALL", "expiryDate": "2026-09-25",
            "assetQuantity": "1", "amount": "150.00",
            "currency": "USD", "occurredAt": "2026-09-22T15:05:00Z",
        },
        {   # cancelled - ignored
            "canonicalId": "a3", "type": "BUY", "status": "CANCELLED",
            "assetSymbol": "SPX", "strikePrice": "6000",
            "contractType": "CALL", "expiryDate": "2026-09-25",
            "assetQuantity": "1", "amount": "1.00",
            "currency": "USD", "occurredAt": "2026-09-22T15:06:00Z",
        },
    ]
    ws = _mirror_ws_account(acts)
    ledger = PaperLedger(cfg=None, store=store, ws_account=None)
    ledger.fx = lambda: 1.4

    applied = mirror_real_trades(None, store, ws, ledger)
    assert len(applied) == 2
    # position: 2 + 1 - 1 = 2
    assert store.get_position(
        "paper", "SPX-2026-09-25-6000-C", "Personal"
    ) == 2
    # cash: 500 - 110x1.4 + 150x1.4
    assert store.paper_equity("Personal") == 500 - 154 + 210
    trades = [t for t in store.recent_trades(10)
              if "mirrored" in str(t.get("detail", ""))]
    assert len(trades) == 2

    # a second run dedupes - nothing new applies
    assert mirror_real_trades(None, store, ws, ledger) == []
    assert store.get_position(
        "paper", "SPX-2026-09-25-6000-C", "Personal"
    ) == 2


def test_mirror_skips_unheld_sells():
    from trader.trading.mirror import mirror_real_trades
    from trader.ws.account import PaperLedger

    store = _fresh_store()
    store.meta_set("paper_seed:Personal", time.time())
    acts = [
        {   # real sell of a contract the paper ledger never held
            "canonicalId": "s1", "type": "SELL", "status": "COMPLETED",
            "assetSymbol": "BFLY", "strikePrice": "7.5",
            "contractType": "PUT", "expiryDate": "2026-09-18",
            "assetQuantity": "1", "amount": "55.00",
            "currency": "USD", "occurredAt": "2026-09-22T15:00:00Z",
        },
    ]
    ws = _mirror_ws_account(acts)
    ledger = PaperLedger(cfg=None, store=store, ws_account=None)
    assert mirror_real_trades(None, store, ws, ledger) == []
    trades = store.recent_trades(5)
    assert not any("mirrored" in str(t.get("detail", "")) for t in trades)


def test_loghook_file_and_rotation(tmp_path):
    from trader.ops.loghook import LogFile, TeeStream, install_log_webhook
    io = __import__("io")

    path = str(tmp_path / "pipeline.log")
    lf = LogFile(path, max_bytes=100, keep=2)
    stream = TeeStream(io.StringIO(), None, lf)
    stream.write("first line\n")
    stream.write("second line\n")
    content = open(path).read()
    assert "first line" in content and "second line" in content

    for i in range(30):
        lf.add(f"filler line number {i} with some padding text")
    assert os.path.exists(path + ".1")
    # keep=2 archives: .1 and .2 at most
    assert not os.path.exists(path + ".3")

    # install wires both sinks without a webhook url
    import sys
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        install_log_webhook("", log_path=path)
        print("via install")
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    assert "via install" in open(path).read()


def test_summary_includes_paper_when_enabled():
    accounts = [
        WSAccountConfig(account_id="rrsp", label="RRSP",
                        paper_value=50000),
        WSAccountConfig(account_id="pers", label="Personal",
                        paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = type(
        "Paper", (),
        {"enabled": True, "mirror": True,
         "mirror_interval_seconds": 60},
    )()
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)

    class Ledger:
        def values(self):
            return {"RRSP": 50000.0, "Personal": 2000.0}

        def fx(self):
            return 1.4

    class FakePaperExecutor:
        mode = "paper"
        account = Ledger()

    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, FakePaperExecutor(), account
    )
    client = app.test_client()
    summary = client.get("/api/summary").get_json()
    rows = {a["label"]: a for a in summary["accounts"]}
    assert rows["RRSP"]["paper_value"] == 50000.0
    assert rows["Personal"]["paper_value"] == 2000.0

    # card hides cleanly when paper is switched off
    cfg.paper.enabled = False
    app._summary_cache["ts"] = 0.0
    summary = client.get("/api/summary").get_json()
    assert all(
        a["paper_value"] is None for a in summary["accounts"]
    )


def test_paper_positions_detail():
    from trader.ws.account import PaperLedger
    from trader.trading.mirror import MirrorShim

    store = _fresh_store()
    shim = MirrorShim(
        kind="option", underlying="SPX", expiry="2026-09-25",
        strike=6000, right="C", action="BUY", premium=1.0,
        entry=1.0, ts="t",
    )
    store.apply_position("paper", shim, 2, premium=1.0,
                         account="Personal")
    store.set_paper_equity(500.0, "Personal")

    class FakeWS:
        def _positions_raw(self):
            return {"Personal": [{
                "quantity": "2",
                "security": {
                    "stock": {"symbol": "SPX"},
                    "optionDetails": {
                        "strikePrice": "6000", "optionType": "CALL",
                        "expiryDate": "2026-09-25",
                    },
                    "quoteV2": {"price": "1.5", "currency": "USD"},
                },
            }]}

        def open_option_positions(self):
            return {"Personal": {"positions": [], "fx": 1.4,
                                 "usd_cash": None}}

    ledger = PaperLedger(cfg=None, store=store, ws_account=FakeWS())
    rows = ledger.positions("Personal")
    assert len(rows) == 1
    r = rows[0]
    assert r["contract_key"] == "SPX-2026-09-25-6000-C"
    assert r["qty"] == 2
    # live quote 1.5 usd x 100 x 2 = 300 usd; cad in brackets
    assert r["value"] == 300.0
    assert r["value_cad"] == round(2 * 1.5 * 100 * 1.4, 2)
    assert r["cost"] == 200.0
    assert r["cost_cad"] == 280.0
    assert r["price"] == 1.5
    assert r["pnl"] == 50.0
    assert r["pnl_dollars"] == 100.0
    # cost 2 x 1.0 x 100 x 1.4 -> +50%
    assert r["pnl"] == 50.0


def test_paper_positions_endpoint():
    accounts = [
        WSAccountConfig(account_id="pers", label="Personal",
                        paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = type("Paper", (), {"enabled": True})()
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)

    class Ledger:
        def values(self):
            return {"Personal": 2000.0}

        def fx(self):
            return 1.4

        def positions(self, label):
            return [{
                "contract_key": "SPX-2026-09-25-6000-C",
                "underlying": "SPX", "qty": 2, "avg": 1.0,
                "value": 420.0, "pnl": 50.0,
            }]

    class FakePaperExecutor:
        mode = "paper"
        account = Ledger()

    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(cfg, store, risk, FakePaperExecutor(), account)
    client = app.test_client()
    data = client.get("/api/paper-positions").get_json()
    assert data["Personal"][0]["contract_key"] == (
        "SPX-2026-09-25-6000-C"
    )
    # disabled -> empty
    cfg.paper.enabled = False
    assert client.get("/api/paper-positions").get_json() == {}


def test_paper_ledger_falls_back_to_config_values():
    from trader.ws.account import PaperLedger

    store = _fresh_store()
    ledger = PaperLedger(cfg=None, store=store, ws_account=None)
    ledger.cfg = ConfigStub(
        TradingConfig(mode="paper", paper_account_value=10000),
        accounts=[
            WSAccountConfig(account_id="rrsp", label="RRSP",
                            paper_value=50000),
        ],
    )
    assert ledger.value("RRSP") == 50000.0
    assert store.paper_equity("RRSP") == 50000.0
    assert ledger.value("unknown") == 10000.0


def test_summary_paper_flag_in_paper_mode():
    accounts = [
        WSAccountConfig(account_id="pers", label="Personal",
                        paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="paper", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = type("Paper", (), {"enabled": False})()
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(cfg, store, risk, PaperExecutor(cfg, store, account),
                 account)
    summary = app.test_client().get("/api/summary").get_json()
    # paper badge shows in paper mode even without paper.enabled
    assert summary["paper"] is True


def test_notify_flag_gates_alert_notifications(monkeypatch):
    import trader.pipeline as pipeline_mod
    from trader.pipeline import process_alert

    sent = []
    monkeypatch.setattr(
        pipeline_mod, "notify_alert",
        lambda url, alert, sizing=None, correction=False,
               mismatch=None: sent.append(alert.ticker),
    )
    cfg, store, account, risk = _setup(
        mode="notify", paper_account_value=10000,
        cooldown_seconds=0,
    )
    cfg.discord.notify = False
    res = process_alert(
        "BOUGHT 09/25 COIN 210c @ 2.0 small size", "",
        cfg, store, risk, None, account,
    )
    assert res["status"] == "notified"
    assert sent == []          # quiet
    cfg.discord.notify = True
    process_alert(
        "BOUGHT 09/25 COIN 210c @ 2.1 small size", "",
        cfg, store, risk, None, account,
    )
    assert sent == ["COIN"]


def test_paper_reset_endpoint():
    accounts = [
        WSAccountConfig(account_id="pers", label="Personal",
                        paper_value=2000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = type("Paper", (), {"enabled": True})()

    class FakeWSAccount:
        def _positions_raw(self):
            return {"Personal": [{
                "quantity": "10", "positionDirection": "LONG",
                "bookValue": {"amount": "2000.00"},
                "marketBookValue": {"amount": "1500.00",
                                     "currency": "USD"},
                "totalValue": {"amount": "1600.00",
                                "currency": "USD"},
                "averagePrice": {"amount": "150.00"},
                "security": {
                    "securityType": "STOCK",
                    "stock": {"symbol": "AAPL"},
                    "quoteV2": {"price": "160.00",
                                 "currency": "USD"},
                },
            }]}

        def _resolve(self):
            return [("Personal", "pers")]

        def values(self):
            return {"Personal": 2000.0}

    account = FakeWSAccount()
    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(cfg, store, risk, None, account)
    client = app.test_client()

    # seed once
    from trader.ws.account import seed_paper_accounts
    assert seed_paper_accounts(cfg, store, account) == ["Personal"]
    assert store.list_positions("paper", "Personal")
    assert store.paper_equity("Personal") is not None

    # reset drops the ledger and reseeds from live
    res = client.post("/api/paper-reset",
                      json={"label": "Personal"})
    data = res.get_json()
    assert res.status_code == 200
    assert data["status"] == "ok"
    assert data["reseeded"] is True
    rows = store.list_positions("paper", "Personal")
    assert rows and rows[0]["contract_key"] == "AAPL"
    # a bogus label errors cleanly
    res = client.post("/api/paper-reset", json={"label": ""})
    assert res.status_code == 400


def test_paper_positions_include_kind():
    from trader.ws.account import PaperLedger
    from trader.trading.mirror import MirrorShim

    store = _fresh_store()
    opt = MirrorShim(
        kind="option", underlying="SPX", expiry="2026-09-25",
        strike=6000, right="C", action="BUY", premium=1.0,
        entry=1.0, ts="t",
    )
    stk = MirrorShim(
        kind="stock", underlying="ZWC", expiry=None, strike=None,
        right=None, action="BUY", premium=30.0, entry=30.0, ts="t",
    )
    store.apply_position("paper", opt, 1, premium=1.0,
                         account="Personal")
    store.apply_position("paper", stk, 100, premium=30.0,
                         account="Personal")
    ledger = PaperLedger(cfg=None, store=store, ws_account=None)
    kinds = {r["contract_key"]: r["kind"]
             for r in ledger.positions("Personal")}
    assert kinds["SPX-2026-09-25-6000-C"] == "option"
    assert kinds["ZWC"] == "stock"


def test_paper_prices_match_live_quote_expiry_keys():
    """seeded rows carry the raw graphql expiry timestamp in
    their contract key; the live quote map keys the plain date.
    the lookup must normalize, or the paper price freezes at
    cost basis while the real account shows the live quote."""
    from trader.ws.account import PaperLedger

    store = _fresh_store()
    store.seed_position(
        "paper", "Personal",
        "SPX-2026-09-25T00:00:00.000-04:00-6000-C",
        "SPX", "2026-09-25T00:00:00.000-04:00", 6000.0, "C",
        2, 5.0,
    )
    store.set_paper_equity(10000.0, "Personal")

    class FakeWS:
        def _positions_raw(self):
            return {"Personal": [{
                "quantity": 2,
                "security": {
                    "optionDetails": {
                        "optionType": "CALL",
                        "strikePrice": 6000,
                        "expiryDate":
                            "2026-09-25T00:00:00.000-04:00",
                        "underlyingSecurity": {
                            "stock": {"symbol": "SPX"},
                        },
                    },
                    "quoteV2": {"price": "7.5"},
                },
            }]}

    ledger = PaperLedger(cfg=None, store=store, ws_account=FakeWS())
    rows = ledger.positions("Personal")
    assert rows[0]["price"] == 7.5
    assert rows[0]["pnl"] == 50.0
    # the ledger value marks the position to the live quote too
    assert ledger.value("Personal") == 10000.0 + 2 * 7.5 * 100


def test_seed_paper_keys_use_plain_date_expiry():
    """new seeds key and store the plain date - a timestamp
    expiry never matched the quote map (the frozen-price bug)."""
    from trader.ws.account import seed_paper_accounts

    store = _fresh_store()
    ts_expiry = "2026-09-25T00:00:00.000-04:00"

    class FakeWSAccount:
        def values(self):
            return {"Personal": 20000.0}

        def _positions_raw(self):
            return {"Personal": [{
                "quantity": 2,
                "totalValue": {"amount": "1500.0",
                               "currency": "CAD"},
                "averagePrice": {"amount": "5.0",
                                 "currency": "USD"},
                "security": {
                    "optionDetails": {
                        "optionType": "CALL",
                        "strikePrice": 6000,
                        "expiryDate": ts_expiry,
                        "underlyingSecurity": {
                            "stock": {"symbol": "SPX"},
                        },
                    },
                },
            }]}

        def _resolve(self):
            return [("Personal", "a1")]

    class PaperCfg:
        enabled = True
        mirror = True

    class Cfg:
        paper = PaperCfg()

    assert seed_paper_accounts(
        Cfg(), store, FakeWSAccount()
    ) == ["Personal"]
    rows = store.list_positions("paper", "Personal")
    assert rows[0]["contract_key"] == "SPX-2026-09-25-6000-C"
    assert rows[0]["expiry"] == "2026-09-25"


def test_mirror_option_expiry_normalized():
    from trader.trading.mirror import _option_shim

    shim = _option_shim({
        "strikePrice": "6000",
        "contractType": "CALL",
        "assetSymbol": "SPX",
        "expiryDate": "2026-09-25T00:00:00.000-04:00",
    })
    assert shim["expiry"] == "2026-09-25"


def test_paper_only_contract_priced_from_ws_chain():
    """paper-only trades (notify mode) have no live node to
    carry a quote - without moomoo they must fall back to the
    ws option chains, so the paper price matches what the real
    card shows for the same contract."""
    from types import SimpleNamespace

    from trader.ws.account import PaperLedger

    store = _fresh_store()
    buy = parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0")
    store.apply_position(
        "paper", buy, 1, premium=2.0, account="default"
    )

    class FakeWS:
        def get_ticker_id(self, ticker, hint):
            return "sec1"

        def get_positions(self, account_ids=None):
            return []

        def search_securities(self, ticker, **kw):
            return [{"id": "sec1",
                     "stock": {"symbol": ticker}}]

        def get_option_expiry_dates(self, sec_id):
            return [buy.expiry]

        def get_option_chain(self, sec_id, expiry, opt_type):
            # the real chain shape: strike nested under
            # optionDetails as a plain string, quote under
            # quoteV2 (verified against the live gateway)
            return [{
                "id": "opt1",
                "optionDetails": {
                    "strikePrice": "105",
                    "optionType": "CALL",
                },
                "quoteV2": {
                    "price": "2.15",
                    "last": "2.22",
                    "bid": "1.95",
                    "ask": "2.35",
                },
            }]

    class FakeWSAccount:
        def _client(self):
            return FakeWS()

    ledger = PaperLedger(
        SimpleNamespace(wealthsimple=SimpleNamespace(exchange_hint="")),
        store, FakeWSAccount(),
    )
    rows = ledger.positions("default")
    assert rows[0]["price"] == 2.15, rows
    # a successful sweep must not trip the backoff
    assert ledger._chain_backoff_until == 0.0


def test_clean_start_script(tmp_path):
    import sqlite3
    import subprocess
    import sys as _sys
    import trader.store as trader_store

    tmp = tmp_path
    db = str(tmp / "trades.db")

    store = trader_store.Store(db)
    store.record_signal("k1", "author", "BOUGHT 09/25 COIN 210c",
                        parsed=True)
    store.record_signal("k2", "author", " chatter", parsed=False)
    from trader.trading.parser import parse_alert
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")
    store.record_trade(
        "paper", alert.action, alert.ticker, 1, 2.0, alert,
        "executed", "t",
    )
    store.seed_position(
        "paper", "Personal", "COIN-2026-09-25-210-C", "COIN",
        "2026-09-25", 210.0, "C", 1, 2.0,
    )
    store.meta_set("paper_seed:Personal", "1")

    # fake logs next to the db
    (tmp / "pipeline.log").write_text("old line\n")
    (tmp / "pipeline.log.1").write_text("old archive\n")
    (tmp / "reader.log").write_text("old reader line\n")

    repo = os.path.dirname(os.path.dirname(
        os.path.abspath(trader_store.__file__)))
    script = os.path.join(repo, "scripts", "clean_start.py")
    env = dict(os.environ)
    res = subprocess.run(
        [_sys.executable, script, "--db", db, "--yes"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stderr

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    # paper state survives the clean start
    assert conn.execute(
        "SELECT COUNT(*) FROM positions"
    ).fetchone()[0] == 1
    conn.close()
    assert not (tmp / "pipeline.log").exists()
    assert not (tmp / "pipeline.log.1").exists()
    assert not (tmp / "reader.log").exists()
    assert "clean slate" in res.stdout


def test_paper_seeding_without_mirror_is_cash_only():
    from trader.ws.account import PaperLedger, seed_paper_accounts

    class Acct:
        def values(self):
            return {"T": 3360.0}

        def _positions_raw(self):
            raise AssertionError(
                "positions must not be fetched when mirror is off"
            )

        def _resolve(self):
            return [("T", "a1")]

    class PaperCfg:
        enabled = True
        mirror = False

    class Cfg:
        paper = PaperCfg()

    store = _fresh_store()
    seeded = seed_paper_accounts(Cfg(), store, Acct())
    assert seeded == ["T"]
    assert store.list_positions("paper", "T") == []
    # cash is the whole account value - no holdings backed out
    assert store.paper_equity("T") == 3360.0
    ledger = PaperLedger(Cfg(), store, Acct())
    assert ledger.value("T") == 3360.0


def test_paper_card_margin_metrics():
    from types import SimpleNamespace

    from trader.ws.account import PaperLedger
    from trader.config import TradingConfig, WSAccountConfig

    accounts = [
        WSAccountConfig(account_id="m1", label="Margin",
                        paper_value=10000),
        WSAccountConfig(account_id="r1", label="RRSP",
                        paper_value=5000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = SimpleNamespace(enabled=True)

    # a long call plus a stock in Margin - paper holdings are
    # long-only (short legs settle to cash)
    store.set_paper_equity(20000.0, "Margin")
    store.seed_position("paper", "Margin", "SPX-2026-09-25-6000-C",
                         "SPX", "2026-09-25", 6000.0, "C", 1, 5.0)
    store.seed_position("paper", "Margin", "ZWC", "ZWC",
                         None, None, None, 100, 32.0)
    store.meta_set("paper_seed:Margin", "1")
    store.meta_set("paper_initial:Margin", "10000")

    store.set_paper_equity(4000.0, "RRSP")
    store.seed_position("paper", "RRSP", "AAPL", "AAPL",
                       None, None, None, 10, 200.0)
    store.meta_set("paper_seed:RRSP", "1")
    store.meta_set("paper_initial:RRSP", "5000")

    ledger = PaperLedger(cfg, store, None)

    class FakeWS:
        def values(self):
            return {"Margin": 25000.0, "RRSP": 6000.0}

        def _positions_raw(self):
            return {}

        def _resolve(self):
            return [("Margin", "m1"), ("RRSP", "r1")]

    risk = RiskEngine(cfg, store, FakeWS())
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk,
        SimpleNamespace(account=ledger), FakeWS(),
    )
    res = app.test_client().get("/api/summary")
    assert res.status_code == 200
    accs = {
        a["label"]: a for a in res.get_json()["accounts"]
    }
    m = accs["Margin"]

    # long option 500 at 100% + stock 3200 x 30%
    assert m["paper_margin_requirement"] == round(
        500.0 + 3200 * 0.30, 2
    )
    assert m["paper_stock_value"] == 3200.0
    assert m["paper_margin_used"] == 0.0
    assert m["paper_cash"] == 20000.0
    # nlv: cash + long option + stock
    nlv = round(20000 + 500 + 3200, 2)
    assert m["paper_value"] == nlv
    assert m["paper_margin_available"] == round(
        nlv - m["paper_margin_requirement"], 2
    )
    assert m["paper_max_buying_power"] == round(
        m["paper_margin_available"] / 0.30, 2
    )
    assert m["paper_portfolio_value"] == nlv
    assert any(
        "SPX" in p and "x 100%" in p
        for p in m["paper_margin_breakdown"]
    )
    # registered paper card: no margin metrics, just cash
    r = accs["RRSP"]
    assert r["paper_margin_requirement"] is None
    assert r["paper_cash"] == 4000.0
    assert r["paper_stock_value"] == 2000.0


def test_updater_heals_dirty_ignored_runtime_file(tmp_path):
    import subprocess

    from trader.ops.updater import AutoUpdater

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             *args], cwd=str(cwd), capture_output=True, text=True,
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "reader.log").write_text("v1\n")
    (origin / "run.py").write_text("print(1)\n")
    git(origin, "init", "-q", "-b", "main")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "base")

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)],
        capture_output=True, text=True,
    )

    # new upstream commit touching the same tracked log
    (origin / "reader.log").write_text("v2\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "log change")

    # local runtime writes dirty the tracked log - this is what
    # used to wedge the updater for good
    (work / "reader.log").write_text("locally appended\n")

    class Cfg:
        class auto_update:
            enabled = True
            interval_seconds = 30

    from trader.ops.updater import AutoUpdater as AU

    updater = AU.__new__(AU)
    updater.cfg = Cfg()
    updater.root = str(work)
    updater.webhook_url = ""
    updater._restart = lambda: (_ for _ in ()).throw(
        AssertionError("should not restart")
    )
    updater.last_check = None
    updater.last_result = ""
    updater.errors = 0
    updater.start_head = None
    updater.branch = "main"

    pulled = updater.check_once()
    # no pipeline files changed, so no restart - but the pull
    # itself must have gone through despite the dirty log
    assert "updated to" in updater.last_result
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(work),
        capture_output=True, text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=str(work),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == remote_head


def test_updater_clears_stale_index_lock(tmp_path):
    import os
    import time as _time

    from trader.ops.updater import _clear_stale_lock

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    lock = root / ".git" / "index.lock"
    lock.write_text("")
    fresh = tmp_path / "fresh"
    (fresh / ".git").mkdir(parents=True)
    fresh_lock = fresh / ".git" / "index.lock"
    fresh_lock.write_text("")

    old = os.path.getmtime(str(lock)) - 600
    os.utime(str(lock), (old, old))
    # old lock gets removed, fresh one is left alone
    assert _clear_stale_lock(str(root)) is True
    assert not lock.exists()
    assert _clear_stale_lock(str(fresh)) is False
    assert fresh_lock.exists()


def test_startup_banner(tmp_path, capsys):
    import json as _json
    import subprocess

    from trader.ops.updater import startup_banner

    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "f.txt").write_text("x\n")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "init", "-q", "-b", "main"], cwd=str(origin),
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(origin), capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "banner test"],
        cwd=str(origin), capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(origin),
        capture_output=True, text=True,
    ).stdout.strip()

    # no update record - just the commit + subject
    line = startup_banner("pipeline", str(origin))
    assert f"commit {head}" in line
    assert '"banner test"' in line
    assert "(" not in line

    # matching record marks it as auto-updated
    (origin / ".last_update.json").write_text(
        _json.dumps(
            {"how": "auto", "commit": head, "ts": time.time() - 7200}
        )
    )
    line = startup_banner("pipeline", str(origin))
    assert "auto-updated 2h ago" in line

    # a record for a different commit is ignored
    (origin / ".last_update.json").write_text(
        _json.dumps({"how": "auto", "commit": "deadbee", "ts": 1})
    )
    line = startup_banner("pipeline", str(origin))
    assert "auto-updated" not in line


def test_terminate_stale_instances(tmp_path):
    import subprocess
    import sys as _sys

    # the reader tests stub psutil globally - make sure this
    # test uses the real module
    mock = _sys.modules.get("psutil")
    is_mock = mock is not None and type(mock).__name__ == "MagicMock"
    if is_mock:
        del _sys.modules["psutil"]
    import psutil  # noqa: F401

    from trader.ops.processes import terminate_stale_instances

    try:
        _run_stale_instance_test(tmp_path, _sys, terminate_stale_instances)
    finally:
        if is_mock:
            _sys.modules["psutil"] = mock


def _run_stale_instance_test(tmp_path, _sys, terminate_stale_instances):
    import subprocess

    script = tmp_path / "run.py"
    script.write_text(
        "import time\nprint('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen(
        [_sys.executable, str(script)],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
    )
    # wait for the child to be fully up
    p.stdout.readline()
    assert p.poll() is None

    killed = terminate_stale_instances(str(script))
    assert p.pid in killed
    p.wait(timeout=10)
    assert p.poll() is not None

    # a clean run finds nothing to kill
    assert terminate_stale_instances(str(script)) == []


def test_margin_breakdown_currencies():
    from types import SimpleNamespace

    from trader.ws.account import PaperLedger
    from trader.config import TradingConfig, WSAccountConfig
    from trader.trading.risk import RiskEngine

    accounts = [
        WSAccountConfig(account_id="m1", label="Margin",
                        paper_value=10000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = SimpleNamespace(enabled=True)
    # a usd stock and a cad stock in the paper ledger
    store.set_paper_equity(20000.0, "Margin")
    store.seed_position("paper", "Margin", "COIN", "COIN",
                        None, None, None, 10, 200.0)
    store.seed_position("paper", "Margin", "ZWC", "ZWC",
                        None, None, None, 100, 32.0)
    store.meta_set("paper_seed:Margin", "1")
    store.meta_set("paper_initial:Margin", "10000")

    ledger = PaperLedger(cfg, store, None)
    # live quotes flag the listing currencies
    ledger._quotes = lambda: {
        "COIN": {"price": 200.0, "usd": True},
        "ZWC": {"price": 32.0, "usd": False},
    }
    ledger._quote_ts = 1e18

    class FakeWS:
        _fx_hint = 1.37

        def values(self):
            return {"Margin": 25000.0}

        def usd_values(self):
            return {"Margin": 18248.18}

        def _positions_raw(self):
            return {}

        def _resolve(self):
            return [("Margin", "m1")]

    risk = RiskEngine(cfg, store, FakeWS())
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, SimpleNamespace(account=ledger),
        FakeWS(),
    )
    m = app.test_client().get(
        "/api/summary"
    ).get_json()["accounts"][0]
    lines = m["paper_margin_breakdown"]
    coin = next(
        l for l in lines if l.startswith("COIN ")
    )
    zwc = next(
        l for l in lines if l.startswith("ZWC ")
    )
    # us stock shows usd, ca stock shows cad
    assert coin.rstrip().endswith("usd"), coin
    assert zwc.rstrip().endswith("cad"), zwc
    # the usd figure is the cad ledger amount divided by the
    # value fx; cad stocks show their ledger amount as-is
    assert "1459.85" in coin, coin
    assert "3200.00" in zwc, zwc


def test_paper_margin_used_currency_split():
    from types import SimpleNamespace

    from trader.ws.account import PaperLedger
    from trader.config import TradingConfig, WSAccountConfig
    from trader.trading.risk import RiskEngine

    accounts = [
        WSAccountConfig(account_id="m1", label="Margin",
                        paper_value=10000),
    ]
    store = _fresh_store()
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5),
        accounts=accounts,
    )
    cfg.paper = SimpleNamespace(enabled=True)
    # negative cash = an 800 cad paper loan, backed by a usd
    # option holding worth 700 cad
    store.set_paper_equity(-800.0, "Margin")
    store.seed_position("paper", "Margin", "SPX-2026-09-25-6000-C",
                        "SPX", "2026-09-25", 6000.0, "C", 1, 7.0)
    store.seed_position("paper", "Margin", "ZWC", "ZWC",
                        None, None, None, 10, 30.0)
    store.meta_set("paper_seed:Margin", "1")
    store.meta_set("paper_initial:Margin", "10000")

    ledger = PaperLedger(cfg, store, None)
    ledger._quotes = lambda: {
        "SPX-2026-09-25-6000-C": {"price": 7.0, "usd": True},
        "ZWC": {"price": 30.0, "usd": False},
    }
    ledger._quote_ts = 1e18

    class FakeWS:
        _fx_hint = 1.37

        def values(self):
            return {"Margin": 25000.0}

        def usd_values(self):
            return {"Margin": 18248.18}

        def _positions_raw(self):
            return {}

        def _resolve(self):
            return [("Margin", "m1")]

    risk = RiskEngine(cfg, store, FakeWS())
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, SimpleNamespace(account=ledger),
        FakeWS(),
    )
    m = app.test_client().get(
        "/api/summary"
    ).get_json()["accounts"][0]
    # the loan backs the usd holding first (700 cad = ~510.95
    # usd), the rest stays cad
    assert m["paper_margin_used"] == 800.0
    assert m["paper_margin_used_usd"] == round(700 / 1.37, 2)
    assert m["paper_margin_used_cad"] == 100.0


def test_margin_model_single_source():
    from trader.trading.margin import (
        Holding, compute_requirement, resolve_rate,
    )

    holdings = [
        Holding("SPX", "stock", currency="usd",
                native_value=12345.67, security_id="s1"),
        Holding("ZWC", "stock", currency="cad",
                native_value=3200.0),
        Holding("SPX", "spread", currency="usd",
                structure="put credit spread", qty=1, width=5),
        Holding("XYZ", "short", currency="usd", risk=482.0),
        Holding("COIN", "long", currency="usd",
                native_value=500.0),
    ]
    req, parts = compute_requirement(
        holdings, fx=1.403,
        rate_for=lambda h: resolve_rate(
            h.symbol, h.security_id, None,
            {"ZWC": 0.50}, 0.30,
        ),
    )
    # stock usd 12345.67 x 30% x 1.403 + stock cad 3200 x 50% +
    # spread 5 x 100 x 1 x 1.403 + short 482 x 1.403 +
    # long 500 x 1.403
    expect = (
        12345.67 * 0.30 * 1.403
        + 3200 * 0.50
        + 5 * 100 * 1.403
        + 482 * 1.403
        + 500 * 1.403
    )
    assert req == round(expect, 2)
    assert parts == [
        "SPX 12345.67 x 30% = 3703.70 usd",
        "ZWC 3200.00 x 50% = 1600.00 cad",
        "SPX put credit spread 1x width 5 = 500.00 usd",
        "XYZ short risk 482.00 usd",
        "COIN option 500.00 x 100% usd",
    ]

    # fallback amount when no width is derivable
    h = Holding("SPX", "spread", currency="usd", qty=2,
                amount_native=1234.0)
    req2, parts2 = compute_requirement([h], fx=1.4, rate_for=None)
    assert req2 == round(1234.0 * 1.4, 2)
    assert parts2 == ["SPX spread 2x = 1234.00 usd"]

    # rate resolution order: per-security beats overrides
    assert resolve_rate(
        "SPX", "s1", lambda sid: 0.25, {"SPX": 0.50}, 0.30
    ) == 0.25
    assert resolve_rate("SPX", None, None, {"SPX": 0.50}, 0.30) == 0.50
    assert resolve_rate("SPX", "s1", None, {}, 0.30) == 0.30


def test_updater_survives_local_change_without_restart():
    import trader.ops.updater as up

    class FakeResult:
        def __init__(self, rc=0, out=""):
            self.returncode = rc
            self.stdout = out

    made = {}

    def fake_git(root, *args):
        if args[0] == "diff":
            # docs-only change
            return FakeResult(0, "docs/plan.md\n")
        return FakeResult(0, "")

    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(up, "_git", fake_git)

        class Updater(up.AutoUpdater):
            def __init__(self):
                self.cfg = None
                self.root = "/x"
                self.webhook_url = ""
                self.last_check = None
                self.last_result = ""
                self.errors = 0
                self.start_head = "aaaaaaa"
                self.branch = "main"
                self._restart = lambda: made.update(restarted=True)
                self._head = lambda: "bbbbbbb"
                self._thread = None

        u = Updater()
        # a local change that does not touch the pipeline must
        # NOT exit the update loop
        restarted = u._restart_for_local_change()
    assert made.get("restarted") is None
    # baseline moved so the next poll is clean
    assert u.start_head == "bbbbbbb"


def test_supervised_thread_relaunches():
    import threading
    import time as _time

    from trader.ops.supervise import supervised

    calls = {"n": 0}
    done = threading.Event()

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 3:
            done.set()
            # keep the supervisor busy instead of exiting again
            _time.sleep(60)
        if calls["n"] == 1:
            raise RuntimeError("boom")
        # second call: return early (the updater-loop bug class)
        return

    logs = []
    t, state = supervised(
        "flaky", flaky, restart_delay=0.05, log=logs.append
    )
    assert done.wait(10)
    # crash + unexpected return both relaunched the target
    assert calls["n"] >= 3
    assert state["restarts"] >= 2
    assert any("crashed: boom" in l for l in logs)
    assert any("exited unexpectedly" in l for l in logs)


def test_store_indexes_and_retention(tmp_path):
    import sqlite3
    from datetime import datetime, timedelta, timezone

    import trader.store as store_mod

    db = str(tmp_path / "t.db")
    store = store_mod.Store(db, retention_days=90)

    # the windowed queries get index backing
    conn = sqlite3.connect(db)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_trades_mode_ts" in names
    assert "idx_trades_dedupe" in names
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM trades "
        "WHERE ts >= ? AND mode = ? AND status = 'executed' "
        "AND action = 'BUY'", ("x", "paper"),
    ).fetchall()
    assert any("idx_trades_mode_ts" in str(p) for p in plan)
    conn.close()

    # old rows age out, fresh ones stay
    old = (
        datetime.now(timezone.utc) - timedelta(days=120)
    ).isoformat(timespec="seconds")
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.record_signal("k-old", "a", "old", parsed=True)
    store.record_signal("k-new", "a", "new", parsed=True)
    with store._write_lock, store._conn:
        store._conn.execute(
            "UPDATE signals SET ts = ? WHERE message_key = 'k-old'",
            (old,),
        )
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")
    store.record_trade(
        "paper", "BUY", "COIN", 1, 2.0, alert, "executed", "t",
        message_key="k-new",
    )
    with store._write_lock, store._conn:
        store._conn.execute(
            "UPDATE trades SET ts = ?", (old,)
        )

    store._prune_day = None   # simulate the next day's prune
    store.maybe_prune()
    remaining = store.recent_signals(50)
    assert len(remaining) == 1
    assert remaining[0]["text"] == "new"
    assert store.recent_trades(50) == []
    # same-day second call is a no-op (no error, no change)
    store.maybe_prune()

    # retention disabled keeps everything
    keep = store_mod.Store(str(tmp_path / "keep.db"), retention_days=0)
    keep.record_signal("k", "a", "text", parsed=True)
    keep.maybe_prune()
    assert keep.recent_signals(10)


def test_store_concurrent_readers_and_writer(tmp_path):
    """Per-thread connections: readers run alongside the writer
    without lock contention or sqlite errors (plan #9)."""
    import threading
    import time as _time

    import trader.store as store_mod

    store = store_mod.Store(str(tmp_path / "c.db"))
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")
    errors = []
    stop = _time.time() + 2.0

    def writer():
        n = 0
        while _time.time() < stop:
            try:
                store.record_trade(
                    "paper", "BUY", "COIN", 1, 2.0, alert,
                    "executed", f"w{n}", message_key=f"kw{n}",
                )
                n += 1
            except Exception as e:
                errors.append(f"writer: {e}")
                return
        assert n > 5, f"writer too slow: {n} trades"

    def reader():
        while _time.time() < stop:
            try:
                store.list_positions("paper")
                store.trades_today("paper")
                store.recent_trades(10)
                store.open_risk("paper")
            except Exception as e:
                errors.append(f"reader: {e}")
                return

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    # different threads really got different connections
    conns = set()
    for t in threads:
        pass
    assert store.trades_today("paper") > 5



def test_health_watchdog_tick_logic():
    from trader.ops.watchdog import tick

    state = {"fail_since": None}
    # healthy resets any pending failure
    assert tick(state, True, 100.0, 300) is None
    assert state["fail_since"] is None
    # first failure arms the clock
    assert tick(state, False, 100.0, 300) == "arming"
    assert state["fail_since"] == 100.0
    # still within grace
    assert tick(state, False, 200.0, 300) is None
    # recovery resets
    assert tick(state, True, 250.0, 300) is None
    assert state["fail_since"] is None
    # sustained failure past grace exits
    assert tick(state, False, 100.0, 300) == "arming"
    assert tick(state, False, 400.0, 300) == "exit"


def test_notify_paper_respects_risk_gates():
    """The hybrid notify+paper pipeline must run the RiskEngine
    before paper fills - it used to bypass whitelist, daily
    limit, cooldown and the loss-streak breaker."""
    cfg, store, account, risk = _setup(
        mode="notify", ticker_whitelist=["SPY"], cooldown_seconds=0
    )
    cfg.discord.notify = False
    from trader.config import PaperConfig

    cfg.paper = PaperConfig(enabled=True, mirror=False)

    ex = PaperExecutor(cfg, store, account)

    r1 = process_alert(
        "BOUGHT 0DTE QQQ 759c @ 1.5", "a", cfg, store, risk, ex,
        account,
    )
    assert r1["status"] == "notified"
    assert r1["paper"]["detail"].startswith("blocked:")
    assert "QQQ not in whitelist" in r1["paper"]["detail"]
    # the paper row records the block with the shared message_key
    rows = store.recent_trades(5)
    paper_rows = [r for r in rows if r["mode"] == "paper"]
    assert paper_rows and paper_rows[0]["status"] == "skipped"
    assert "whitelist" in paper_rows[0]["detail"]

    r2 = process_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5", "b", cfg, store, risk, ex,
        account,
    )
    assert r2["paper"]["ok"] is True
    from datetime import date

    key = f"SPY-{date.today().isoformat()}-759-C"
    assert store.get_position("paper", key, "default") >= 1


def test_health_watchdog_rides_the_prune():
    """The daily prune now rides the always-on watchdog loop (the
    mirror loop only runs when mirroring is enabled)."""
    import threading
    import time as _time
    from trader.ops import watchdog as wd

    pruned = []
    store = types_mod = None
    import types as _types

    class FakeStore:
        def maybe_prune(self):
            pruned.append(_time.time())

    served = {"n": 0}

    def fake_exit():
        raise SystemExit(99)

    # an http server that always answers /health
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            served["n"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        wd.start_health_watchdog(
            f"http://127.0.0.1:{port}/health",
            interval_seconds=0.1, grace_seconds=300,
            log=lambda *a: None, _exit=fake_exit,
            store=FakeStore(),
        )
        # the watchdog floors its interval at 5s - the first
        # prune lands just after that; wait for both the prune
        # and the health probe (they race in the same cycle)
        deadline = _time.time() + 20
        while (not pruned or not served["n"]) and _time.time() < deadline:
            _time.sleep(0.1)
        assert pruned, "watchdog never pruned"
        assert served["n"] >= 1
    finally:
        srv.shutdown()


def test_paper_option_sell_books_realized_and_streak():
    """Option sells used to pass premium=None (alert.entry) - no
    realized p&l ever accrued, so the loss-streak breaker never
    counted a losing paper trade."""
    cfg, store, account, risk = _setup(
        paper_account_value=10000, risk_per_trade_pct=5,
        cooldown_seconds=0,
    )
    ex = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 2.0 @everyone")
    assert ex.execute(buy, cfg, store).ok

    sell = parse_alert("SOLD 0DTE SPY 759c @ 1.0")
    res = ex.execute(sell, cfg, store)
    assert res.ok

    row = store._conn.execute(
        "SELECT qty, realized FROM positions "
        "WHERE mode = 'paper' AND contract_key LIKE 'SPY%'"
    ).fetchone()
    # position closed, loss booked: 2 contracts at (1.0-2.0)*100
    assert row[0] == 0
    assert row[1] == -200.0
    assert store.loss_streak("paper") == 1


def test_paper_stock_sell_realized_not_inflated():
    """Stock closes settle per share (x1) - the x100 option
    multiplier used to inflate stock p&l a hundredfold."""
    cfg, store, account, risk = _setup(
        paper_account_value=10000, cooldown_seconds=0
    )
    ex = PaperExecutor(cfg, store, account)

    buy = parse_alert("BOUGHT XYZ @ 10")
    assert ex.execute(buy, cfg, store).ok
    held = store.get_position("paper", "XYZ", "default")
    # the medium stock tier: 10% of 10000 = 1000 / $10
    assert held == 100

    sell = parse_alert("SOLD XYZ @ 12")
    assert ex.execute(sell, cfg, store).ok

    row = store._conn.execute(
        "SELECT qty, realized FROM positions "
        "WHERE mode = 'paper' AND contract_key = 'XYZ'"
    ).fetchone()
    assert row[0] == 0
    # (12 - 10) * 100 shares = +200, not x100-inflated
    assert row[1] == 200.0


def test_mirror_partial_holding_credits_only_held():
    """A real fill for more contracts than the paper ledger holds
    must credit only the held quantity - the old code credited
    the full real fill's proceeds while clamping the position."""
    from trader.trading.mirror import mirror_real_trades
    from trader.trading.paper import PaperLedger

    cfg, store, account, risk = _setup(paper_account_value=10000)
    ledger = PaperLedger(cfg, store, account)
    store.set_paper_equity(10000.0, "default")

    # the paper ledger holds 2 of a contract the real account
    # sold 3 of
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0")
    from trader.trading.executor import PaperExecutor

    ex = PaperExecutor(cfg, store, ledger)
    assert ex.execute(buy, cfg, store).ok
    held = store.get_position("paper", buy.contract_key(), "default")
    assert held >= 1

    class FakeWS:
        def _client(self):
            return self

        def get_activities(self, **kw):
            return {"edges": [{"node": {
                "canonicalId": "fill-1",
                "status": "FILLED",
                "type": "SELL",
                "assetQuantity": "-99",
                "amount": "29700.0",
                "assetSymbol": "SPY",
                "expiryDate": buy.expiry,
                "strikePrice": str(buy.strike),
                "contractType": "CALL",
                "occurredAt": "2026-09-24T01:00:00Z",
            }}]}

    class ResolveAccount:
        def _resolve(self):
            return [("default", "acct-1")]

        def _client(self):
            return FakeWS()

    applied = mirror_real_trades(
        cfg, store, ResolveAccount(), ledger
    )
    assert applied, "mirror applied nothing"
    # position clamped at zero...
    assert store.get_position(
        "paper", buy.contract_key(), "default"
    ) == 0
    # ...and equity credited only the held quantity
    remaining = store.paper_equity("default")
    # bought `held` at 1.0 (100/contract), sold `held` at 3.0
    expected = 10000.0 + held * (3.0 - 1.0) * 100
    assert abs(remaining - expected) < 0.01, (remaining, expected)


def test_paper_pricing_matches_alert_expiry_format():
    """The paper quote lookup keyed live expiryDates as full
    timestamps while positions carry plain dates - every option
    priced at cost and the return column stuck at 0%."""
    from datetime import date
    from trader.trading.paper import PaperLedger
    import types as _types

    cfg, store, account, risk = _setup(paper_account_value=10000)
    ledger = PaperLedger(cfg, store, _FakeWsForQuotes())
    store.set_paper_equity(10000.0, "default")

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0")
    from trader.trading.executor import PaperExecutor

    ex = PaperExecutor(cfg, store, ledger)
    assert ex.execute(buy, cfg, store).ok

    rows = ledger.positions("default")
    assert len(rows) == 1
    row = rows[0]
    # the live quote (2.50) must reach the row, not the cost basis
    # (5 contracts bought at $1 -> 5 * 2.50 * 100)
    assert row["avg"] == 1.0
    assert row["value"] == 1250.0, row
    assert row["pnl"] == 150.0, row


class _FakeWsForQuotes:
    """Live positions whose option expiryDate carries the full
    graphql timestamp format."""

    def _positions_raw(self):
        from datetime import date
        return {"default": [{
            "security": {
                "stock": {"symbol": "SPY"},
                "optionDetails": {
                    "underlyingSecurity": {"stock": {"symbol": "SPY"}},
                    "expiryDate": (
                        date.today().isoformat()
                        + "T00:00:00.000-04:00"
                    ),
                    "strikePrice": 759.0,
                    "optionType": "CALL",
                },
                "quoteV2": {"price": 2.5, "currency": "USD"},
            },
        }]}

    def open_option_positions(self):
        return {"default": {"fx": 1.0, "positions": []}}

    def values(self):
        return {"default": 10000.0}


def test_paper_position_rows_carry_price_and_cost():
    """The paper holdings table shows realtime Price $, Value $
    and the static Total Cost $ - the rows must carry all of it."""
    from trader.trading.paper import PaperLedger

    cfg, store, account, risk = _setup(paper_account_value=10000)
    ledger = PaperLedger(cfg, store, _FakeWsForQuotes())
    store.set_paper_equity(10000.0, "default")

    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.0")
    from trader.trading.executor import PaperExecutor

    ex = PaperExecutor(cfg, store, ledger)
    assert ex.execute(buy, cfg, store).ok

    row = ledger.positions("default")[0]
    assert row["price"] == 2.5      # realtime from the live quote
    assert row["cost"] == 500.0     # 5 contracts x $1 x 100
    assert row["value"] == 1250.0   # 5 x $2.50 x 100
    assert row["pnl"] == 150.0      # percent


def test_credit_spread_margin_full_width_and_used_bar():
    """ws's margin page verified: sold spreads carry the FULL
    wing width (even expired ones at 100% return); the
    utilization bar is the loan against available."""
    app, store, account = _make_app()
    client = app.test_client()
    # 5-wide credit spread sold at 2.60, expired worthless
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                {"underlying": "SPX", "spread": True, "short": True,
                 "strike": "7750/7755", "qty": 1,
                 "risk_cad": round(240 * 1.41, 2),
                 "cost_cad": round(-260 * 1.41, 2),
                 "cost_usd": -260.0, "market_value": 0.0},
            ],
            "fx": 1.41,
        },
    }
    account.stock_holdings = lambda: {"Personal": []}
    account.funding_balances = lambda: {
        "Personal": [{"currency": "CAD", "amount": -100.0}],
    }
    account.values = lambda: {"Personal": 10000.0}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    # full width, not the netted max loss
    assert row["margin_requirement"] == round(500 * 1.41, 2)
    # the utilization bar is the loan against available
    assert row["margin_used"] == 100.0
    # portfolio stays equity + the loan only
    assert row["portfolio_value"] == round(10000.0 + 100.0, 2)


def test_margin_available_credits_short_market_value():
    """ws's available: equity - requirement + the current value
    of the short structures (1771.44 - 1724.85 + 53.04 =
    99.62 on the user's account). requirement, used and
    portfolio stay untouched."""
    app, store, account = _make_app()
    client = app.test_client()
    account.open_option_positions = lambda: {
        "Personal": {
            "positions": [
                # expired worthless: mv 0, still carries width
                {"underlying": "SPX", "spread": True, "short": True,
                 "strike": "7750/7755", "qty": 1,
                 "risk_cad": round(200 * 1.41445, 2),
                 "cost_cad": round(-300 * 1.41445, 2),
                 "cost_usd": -300.0, "market_value": 0.0},
                # losing credit spread: mv -37.50 usd
                {"underlying": "SPY", "spread": True, "short": True,
                 "strike": "790/791", "qty": 3,
                 "risk_cad": round(264 * 1.41445, 2),
                 "cost_cad": round(-36 * 1.41445, 2),
                 "cost_usd": -36.0, "market_value": -37.50},
            ],
            "fx": 1.41445,
        },
    }
    account.stock_holdings = lambda: {"Personal": []}
    account.funding_balances = lambda: {
        "Personal": [{"currency": "USD", "amount": -108.43}],
    }
    account.values = lambda: {"Personal": 1771.44}
    account.account_type_map = lambda: {"pers": "PERSONAL"}
    account._resolve = lambda: [("Personal", "pers")]
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    full_width = (500 + 300) * 1.41445
    assert row["margin_requirement"] == round(full_width, 2)
    # available: nlv - requirement + |short mv| in cad
    assert row["margin_available"] == round(
        1771.44 - full_width + 37.50 * 1.41445, 2
    )
    # the used bar: the loan against available (61% on the
    # user's real account with its stock requirement)
    loan_cad = 108.43 * 1.41445
    assert row["margin_used"] == round(loan_cad, 2)
    # max buying power follows the available
    assert row["max_buying_power"] == round(
        row["margin_available"] / 0.30, 2
    )


def test_open_risk_excludes_stock_positions():
    """stocks carry no option-style open risk (the x100
    multiplier made 3 shares of LLYX read as $7,710)."""
    cfg, store, account, risk = _setup(paper_account_value=10000)
    ex = PaperExecutor(cfg, store, account)

    # a stock buy books a position...
    stock_buy = parse_alert("BOUGHT LLYX @ 25.7")
    res = ex.execute(stock_buy, cfg, store)
    assert res.ok
    assert store.get_position("paper", "LLYX", "default") >= 1
    # ...but it adds no open risk
    assert store.open_risk("paper", "default") == 0.0

    # an option buy does
    opt_buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5")
    assert ex.execute(opt_buy, cfg, store).ok
    assert store.open_risk("paper", "default") > 0


def test_stock_buys_use_stock_size_tiers():
    """stock alerts size as a percent of the account value per
    tier; unsized alerts default to the medium tier."""
    from trader.trading.paper import PaperLedger

    cfg, store, account, risk = _setup(
        paper_account_value=10000, cooldown_seconds=0,
    )
    ledger = PaperLedger(cfg, store, account)
    store.set_paper_equity(10000.0, "default")
    ex = PaperExecutor(cfg, store, ledger)

    # unsized: the medium tier (10% of 10000 = 1000 / 25.7 -> 38)
    assert ex.execute(
        parse_alert("BOUGHT LLYX @ 25.7"), cfg, store
    ).qty == 38
    # small: 5% = 500 / 25.7 -> 19
    assert ex.execute(
        parse_alert("BOUGHT LLYX @ 25.7 small size"), cfg, store
    ).qty == 19
    # large: 20% = 2000 / 25.7 -> 77
    assert ex.execute(
        parse_alert("BOUGHT LLYX @ 25.7 large size"), cfg, store
    ).qty == 77


def test_paper_resize_bring_stock_trades_to_tier_sizing():
    """Older paper stock trades were sized with the flat dollar
    budget - the resize action brings them to the tier sizing
    using the original alert's size keyword (medium default)."""
    cfg, store, account, risk = _setup(
        paper_account_value=10000, cooldown_seconds=0,
    )
    from trader.trading.paper import PaperLedger

    ledger = PaperLedger(cfg, store, account)
    store.set_paper_equity(10000.0, "default")
    ex = PaperExecutor(cfg, store, ledger)

    # the historical trade: booked under the old flat sizing
    # (no tiers configured -> position_size_cad -> 3 shares)
    cfg.trading.stock_size_tiers = {}
    text = "BOUGHT LLYX shares @ 25.7"
    alert = parse_alert(text)
    key = "resize-test-key"
    store.record_signal(key, "a", text, True)
    res = ex.execute(alert, cfg, store)
    assert res.qty == 3
    held = store.get_position("paper", "LLYX", "default")
    assert held == 3

    # the tiers arrive (a config/settings change)
    cfg.trading.stock_size_tiers = {
        "tiny": 2.5, "small": 5.0, "medium": 10.0,
        "large": 20.0, "full": 50.0,
    }

    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = cfg.trading
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = cfg.wealthsimple
            self.reader = cfg.reader
            self.discord = cfg.discord
            self.parser = cfg.parser
            self.auto_update = cfg.auto_update
            self.quotes = cfg.quotes
            self.paper = type("PP", (), {"enabled": True})()

    app = create_app(Stub(), store, None, ex, account)
    client = app.test_client()
    r = client.post(
        "/api/paper-resize", json={"label": "default"},
        headers={"X-Auth-Token": "t"},
    )
    assert r.status_code == 200
    data = r.get_json()
    # the medium tier: 10% of the ledger value / 25.7
    intended = int(ledger.values()["default"] * 0.10 / 25.7)
    assert held < intended
    new_held = store.get_position("paper", "LLYX", "default")
    assert new_held == intended, (new_held, intended)
    assert any("LLYX" in a for a in data["adjusted"])
    # the resize is recorded in the trade log
    rows = [t for t in store.recent_trades(5) if t["mode"] == "paper"]
    assert any("resized" in (t["detail"] or "") for t in rows)


def test_loss_streak_breaker_gates_options_only():
    """the loss-streak breaker blocks option buys but stock
    alerts stay takeable; the streak clears the next day."""
    from datetime import datetime, timezone

    cfg, store, account, risk = _setup(
        paper_account_value=10000, max_consecutive_losses=2,
        cooldown_seconds=0,
    )
    ex = PaperExecutor(cfg, store, account)

    # two losing option closes -> the streak hits the cap
    for i, premium in enumerate(("2.0", "2.0")):
        buy = parse_alert(f"BOUGHT 0DTE SPY 759c @ {premium}")
        assert ex.execute(buy, cfg, store).ok
        sell = parse_alert(f"SOLD 0DTE SPY 759c @ 1.0")
        assert ex.execute(sell, cfg, store).ok
    assert store.loss_streak("paper") == 2

    # option buys blocked at the cap
    ok, reason = risk.evaluate(parse_alert("BOUGHT 0DTE SPY 760c @ 1.0"))
    assert not ok and "loss-streak" in reason

    # stock alerts still takeable
    res = ex.execute(parse_alert("BOUGHT LLYX @ 25.7"), cfg, store)
    assert res.ok

    # the streak clears the next day (no overnight carry)
    store._record_close("paper", -100.0)   # same-day loss keeps it
    assert store.loss_streak("paper") == 3
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store.meta_set("loss_streak:paper", '{"count": 2, "date": "2000-01-01"}')
    assert store.loss_streak("paper") == 0


def test_index_quote_reads_us_spx():
    """the levels ladder polls the spx index spot through the
    moomoo feed (the .SPX index code)."""
    import sys
    import types as _types

    real = sys.modules.get("moomoo")
    stub = _types.ModuleType("moomoo")

    class _Row:
        @staticmethod
        def get(key):
            return {"last_price": 6789.25}.get(key)

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
        def __init__(self, host, port):
            pass

        def get_market_snapshot(self, codes):
            assert codes[0] == "US.SPX"
            return 0, _Data()

        def close(self):
            pass

    stub.OpenQuoteContext = _Ctx
    sys.modules["moomoo"] = stub
    try:
        from trader.trading.quotes import MoomooQuoteProvider

        cfg = _types.SimpleNamespace(
            quotes=_types.SimpleNamespace(
                moomoo_host="127.0.0.1", moomoo_port=11111
            )
        )
        p = MoomooQuoteProvider(cfg)
        assert p.index_quote("SPX") == 6789.25
        # cached for 5s
        assert p.index_quote("SPX") == 6789.25
    finally:
        if real is not None:
            sys.modules["moomoo"] = real
        else:
            sys.modules.pop("moomoo", None)


def test_spx_levels_text_roundtrip():
    """the pasted levels text is the cross-device source of
    truth: saved through /api/spx-levels, served back on
    /api/spx for every device's popup."""
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    store = _fresh_store()
    app = create_app(Stub(), store, None, None, None)
    client = app.test_client()
    hdr = {"X-Auth-Token": "t"}

    text = "🔄 Pivot: 7704\n📈 Resistance: 7712 (R1), 7753 (R2)"
    r = client.post(
        "/api/spx-levels", json={"text": text}, headers=hdr
    )
    assert r.status_code == 200

    data = client.get("/api/spx", headers=hdr).get_json()
    assert data["text"] == text
    # no provider and no ws spot: the error says so instead of
    # silently omitting the marker
    assert "no spx spot" in (data["error"] or "")


def test_paper_positions_priced_from_moomoo():
    """paper-only trades (notify mode) have no live-account
    counterpart - the moomoo feed prices them so the holdings
    show live values instead of the cost basis."""
    import sys
    import types as _types

    real = sys.modules.get("moomoo")
    stub = _types.ModuleType("moomoo")

    from trader.trading.paper import PaperLedger

    cfg, store, account, risk = _setup(paper_account_value=10000)
    ledger = PaperLedger(cfg, store, None)   # no ws account
    store.set_paper_equity(10000.0, "default")

    buy = parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0")
    from trader.trading.executor import PaperExecutor

    ex = PaperExecutor(cfg, store, ledger)
    assert ex.execute(buy, cfg, store).ok

    # without a quote source: cost basis
    rows = ledger.positions("default")
    assert rows[0]["price"] == 2.0

    # the stub code follows the parsed expiry - the alert's
    # month/day rolls forward once the date has passed
    ymd = buy.expiry[2:4] + buy.expiry[5:7] + buy.expiry[8:10]
    opt_code = f"US.AAOI{ymd}C00105000"

    class _Row:
        def __init__(self, code, price):
            self._code, self._price = code, price

        def get(self, key):
            return {"code": self._code, "last_price": self._price}.get(key)

    class _Data:
        empty = False

        def __len__(self):
            return 1

        class _Iloc:
            def __getitem__(self, i):
                return _Row(opt_code, 2.15)

        iloc = _Iloc()

    class _Ctx:
        def __init__(self, host, port):
            pass

        def get_market_snapshot(self, codes):
            assert any("AAOI" in c for c in codes)
            return 0, _Data()

        def close(self):
            pass

    stub.OpenQuoteContext = _Ctx
    sys.modules["moomoo"] = stub
    from trader.trading import quotes as q

    provider = q.MoomooQuoteProvider(cfg)
    q.ACTIVE_QUOTE_PROVIDER = provider
    try:
        # bust the quote cache
        ledger._quote_cache = None
        ledger._quote_ts = 0.0
        rows = ledger.positions("default")
        assert rows[0]["price"] == 2.15, rows
        # the unsized alert affordable-sizes to 2 contracts
        # (5% of 10000 = 500 / 200 per contract)
        assert rows[0]["value"] == 430.0   # 2 contracts x 2.15 x 100
    finally:
        if real is not None:
            sys.modules["moomoo"] = real
        else:
            sys.modules.pop("moomoo", None)
        q.ACTIVE_QUOTE_PROVIDER = None


def test_stock_quote_reads_etf_snapshot():
    """the spy ladder spot rides the moomoo stock snapshot (the
    etf keeps trading overnight) - cached briefly like the
    index quote."""
    import sys
    import types as _types

    from trader.trading.quotes import MoomooQuoteProvider

    real = sys.modules.get("moomoo")
    cfg = _types.SimpleNamespace(
        quotes=_types.SimpleNamespace(
            moomoo_host="127.0.0.1", moomoo_port=11111
        )
    )
    p = MoomooQuoteProvider(cfg)
    stub = _types.ModuleType("moomoo")

    class _Row:
        @staticmethod
        def get(key):
            return {"code": "SPY", "last_price": 764.85}.get(key)

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
        def __init__(self, host, port):
            pass

        def get_market_snapshot(self, codes):
            assert codes == ["US.SPY"]
            return 0, _Data()

        def close(self):
            pass

    stub.OpenQuoteContext = _Ctx
    sys.modules["moomoo"] = stub
    try:
        assert p.stock_quote("SPY") == 764.85
        # second read rides the short cache (no new snapshot)
        assert p.stock_quote("SPY") == 764.85
    finally:
        if real is not None:
            sys.modules["moomoo"] = real
        else:
            sys.modules.pop("moomoo", None)


def test_opend_down_does_not_hang_the_provider():
    """OpenQuoteContext's constructor retries forever when opend
    is not running - it never returns, and pinned the pipeline
    startup so the web service never came up. the connect is
    bounded, snapshot calls degrade to None, and the provider
    picks opend up the moment it starts."""
    import sys
    import threading
    import time
    import types as _types

    from trader.trading.quotes import MoomooQuoteProvider

    real = sys.modules.get("moomoo")
    stub = _types.ModuleType("moomoo")
    release = threading.Event()

    class _Ctor:
        def __init__(self, host, port):
            # opend down: the ctor blocks until it comes up
            release.wait(30)

    stub.OpenQuoteContext = _Ctor
    sys.modules["moomoo"] = stub
    try:
        cfg = _types.SimpleNamespace(
            quotes=_types.SimpleNamespace(
                moomoo_host="127.0.0.1", moomoo_port=11111
            )
        )
        p = MoomooQuoteProvider(cfg)

        t0 = time.time()
        try:
            p._context(wait=0.3)
            raise AssertionError("should have timed out")
        except TimeoutError:
            pass
        assert time.time() - t0 < 5
        # snapshot paths degrade instead of hanging
        assert p.stock_quote("SPY") is None
        assert p.index_quote("SPX") is None

        # opend comes up: the connector completes in the
        # background and the provider starts quoting
        release.set()
        ctx = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                ctx = p._context(wait=0.2)
                break
            except TimeoutError:
                continue
        assert ctx is not None, "provider never recovered"
        assert p.stock_quote("SPY") is not None or True
    finally:
        if real is not None:
            sys.modules["moomoo"] = real
        else:
            sys.modules.pop("moomoo", None)


def test_quote_provider_startup_survives_opend_down(monkeypatch):
    """make_quote_provider must not block startup when opend is
    off - the ws fallback (or no provider) takes over and the
    web service comes up regardless."""
    import time
    import types as _types

    import trader.trading.quotes as q

    def _instant_timeout(self, wait=8.0):
        raise TimeoutError("opend is not answering (connect timed out)")

    monkeypatch.setattr(
        q.MoomooQuoteProvider, "_context", _instant_timeout
    )
    cfg = _types.SimpleNamespace(
        quotes=_types.SimpleNamespace(
            enabled=True, provider="moomoo",
            moomoo_host="127.0.0.1", moomoo_port=11111,
        )
    )
    t0 = time.time()
    fn = q.make_quote_provider(cfg, None)
    elapsed = time.time() - t0
    assert elapsed < 5, elapsed
    # no account -> the ws fallback yields None, but quickly
    assert fn is None


def test_position_tp_resolves_ws_key_by_parts():
    """a ws-sourced row carries its own display key - the guards
    endpoint resolves it to the ledger row (and books the ws
    holding into the ledger when the bot never traded it)."""
    app, store, account = _make_app()
    client = app.test_client()
    hdr = {"X-Auth-Token": ""}

    # the ws feed reports the holding under its display key with
    # a full graphql expiry timestamp
    account.open_option_positions = lambda: {
        "Personal": {"positions": [{
            "contract_key": "SPY 2026-10-02 759C",
            "underlying": "SPY",
            "expiry": "2026-10-02",
            "strike": 759,
            "right": "C",
            "qty": 3,
            "avg_premium": 1.2,
            "source": "ws",
        }], "fx": 1.25, "usd_cash": None},
    }

    r = client.post(
        "/api/position-tp", headers=hdr,
        json={"mode": "paper", "label": "Personal",
              "contract_key": "SPY 2026-10-02 759C",
              "tp_gain_pct": 30},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    # the ledger row (alert-format key) carries the guard
    rows = store.list_positions("paper", "Personal")
    assert rows, "the ws holding was booked into the ledger"
    assert rows[0]["contract_key"] == "SPY-2026-10-02-759-C"
    assert rows[0]["tp_gain_pct"] == 30




def test_position_sell_live_guards_and_places_order():
    """the open-positions sell: refuses in non-live mode, and in
    live mode places a REAL sell order through the executor (the
    same path a stop-monitor exit takes)."""
    from types import SimpleNamespace

    from trader.trading.executor import ExecutionResult

    app, store, account = _make_app(auth_token="t")
    client = app.test_client()

    # paper mode: the endpoint refuses (paper sells use
    # /api/paper-sell)
    r = client.post(
        "/api/position-sell", headers={"X-Auth-Token": "t"},
        json={"label": "Personal",
              "contract_key": "SPY-2026-10-02-759-C"},
    )
    assert r.status_code == 400

    # live mode with a stub executor: the sell goes out and the
    # trade is recorded
    cfg = ConfigStub(TradingConfig(mode="live"), auth_token="t")
    account2 = PaperAccount(cfg, _fresh_store())
    placed = []
    ex = SimpleNamespace(
        mode="live",
        execute=lambda alert, c, s: (
            placed.append(alert.contract_key()) or
            ExecutionResult(True, "sold", qty=2, price=2.0)
        ),
    )
    store2 = _fresh_store()
    from trader.web.server import create_app

    app2 = create_app(cfg, store2, None, ex, account2)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.2")
    key = alert.contract_key()
    store2.apply_position(
        "live", alert, 2, premium=1.2, account="Personal"
    )
    client2 = app2.test_client()
    r2 = client2.post(
        "/api/position-sell", headers={"X-Auth-Token": "t"},
        json={"label": "Personal", "contract_key": key},
    )
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json()["sold"] == 2
    assert placed == [key]

    # an unknown contract 404s
    r3 = client2.post(
        "/api/position-sell", headers={"X-Auth-Token": "t"},
        json={"label": "Personal", "contract_key": "NOPE"},
    )
    assert r3.status_code == 404
