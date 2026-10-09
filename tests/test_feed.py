"""Alert feed: consumer-token auth, since-cursor, long-poll,
push fan-out, and the atomic dedupe that makes dual delivery
(push + pull) safe."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_pipeline import _fresh_store, ConfigStub, TradingConfig  # noqa: E402
from info import fanout  # noqa: E402
from consumer.pipeline import ingest_alert, process_alert  # noqa: E402
from consumer.trading.risk import RiskEngine  # noqa: E402
from consumer.ws.account import PaperAccount  # noqa: E402
import pytest  # noqa: E402


def _info_app(consumers):
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token="admin-token")
    cfg.consumers = consumers
    fanout.FEED_STATE["consumers"] = {}  # fresh shared state per test
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    return app, store, cfg


def _h(token):
    return {"X-Auth-Token": token}


def _record(store, text, author="a", parsed=True):
    return ingest_alert(text, author, _info_cfg(), store,
                        channel="player-alerts", ts=1728000000.0)


def _info_cfg():
    return ConfigStub(TradingConfig(mode="notify"))


# ---------------------------------------------------------------- feed api

pytestmark = pytest.mark.essential


def test_feed_rejects_bad_token():
    app, store, cfg = _info_app(
        [__import__("core.config", fromlist=["ConsumerEntry"])
         .ConsumerEntry(label="c1", token="tok1")]
    )
    client = app.test_client()
    r = client.get("/api/feed", headers=_h("wrong"))
    assert r.status_code == 401
    r = client.get("/api/feed")
    assert r.status_code == 401


@pytest.mark.minimum
def test_feed_serves_alerts_since_cursor():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    app, store, cfg = _info_app([entry])
    r1 = _record(store, "BOUGHT 0DTE SPY 759c @ 1.5 small")
    r2 = _record(store, "BOUGHT 0DTE QQQ 300c @ 2.0 tiny")
    client = app.test_client()
    resp = client.get("/api/feed?since=0", headers=_h("tok1"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["alerts"]) == 2
    assert data["cursor"] == 2
    first = data["alerts"][0]
    assert first["id"] == 1
    assert first["alert"]["ticker"] == "SPY"
    assert first["channel"] == "player-alerts"
    # incremental: only newer rows after the cursor
    resp = client.get("/api/feed?since=1", headers=_h("tok1"))
    data = resp.get_json()
    assert len(data["alerts"]) == 1
    assert data["alerts"][0]["alert"]["ticker"] == "QQQ"


def test_feed_includes_levels():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    app, store, cfg = _info_app([entry])
    store.meta_set("spx_levels_text", "Pivot 6800\nR1 6810")
    client = app.test_client()
    resp = client.get("/api/feed?since=0&wait=0", headers=_h("tok1"))
    assert resp.get_json()["levels"] == "Pivot 6800\nR1 6810"


def test_feed_long_poll_wakes_on_new_signal():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    app, store, cfg = _info_app([entry])
    client = app.test_client()

    import threading

    def _deliver():
        time.sleep(1.0)
        _record(store, "BOUGHT 0DTE SPY 759c @ 1.5 small")

    t = threading.Thread(target=_deliver, daemon=True)
    t.start()
    start = time.time()
    resp = client.get("/api/feed?since=0&wait=10", headers=_h("tok1"))
    elapsed = time.time() - start
    assert resp.get_json()["alerts"], "long-poll returned the new alert"
    assert elapsed < 5, f"woke on delivery ({elapsed:.1f}s), not after the wait"


def test_feed_records_last_seen():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    app, store, cfg = _info_app([entry])
    client = app.test_client()
    client.get("/api/feed?since=0&wait=0", headers=_h("tok1"))
    snap = fanout.snapshot()["consumers"]
    assert len(snap) == 1 and snap[0]["label"] == "c1"
    assert snap[0]["last_seen"] is not None


def test_feed_status_lists_all_configured_consumers():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1", push_url="http://x/alert")
    app, store, cfg = _info_app([entry])
    client = app.test_client()
    resp = client.get("/api/feed-status", headers=_h("admin-token"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["consumers"]) == 1
    c = data["consumers"][0]
    assert c["label"] == "c1"
    assert c["push_url"] == "http://x/alert"
    assert c["alive"] is False  # never polled


# ---------------------------------------------------------------- fan-out

def test_fanout_pushes_new_signals():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(
            label="c1", token="tok1", push_url="http://push-target/alert"
        )
    app, store, cfg = _info_app([entry])
    delivered = []

    class _Resp:
        status_code = 200

    def _fake_post(url, json=None, headers=None, timeout=0, verify=False):
        delivered.append((url, json, headers))
        return _Resp()

    import requests as _requests
    orig = _requests.post
    _requests.post = _fake_post
    try:
        from info.fanout import _tick
        _record(store, "BOUGHT 0DTE SPY 759c @ 1.5 small")
        _tick(store, [entry], {entry.token: 0})
    finally:
        _requests.post = orig

    assert len(delivered) == 1
    url, payload, headers = delivered[0]
    assert url == "http://push-target/alert"
    assert headers["X-Auth-Token"] == "tok1"
    assert payload["text"] == "BOUGHT 0DTE SPY 759c @ 1.5 small"
    assert payload["channel"] == "player-alerts"
    snap = fanout.snapshot()["consumers"][0]
    assert snap["pushed"] == 1


def test_fanout_push_failure_advances_cursor():
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(
            label="c1", token="tok1", push_url="http://push-target/alert"
        )
    app, store, cfg = _info_app([entry])

    class _Resp:
        status_code = 500

    def _fake_post(url, json=None, headers=None, timeout=0, verify=False):
        return _Resp()

    import requests as _requests
    orig = _requests.post
    _requests.post = _fake_post
    try:
        fanout.FEED_STATE["consumers"] = {}
        _record(store, "BOUGHT 0DTE SPY 759c @ 1.5 small")
        from info import fanout as f

        orig_retry = f.PUSH_RETRY_SECONDS
        f.PUSH_RETRY_SECONDS = 0
        try:
            f._tick(store, [entry], {entry.token: 0})
        finally:
            f.PUSH_RETRY_SECONDS = orig_retry
    finally:
        _requests.post = orig

    snap = fanout.snapshot()["consumers"][0]
    assert snap["push_failed"] >= 1
    assert snap["last_error"]


# ------------------------------------------------- atomic dual delivery

def test_dual_delivery_dedupe_race():
    """push + pull delivering the same message concurrently:
    exactly one process_alert executes it."""
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    from consumer.trading.executor import PaperExecutor

    executor = PaperExecutor(cfg, store, account)
    text = "BOUGHT 0DTE SPY 759c @ 1.5 small"
    r1 = process_alert(text, "a", cfg, store, risk, executor)
    r2 = process_alert(text, "a", cfg, store, risk, executor)
    assert r1["status"] == "executed"
    assert r2["status"] == "ignored"
    assert r2["reason"] == "duplicate message"
    # the position was opened exactly once
    positions = store.list_positions("paper")
    assert len(positions) == 1


def test_fanout_cursors_keyed_by_label():
    """the cursors key by label (the identity everywhere) - a
    regression to token-keying would make two consumers sharing
    a token skip each other's rows; the successful consumer's
    cursor advances past the failed one's row."""
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(
            label="c1", token="tok1", push_url="http://push-target/alert"
        )
    app, store, cfg = _info_app([entry])

    delivered = []

    def _fake_post(url, json=None, headers=None, timeout=0, verify=False):
        delivered.append((url, json, headers))

        class _Resp:
            status_code = 200

        return _Resp()

    import requests as _requests
    orig = _requests.post
    _requests.post = _fake_post
    try:
        fanout.FEED_STATE["consumers"] = {}
        _record(store, "BOUGHT 0DTE SPY 759c @ 1.5 small")
        from info.fanout import _tick
        cursors = {entry.label: 0}
        _tick(store, [entry], cursors)
        # the cursor advanced under the LABEL key
        assert cursors.get(entry.label, 0) > 0
    finally:
        _requests.post = orig


def test_fanout_duplicate_labels_are_rejected():
    """two consumers sharing a label would share one feed
    cursor - the config refuses the duplicate at load."""
    import os
    import tempfile as tf

    from core.config import load_config

    yaml_src = (
        "pipeline:\n"
        "  role: info\n"
        "  auth_token: tok\n"
        "consumers:\n"
        "  - label: dup\n"
        "    token: t1\n"
        "  - label: dup\n"
        "    token: t2\n"
    )
    fd, path = tf.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(yaml_src)
    try:
        cfg = load_config(path)
        labels = [c.label for c in cfg.consumers if c.label == "dup"]
        assert len(labels) == 1
    finally:
        os.unlink(path)
