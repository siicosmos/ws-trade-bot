"""The state-file layout pins: per-role update records, the
root session key, the per-role exit files, the history kind
fallback."""

import os

import core.web_common as wc


def test_update_records_are_per_role(tmp_path):
    from core.ops.updater import update_record_path

    assert update_record_path(str(tmp_path), "info") == (
        os.path.join(str(tmp_path), ".last_update_info.json")
    )
    assert update_record_path(str(tmp_path), "consumer") == (
        os.path.join(str(tmp_path), ".last_update_consumer.json")
    )


def test_session_key_lives_at_the_repo_root(tmp_path, monkeypatch):
    """the login-session signing key is created at the repo
    root (outside the swapped code dirs) and reused across
    restarts - a key inside a swapped dir would log everyone
    out on every update."""
    key_file = tmp_path / ".consumer_session_key"
    monkeypatch.setattr(wc, "_session_key_file",
                        lambda role="consumer": str(key_file))
    k1 = wc.load_secret_key(str(tmp_path / "config" / "x.yaml"))
    assert key_file.exists()
    k2 = wc.load_secret_key(str(tmp_path / "config" / "x.yaml"))
    assert k1 == k2
    # not inside the consumer folder (the pre-refactor home)
    assert not (tmp_path / "consumer" / ".consumer_session_key").exists()


def test_exit_files_are_per_role():
    """the crash-restart exit markers are keyed by role (the
    shared file the old layout used collided)."""
    import consumer.app as capp
    import info.server as iserver

    assert "pipeline_exit_consumer.txt" in capp.EXIT_FILE
    assert "pipeline_exit_info.txt" in iserver.EXIT_FILE


def test_history_search_unknown_kind_falls_back_to_trades(tmp_path):
    from core.parser import parse_alert
    from core.store import Store

    store = Store(str(tmp_path / "t.db"))
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")
    store.record_signal("k1", "a", "BOUGHT 09/25 COIN 210c @ 2.0",
                        parsed=True)
    store.record_trade("paper", alert.action, alert.ticker, 1, 2.0,
                       alert, "executed", "t")
    rows, total = store.search_history(kind="bogus")
    assert total == 1          # the trades table
    assert rows[0]["type"] == "trade"
    rows, total = store.search_history(kind="signals")
    assert total == 1
    assert rows[0]["type"] == "signal"


def test_stop_exit_sells_price_marketable(monkeypatch):
    """a stop exit's sell limit never sits above the live bid (a
    limit above a gap-down market would not fill and the
    position would sit unprotected); a normal sell keeps the
    clamped floor."""
    import tests.test_live_executor as le

    ws = le.FakeWS(ask=1.5, bid=0.50)
    le._patch_client(monkeypatch, ws)
    cfg = le._live_cfg()
    store = le._store()
    account = le.StubAccount(le._account_values())
    ex = le._executor(cfg, store, account)

    # hold 2 contracts, then fire a stop exit
    from core.parser import parse_alert
    buy = parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 medium size")
    assert ex.execute(buy, cfg, store).ok
    ws.orders.clear()

    stop = parse_alert("SOLD 0DTE SPY 759c @ 0.5")
    stop.stop_exit = True
    res = ex.execute(stop, cfg, store)
    assert res.ok
    name, qty, limit = ws.orders[-1]
    # the limit is at (or below) the live bid - marketable
    assert limit <= 0.50 + 1e-9, (name, qty, limit)

    # a non-stop sell keeps the slippage floor above the bid
    ws.orders.clear()
    store.apply_position(
        "live", buy, 2, premium=1.2, account="RRSP"
    )
    normal = parse_alert("SOLD 0DTE SPY 759c @ 1.2")
    res2 = ex.execute(normal, cfg, store)
    assert res2.ok and ws.orders, res2.detail
    name2, qty2, limit2 = ws.orders[-1]
    assert limit2 >= 0.50   # the clamp holds the floor


def test_concurrent_buys_respect_the_daily_cap(monkeypatch):
    """the risk gates re-check under the order lock: N concurrent
    deliveries with max_trades_per_day=1 execute exactly once."""
    import threading

    import tests.test_live_executor as le

    ws = le.FakeWS(ask=1.5, bid=1.2)
    le._patch_client(monkeypatch, ws)
    cfg = le._live_cfg()
    cfg.trading.max_trades_per_day = 1
    store = le._store()
    account = le.StubAccount(le._account_values())
    ex = le._executor(cfg, store, account)

    alert = le.parse_alert("BOUGHT 0DTE SPY 759c @ 1.2 medium size")
    results = []
    lock = threading.Lock()

    def run():
        r = ex.execute(alert, cfg, store)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed = [r for r in results if r.ok]
    gated = [r for r in results if "risk gate" in r.detail]
    assert len(executed) == 1, [r.detail for r in results]
    assert len(gated) == 3
    assert len(ws.orders) == 1


def test_et_day_bucketing(monkeypatch, tmp_path):
    """the day-boundary gates bucket by the ET calendar day: a
    trade booked on the 9th (ET) is invisible to a check on the
    10th (ET) even though the utc clock has not flipped."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    import core.store as cs
    from core.parser import parse_alert

    def et_at(day, hour):
        return datetime(
            2026, 10, day, hour, 0, tzinfo=ZoneInfo("America/New_York")
        )

    store = cs.Store(str(tmp_path / "t.db"))
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")

    # 23:59 ET on the 9th (= 03:59 utc on the 10th): the trade
    # books on the ET day "2026-10-09"
    monkeypatch.setattr(cs, "et_now", lambda: et_at(9, 23))
    store.record_trade("paper", alert.action, alert.ticker, 1, 2.0,
                       alert, "executed", "t")
    assert store.trades_today("paper") == 1

    # 00:01 ET on the 10th (utc still the 10th too): the et day
    # flipped - the counter resets
    monkeypatch.setattr(cs, "et_now", lambda: et_at(10, 0))
    assert store.trades_today("paper") == 0


def test_launcher_bats_are_ascii_crlf():
    """cmd reads batch files in the oem codepage (gbk on the
    owner's box) - a utf-8 middle dot decoded as a hanzi that
    swallowed the next line's leading 'r' and cmd executed
    'em ...'. the launchers stay pure ascii + crlf."""
    import glob

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bats = glob.glob(os.path.join(root, "scripts", "*.bat"))
    assert bats, "no launcher scripts found"
    for path in bats:
        raw = open(path, "rb").read()
        raw.decode("ascii")   # raises on any non-ascii byte
        assert b"\r\n" in raw, path
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0, (
            f"{path}: bare lf lines"
        )


def test_info_settings_rollback_on_write_failure(monkeypatch):
    """a failed config write rolls the in-memory settings back -
    the running process must not diverge from what a restart
    would load."""
    import importlib

    info_web = importlib.import_module("info.web")
    core_config = importlib.import_module("core.config")

    # build a minimal info config
    import tempfile as tf

    fd, path = tf.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(
            "info:\n"
            "  auth_token: tok\n"
            "auto_update:\n"
            "  enabled: true\n"
            "  interval_seconds: 600\n"
        )
    try:
        cfg = core_config.load_config(path)
        store = __import__("core.store", fromlist=["Store"]).Store(
            str(path + ".db")
        )
        app = info_web.create_app(cfg, store, config_path=path)
        client = app.test_client()
        hdr = {"X-Auth-Token": "tok"}

        before_enabled = cfg.auto_update.enabled
        before_interval = cfg.auto_update.interval_seconds

        def boom(raw, config_path):
            raise OSError("disk full")

        monkeypatch.setattr(core_config, "dump_yaml_config", boom)
        r = client.post(
            "/api/settings", headers=hdr,
            json={"auto_update": {"enabled": False,
                                  "interval_seconds": 30}},
        )
        assert r.status_code == 500
        # the in-memory config rolled back
        assert cfg.auto_update.enabled == before_enabled
        assert cfg.auto_update.interval_seconds == before_interval
    finally:
        os.unlink(path)


def test_exit_marker_is_read_next_to_the_db(tmp_path):
    """the launcher writes db\\pipeline_exit_<role>.txt - the
    marker is read from the db's directory (a bare relative read
    looked in the cwd and the previous-run banner never fired)
    and removed after the read."""
    from core.ops.updater import pop_exit_marker

    db = tmp_path / "db" / "consumer.trades.db"
    db.parent.mkdir()
    marker = db.parent / "pipeline_exit_consumer.txt"
    marker.write_text("consumer app exited with code 1",
                      encoding="utf-8")
    prev = pop_exit_marker(str(db), "consumer")
    assert prev == "consumer app exited with code 1"
    assert not marker.exists()
    assert pop_exit_marker(str(db), "consumer") == ""


def test_startup_config_merge_adds_missing_keys(tmp_path):
    """a new release's config knobs land in the live config at
    startup: the example's missing keys are appended to their
    section with their comments - add-only (the user's values
    are never touched) and idempotent."""
    from core.ops.config_merge import merge_new_config_keys

    live = tmp_path / "consumer.config.yaml"
    live.write_text(
        "consumer:\n"
        "  auth_token: my-token\n"
        "\n"
        "trading:\n"
        "  mode: paper\n"
        "  stop_loss_pct: 35\n",
        encoding="utf-8",
    )
    example = tmp_path / "consumer.example.config.yaml"
    example.write_text(
        "consumer:\n"
        "  auth_token: x\n"
        "\n"
        "trading:\n"
        "  mode: notify\n"
        "  stop_loss_pct: 25\n"
        "  trailing_stop_pct: 0    # % off the peak\n"
        "  adaptive_trail: true\n"
        "\n"
        "quotes:\n"
        "  enabled: false\n",
        encoding="utf-8",
    )
    added = merge_new_config_keys(str(live), str(example))
    assert "trading.trailing_stop_pct" in added
    assert "trading.adaptive_trail" in added
    assert "quotes.enabled" in added
    import yaml as _yaml

    d = _yaml.safe_load(live.read_text(encoding="utf-8"))
    # the user's values untouched
    assert d["consumer"]["auth_token"] == "my-token"
    assert d["trading"]["mode"] == "paper"
    assert d["trading"]["stop_loss_pct"] == 35
    # the new keys landed with their comments
    text = live.read_text(encoding="utf-8")
    assert "adaptive_trail: true" in text
    assert "# % off the peak" in text
    assert d["quotes"]["enabled"] is False
    # idempotent
    assert merge_new_config_keys(str(live), str(example)) == []
