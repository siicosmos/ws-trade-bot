import os

import trader.config as config

EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.example.yaml",
)


def _load():
    return config.load_config(EXAMPLE)


def test_example_has_all_size_tiers():
    tiers = _load().trading.size_tiers
    assert set(tiers) == {
        "lotto", "micro", "tiny", "small", "medium", "large", "big", "full"
    }
    assert tiers["micro"]["risk_pct_max"] == 0.5
    assert tiers["big"]["risk_pct_max"] == 10
    assert tiers["full"]["contracts_max"] == 10


def test_example_float_fields_are_floats():
    cfg = _load()
    for tier in cfg.trading.size_tiers.values():
        assert isinstance(tier["risk_pct_max"], float)
    for acct in cfg.wealthsimple.accounts:
        assert isinstance(acct.paper_value, float)


def test_example_defaults():
    cfg = _load()
    assert cfg.quotes.enabled is False
    assert cfg.trading.skip_underlyings == []
    assert cfg.reader.channels
    assert all(a.enabled for a in cfg.wealthsimple.accounts)
