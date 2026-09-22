import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.account import PaperAccount
from trader.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig
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


def test_load_env_tokens():
    import trader.ws_tokens as wt

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
    import trader.ws_tokens as wt

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
    import trader.notify as notify_mod

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
    import trader.notify as notify_mod

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

    # wrong password stays on the login page
    bad = client.post("/login", data={"password": "nope"})
    assert bad.status_code == 200
    assert "wrong access token" in bad.get_data(as_text=True)

    # correct password logs in and sets the session
    ok = client.post("/login", data={"password": "s3cret"})
    assert ok.status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/api/summary").status_code == 200

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
    import trader.server as srv

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
    import trader.dashboard as dash

    js = re.findall(r"<script>(.*?)</script>",
                   dash.DASHBOARD_HTML, re.S)[0]
    assert "function esc(" in js
    assert '.replace(/</g, "&lt;")' not in js
    for field in ("s.text", "s.channel", "p.underlying", "p.strike",
                  "t.detail", "t.ticker", "a.label"):
        assert f"esc({field}" in js
    # expiry goes through esc wrapped in the date slice
    assert 'esc(String(p.expiry || "").slice(0, 10))' in js


def test_notify_sell_fields_scaling_and_sold(monkeypatch):
    import json as json_mod

    import trader.notify as notify
    from trader.parser import parse_alert

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
    import trader.notify as notify
    from trader.parser import parse_alert

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
    from trader.account import WealthsimpleAccount
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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    from trader.parser import parse_alert

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
    from trader.account import WealthsimpleAccount

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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    import trader.notify as notify
    from trader.parser import parse_alert

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
        "trader.notify.requests.post",
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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    from trader.account import WealthsimpleAccount

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
    from trader.account import WealthsimpleAccount
    from trader.ws_positions_query import (
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
            {"security": {"optionDetails": {
                "strikePrice": "6000", "optionType": "CALL",
                "expiryDate": "2026-09-18",
                "underlyingSecurity": {"stock": {"symbol": "SPX"}},
            }}},
            {"security": {"optionDetails": {
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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
    assert sp["strategy_type"] == "VERTICAL_SPREAD"
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
    summary = client.get("/api/summary").get_json()
    row = next(a for a in summary["accounts"] if a["label"] == "Personal")
    assert row["margin_requirement"] is not None

def test_account_type_map_cached():
    from trader.account import WealthsimpleAccount

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
    from trader.account import WealthsimpleAccount
    from trader.ws_security_query import FETCH_SECURITY

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
    from trader.account import WealthsimpleAccount

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
    acct._pos_cache = None
    acct._pos_cache_ts = 0.0
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
