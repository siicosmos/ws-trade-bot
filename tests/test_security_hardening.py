"""Security hardening: TLS verification defaults, token
redaction in error paths, and the info dashboard's session
login."""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import ConfigStub, TradingConfig, _fresh_store  # noqa: E402

pytestmark = pytest.mark.essential

TOKEN = "s3cret-feed-token-value"


class _StopLoop(BaseException):
    """Ends the feed loop under test: the fake requests.get
    raises a plain RuntimeError (which the loop's
    `except Exception` catches), the loop then calls time.sleep -
    patched below to raise this instead, which escapes the loop
    and lands in the test's pytest.raises. BaseException keeps
    the sentinel out of the loop's own error handling."""


class _StopTime:
    """Stands in for feedclient's time module (patched in
    feedclient's namespace, not on the global time module):
    sleep ends the loop, time stays real."""

    @staticmethod
    def sleep(seconds):
        raise _StopLoop()

    @staticmethod
    def time():
        return time.time()


def _run_one_loop_iteration(monkeypatch, cfg, store, state=None):
    """Run feedclient._loop until its first sleep: the fake
    requests.get raises exactly once, the loop's error path then
    reaches time.sleep - which raises _StopLoop and ends the
    loop. Synchronous, in the test's own thread."""
    from consumer import feedclient

    monkeypatch.setattr(feedclient, "time", _StopTime)
    with pytest.raises(_StopLoop):
        feedclient._loop(cfg, store, print, state)


# ------------------------------------------------- tls defaults

def test_verify_ssl_defaults_to_true(tmp_path):
    """a config without the keys verifies TLS - verification off
    must be an explicit opt-out, never a silent default."""
    from core.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        "consumer:\n"
        "  auth_token: t\n"
        "feed:\n"
        "  url: https://info:8081\n"
        "  token: tok\n"
        "consumers: []\n"
    )
    cfg = load_config(str(path))
    assert cfg.feed.verify_ssl is True
    assert cfg.consumers == []


def test_verify_ssl_explicit_false_survives(tmp_path):
    from core.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        "consumer:\n"
        "  auth_token: t\n"
        "feed:\n"
        "  url: https://info:8081\n"
        "  token: tok\n"
        "  verify_ssl: false\n"
        "consumers:\n"
        "  - label: c1\n"
        "    token: tok\n"
        "    push_url: https://c1:8080/alert\n"
        "    push_verify_ssl: false\n"
    )
    cfg = load_config(str(path))
    assert cfg.feed.verify_ssl is False
    assert cfg.consumers[0].push_verify_ssl is False


def test_push_verify_ssl_defaults_to_true(tmp_path):
    from core.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        "info:\n"
        "  auth_token: t\n"
        "consumers:\n"
        "  - label: c1\n"
        "    token: tok\n"
        "    push_url: https://c1:8080/alert\n"
    )
    cfg = load_config(str(path))
    assert cfg.consumers[0].push_verify_ssl is True


def test_feedclient_defaults_verify_to_true(monkeypatch, capsys):
    """a stub config without the verify_ssl attr verifies (the
    getattr fallback must not silently downgrade)."""
    from consumer import feedclient

    class _Feed:
        url = "https://info:8081"
        token = TOKEN

    cfg = type("C", (), {"feed": _Feed()})()
    seen = {}

    def _fake_get(url, params=None, headers=None, timeout=0, verify=True):
        seen["verify"] = verify
        raise RuntimeError("stop")

    import requests as _requests
    monkeypatch.setattr(_requests, "get", _fake_get)
    _run_one_loop_iteration(monkeypatch, cfg, _fresh_store())
    assert seen.get("verify") is True
    out = capsys.readouterr().out
    assert "verification is DISABLED" not in out


def test_feedclient_warns_when_verification_off(monkeypatch, capsys):
    from consumer import feedclient

    class _Feed:
        url = "https://info:8081"
        token = TOKEN
        verify_ssl = False

    cfg = type("C", (), {"feed": _Feed()})()

    def _fake_get(url, params=None, headers=None, timeout=0, verify=True):
        raise RuntimeError("stop")

    import requests as _requests
    monkeypatch.setattr(_requests, "get", _fake_get)
    _run_one_loop_iteration(monkeypatch, cfg, _fresh_store())
    out = capsys.readouterr().out
    assert "verification is DISABLED" in out
    assert "feed.verify_ssl" in out


# ---------------------------------------------------- redaction

def test_redact_replaces_long_secrets_only():
    from core.redact import redact

    assert redact("boom for s3cret-feed-token-value done",
                  TOKEN) == "boom for *** done"
    # a short secret would mangle every matching character
    assert redact("connection to t failed", "t") == (
        "connection to t failed")


def test_redact_url_scrubs_userinfo():
    from core.redact import redact_url

    out = redact_url(
        "https://user:hunter2@host.example/alert", TOKEN)
    assert "hunter2" not in out
    assert "user" not in out
    assert "host.example/alert" in out
    # a plain url passes through untouched
    assert redact_url("http://127.0.0.1:8080/alert") == (
        "http://127.0.0.1:8080/alert")


def test_fanout_last_error_never_carries_token(monkeypatch):
    """an exception whose text embeds the token must not leak it
    into FEED_STATE (the info dashboard renders last_error)."""
    from info import fanout

    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(
            label="c1", token=TOKEN,
            push_url="https://push-target/alert")
    fanout.FEED_STATE["consumers"] = {}

    def _fake_post(url, json=None, headers=None, timeout=0,
                   verify=True):
        raise RuntimeError(f"handshake with {TOKEN} at {url} failed")

    import requests as _requests
    monkeypatch.setattr(_requests, "post", _fake_post)

    orig_retry = fanout.PUSH_RETRY_SECONDS
    fanout.PUSH_RETRY_SECONDS = 0
    try:
        ok, err = fanout._push(entry, {"ts": "2026-10-06T14:30:00+00:00",
                                       "text": "x", "author": "a",
                                       "channel": ""}, True)
    finally:
        fanout.PUSH_RETRY_SECONDS = orig_retry
    assert ok is False
    assert err
    # the dashboard cell keeps the identification
    assert err.startswith("RuntimeError:")
    assert TOKEN not in err
    assert "push-target" not in err


def test_feedclient_error_state_never_carries_token(monkeypatch, capsys):
    """the feed loop's error lands in state (dashboard reader
    line) and the log (posted to discord) - the token must be
    scrubbed from both."""
    from consumer import feedclient

    class _Feed:
        url = "https://info:8081"
        token = TOKEN
        verify_ssl = True

    cfg = type("C", (), {"feed": _Feed()})()
    state = {}

    def _fake_get(url, params=None, headers=None, timeout=0, verify=True):
        raise RuntimeError(f"handshake with {TOKEN} at {url} failed")

    import requests as _requests
    monkeypatch.setattr(_requests, "get", _fake_get)
    _run_one_loop_iteration(monkeypatch, cfg, _fresh_store(), state=state)
    assert state.get("error")
    # the dashboard line keeps the identification (type + message)
    assert state["error"].startswith("RuntimeError:")
    assert TOKEN not in state["error"]
    out = capsys.readouterr().out
    # the backoff line stays ONE line: a long-poll reset is
    # routine (every info restart drops every poller) - type +
    # message, no traceback, and token-free
    assert "RuntimeError: handshake" in out
    assert "Traceback" not in out
    assert TOKEN not in out


# ------------------------------------------------- info login

def _info_app(auth_token="t"):
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token=auth_token)
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    return app, store, cfg


def test_info_dashboard_requires_login():
    app, store, cfg = _info_app(auth_token="t")
    client = app.test_client()
    # the page redirects to the login form
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")
    # the api paths get the clean 401
    for path in ("/api/signals", "/api/levels", "/api/feed-status",
                 "/api/reader_status", "/api/settings",
                 "/api/update_status"):
        assert client.get(path).status_code == 401, path


def test_info_login_flow_and_session_write():
    app, store, cfg = _info_app(auth_token="t")
    client = app.test_client()
    # the token seeded the admin account
    r = client.post("/login", data={"username": "admin",
                                    "password": "t"})
    assert r.status_code == 302
    # the dashboard and the reads open for the session
    assert client.get("/").status_code == 200
    assert client.get("/api/signals").status_code == 200
    # a session can write (no token header needed) - verified
    # through the public read, not the store's internal meta key
    r = client.post("/api/spx-levels", json={"text": "Pivot 6800"})
    assert r.status_code == 200
    assert client.get("/api/levels").get_json()["text"] == "Pivot 6800"
    # the settings endpoint trusts the session (no masking)
    s = client.get("/api/settings").get_json()
    assert "discord" in s
    # logout closes it again
    client.get("/logout")
    assert client.get("/api/signals").status_code == 401


def test_info_session_cookie_is_httponly():
    app, store, cfg = _info_app(auth_token="t")
    client = app.test_client()
    client.post("/login", data={"username": "admin", "password": "t"})
    # grab the Set-Cookie from the login response
    r = client.post("/login", data={"username": "admin",
                                    "password": "t"},
                    follow_redirects=False)
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert cookie.startswith("ws_session_info_")


def test_info_wrong_login_is_rejected():
    app, store, cfg = _info_app(auth_token="t")
    client = app.test_client()
    r = client.post("/login", data={"username": "admin",
                                    "password": "nope"})
    assert r.status_code == 200   # form re-rendered, no session
    assert client.get("/api/signals").status_code == 401


def test_info_feed_stays_consumer_token_guarded():
    """/api/feed is exempt from the session guard - it does its
    own per-consumer token auth (a browser session must not be
    able to read another consumer's feed)."""
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token="t")
    cfg.consumers = [entry]
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    client = app.test_client()
    # no session, wrong consumer token -> the feed's own 401
    r = client.get("/api/feed", headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 401
    # the right consumer token passes without any session
    r = client.get("/api/feed", headers={"X-Auth-Token": "tok1"})
    assert r.status_code == 200


def test_info_alert_stays_reader_token_guarded():
    app, store, cfg = _info_app(auth_token="t")
    client = app.test_client()
    payload = {"text": "BOUGHT 0DTE SPY 759c @ 1.5 small",
               "author": "a"}
    assert client.post("/alert", json=payload).status_code == 401
    r = client.post("/alert", json=payload,
                    headers={"X-Auth-Token": "t"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "recorded"


def test_info_session_key_file_is_separate(tmp_path, monkeypatch):
    """the info server signs its sessions with its own key file -
    sharing the consumer's key would let one app forge the
    other's cookies."""
    import core.web_common as wc

    captured = {}

    def _fake_file(role="consumer"):
        captured["role"] = role
        return str(tmp_path / f".{role}_session_key")

    monkeypatch.setattr(wc, "_session_key_file", _fake_file)
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    try:
        wc.load_secret_key(path, "info")
        assert captured["role"] == "info"
    finally:
        os.unlink(path)


def test_info_signals_serves_the_consumer_backfill():
    """/api/signals is exempt from the session guard for the same
    identity as /api/feed: the consumer's backfill reads it with
    its per-consumer token (every fresh consumer start anchors at
    the head and backfills its recent-alerts list). Regression:
    the session lockdown 401'd that call - the backfill failed on
    every consumer restart while the feed itself kept working."""
    entry = __import__("core.config", fromlist=["ConsumerEntry"]) \
        .ConsumerEntry(label="c1", token="tok1")
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"), auth_token="t")
    cfg.consumers = [entry]
    app = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store)
    client = app.test_client()

    # anonymous and wrong-token requests stay rejected
    assert client.get("/api/signals").status_code == 401
    r = client.get("/api/signals",
                   headers={"X-Auth-Token": "wrong"})
    assert r.status_code == 401

    # the consumer's feed token passes - the backfill path
    r = client.get("/api/signals?limit=10",
                   headers={"X-Auth-Token": "tok1"})
    assert r.status_code == 200
    assert r.get_json() == []

    # a logged-in browser still reads it as before
    client.post("/login", data={"username": "admin", "password": "t"})
    assert client.get("/api/signals").status_code == 200


def test_info_admin_reseeds_when_the_token_rotates():
    """the token IS the info app's admin credential (no users
    panel, no recovery path) - a token rotation after the first
    boot re-seeds the admin login instead of locking the
    dashboard out. regression: the seed only ran on an empty
    users table, so rotating the token stranded the login."""
    import importlib

    info_web = importlib.import_module("info.web")

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="notify"),
                     auth_token="old-token-value")
    info_web.create_app(cfg, store)          # first boot: seeded
    assert store.verify_user("admin", "old-token-value") is not None

    # the token rotates in the yaml; the restart rebuilds the app
    # on the SAME store
    cfg.pipeline.auth_token = "fresh-token-value"
    info_web.create_app(cfg, store)

    # the current token logs in, the old one is gone
    assert store.verify_user("admin", "fresh-token-value") is not None
    assert store.verify_user("admin", "old-token-value") is None
    client = __import__(
        "info.web", fromlist=["create_app"]
    ).create_app(cfg, store).test_client()
    r = client.post("/login", data={"username": "admin",
                                    "password": "fresh-token-value"})
    assert r.status_code == 302
