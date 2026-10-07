"""Role split: info server (reader ingest + feed, no trading)
vs consumer app (the trading pipeline)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from test_pipeline import _fresh_store, ConfigStub, TradingConfig  # noqa: E402
from trader.config import load_config  # noqa: E402
from trader.pipeline import ingest_alert  # noqa: E402
from trader.trading.risk import RiskEngine  # noqa: E402
from trader.ws.account import PaperAccount  # noqa: E402


# ---------------------------------------------------------------- config

def _write_cfg(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(raw)
    return str(path)


def test_role_defaults_to_consumer(tmp_path):
    path = _write_cfg(tmp_path, "trading:\n  mode: notify\n")
    cfg = load_config(path)
    assert cfg.pipeline.role == "consumer"
    assert cfg.consumers == []
    assert cfg.feed.url == ""


def test_role_info_and_consumers_parsed(tmp_path):
    path = _write_cfg(
        tmp_path,
        """
pipeline:
  role: info
  auth_token: t
consumers:
  - label: owner
    token: abc
    push_url: "http://127.0.0.1:8081/alert"
  - label: friend
    token: def
""",
    )
    cfg = load_config(path)
    assert cfg.pipeline.role == "info"
    assert len(cfg.consumers) == 2
    assert cfg.consumers[0].label == "owner"
    assert cfg.consumers[0].token == "abc"
    assert cfg.consumers[0].push_url == "http://127.0.0.1:8081/alert"
    assert cfg.consumers[1].push_url == ""


def test_role_invalid_falls_back_to_consumer(tmp_path):
    path = _write_cfg(tmp_path, "pipeline:\n  role: banana\n")
    assert load_config(path).pipeline.role == "consumer"


def test_feed_section_parsed(tmp_path):
    path = _write_cfg(
        tmp_path,
        """
feed:
  url: "http://127.0.0.1:8080"
  token: xyz
  poll_seconds: 2.5
""",
    )
    cfg = load_config(path)
    assert cfg.feed.url == "http://127.0.0.1:8080"
    assert cfg.feed.token == "xyz"
    assert cfg.feed.poll_seconds == 2.5


# ---------------------------------------------------------------- ingest

def _info_cfg():
    return ConfigStub(TradingConfig(mode="notify"))


def test_ingest_alert_records_signal_without_trading():
    store = _fresh_store()
    cfg = _info_cfg()
    res = ingest_alert(
        "BOUGHT 0DTE SPY 759c @ 1.5 small", "author", cfg, store,
        channel="player-alerts", ts=1728000000.0,
    )
    assert res["status"] == "recorded"
    assert res["alert"]["ticker"] == "SPY"
    assert res["correction"] is False
    # the signal is in the store, parsed
    rows, total = store.search_history(kind="signals", q="SPY")
    assert total == 1
    assert rows[0]["parsed"] == 1
    # no trades recorded - the info server does not trade
    assert store.search_history(kind="trades")[1] == 0


def test_ingest_alert_dedupes():
    store = _fresh_store()
    cfg = _info_cfg()
    text = "BOUGHT 0DTE SPY 759c @ 1.5 small"
    assert ingest_alert(text, "a", cfg, store)["status"] == "recorded"
    assert (
        ingest_alert(text, "a", cfg, store)["status"] == "ignored"
    )


def test_ingest_alert_chatter_recorded_unparsed():
    store = _fresh_store()
    cfg = _info_cfg()
    res = ingest_alert("gm", "a", cfg, store)
    assert res["status"] == "recorded"
    assert res["alert"] is None
    rows, total = store.search_history(kind="signals")
    assert total == 1 and rows[0]["parsed"] == 0


def test_ingest_alert_empty():
    store = _fresh_store()
    res = ingest_alert("  ", "a", _info_cfg(), store)
    assert res["status"] == "ignored"


# ---------------------------------------------------------------- routes

def _make_app_role(role, auth_token=""):
    """Build an app in the given role with a stubbed config."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"), auth_token=auth_token)
    cfg.pipeline.role = role
    account = PaperAccount(cfg, store)
    from trader.trading.executor import PaperExecutor

    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "trader.web.server", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    return app, store


def _headers(token):
    return {"X-Auth-Token": token, "Content-Type": "application/json"}


def test_info_role_alert_route_ingests_only():
    app, store = _make_app_role("info", auth_token="t")
    client = app.test_client()
    r = client.post(
        "/alert",
        json={"text": "BOUGHT 0DTE SPY 759c @ 1.5 small", "author": "a"},
        headers=_headers("t"),
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "recorded"
    assert store.search_history(kind="trades")[1] == 0


def test_info_role_trading_routes_404():
    app, _ = _make_app_role("info", auth_token="t")
    client = app.test_client()
    h = _headers("t")
    for method, path in (
        ("post", "/api/paper-reset"),
        ("get", "/api/paper-positions"),
        ("post", "/api/position-sell"),
        ("post", "/api/position-tp"),
        ("post", "/api/paper-sell"),
        ("get", "/api/positions"),
        ("post", "/api/paper-resize"),
    ):
        r = client.open(path, method=method, headers=h, json={})
        assert r.status_code == 404, path
        assert "info server" in r.get_json()["error"]


def test_consumer_role_alert_route_executes():
    app, store = _make_app_role("consumer", auth_token="t")
    client = app.test_client()
    r = client.post(
        "/alert",
        json={"text": "BOUGHT 0DTE SPY 759c @ 1.5 small", "author": "a"},
        headers=_headers("t"),
    )
    assert r.status_code == 200
    assert r.get_json()["status"] in ("executed", "notified", "skipped")
    assert store.search_history(kind="trades")[1]


def test_consumer_role_trading_routes_work():
    app, _ = _make_app_role("consumer", auth_token="t")
    client = app.test_client()
    r = client.get("/api/positions", headers=_headers("t"))
    assert r.status_code == 200
