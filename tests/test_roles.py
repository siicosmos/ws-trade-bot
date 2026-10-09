"""App split: the info server (reader ingest + feed, no
trading) vs the consumer app (the trading pipeline)."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

from test_pipeline import _fresh_store, ConfigStub, TradingConfig  # noqa: E402
from core.config import FeedConfig, load_config  # noqa: E402
from info.ingest import ingest_alert  # noqa: E402
from consumer.trading.risk import RiskEngine  # noqa: E402
from consumer.ws.account import PaperAccount  # noqa: E402


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
info:
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


def test_unknown_role_section_defaults_to_consumer(tmp_path):
    # an unrecognized section name is not a role - the consumer
    # default applies
    path = _write_cfg(tmp_path, "banana:\n  role: info\n")
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

def _make_consumer_app(auth_token=""):
    """Build the consumer (trading) app with a stubbed config."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"), auth_token=auth_token)
    account = PaperAccount(cfg, store)
    from consumer.trading.executor import PaperExecutor

    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "consumer.web", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    return app, store


def _make_info_app(auth_token=""):
    """Build the info (alert source) app with a stubbed config."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token=auth_token)
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    return app, store


def _headers(token):
    return {"X-Auth-Token": token, "Content-Type": "application/json"}


def test_info_app_alert_route_ingests_only():
    app, store = _make_info_app(auth_token="t")
    client = app.test_client()
    r = client.post(
        "/alert",
        json={"text": "BOUGHT 0DTE SPY 759c @ 1.5 small", "author": "a"},
        headers=_headers("t"),
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "recorded"
    assert store.search_history(kind="trades")[1] == 0


def test_info_app_has_no_trading_routes():
    """The trading routes do not exist on the info app - the
    executors, ledgers and positions live on consumer apps."""
    app, _ = _make_info_app(auth_token="t")
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


def test_consumer_app_alert_route_executes():
    app, store = _make_consumer_app(auth_token="t")
    client = app.test_client()
    r = client.post(
        "/alert",
        json={"text": "BOUGHT 0DTE SPY 759c @ 1.5 small", "author": "a"},
        headers=_headers("t"),
    )
    assert r.status_code == 200
    assert r.get_json()["status"] in ("executed", "notified", "skipped")
    assert store.search_history(kind="trades")[1]


def test_consumer_app_trading_routes_work():
    app, _ = _make_consumer_app(auth_token="t")
    client = app.test_client()
    r = client.get("/api/positions", headers=_headers("t"))
    assert r.status_code == 200


# ---------------------------------------------------------------- mode api

def _tmp_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("trading:\n  mode: notify\n  stop_loss_pct: 25\n")
    return str(path)


def _app_with_config(config_path, auth_token="t"):
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token=auth_token)
    account = PaperAccount(cfg, store)
    from consumer.trading.executor import PaperExecutor

    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "consumer.web", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account,
        config_path=config_path,
    )
    return app, store, cfg


def test_mode_endpoint_persists_and_reports_restart(tmp_path):
    config_path = _tmp_config(tmp_path)
    app, store, cfg = _app_with_config(config_path)
    client = app.test_client()
    r = client.post(
        "/api/mode", json={"mode": "paper"}, headers=_headers("t")
    )
    assert r.status_code == 200
    data = r.get_json()
    # no restart callback on the test app - it reports no restart
    assert data["restarting"] is False
    assert data["mode"] == "paper"
    # persisted to the config file
    import yaml

    raw = yaml.safe_load(open(config_path, encoding="utf-8"))
    assert raw["trading"]["mode"] == "paper"
    # and the live config object moved too
    assert cfg.trading.mode == "paper"


def test_mode_endpoint_restarts_via_callback(tmp_path):
    config_path = _tmp_config(tmp_path)
    app, store, cfg = _app_with_config(config_path)
    restarted = []
    app.restart_pipeline = lambda *a, **k: restarted.append(1)
    client = app.test_client()
    r = client.post(
        "/api/mode", json={"mode": "paper"}, headers=_headers("t")
    )
    assert r.get_json()["restarting"] is True
    deadline = time.time() + 5
    while not restarted and time.time() < deadline:
        time.sleep(0.05)
    assert restarted, "the restart callback fired after the response"


def test_mode_endpoint_validation(tmp_path):
    config_path = _tmp_config(tmp_path)
    app, store, cfg = _app_with_config(config_path)
    client = app.test_client()
    r = client.post(
        "/api/mode", json={"mode": "banana"}, headers=_headers("t")
    )
    assert r.status_code == 400
    # same mode: no-op, no restart
    r = client.post(
        "/api/mode", json={"mode": "notify"}, headers=_headers("t")
    )
    assert r.get_json()["restarting"] is False


def test_mode_endpoint_requires_admin(tmp_path):
    config_path = _tmp_config(tmp_path)
    app, store, cfg = _app_with_config(config_path)
    client = app.test_client()
    r = client.post("/api/mode", json={"mode": "paper"})
    assert r.status_code == 401


def test_mode_endpoint_survives_missing_config(tmp_path):
    # a config path whose directory does not exist: the persist
    # fails, the endpoint reports 500 and the live mode stays
    app, store, cfg = _app_with_config(
        str(tmp_path / "nope" / "config.yaml")
    )
    client = app.test_client()
    r = client.post(
        "/api/mode", json={"mode": "live"}, headers=_headers("t")
    )
    assert r.status_code == 500
    assert cfg.trading.mode == "notify"


def test_spx_levels_readonly_with_feed(tmp_path):
    """levels on a consumer are feed-owned: the ladder serves
    the store copy the feedclient syncs, and there is no write
    endpoint at all."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token="t")
    cfg.feed = FeedConfig(url="http://info:8080", token="x")
    account = PaperAccount(cfg, store)
    from consumer.trading.executor import PaperExecutor

    risk = RiskEngine(cfg, store, account)
    app = __import__(
        "consumer.web", fromlist=["create_app"]
    ).create_app(
        cfg, store, risk, PaperExecutor(cfg, store, account), account
    )
    client = app.test_client()
    # the write endpoint is gone - levels come from the feed
    r = client.post(
        "/api/spx-levels", json={"text": "Pivot 6800"},
        headers=_headers("t"),
    )
    assert r.status_code == 404
    # the ladder serves the store copy (what the feed synced)
    store.meta_set("spx_levels_text", "Pivot 6800")
    r = client.get("/api/spx", headers=_headers("t"))
    assert r.get_json()["text"] == "Pivot 6800"


def test_get_settings_includes_mode():
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    from consumer.settings import get_settings

    s = get_settings(cfg)
    assert s["trading"]["mode"] == "paper"


def test_session_cookies_never_collide_between_apps():
    """Cookies ignore ports - two apps on one host (two
    consumers, or a consumer next to anything else) must set
    differently-named session cookies or each login logs the
    other out."""
    from consumer.web import create_app

    apps = []
    for port in (8081, 8082):
        store = _fresh_store()
        cfg = ConfigStub(TradingConfig(mode="paper"), auth_token="t")
        cfg.pipeline.port = port
        account = PaperAccount(cfg, store)
        from consumer.trading.executor import PaperExecutor
        from consumer.trading.risk import RiskEngine

        risk = RiskEngine(cfg, store, account)
        apps.append(create_app(
            cfg, store, risk,
            PaperExecutor(cfg, store, account), account,
        ))
    names = []
    for app in apps:
        with app.test_request_context():
            names.append(app.config["SESSION_COOKIE_NAME"])
    assert names == ["ws_session_8081", "ws_session_8082"]
    assert len(set(names)) == 2


def test_info_write_routes_are_token_guarded():
    # a fake levels text reaches every consumer's ladder and a
    # fake settings POST redirects this app's webhooks - both
    # guard with the reader's token, like POST /alert
    app, store = _make_info_app(auth_token="t")
    client = app.test_client()

    for path, payload in (
        ("/api/spx-levels", {"text": "fake levels"}),
        ("/api/settings", {"auto_update": {"enabled": False}}),
    ):
        r = client.post(path, json=payload)
        assert r.status_code == 401, path
        r = client.post(path, json=payload, headers=_headers("wrong"))
        assert r.status_code == 401, path
        r = client.post(path, json=payload, headers=_headers("t"))
        assert r.status_code == 200, path

    # the levels text actually saved
    assert store.meta_get("spx_levels_text") == "fake levels"

    # an info server without a token configured: the guard stays
    # closed (a guarded route must never turn open by misconfig)
    app2, _ = _make_info_app(auth_token="")
    client2 = app2.test_client()
    r = client2.post("/api/spx-levels", json={"text": "x"})
    assert r.status_code == 401


def test_info_reader_status_is_token_guarded():
    """a fake heartbeat would mask a dead reader on the dashboard -
    the POST carries the same guard as the other write routes."""
    app, store = _make_info_app(auth_token="t")
    client = app.test_client()

    r = client.post("/api/reader_status",
                    json={"channel": "fake", "ok": True})
    assert r.status_code == 401
    r = client.post("/api/reader_status", json={"channel": "fake",
                                                "ok": True},
                    headers=_headers("wrong"))
    assert r.status_code == 401
    r = client.post("/api/reader_status", json={"channel": "real",
                                                "ok": True},
                    headers=_headers("t"))
    assert r.status_code == 200
    # the state was only touched by the authenticated post
    state = client.get("/api/reader_status").get_json()
    assert state["channel"] == "real"


def test_info_settings_get_masks_webhooks_without_token():
    """the info dashboard is open-read, but webhook urls are
    bearer credentials - only a request carrying the write token
    sees the real values."""
    app, store = _make_info_app(auth_token="t")
    client = app.test_client()

    anon = client.get("/api/settings").get_json()
    assert anon["discord"]["consumer_log_webhook_url"] == ""
    assert anon["discord"]["update_webhook_url"] == ""

    # set real values through the guarded POST
    r = client.post("/api/settings", json={"discord": {
        "consumer_log_webhook_url":
            "https://discord.com/api/webhooks/log123",
        "update_webhook_url":
            "https://discord.com/api/webhooks/upd456",
    }}, headers=_headers("t"))
    assert r.status_code == 200

    anon = client.get("/api/settings").get_json()
    assert anon["discord"]["consumer_log_webhook_url"] == "••••••••"
    assert anon["discord"]["update_webhook_url"] == "••••••••"

    trusted = client.get("/api/settings", headers=_headers("t")).get_json()
    assert trusted["discord"]["consumer_log_webhook_url"] == (
        "https://discord.com/api/webhooks/log123")
    assert trusted["discord"]["update_webhook_url"] == (
        "https://discord.com/api/webhooks/upd456")

    # a masked value round-tripping in a POST means "unchanged"
    r = client.post("/api/settings", json={"discord": {
        "consumer_log_webhook_url": "••••••••",
        "update_webhook_url": "••••••••",
    }}, headers=_headers("t"))
    assert r.status_code == 200
    trusted = client.get("/api/settings", headers=_headers("t")).get_json()
    assert trusted["discord"]["consumer_log_webhook_url"] == (
        "https://discord.com/api/webhooks/log123")


def test_non_ascii_token_header_is_401_not_500():
    """headers decode as latin-1 - a non-ascii token value used to
    raise a TypeError inside compare_digest (a 500) instead of a
    clean 401."""
    app, store = _make_info_app(auth_token="t")
    client = app.test_client()
    r = client.post("/alert", json={"text": "x"},
                    headers={"X-Auth-Token": "tökën"})
    assert r.status_code == 401


def test_info_feed_rejects_unknown_consumer_token_without_seen():
    """a pull with a token that matches no consumers[] entry gets a
    401 and must not touch the consumer's last_seen - the table's
    dot goes hollow (the visible symptom) while the rejection is
    logged on the server."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token="t")
    cfg.consumers = [type(
        "C", (), {"label": "owner", "token": "t", "push_url": "",
                  "push_verify_ssl": False},
    )()]
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    client = app.test_client()

    # the fan-out state is module-level and persists across tests
    fanout = __import__("info.fanout", fromlist=["FEED_STATE"])
    with fanout._lock:
        fanout.FEED_STATE["consumers"].clear()

    # an unknown token: rejected, and the consumer state untouched
    r = client.get("/api/feed", headers=_headers("wrong"))
    assert r.status_code == 401
    snap = __import__(
        "info.fanout", fromlist=["snapshot"]).snapshot()
    assert snap["consumers"] == []

    # the right token anchors and marks the consumer seen
    client.get("/api/feed", headers=_headers("t"))
    snap = __import__(
        "info.fanout", fromlist=["snapshot"]).snapshot()
    assert len(snap["consumers"]) == 1
    assert snap["consumers"][0]["last_seen"] is not None
