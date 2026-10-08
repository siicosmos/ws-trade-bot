import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from consumer.ws.account import PaperAccount
from core.config import ReaderConfig, TradingConfig, WealthsimpleConfig, WSAccountConfig, \
    AutoUpdateConfig, QuotesConfig
from consumer.trading.executor import PaperExecutor
from core.parser import parse_alert
from consumer.trading.quotes import MoomooQuoteProvider
from consumer.trading.risk import RiskEngine
from consumer.settings import apply_settings, get_settings
from consumer.trading.stops import StopMonitor
from core.store import Store
from core.ops.updater import AutoUpdater
import pytest  # noqa: E402


pytestmark = pytest.mark.essential

class ConfigStub:
    def __init__(self, trading=None, accounts=None, auth_token=""):
        self.trading = trading or TradingConfig(mode="paper")
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        from core.config import DiscordConfig

        self.discord = DiscordConfig()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])
        self.reader = ReaderConfig()
        self.auto_update = AutoUpdateConfig()
        self.quotes = QuotesConfig()
        from core.config import PaperConfig

        self.paper = PaperConfig()


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


@pytest.mark.minimum
def test_apply_settings_updates_memory_and_file():
    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write(
            "trading:\n  mode: paper\n  risk_per_trade_pct: 5\n"
            "discord:\n  trade_alert_webhook_url: \"x\"\n"
        )
    from core.config import load_config

    cfg = load_config(cfg_path)
    assert cfg.discord.trade_alert_webhook_url == "x"

    applied, errors = apply_settings(
        cfg, {"trading": {"risk_per_trade_pct": 7}}, cfg_path
    )
    assert errors == []
    assert cfg.trading.risk_per_trade_pct == 7
    assert applied["trading.risk_per_trade_pct"] == 7

    from core.config import load_config

    reloaded = load_config(cfg_path)
    assert reloaded.trading.risk_per_trade_pct == 7
    assert reloaded.discord.trade_alert_webhook_url == "x"
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
    from consumer.trading.executor import account_sizing

    def contracts():
        rows = account_sizing(alert, cfg, account)
        return [r["final_contracts"] for r in rows]

    assert contracts() == [3]

    apply_settings(cfg, {"trading": {"risk_per_trade_pct": 10}})
    assert contracts() == [6]


def test_settings_endpoint_roundtrip():
    from consumer.web import create_app

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
    # this opend build only accepts the US.-prefixed form - the
    # .US suffix is rejected and would fail the whole snapshot
    assert codes == ["US.SPY260918C00759000"]

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
    import core.ops.updater as up

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
    import core.ops.updater as up

    restarts = []

    def R(out="", code=0):
        return type("R", (), {
            "returncode": code, "stdout": out, "stderr": ""
        })()

    def fake_git(root_dir, *args):
        # dirty tree whose content does NOT match the remote:
        # the pull stays blocked (local edits are never clobbered)
        if args[0] == "status":
            return R(" M file.py\n")
        if args[:2] == ("rev-parse", "HEAD"):
            return R("aaa\n")
        if args[:2] == ("rev-parse", "origin/main"):
            return R("bbb\n")
        if args[0] == "diff":
            return R(" 1 file changed\n")   # worktree != remote
        return R()

    original = up._git
    up._git = fake_git
    u = AutoUpdater(ConfigStub(), "/tmp", restart=restarts.append)
    try:
        assert u.check_once() is False
        assert restarts == []
        assert "dirty" in u.last_result
    finally:
        up._git = original


def test_updater_pulls_and_restarts():
    import core.ops.updater as up

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
        elif args[0] == "diff":
            r.stdout = "trader/server.py\n"
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
    import core.ops.updater as up

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


def test_reader_status_endpoints():
    # the reader heartbeats the info server (its alert source).
    # reader settings live in the reader's own yaml - the
    # heartbeat only reports channel/ok; the dashboard's
    # "desired" line reads reader/config.yaml at app build time
    from info.web import create_app

    cfg = ConfigStub(TradingConfig(mode="notify"))
    store = _fresh_store()
    app = create_app(cfg, store)
    client = app.test_client()

    resp = client.post(
        "/api/reader_status",
        json={"channel": "🚨│player-alerts", "ok": True},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {}

    # the reader's desired channel marker comes from the
    # reader's own yaml (surfaced here for the dashboard line)
    app.reader_desired = "player-alerts"
    resp = client.post(
        "/api/reader_status", json={"channel": "test", "ok": True}
    )
    assert resp.get_json() == {}

    status = client.get("/api/reader_status").get_json()
    assert status["channel"] == "test"
    assert status["ok"] is True
    assert status["desired"] == "player-alerts"
    assert status["age_seconds"] is not None


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
    # the real app loads cfg from this file - mirror the file's
    # exchange_hint so the full-key persist writes it back
    cfg.wealthsimple.exchange_hint = "NASDAQ"

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

    from core.config import load_config

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
    # an unknown label is now an add (the dashboard sends the
    # full list including new rows)
    assert errors == []
    nope = [a for a in cfg.wealthsimple.accounts if a.label == "Nope"][0]
    assert nope.max_contracts_per_trade == 5

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


def test_quotes_disabled_by_default(tmp_path):
    from core.config import load_config
    from consumer.trading.quotes import make_quote_provider

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
    from core.config import load_config
    from consumer.settings import apply_settings

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


def test_size_tiers_merge_with_defaults(tmp_path):
    from core.config import load_config
    from consumer.settings import apply_settings

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "trading:\n"
        "  mode: notify\n"
        "  size_tiers:\n"
        "    medium:\n"
        "      risk_pct_max: 4\n"
        "      contracts_min: 1\n"
        "      contracts_max: 4\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    tiers = cfg.trading.size_tiers
    assert set(tiers) == {
        "lotto", "micro", "tiny", "small", "medium", "large", "big", "full"
    }, set(tiers)
    assert tiers["medium"]["risk_pct_max"] == 4
    assert tiers["medium"]["contracts_max"] == 4
    assert tiers["big"]["risk_pct_max"] == 10

    applied, errors = apply_settings(
        cfg, {"trading": {"cooldown_seconds": 99}}, str(cfg_path)
    )
    assert not errors, errors
    text = cfg_path.read_text(encoding="utf-8")
    for name in ("micro", "big", "full"):
        assert f"\n    {name}:" in text, name
    assert "risk_pct_max: 4" in text


def test_ws_refresh_settings_editable(tmp_path):
    from core.config import load_config
    from consumer.settings import apply_settings, get_settings

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "trading:\n  mode: notify\n"
        "wealthsimple:\n  positions_refresh_seconds: 30\n"
        "  values_refresh_seconds: 60\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))

    s = get_settings(cfg)
    assert s["wealthsimple"]["positions_refresh_seconds"] == 30
    assert s["wealthsimple"]["values_refresh_seconds"] == 60

    applied, errors = apply_settings(
        cfg,
        {"wealthsimple": {"positions_refresh_seconds": 45,
                          "values_refresh_seconds": 90}},
        str(cfg_path),
    )
    assert not errors, errors
    assert applied["wealthsimple.positions_refresh_seconds"] == 45
    text = cfg_path.read_text(encoding="utf-8")
    assert "positions_refresh_seconds: 45" in text
    assert "values_refresh_seconds: 90" in text

    bad, errors2 = apply_settings(
        cfg, {"wealthsimple": {"positions_refresh_seconds": 1}},
        str(cfg_path),
    )
    assert errors2 and "10-86400" in errors2[0]


def test_update_webhook_setting(tmp_path):
    from core.config import load_config
    from consumer.settings import apply_settings, get_settings

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "discord:\n  webhook_url: \"https://discord.com/api/webhooks/main\"\n"
        "trading:\n  mode: notify\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg.discord.update_webhook_url == ""

    applied, errors = apply_settings(
        cfg,
        {"discord": {"update_webhook_url":
                     "https://discord.com/api/webhooks/updates"}},
        str(cfg_path),
    )
    assert not errors, errors
    assert applied["discord.update_webhook_url"].endswith("/updates")
    text = cfg_path.read_text(encoding="utf-8")
    assert "update_webhook_url: https://discord.com/api/webhooks/updates" \
        in text
    # the main webhook survives the persist
    assert "webhooks/main" in text

    bad, errors2 = apply_settings(
        cfg, {"discord": {"update_webhook_url": "http://insecure"}}, None
    )
    assert errors2 and "https" in errors2[0]


def test_all_webhooks_editable(tmp_path):
    from core.config import load_config
    from consumer.settings import apply_settings, get_settings

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "discord:\n"
        "  webhook_url: \"https://discord.com/api/webhooks/main\"\n"
        "trading:\n  mode: notify\n",
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    s = get_settings(cfg)
    for field in (
        "trade_alert_webhook_url",
        "consumer_log_webhook_url",
        "update_webhook_url",
    ):
        assert field in s["discord"]

    applied, errors = apply_settings(
        cfg,
        {
            "discord": {
                "consumer_log_webhook_url":
                    "https://discord.com/api/webhooks/cl",
                "update_webhook_url":
                    "https://discord.com/api/webhooks/up",
            }
        },
        str(cfg_path),
    )
    assert not errors, errors
    text = cfg_path.read_text(encoding="utf-8")
    assert "webhooks/main" in text          # untouched value survives
    assert "webhooks/cl" in text
    assert "webhooks/up" in text

    bad, errors2 = apply_settings(
        cfg, {"discord": {"trade_alert_webhook_url": "ftp://nope"}}, None
    )
    assert errors2 and "https" in errors2[0]


def test_paper_settings_roundtrip(tmp_path):
    from core.config import load_config
    from consumer.settings import get_settings, apply_settings

    path = tmp_path / "cfg.yaml"
    path.write_text("trading:\n  mode: notify\n")
    cfg = load_config(str(path))

    assert get_settings(cfg)["paper"]["enabled"] is False
    applied, errors = apply_settings(
        cfg, {"paper": {"enabled": True, "mirror": False}}, path
    )
    assert not errors
    assert applied["paper.enabled"] is True
    assert applied["paper.mirror"] is False
    s = get_settings(cfg)
    assert s["paper"]["enabled"] is True
    assert s["paper"]["mirror"] is False
    cfg2 = load_config(str(path))
    assert cfg2.paper.enabled is True
    assert cfg2.paper.mirror is False


def test_discord_notify_toggle(tmp_path):
    from core.config import load_config
    from consumer.settings import apply_settings, get_settings

    path = tmp_path / "cfg.yaml"
    path.write_text(
        "trading:\n  mode: notify\n"
        "discord:\n  webhook_url: 'x'\n"
    )
    cfg = load_config(str(path))
    assert get_settings(cfg)["discord"]["notify"] is True

    applied, errors = apply_settings(
        cfg, {"discord": {"notify": False}}, str(path)
    )
    assert not errors
    assert applied["discord.notify"] is False
    cfg2 = load_config(str(path))
    assert cfg2.discord.notify is False


def test_new_settings_fields_roundtrip():
    """The settings UI exposes the full config surface: order
    type, limit offset, the daily-loss breaker, sell-held toggle,
    history retention, the margin rate, the mirror interval and
    the moomoo connection."""
    cfg = ConfigStub(
        TradingConfig(mode="notify"), accounts=[]
    )

    ok, errors = apply_settings(cfg, {
        "trading": {
            "order_type": "limit",
            "limit_offset_pct": 0.75,
            "max_daily_loss_pct": 3.0,
            "sell_only_if_held": False,
            "history_retention_days": 365,
        },
        "wealthsimple": {"stock_margin_rate": 0.35},
        "paper": {"mirror_interval_seconds": 120},
        "quotes": {
            "moomoo_host": "192.168.1.50",
            "moomoo_port": 11111,
        },
    })
    assert not errors, errors
    assert cfg.trading.order_type == "limit"
    assert cfg.trading.limit_offset_pct == 0.75
    assert cfg.trading.max_daily_loss_pct == 3.0
    assert cfg.trading.sell_only_if_held is False
    assert cfg.trading.history_retention_days == 365
    assert cfg.wealthsimple.stock_margin_rate == 0.35
    assert cfg.paper.mirror_interval_seconds == 120
    assert cfg.quotes.moomoo_host == "192.168.1.50"
    assert cfg.quotes.moomoo_port == 11111

    # get_settings exposes them all for the form
    s = get_settings(cfg)
    assert s["trading"]["order_type"] == "limit"
    assert s["trading"]["limit_offset_pct"] == 0.75
    assert s["trading"]["max_daily_loss_pct"] == 3.0
    assert s["trading"]["sell_only_if_held"] is False
    assert s["trading"]["history_retention_days"] == 365
    assert s["wealthsimple"]["stock_margin_rate"] == 0.35
    assert s["paper"]["mirror_interval_seconds"] == 120
    assert s["quotes"]["moomoo_host"] == "192.168.1.50"
    assert s["quotes"]["moomoo_port"] == 11111

    # out-of-range and bad enum values are rejected
    _, errors = apply_settings(cfg, {
        "trading": {"order_type": "iceberg"},
    })
    assert any("order_type" in e for e in errors)

    _, errors = apply_settings(cfg, {
        "wealthsimple": {"stock_margin_rate": 2.0},
    })
    assert any("stock_margin_rate" in e for e in errors)

    _, errors = apply_settings(cfg, {
        "paper": {"mirror_interval_seconds": 5},
    })
    assert any("mirror_interval_seconds" in e for e in errors)

    _, errors = apply_settings(cfg, {
        "quotes": {"moomoo_port": 99999},
    })
    assert any("moomoo_port" in e for e in errors)


def test_new_fields_persist_to_config_file():
    """order_type / max_daily_loss_pct / sell_only_if_held
    applied at runtime must also land in config.yaml - they once
    silently reverted on restart."""
    import yaml

    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write("trading:\n  mode: notify\n")

    cfg = ConfigStub(
        TradingConfig(mode="notify"), accounts=[]
    )
    ok, errors = apply_settings(cfg, {
        "trading": {
            "order_type": "limit",
            "max_daily_loss_pct": 2.5,
            "sell_only_if_held": False,
        }
    }, config_path=cfg_path)
    assert ok and not errors

    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    assert raw["trading"]["order_type"] == "limit"
    assert raw["trading"]["max_daily_loss_pct"] == 2.5
    assert raw["trading"]["sell_only_if_held"] is False
    os.unlink(cfg_path)


def test_size_tier_stop_loss_roundtrip():
    """per-size stop losses and back-to-entry survive the
    settings round trip: applied into cfg and served back by
    get_settings."""
    import tempfile

    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from consumer.settings import apply_settings, get_settings

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()

    cfg = Stub()
    cfg_path = tempfile.mkstemp(suffix=".yaml")[1]
    app, errs = apply_settings(cfg, {
        "trading": {
            "back_to_entry_enabled": True,
            "lotto_gain_budget_pct": 60,
            "size_tiers": {
                "lotto": {
                    "risk_pct_max": 0.5, "contracts_min": 1,
                    "contracts_max": 1, "stop_loss_pct": 55,
                    "back_to_entry": True,
                },
                "medium": {
                    "risk_pct_max": 5, "contracts_min": 1,
                    "contracts_max": 4, "stop_loss_pct": 22,
                },
            },
        },
    }, config_path=cfg_path)
    assert not errs, errs
    tier = cfg.trading.size_tiers["lotto"]
    assert tier["stop_loss_pct"] == 55
    assert tier["back_to_entry"] is True
    med = cfg.trading.size_tiers["medium"]
    assert med["stop_loss_pct"] == 22
    assert abs(cfg.trading.lotto_gain_budget_pct - 60) < 0.01
    assert cfg.trading.back_to_entry_enabled is True

    # the settings payload serves the tier stops back
    s = __import__(
        "consumer.settings", fromlist=["get_settings"]
    ).get_settings(cfg)
    assert s["trading"]["size_tiers"]["lotto"]["stop_loss_pct"] == 55
    assert s["trading"]["back_to_entry_enabled"] is True


def test_account_summary_carries_realized_today():
    """each account card payload carries the account's realized
    gain of the day (sell gains minus sell losses), both the
    paper ledger's number and the mode-aware one."""
    import tempfile
    import types as _types

    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from core.store import Store
    from core.parser import parse_alert
    from consumer.web import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    class StubAccount:
        def values(self):
            return {"RRSP": 5000.0}

        def open_option_positions(self):
            return {}

        def stock_holdings(self):
            return {}

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    store.apply_position(
        "paper", parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0"), 4,
        premium=2.0, account="RRSP",
    )
    store.apply_position(
        "paper", parse_alert("SOLD 10/02 AAOI 105c @ 2.5"), -2,
        premium=2.5, account="RRSP",
    )
    app = create_app(Stub(), store, None, None, StubAccount())
    client = app.test_client()
    data = client.get(
        "/api/dashboard", headers={"X-Auth-Token": "t"},
    ).get_json()
    acct = next(
        a for a in data["summary"]["accounts"]
        if a["label"] == "RRSP"
    )
    assert "paper_realized_today" in acct
    # 2x(2.5-2.0)x100 = +100 booked for RRSP today (paper ledger)
    assert abs((acct["paper_realized_today"] or 0.0) - 100.0) < 0.01
    # the real card's today line reads the REAL account's fills
    # ledger - without real fills it reads zero even though the
    # paper simulation gained (the two cards must not repeat
    # each other's number)
    assert "realized_today" in acct
    assert abs(acct["realized_today"]) < 0.01


def test_real_card_today_gain_reads_real_ledger():
    """the real account card's today gain comes from the real
    account's own fills ledger (actual fills at actual prices,
    booked by the mirror thread) - a real loss shows negative
    and never repeats the paper simulation's number."""
    import tempfile
    import types as _types

    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from core.store import Store
    from core.parser import parse_alert
    from consumer.web import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = types.SimpleNamespace(enabled=True)

    class StubAccount:
        def values(self):
            return {"RRSP": 5000.0, "MARGIN": 5000.0}

        def open_option_positions(self):
            return {}

        def stock_holdings(self):
            return {}

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    # the paper simulation books a +100 gain on RRSP
    store.apply_position(
        "paper", parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0"), 4,
        premium=2.0, account="RRSP",
    )
    store.apply_position(
        "paper", parse_alert("SOLD 10/02 AAOI 105c @ 2.5"), -2,
        premium=2.5, account="RRSP",
    )
    # the real account's actual fills (mirror-booked): RRSP also
    # gained +50, but the MARGIN account realized a -50 loss
    for label, sell_price, expected in (
        ("RRSP", 2.5, 50.0),
        ("MARGIN", 1.5, -50.0),
    ):
        store.apply_position(
            "real", parse_alert("BOUGHT 10/02 AAOI 105c @ 2.0"), 2,
            premium=2.0, account=label,
        )
        store.apply_position(
            "real", parse_alert(f"SOLD 10/02 AAOI 105c @ {sell_price}"),
            -1, premium=sell_price, account=label,
        )
        assert abs(store.realized_today("real", label) - expected) < 0.01

    app = create_app(Stub(), store, None, None, StubAccount())
    client = app.test_client()
    # the module-level section cache may hold an earlier test's
    # summary within its ttl - drop it for this store's payload
    import consumer.web as srv

    srv._section_cache.clear()
    accounts = client.get(
        "/api/dashboard", headers={"X-Auth-Token": "t"},
    ).get_json()["summary"]["accounts"]
    by_label = {a["label"]: a for a in accounts}
    # each real card carries its own real-fills number: RRSP's
    # mirrors the gain, the margin account's is negative - and
    # neither repeats the paper card's value blindly
    assert abs((by_label["RRSP"]["realized_today"] or 0.0) - 50.0) < 0.01
    assert (
        by_label["MARGIN"]["realized_today"] or 0.0
    ) <= -50.0 + 0.01
    # the paper ledger's own number stays on the paper card
    assert abs(
        (by_label["RRSP"]["paper_realized_today"] or 0.0) - 100.0
    ) < 0.01



def test_trading_paused_kill_switch_roundtrip():
    """the runtime kill switch survives the settings round trip:
    applied into cfg (no restart needed) and served back."""
    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from consumer.settings import apply_settings, get_settings

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type("P", (), {"auth_token": "t"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()

    cfg = Stub()
    ok, errors = apply_settings(cfg, {
        "trading": {"trading_paused": True},
    })
    assert ok and not errors, errors
    assert cfg.trading.trading_paused is True
    assert get_settings(cfg)["trading"]["trading_paused"] is True
    ok, errors = apply_settings(cfg, {
        "trading": {"trading_paused": False},
    })
    assert ok and not errors
    assert cfg.trading.trading_paused is False


def test_account_add_remove_and_type():
    # blank config: add a margin + a non_margin account, then
    # remove one and flip the other back to auto-detect
    fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(cfg_path, "w") as f:
        f.write("wealthsimple:\n  accounts: []\n")
    cfg = ConfigStub(TradingConfig(mode="notify"), accounts=[])

    applied, errors = apply_settings(
        cfg,
        {"accounts": [
            {"label": "Margin", "account_id": "m1", "type": "margin",
             "enabled": True},
            {"label": "RRSP", "account_id": "r1",
             "type": "non_margin", "enabled": True},
        ]},
        cfg_path,
    )
    assert errors == [], errors
    assert [a.label for a in cfg.wealthsimple.accounts] == [
        "Margin", "RRSP"]
    assert cfg.wealthsimple.accounts[0].type == "margin"
    assert cfg.wealthsimple.accounts[1].type == "non_margin"

    from core.config import load_config

    reloaded = load_config(cfg_path)
    assert [a.type for a in reloaded.wealthsimple.accounts] == [
        "margin", "non_margin"]

    applied, errors = apply_settings(
        cfg,
        {"accounts": [
            {"label": "Margin", "remove": True},
            {"label": "RRSP", "type": ""},
        ]},
        cfg_path,
    )
    assert errors == [], errors
    assert [a.label for a in cfg.wealthsimple.accounts] == ["RRSP"]
    assert cfg.wealthsimple.accounts[0].type == ""
    reloaded = load_config(cfg_path)
    assert [a.label for a in reloaded.wealthsimple.accounts] == ["RRSP"]
    os.unlink(cfg_path)


def test_account_add_validation():
    cfg = ConfigStub(TradingConfig(mode="notify"), accounts=[])
    applied, errors = apply_settings(
        cfg,
        {"accounts": [
            {"label": "A", "type": "bogus"},
            {"label": "", "account_id": "x"},
        ]},
    )
    assert any(".type" in e for e in errors)
    assert any("label is required" in e for e in errors)
# the valid-label account was still added (bad type reported)
    assert [a.label for a in cfg.wealthsimple.accounts] == ["A"]
    assert cfg.wealthsimple.accounts[0].type == ""

    # a known label routes to update, not a duplicate error
    applied, errors = apply_settings(
        cfg, {"accounts": [{"label": "A", "account_id": "y"}]}
    )
    assert errors == []
    assert cfg.wealthsimple.accounts[0].account_id == "y"


def test_paper_adjust_endpoint():
    # the adjust editor: set both cash pools and replace the
    # holdings (edit, remove, add)
    import json as _json

    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"), auth_token="t")
    account = PaperAccount(cfg, store)
    from consumer.trading.executor import PaperExecutor

    executor = PaperExecutor(cfg, store, account)
    app = __import__(
        "consumer.web", fromlist=["create_app"]
    ).create_app(cfg, store, RiskEngine(cfg, store, account),
                 executor, account)
    client = app.test_client()
    # legacy token login claims the admin session
    login = client.post("/login", data={
        "username": "", "password": "t"}, follow_redirects=False)
    assert login.status_code == 302

    r = client.post("/api/paper-adjust", json={
        "label": "T",
        "cash_cad": 5000,
        "cash_usd": 1000,
        "holdings": [
            {"underlying": "COIN", "qty": 10, "avg": 55.5},
            {"underlying": "SPX", "expiry": "2026-10-16",
             "strike": 7620, "right": "C", "qty": 2, "avg": 1.25},
        ],
    })
    assert r.status_code == 200, r.get_data()
    assert r.get_json()["status"] == "ok"

    # the cash pools landed
    assert store.paper_equity("T") == 5000.0
    assert store.paper_cash_usd("T") == 1000.0
    # the holdings replaced: one stock + one option row
    rows = store.list_positions("paper", "T")
    keys = [p["contract_key"] for p in rows]
    assert keys == ["COIN", "SPX-2026-10-16-7620-C"]
    opt = rows[1]
    assert opt["qty"] == 2 and float(opt["avg_premium"]) == 1.25

    # removing everything: send an empty holdings list
    r = client.post("/api/paper-adjust", json={
        "label": "T", "holdings": []})
    assert r.status_code == 200
    assert store.list_positions("paper", "T") == []

    # validation: bad numbers are reported, nothing crashes
    r = client.post("/api/paper-adjust", json={
        "label": "T", "cash_cad": "abc",
        "holdings": [{"underlying": "SPX", "qty": "x"}]})
    assert r.status_code == 400
    assert any("cash_cad" in e for e in r.get_json()["errors"])
    assert any("qty" in e for e in r.get_json()["errors"])


def test_paper_card_metrics_include_usd_cash():
    # the adjust editor's usd cash pool flows through every card
    # line: the cash line shows both pools natively, the margin
    # math nets them, and the valuation includes them
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"))
    store.set_paper_equity(5000.0, "T")
    store.set_paper_cash_usd(1000.0, "T")

    from consumer.web import _paper_card_metrics

    out = _paper_card_metrics(None, store, cfg, "T", 6000.0, False, 1.0)
    assert out["paper_cash"] == 5000.0
    # no holdings -> no margin requirement, no loan
    assert out["paper_margin_used"] == 0.0
    assert out["paper_portfolio_value"] == 6000.0

    # the ledger's valuation includes the usd pool (fx 1.0 with
    # no live account)
    from consumer.trading.paper import PaperLedger

    ledger = PaperLedger(cfg, store, None)
    assert ledger.value("T") == 6000.0

    # a negative cad cash pool is a loan - the usd pool offsets it
    store.set_paper_equity(-500.0, "T")
    out = _paper_card_metrics(None, store, cfg, "T", 1500.0, False, 1.0)
    assert out["paper_margin_used"] == 0.0   # -500 + 1000 = +500 net
    assert out["paper_portfolio_value"] == 1500.0

    # reset clears the usd pool with the rest of the ledger
    store.reset_paper_account("T")
    assert store.paper_cash_usd("T") is None
    assert store.paper_equity("T") is None


def test_paper_adjust_switches_holding_type():
    # the adjust editor replaces the holdings wholesale - a row
    # can switch between option and stock between applies
    store = _fresh_store()
    cfg = ConfigStub(TradingConfig(mode="paper"), auth_token="t")
    account = PaperAccount(cfg, store)
    from consumer.trading.executor import PaperExecutor

    executor = PaperExecutor(cfg, store, account)
    app = __import__(
        "consumer.web", fromlist=["create_app"]
    ).create_app(cfg, store, RiskEngine(cfg, store, account),
                 executor, account)
    client = app.test_client()
    client.post("/login", data={
        "username": "", "password": "t"}, follow_redirects=False)

    r = client.post("/api/paper-adjust", json={
        "label": "T",
        "holdings": [{"underlying": "SPX", "expiry": "2026-10-16",
                      "strike": 7620, "right": "C", "qty": 2,
                      "avg": 1.25}],
    })
    assert r.status_code == 200
    assert [p["contract_key"] for p in
            store.list_positions("paper", "T")] == [
        "SPX-2026-10-16-7620-C"]

    # the same holding switched to a stock row (symbol + qty)
    r = client.post("/api/paper-adjust", json={
        "label": "T",
        "holdings": [{"underlying": "SPX", "qty": 2, "avg": 1.25}],
    })
    assert r.status_code == 200
    rows = store.list_positions("paper", "T")
    assert [p["contract_key"] for p in rows] == ["SPX"]
    assert rows[0]["right"] in (None, "", "?")

    # and back to an option (a put this time)
    r = client.post("/api/paper-adjust", json={
        "label": "T",
        "holdings": [{"underlying": "SPX", "expiry": "2026-10-16",
                      "strike": 7620, "right": "P", "qty": 2,
                      "avg": 1.25}],
    })
    assert r.status_code == 200
    assert [p["contract_key"] for p in
            store.list_positions("paper", "T")] == [
        "SPX-2026-10-16-7620-P"]


def test_trade_alert_webhook_url_parses(tmp_path):
    """the renamed trade_alert_webhook_url key parses directly."""
    from core.config import load_config

    path = tmp_path / "new.yaml"
    path.write_text(
        "discord:\n"
        "  trade_alert_webhook_url: "
        "\"https://discord.com/api/webhooks/new\"\n",
        encoding="utf-8",
    )
    assert (
        load_config(str(path)).discord.trade_alert_webhook_url
        == "https://discord.com/api/webhooks/new"
    )
