import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.store import Store
from trader.trading.parser import parse_alert


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _record_trade(store, mode, action, ticker, qty, price, status,
                  detail, key):
    store.record_trade(
        mode, action, ticker, qty, price,
        parse_alert(f"BOUGHT 0DTE {ticker} 100c @ {price}"),
        status, detail, message_key=key,
    )


def test_search_trades_filters():
    s = _fresh_store()
    _record_trade(s, "paper", "BUY", "SPY", 3, 1.5, "executed",
                  "bought 3 @ 1.5", "k1")
    _record_trade(s, "paper", "SELL", "QQQ", 2, 2.0, "skipped",
                  "risk cap", "k2")
    _record_trade(s, "live", "BUY", "SPY", 1, 0.5, "error",
                  "order rejected", "k3")

    rows, total = s.search_history(ticker="spy")
    assert total == 2 and {r["mode"] for r in rows} == {"paper", "live"}

    rows, total = s.search_history(action="buy")
    assert total == 2 and all(r["action"] == "BUY" for r in rows)

    rows, total = s.search_history(status="skipped")
    assert total == 1 and rows[0]["ticker"] == "QQQ"

    rows, total = s.search_history(mode="live")
    assert total == 1 and rows[0]["status"] == "error"

    rows, total = s.search_history(q="risk cap")
    assert total == 1 and rows[0]["ticker"] == "QQQ"

    # newest first
    rows, _ = s.search_history()
    assert rows[0]["detail"] == "order rejected"


def test_search_trades_pagination():
    s = _fresh_store()
    for i in range(7):
        _record_trade(s, "paper", "BUY", "SPY", 1, 1.0, "executed",
                      f"t{i}", f"k{i}")
    rows, total = s.search_history(limit=3)
    assert total == 7 and len(rows) == 3
    rows, _ = s.search_history(limit=3, offset=6)
    assert len(rows) == 1


def test_search_trades_date_range():
    s = _fresh_store()
    _record_trade(s, "paper", "BUY", "SPY", 1, 1.0, "executed",
                  "today", "k1")

    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()

    # date-only until is stretched to include the whole end date
    rows, total = s.search_history(since=yesterday, until=today)
    assert total == 1

    # the until date itself is covered
    rows, total = s.search_history(until=today.isoformat())
    assert total == 1

    rows, total = s.search_history(since=tomorrow)
    assert total == 0


def test_search_trades_like_escaping():
    s = _fresh_store()
    _record_trade(s, "paper", "BUY", "SPY", 1, 1.0, "executed",
                  "plain detail", "k1")
    # a bare % must not act as a wildcard matching everything
    rows, total = s.search_history(q="%")
    assert total == 0


def test_search_signals_filters():
    s = _fresh_store()
    s.record_signal("s1", "alpha", "BOUGHT 0DTE SPY 759c @ 1.5", True,
                    channel="options")
    s.record_signal("s2", "beta", "SOLD 1/4 SPX 7650c @ 2.32", True,
                    channel="options")
    s.record_signal("s3", "gamma", "unrelated chatter", False,
                    channel="general")

    rows, total = s.search_history(kind="signals", q="alpha")
    assert total == 1 and "SPY" in rows[0]["text"]

    # ticker filters signals by free-text match
    rows, total = s.search_history(kind="signals", ticker="spx")
    assert total == 1 and rows[0]["author"] == "beta"

    rows, total = s.search_history(kind="signals")
    assert total == 3
    # newest first
    assert rows[0]["author"] == "gamma"


def test_history_endpoint():
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type(
                "P", (), {"auth_token": "secret"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()

    store = _fresh_store()
    _record_trade(store, "paper", "BUY", "SPY", 3, 1.5, "executed",
                  "bought", "k1")
    _record_trade(store, "paper", "BUY", "QQQ", 1, 2.0, "skipped",
                  "capped", "k2")
    store.record_signal("s1", "alpha", "BOUGHT 0DTE SPY 759c", True)

    app = create_app(Stub(), store, None, None, None)
    client = app.test_client()

    # auth guard applies to the history endpoint too
    r = client.get("/api/history")
    assert r.status_code == 401

    hdr = {"X-Auth-Token": "secret"}

    r = client.get("/api/history?ticker=SPY", headers=hdr)
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 1 and data["rows"][0]["ticker"] == "SPY"

    r = client.get("/api/history?kind=signals", headers=hdr)
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 1 and data["kind"] == "signals"

    r = client.get("/api/history?status=skipped", headers=hdr)
    assert r.get_json()["total"] == 1


def test_search_both_merges_chronologically():
    import time

    s = _fresh_store()
    # the alert first, then the trade it produced (linked via
    # message_key), then an unrelated newer alert
    base = time.time()
    s.record_signal("s1", "alpha", "BOUGHT 0DTE SPY 759c @ 1.5",
                    True, channel="options", ts_epoch=base)
    _record_trade(s, "paper", "BUY", "SPY", 3, 1.5, "executed",
                  "bought 3", "s1")
    s.record_signal("s2", "beta", "chatter", False, channel="general",
                    ts_epoch=base + 5)

    rows, total = s.search_history(kind="both")
    assert total == 3
    # newest first; the s1 alert rides its trade's timestamp so
    # the pair lands together, alert on top
    assert [r["type"] for r in rows] == ["signal", "signal", "trade"]
    assert rows[0]["message_key"] == "s2"
    assert rows[1]["message_key"] == "s1"
    assert rows[2]["message_key"] == "s1"


def test_search_both_filters_per_table():
    s = _fresh_store()
    _record_trade(s, "paper", "BUY", "SPY", 3, 1.5, "executed",
                  "bought", "k1")
    s.record_signal("s1", "alpha", "BOUGHT 0DTE SPY 759c", True)

    # status applies to trades only - the alert still lists
    rows, total = s.search_history(kind="both", status="skipped")
    assert total == 1 and rows[0]["type"] == "signal"

    # free text applies to both tables
    rows, total = s.search_history(kind="both", q="SPY")
    assert total == 2


def test_search_both_pagination():
    s = _fresh_store()
    for i in range(5):
        _record_trade(s, "paper", "BUY", "SPY", 1, 1.0, "executed",
                      f"t{i}", f"k{i}")
        s.record_signal(f"s{i}", "a", f"alert {i}", True)
    rows, total = s.search_history(kind="both", limit=4)
    assert total == 10 and len(rows) == 4
    rows, _ = s.search_history(kind="both", limit=4, offset=8)
    assert len(rows) == 2


def test_history_endpoint_both():
    from trader.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from trader.web.server import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type(
                "P", (), {"auth_token": "secret"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()

    store = _fresh_store()
    s1 = "BOUGHT 0DTE SPY 759c @ 1.5"
    store.record_signal("s1", "alpha", s1, True)
    _record_trade(store, "paper", "BUY", "SPY", 3, 1.5, "executed",
                  "bought", "s1")

    app = create_app(Stub(), store, None, None, None)
    client = app.test_client()
    r = client.get("/api/history?kind=both", headers={"X-Auth-Token": "secret"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["total"] == 2
    assert {row["type"] for row in data["rows"]} == {"signal", "trade"}
