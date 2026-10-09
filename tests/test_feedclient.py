"""Consumer-side feed client: head anchoring, alert delivery,
levels sync, error backoff."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_pipeline import _fresh_store, ConfigStub, TradingConfig  # noqa: E402
from consumer import feedclient  # noqa: E402
import pytest  # noqa: E402


pytestmark = pytest.mark.essential

class _FeedCfg:
    def __init__(self, url="http://info:8080", token="tok"):
        self.feed = type("F", (), {
            "url": url, "token": token,
            "poll_seconds": 0.5, "verify_ssl": False,
        })()


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(f"HTTP {self.status_code}")


def _store_with_meta():
    store = _fresh_store()
    return store


def test_start_feed_client_requires_config():
    cfg = _FeedCfg(url="")
    assert feedclient.start_feed_client(cfg, None, print) is None
    cfg = _FeedCfg(token="")
    assert feedclient.start_feed_client(cfg, None, print) is None


@pytest.mark.minimum
def test_client_anchors_at_head_then_delivers(monkeypatch):
    """First call (no cursor) returns the head - no history
    replay; subsequent long-polls deliver new alerts."""
    store = _fresh_store()
    cfg = _FeedCfg()
    calls = []
    state = {"phase": "head"}

    def _fake_get(url, params=None, headers=None, timeout=0, verify=False):
        if params is None:
            # head-only anchor
            assert "since" not in (params or {})
            state["phase"] = "polling"
            return _Resp({"alerts": [], "cursor": 41, "levels": ""})
        assert params["since"] == 41 and params["wait"] == 25
        if state["phase"] == "polling":
            state["phase"] = "done"
            return _Resp({
                "alerts": [{
                    "id": 42,
                    "ts": "2026-10-06T14:30:00+00:00",
                    "author": "a",
                    "text": "BOUGHT 0DTE SPY 759c @ 1.5 small",
                    "correction": False,
                    "channel": "player-alerts",
                    "alert": {"ticker": "SPY"},
                }],
                "levels": "Pivot 6800",
                "cursor": 42,
            })
        # stay quiet afterwards
        return _Resp({"alerts": [], "cursor": 42, "levels": "Pivot 6800"})

    import requests as _requests
    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(feedclient.time, "sleep", lambda s: None)

    def _on_alert(text, author, ts, channel):
        calls.append((text, author, ts, channel))

    import threading

    t = feedclient.start_feed_client(cfg, store, _on_alert)
    # give the loop a few iterations
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls, "feed client delivered the alert"
    text, author, ts, channel = calls[0]
    assert text == "BOUGHT 0DTE SPY 759c @ 1.5 small"
    assert author == "a"
    assert channel == "player-alerts"
    # ts converted from the iso string to epoch
    assert abs(ts - 1791297000.0) < 1
    # levels synced into the local store
    assert store.meta_get("spx_levels_text") == "Pivot 6800"


def test_client_backs_off_on_errors(monkeypatch):
    store = _fresh_store()
    cfg = _FeedCfg()
    sleeps = []

    def _fake_get(*a, **k):
        raise RuntimeError("connection refused")

    import requests as _requests
    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(feedclient.time, "sleep", sleeps.append)

    import threading
    t = threading.Thread(
        target=feedclient._loop, args=(cfg, store, print), daemon=True,
    )
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline:
        # the patched sleep is the global time.sleep - this poll
        # loop records its own 0.05s naps too; the backoff sleeps
        # are the >= 1s ones
        time.sleep(0.05)
        backoffs = [s for s in sleeps if s >= 1]
        if len(backoffs) >= 3:
            break
    # exponential: 2, 4, 8...
    assert backoffs[:3] == [2.0, 4.0, 8.0]
