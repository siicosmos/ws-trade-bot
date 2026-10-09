import os
import sys
import types
from unittest.mock import MagicMock

_READER_DIR = os.path.join(os.path.dirname(__file__), "..", "reader")
sys.path.insert(0, _READER_DIR)

sys.modules.setdefault("uiautomation", MagicMock())
try:
    import psutil  # noqa: F401
except ImportError:
    sys.modules.setdefault("psutil", MagicMock())

import discord_reader as dr  # noqa: E402
import discord_window as dw  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_keys_mock():
    dr.auto.SendKeys.reset_mock()
    yield


class _FakePattern:
    def __init__(self, pct=50.0):
        self._pct = pct

    @property
    def VerticalViewSize(self):
        return 60.0

    @property
    def VerticalScrollPercent(self):
        return self._pct

    def SetScrollPercent(self, x, y):
        pass


class _FakeContainer:
    def __init__(self, pattern=None):
        self._pattern = pattern
        self.calls = []

    def GetScrollPattern(self):
        return self._pattern

    def WheelDown(self, **kw):
        self.calls.append("wheel")

    def SetFocus(self):
        self.calls.append("focus")

    def GetTopLevelControl(self):
        return types.SimpleNamespace(NativeWindowHandle=4321)


def _patch_foreground(monkeypatch, ok=True):
    monkeypatch.setattr(
        dw, "_foreground_discord",
        lambda w, log=print: ok,
    )


def test_end_fallback_sends_keys_when_focused(monkeypatch):
    monkeypatch.setattr(dw, "_snap_warn_ts", 0.0)
    _patch_foreground(monkeypatch, ok=True)
    monkeypatch.setattr(
        dw, "ensure_visible", lambda *a, **k: False
    )
    container = _FakeContainer(_FakePattern())
    dr.snap_to_bottom(container, log_fn=lambda *a, **k: None)
    assert container.calls == ["wheel", "focus"]
    dr.auto.SendKeys.assert_called_with("{End}", waitTime=0.05)


def test_end_fallback_skipped_without_focus(monkeypatch):
    _patch_foreground(monkeypatch, ok=False)
    monkeypatch.setattr(
        dw, "ensure_visible", lambda *a, **k: False
    )
    container = _FakeContainer(_FakePattern())
    dr.snap_to_bottom(container, log_fn=lambda *a, **k: None)
    assert "focus" not in container.calls
    dr.auto.SendKeys.assert_not_called()


def test_end_fallback_skipped_without_window(monkeypatch):
    _patch_foreground(monkeypatch, ok=True)
    monkeypatch.setattr(
        dw, "ensure_visible", lambda *a, **k: False
    )
    container = _FakeContainer(_FakePattern())
    container.GetTopLevelControl = lambda: (_ for _ in ()).throw(
        Exception("no window")
    )
    dr.snap_to_bottom(container, log_fn=lambda *a, **k: None)
    assert "focus" not in container.calls
    dr.auto.SendKeys.assert_not_called()


def test_no_end_fallback_when_scroll_succeeds(monkeypatch):

    class _GoodPattern(_FakePattern):
        @property
        def VerticalScrollPercent(self):
            return 99.0

    _patch_foreground(monkeypatch, ok=True)
    monkeypatch.setattr(
        dw, "ensure_visible", lambda *a, **k: False
    )
    container = _FakeContainer(_GoodPattern())
    dr.snap_to_bottom(container, log_fn=lambda *a, **k: None)
    assert container.calls == []
    dr.auto.SendKeys.assert_not_called()
