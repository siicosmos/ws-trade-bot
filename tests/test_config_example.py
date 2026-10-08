import os

import yaml

import core.config as config

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(
    os.path.dirname(HERE), "config"
)


def _raw(name):
    with open(os.path.join(CONFIG_DIR, name)) as f:
        return yaml.safe_load(f)


def _load(name):
    return config.load_config(os.path.join(CONFIG_DIR, name))


def test_consumer_example_role_and_defaults():
    cfg = _load("consumer.config.yaml")
    # the section key IS the role - no explicit role: field needed
    assert cfg.pipeline.role == "consumer"
    assert cfg.pipeline.port == 8081
    assert cfg.quotes.enabled is False
    assert cfg.trading.skip_underlyings == []
    assert cfg.auto_update.enabled is True


def test_consumer_example_has_all_size_tiers():
    tiers = _load("consumer.config.yaml").trading.size_tiers
    assert set(tiers) == {
        "lotto", "micro", "tiny", "small", "medium", "large", "big", "full"
    }
    assert tiers["micro"]["risk_pct_max"] == 0.5
    assert tiers["big"]["risk_pct_max"] == 10
    assert tiers["full"]["contracts_max"] == 10


def test_consumer_example_float_fields_are_floats():
    cfg = _load("consumer.config.yaml")
    for tier in cfg.trading.size_tiers.values():
        assert isinstance(tier["risk_pct_max"], float)
    for acct in cfg.wealthsimple.accounts:
        assert isinstance(acct.paper_value, float)
        assert acct.enabled


def test_consumer_example_ships_no_secrets():
    raw = _raw("consumer.config.yaml")
    text = yaml.safe_dump(raw)
    assert not (raw.get("auto_update") or {}).get("github_token")
    for secret in ("ws_tokens", "trades.db"):
        assert secret not in text


def test_info_example_role_and_no_trading_sections():
    cfg = _load("info.config.yaml")
    assert cfg.pipeline.role == "info"
    assert cfg.pipeline.port == 8080
    # sections that do not apply to the info role stay defaulted
    assert cfg.trading.mode == "notify"
    assert cfg.wealthsimple.accounts == []
    assert cfg.quotes.enabled is False


def test_reader_example_flat_keys():
    raw = _raw("reader.config.yaml")
    # the reader reads flat keys (no nesting)
    assert raw["pipeline_url"].startswith("http")
    assert raw["channels"]
    assert all(isinstance(c, str) for c in raw["channels"])
    assert raw["auth_token"] == ""
    assert isinstance(raw["poll_interval"], (int, float))
    assert isinstance(raw["max_items"], int)
    for key in ("webhook_url", "reader_log_webhook_url",
                "raw_alert_webhook_url", "update_webhook_url"):
        assert raw[key] == ""


def test_examples_load_through_core_config():
    # the info + consumer examples must survive the real loader
    for name in ("consumer.config.yaml", "info.config.yaml"):
        cfg = _load(name)
        assert cfg.pipeline.role in ("consumer", "info")


def test_legacy_pipeline_section_still_loads(tmp_path):
    # legacy configs: pipeline: with an explicit role: field
    path = tmp_path / "legacy.yaml"
    path.write_text(
        "pipeline:\n"
        "  role: info\n"
        "  port: 9999\n"
    )
    cfg = config.load_config(str(path))
    assert cfg.pipeline.role == "info"
    assert cfg.pipeline.port == 9999

    path.write_text(
        "pipeline:\n"
        "  port: 9998\n"
    )
    cfg = config.load_config(str(path))
    assert cfg.pipeline.role == "consumer"


def test_section_key_wins_over_role_field(tmp_path):
    path = tmp_path / "new.yaml"
    path.write_text(
        "info:\n"
        "  role: consumer\n"   # ignored - the section key is the role
        "  port: 9997\n"
    )
    cfg = config.load_config(str(path))
    assert cfg.pipeline.role == "info"
    assert cfg.pipeline.port == 9997


def test_run_db_migration(tmp_path):
    import run

    db = tmp_path / "consumer.trades.db"
    (tmp_path / "trades.db").write_text("old")
    (tmp_path / "trades.db-wal").write_text("wal")
    run.migrate_legacy_db(str(db))
    assert db.read_text() == "old"
    assert (tmp_path / "consumer.trades.db-wal").read_text() == "wal"
    assert not os.path.exists(tmp_path / "trades.db")
    # a second run is a no-op
    run.migrate_legacy_db(str(db))

    # explicit old name or missing old file: untouched
    other = tmp_path / "sub" / "x.trades.db"
    (tmp_path / "sub").mkdir()
    run.migrate_legacy_db(str(tmp_path / "sub" / "trades.db"))
    assert not other.exists()

def test_account_type_loaded_and_normalized(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "consumer:\n"
        "  port: 8081\n"
        "wealthsimple:\n"
        "  accounts:\n"
        "    - account_id: a1\n"
        "      label: M\n"
        "      type: MARGIN\n"     # normalized to lower
        "    - account_id: a2\n"
        "      label: R\n"
        "      type: bogus\n"     # unknown -> auto-detect ("")
    )
    cfg = config.load_config(str(path))
    assert cfg.wealthsimple.accounts[0].type == "margin"
    assert cfg.wealthsimple.accounts[1].type == ""


def test_account_is_non_margin():
    from consumer.ws.account_types import account_is_non_margin

    # an explicit config type always wins
    assert account_is_non_margin(
        type("A", (), {"type": "non_margin", "label": "Margin"})()
    ) is True
    assert account_is_non_margin(
        type("A", (), {"type": "margin", "label": "RRSP"})()
    ) is False
    # api type decides when the config type is empty
    assert account_is_non_margin(
        type("A", (), {"type": "", "label": "Whatever"})(),
        api_type="TFSA",
    ) is True
    assert account_is_non_margin(
        type("A", (), {"type": "", "label": "Whatever"})(),
        api_type="Margin",
    ) is False
    # label keyword fallback
    assert account_is_non_margin(
        type("A", (), {"type": "", "label": "My RRSP"})()
    ) is True
    assert account_is_non_margin(
        type("A", (), {"type": "", "label": "Margin"})()
    ) is False
