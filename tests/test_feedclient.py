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


def test_wrong_token_is_reported_specifically(monkeypatch):
    """a wrong feed.token used to be fully silent: the state
    captured a generic error that nothing rendered, and no log
    line said why. the error now names the mismatch and lands in
    the reader line via the state."""
    import requests as _requests

    store = _fresh_store()
    cfg = _FeedCfg()
    calls = []
    state = {}

    class _Resp:
        status_code = 401

        def raise_for_status(self):
            import requests as _r

            raise _r.HTTPError("401 Client Error")

        def json(self):
            return {}

    def _fake_get(url, params=None, headers=None, timeout=0,
                  verify=False):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(feedclient.time, "sleep", lambda s: None)

    t = feedclient.start_feed_client(cfg, store, lambda *a: None,
                                     state=state)
    deadline = time.time() + 2
    while time.time() < deadline and "error" not in state:
        time.sleep(0.05)
    assert state.get("ok") is False
    assert "feed token rejected (401)" in state.get("error", "")
    t.join(timeout=1)


def test_backfill_claims_recent_signals_without_executing(monkeypatch):
    """a wiped/rebuilt consumer db backfills the info server's
    recent signals as ALREADY-CLAIMED rows: the dashboard's recent
    alerts list survives, and an old alert can never re-trade
    (the claim is the dedupe)."""
    import requests as _requests

    store = _fresh_store()
    cfg = _FeedCfg()
    executed = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            if not executed:   # the anchor call
                return {"alerts": [], "cursor": 41, "levels": ""}
            return {"alerts": [], "cursor": 41, "levels": ""}

    # the anchor + the backfill's /api/signals call
    signals = [{
        "message_key": "k-backfill-1",
        "ts": "2026-10-06T14:30:00+00:00",
        "author": "a",
        "text": "BOUGHT 0DTE SPY 759c @ 1.5 small",
        "parsed": True,
        "correction": False,
        "channel": "player-alerts",
    }]

    def _fake_get(url, params=None, headers=None, timeout=0,
                  verify=False):
        if "/api/signals" in url:
            return _Resp2(signals)
        return _Resp()

    class _Resp2(_Resp):
        def __init__(self, rows):
            self._rows = rows

        def json(self):
            return self._rows

    monkeypatch.setattr(_requests, "get", _fake_get)
    monkeypatch.setattr(feedclient.time, "sleep", lambda s: None)

    def on_alert(text, author, ts, channel):
        executed.append(text)   # would trade

    t = feedclient.start_feed_client(cfg, store, on_alert)
    deadline = time.time() + 2
    while time.time() < deadline:
        if store.seen_signal("k-backfill-1"):
            break
        time.sleep(0.05)

    # the backfilled signal is claimed (visible + deduped)...
    assert store.seen_signal("k-backfill-1")
    # the recent-signals view carries the text (no keys) - the
    # dashboard's list is what we are asserting on
    rows = store.recent_signals(limit=10)
    assert any("SPY 759c" in r["text"] for r in rows)
    # ...and was NOT executed (no on_alert call, no trade)
    assert executed == []
    t.join(timeout=1)
