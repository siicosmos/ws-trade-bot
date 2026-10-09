"""Shared test helpers: the config stub and the fresh-store
builder.

Imported flat (`from _helpers import ...`) - this directory is
on sys.path via conftest.py and the test modules' own inserts.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from core.config import (
    ReaderConfig, TradingConfig, WealthsimpleConfig,  # noqa: F401
)
from core.store import Store  # noqa: E402


class ConfigStub:
    def __init__(self, trading, accounts=None, auth_token=""):
        self.trading = trading
        self.pipeline = type("PI", (), {"auth_token": auth_token})()
        self.auto_update = type("AU", (), {
            "enabled": False, "interval_seconds": 600})()
        self.quotes = type("Q", (), {
            "enabled": False, "provider": "ws",
            "moomoo_host": "", "moomoo_port": 11111})()
        self.discord = type("D", (), {
            "trade_alert_webhook_url": "",
            "consumer_log_webhook_url": "",
            "update_webhook_url": "", "notify": True})()
        self.parser = type("P", (), {"custom_patterns": []})()
        self.wealthsimple = WealthsimpleConfig(accounts=accounts or [])
        self.reader = ReaderConfig()


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)
