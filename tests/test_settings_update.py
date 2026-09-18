import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trader.account import PaperAccount
from trader.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig, \
    AutoUpdateConfig, QuotesConfig
from trader.executor import PaperExecutor
from trader.parser import parse_alert
from trader.quotes import MoomooQuoteProvider
from trader.risk import RiskEngine
from trader.settings import apply_settings, get_settings
from trader.stops import StopMonitor
from trader.store import Store
from trader.updater import AutoUpdater


class ConfigStub:
    def __init__(self, trading=None, accounts=None, auth_token=""):
        self.trading = trading or TradingConfig(mode="paper")
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.discord = type("D", (), {"webhook_url": ""})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])
        self.reader = ReaderConfig()
        self.auto_update = AutoUpdateConfig()
        self.quotes = QuotesConfig()


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def test_get_settings_shape():
    cfg = ConfigStub(TradingConfig(mode="paper"))
    s = get_settings(cfg)
    assert "risk_per_trade_pct" in s["trading"]
    assert "small" in s["trading"]["size_tiers"]
    assert s["auto_update"]["enabled"] is True
    assert s["quotes"]["provider"] == "ws"


def test_apply_settings_updates_memory_and_file():
    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write(
            "trading:\n  mode: paper\n  risk_per_trade_pct: 5\n"
            "discord:\n  webhook_url: \"x\"\n"
        )
    cfg = ConfigStub(TradingConfig(mode="paper"))

    applied, errors = apply_settings(
        cfg, {"trading": {"risk_per_trade_pct": 7}}, cfg_path
    )
    assert errors == []
    assert cfg.trading.risk_per_trade_pct == 7
    assert applied["trading.risk_per_trade_pct"] == 7

    from trader.config import load_config

    reloaded = load_config(cfg_path)
    assert reloaded.trading.risk_per_trade_pct == 7
    assert reloaded.discord.webhook_url == "x"
    assert reloaded.trading.mode == "paper"
    os.unlink(cfg_path)


def test_apply_settings_validation():
    cfg = ConfigStub(TradingConfig(mode="paper"))
    applied, errors = apply_settings(
        cfg, {"trading": {"risk_per_trade_pct": 150}}
    )
    assert errors and "between 0 and 100" in errors[0]

    applied, errors = apply_settings(
        cfg, {"trading": {"stop_check_seconds": 1}}
    )
    assert errors

    applied, errors = apply_settings(cfg, {"auto_update": {"interval_seconds": 5}})
    assert errors

    applied, errors = apply_settings(
        cfg,
        {"trading": {"ticker_whitelist": "spy, iren, "}},
    )
    assert errors == []
    assert cfg.trading.ticker_whitelist == ["SPY", "IREN"]


def test_apply_settings_tiers():
    cfg = ConfigStub(TradingConfig(mode="paper"))
    applied, errors = apply_settings(
        cfg,
        {
            "trading": {
                "size_tiers": {
                    "small": {
                        "risk_pct_max": 1.5,
                        "contracts_min": 1,
                        "contracts_max": 2,
                    }
                }
            }
        },
    )
    assert errors == []
    assert cfg.trading.size_tiers["small"]["risk_pct_max"] == 1.5
    assert cfg.trading.size_tiers["medium"]["risk_pct_max"] == 5.0

    applied, errors = apply_settings(
        cfg,
        {
            "trading": {
                "size_tiers": {"bad": {"risk_pct_max": 500}}
            }
        },
    )
    assert errors


def test_settings_take_effect_immediately():
    cfg = ConfigStub(
        TradingConfig(mode="notify", risk_per_trade_pct=5,
                      paper_account_value=10000)
    )
    store = _fresh_store()
    account = PaperAccount(cfg, store)

    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone")
    from trader.executor import contracts_for

    assert contracts_for(alert, cfg, 10000, 1.5) == 3

    apply_settings(cfg, {"trading": {"risk_per_trade_pct": 10}})
    assert contracts_for(alert, cfg, 10000, 1.5) == 6


def test_settings_endpoint_roundtrip():
    from trader.server import create_app

    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write("trading:\n  mode: notify\n")

    cfg = ConfigStub(TradingConfig(mode="notify"), accounts=[])
    store = _fresh_store()
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    app = create_app(
        cfg, store, risk, None, account, config_path=cfg_path
    )
    client = app.test_client()

    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert "trading" in resp.get_json()

    resp = client.post(
        "/api/settings",
        json={"trading": {"stop_loss_pct": 20}},
    )
    assert resp.status_code == 200
    assert cfg.trading.stop_loss_pct == 20

    resp = client.post(
        "/api/settings", json={"trading": {"stop_loss_pct": 999}}
    )
    assert resp.status_code == 400
    os.unlink(cfg_path)


def test_moomoo_option_codes():
    pos = {
        "underlying": "SPY",
        "expiry": "2026-09-18",
        "strike": 759.0,
        "right": "C",
    }
    codes = MoomooQuoteProvider.candidate_codes(pos)
    assert codes == ["US.SPY260918C00759000", "SPY260918C00759000.US"]

    pos["strike"] = 347.5
    codes = MoomooQuoteProvider.candidate_codes(pos)
    assert "US.34750000" not in str(codes)
    assert "GOOGL" or True
    assert all("0347500" in c or "34750000" in c for c in codes), codes


def test_moomoo_price_extraction():
    row = {"bid_price": 1.25, "last_price": 1.30}
    assert MoomooQuoteProvider.extract_price(row) == 1.25
    row2 = {"last_price": 1.30}
    assert MoomooQuoteProvider.extract_price(row2) == 1.30
    row3 = {"option_data": {"bid_price": 0.9}, "last_price": 0}
    assert MoomooQuoteProvider.extract_price(row3) == 0.9
    assert MoomooQuoteProvider.extract_price({}) is None


def _fake_updater(root, results):
    import trader.updater as up

    calls = []

    def fake_git(root_dir, *args):
        calls.append(args)
        key = args[0]
        if key in results:
            return results[key]

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    original = up._git
    up._git = fake_git
    restarts = []
    u = AutoUpdater(
        ConfigStub(), root, restart=restarts.append
    )
    return u, up, original, calls, restarts


def test_updater_skips_when_dirty():
    import trader.updater as up

    restarts = []

    def record():
        restarts.append(True)

    u, mod, original, calls, restarts2 = _fake_updater(
        "/tmp", {
            "rev-parse": type("R", (), {
                "returncode": 0, "stdout": "true\n", "stderr": ""
            })(),
            "status": type("R", (), {
                "returncode": 0, "stdout": " M file.py\n", "stderr": ""
            })(),
        },
    )
    u._restart = record
    try:
        assert u.check_once() is False
        assert restarts == []
        assert "dirty" in u.last_result
    finally:
        mod._git = original


def test_updater_pulls_and_restarts():
    import trader.updater as up

    seq = {
        "rev-parse": type("R", (), {
            "returncode": 0, "stdout": "true\n", "stderr": ""
        })(),
    }
    outputs = {
        ("--is-inside-work-tree",): "true\n",
        ("--abbrev-ref", "HEAD"): "main\n",
        ("HEAD",): "aaa\n",
        ("origin/main",): "bbb\n",
    }

    def fake_git(root, *args):
        calls.append(args)

        class R:
            pass

        r = R()
        r.returncode = 0
        if args[0] == "status":
            r.stdout = ""
        elif args[0] == "fetch":
            r.stdout = ""
        elif args[0] == "pull":
            r.stdout = "ok"
        elif args[0] == "log":
            r.stdout = "abc123 new commit\n"
        else:
            r.stdout = outputs.get(tuple(args[1:]), "aaa\n")
        r.stderr = ""
        return r

    calls = []
    restarts = []

    def record():
        restarts.append(True)

    original = up._git
    up._git = fake_git
    try:
        cfg = ConfigStub()
        u = AutoUpdater(cfg, "/tmp", restart=record)
        assert u.check_once() is True
        assert restarts == [True]
        assert "updated to" in u.last_result
    finally:
        up._git = original


def test_updater_up_to_date_no_restart():
    import trader.updater as up

    def fake_git(root, *args):
        class R:
            pass

        r = R()
        r.returncode = 0
        r.stderr = ""
        if args[0] == "status":
            r.stdout = ""
        elif args[0] == "log":
            r.stdout = ""
        elif args[0] == "pull":
            r.stdout = ""
        elif args[:2] == ("rev-parse", "--is-inside-work-tree"):
            r.stdout = "true\n"
        elif args[:2] == ("rev-parse", "--abbrev-ref"):
            r.stdout = "main\n"
        else:
            r.stdout = "same\n"
        return r

    restarts = []

    def record():
        restarts.append(True)

    original = up._git
    up._git = fake_git
    try:
        u = AutoUpdater(ConfigStub(), "/tmp", restart=record)
        assert u.check_once() is False
        assert restarts == []
        assert u.last_result == "up to date"
    finally:
        up._git = original


def test_reader_settings_apply_and_persist():
    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write(
            "reader:\n"
            "  pipeline_url: http://localhost:8080/alert\n"
            "  auth_token: secret123\n"
        )
    cfg = ConfigStub(TradingConfig(mode="notify"))

    applied, errors = apply_settings(
        cfg,
        {"reader": {
            "channel_marker": "🚨│player-alerts",
            "poll_interval": 0.75,
            "max_items": 60,
        }},
        cfg_path,
    )
    assert errors == []
    assert cfg.reader.channel_marker == "🚨│player-alerts"
    assert cfg.reader.poll_interval == 0.75
    assert cfg.reader.max_items == 60

    from trader.config import load_config

    reloaded = load_config(cfg_path)
    assert reloaded.reader.channel_marker == "🚨│player-alerts"
    assert reloaded.reader.pipeline_url == "http://localhost:8080/alert"
    assert reloaded.reader.auth_token == "secret123"
    os.unlink(cfg_path)


def test_reader_settings_validation():
    cfg = ConfigStub(TradingConfig(mode="notify"))
    applied, errors = apply_settings(
        cfg, {"reader": {"poll_interval": 0.01}}
    )
    assert errors
    applied, errors = apply_settings(
        cfg, {"reader": {"max_items": 100000}}
    )
    assert errors


def test_reader_status_endpoints():
    from trader.server import create_app

    cfg = ConfigStub(TradingConfig(mode="notify"))
    store = _fresh_store()
    account = PaperAccount(cfg, store)
    risk = RiskEngine(cfg, store, account)
    app = create_app(cfg, store, risk, None, account)
    client = app.test_client()

    resp = client.post(
        "/api/reader_status",
        json={"channel": "🚨│player-alerts", "ok": True},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["channel_marker"] == ""
    assert data["poll_interval"] == 0.5

    client.post(
        "/api/settings",
        json={"reader": {"channel_marker": "player-alerts"}},
    )
    resp = client.post(
        "/api/reader_status", json={"channel": "test", "ok": True}
    )
    assert resp.get_json()["channel_marker"] == "player-alerts"

    summary = client.get("/api/summary").get_json()
    assert summary["reader"]["channel"] == "test"
    assert summary["reader"]["desired"] == "player-alerts"
    assert summary["reader"]["age_seconds"] is not None

    state = client.get("/api/reader_status").get_json()
    assert state["channel"] == "test"
    assert state["desired"] == "player-alerts"


def test_account_settings_apply_and_persist():
    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write(
            "wealthsimple:\n"
            "  exchange_hint: NASDAQ\n"
            "  accounts:\n"
            "    - account_id: r1\n"
            "      label: RRSP\n"
            "      max_contracts_per_trade: 20\n"
            "    - account_id: p1\n"
            "      label: Personal\n"
        )
    cfg = ConfigStub(
        TradingConfig(mode="notify"),
        accounts=[
            WSAccountConfig(account_id="r1", label="RRSP",
                            max_contracts_per_trade=20),
            WSAccountConfig(account_id="p1", label="Personal"),
        ],
    )

    applied, errors = apply_settings(
        cfg,
        {"accounts": [
            {"label": "Personal", "max_contracts_per_trade": 5,
             "risk_per_trade_pct": 8, "paper_value": 2500,
             "account_id": "p1-updated", "enabled": True},
        ]},
        cfg_path,
    )
    assert errors == [], errors

    personal = [a for a in cfg.wealthsimple.accounts if a.label == "Personal"][0]
    assert personal.max_contracts_per_trade == 5
    assert personal.risk_per_trade_pct == 8
    assert personal.paper_value == 2500
    assert personal.account_id == "p1-updated"

    from trader.config import load_config

    reloaded = load_config(cfg_path)
    assert reloaded.wealthsimple.exchange_hint == "NASDAQ"
    rp = [a for a in reloaded.wealthsimple.accounts if a.label == "Personal"][0]
    assert rp.max_contracts_per_trade == 5
    assert rp.risk_per_trade_pct == 8.0
    rrsp = [a for a in reloaded.wealthsimple.accounts if a.label == "RRSP"][0]
    assert rrsp.max_contracts_per_trade == 20
    os.unlink(cfg_path)


def test_account_settings_clear_override_with_null():
    cfg = ConfigStub(
        TradingConfig(mode="notify"),
        accounts=[WSAccountConfig(account_id="r1", label="RRSP",
                                  max_contracts_per_trade=20)],
    )
    applied, errors = apply_settings(
        cfg,
        {"accounts": [
            {"label": "RRSP", "max_contracts_per_trade": None},
        ]},
    )
    assert errors == []
    rrsp = [a for a in cfg.wealthsimple.accounts if a.label == "RRSP"][0]
    assert rrsp.max_contracts_per_trade is None


def test_account_settings_validation():
    cfg = ConfigStub(
        TradingConfig(mode="notify"),
        accounts=[
            WSAccountConfig(account_id="r1", label="RRSP"),
            WSAccountConfig(account_id="p1", label="Personal"),
        ],
    )
    applied, errors = apply_settings(
        cfg, {"accounts": [{"label": "Nope", "max_contracts_per_trade": 5}]}
    )
    assert errors and "unknown account" in errors[0]

    applied, errors = apply_settings(
        cfg, {"accounts": [{"label": "RRSP", "risk_per_trade_pct": 900}]}
    )
    assert errors

    applied, errors = apply_settings(
        cfg, {"accounts": [{"label": "Personal", "max_contracts_per_trade": 3}]}
    )
    assert errors == []
    personal = [a for a in cfg.wealthsimple.accounts
                if a.label == "Personal"][0]
    assert personal.max_contracts_per_trade == 3


def test_reader_channels_setting(tmp_path):
    import os
    import yaml

    from trader.config import load_config
    from trader.settings import apply_settings, get_settings

    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w") as f:
        f.write("reader:\n  poll_interval: 0.5\n")
    cfg = load_config(str(cfg_path))
    assert cfg.reader.channels == []

    applied, errors = apply_settings(
        cfg,
        {"reader": {"channels": ["Test-Alerts", "player-alerts"]}},
        str(cfg_path),
    )
    assert not errors, errors
    assert cfg.reader.channels == ["player-alerts", "test-alerts"]

    s = get_settings(cfg)
    assert s["reader"]["channels"] == ["player-alerts", "test-alerts"]

    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    assert set(raw["reader"]["channels"]) == {
        "player-alerts", "test-alerts",
    }

    applied, errors = apply_settings(
        cfg, {"reader": {"channels": []}}, str(cfg_path)
    )
    assert not errors, errors
    assert cfg.reader.channels == []


def test_quotes_disabled_by_default(tmp_path):
    from trader.config import load_config
    from trader.quotes import make_quote_provider

    cfg_path = tmp_path / "config.yaml"
    with open(cfg_path, "w") as f:
        f.write("quotes:\n  provider: ws\n")
    cfg = load_config(str(cfg_path))
    assert cfg.quotes.enabled is False
    assert make_quote_provider(cfg, None) is None

    applied, errors = apply_settings(
        cfg, {"quotes": {"enabled": True}}, str(cfg_path)
    )
    assert not errors, errors
    assert cfg.quotes.enabled is True
    assert applied["quotes.enabled"] is True


def test_persist_preserves_blank_lines_and_order(tmp_path):
    from trader.config import load_config
    from trader.settings import apply_settings

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "pipeline:\n"
        "  port: 8080\n"
        "\n"
        "trading:\n"
        "  mode: notify\n"
        "  cooldown_seconds: 60\n"
        "\n"
        "quotes:\n"
        "  provider: ws\n"
        "\n"
        "reader:\n"
        "  channels:\n"
        "    - 频道-one\n"
        "    - test-alerts\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    applied, errors = apply_settings(
        cfg, {"trading": {"cooldown_seconds": 99}}, str(cfg_path)
    )
    assert not errors, errors
    text = cfg_path.read_text(encoding="utf-8")

    assert text.index("pipeline:") < text.index("trading:") < (
        text.index("quotes:")
    ) < text.index("reader:"), text
    assert "cooldown_seconds: 99" in text
    assert "\n\n" in text, text
    assert "频道-one" in text
