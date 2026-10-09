import ctypes
import importlib.util
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# loaded under a private name so a MagicMock planted in
# sys.modules by another test module cannot shadow it
id_mod = _load(
    "inspect_discord_switch_test",
    os.path.join(_READER_DIR, "inspect_discord.py"),
)
import discord_reader as dr
import discord_window as dw  # noqa: E402

# windll does not exist off-windows; the code under test only
# touches it at call time, so a stand-in module attribute works
_focus = [0]


def _install_win32():
    ctypes.windll = types.SimpleNamespace(
        user32=types.SimpleNamespace(
            GetForegroundWindow=lambda: _focus[0],
            SetForegroundWindow=lambda hwnd: 0,
        )
    )


class _FakeWindow:
    def __init__(self, hwnd, names):
        self._hwnd = hwnd
        self._names = list(names)
        self._i = 0

    @property
    def NativeWindowHandle(self):
        return self._hwnd

    @property
    def Name(self):
        return self._names[min(self._i, len(self._names) - 1)]

    def SetActive(self):
        _focus[0] = self._hwnd


def _wire_keyboard(win):
    """sendkeys advances the fake window's title history"""
    id_mod.auto = types.SimpleNamespace(
        SendKeys=lambda *a, **k: setattr(win, "_i", win._i + 1)
    )
    id_mod._time = types.SimpleNamespace(
        sleep=lambda s: None, time=lambda: 1000.0
    )


def test_server_from_title_parses_channel_server():
    assert id_mod.server_from_title(
        "#\U0001f4bemessage-deposit | my-trade-alert-server - Discord"
    ) == "my-trade-alert-server"
    assert id_mod.server_from_title("好友 - Discord") == ""


def test_switch_aborts_without_focus_and_sends_no_keys():
    _install_win32()
    _focus[0] = 999
    win = _FakeWindow(4321, ["好友 - Discord"])
    win.SetActive = lambda: None  # focus never lands
    _wire_keyboard(win)
    sent = []

    assert id_mod.switch_server_keyboard(
        win, "SPX Plays", log=lambda *a, **k: None
    ) is False
    assert sent == []


def test_switch_returns_immediately_when_already_there():
    _install_win32()
    _focus[0] = 4321
    win = _FakeWindow(4321, ["#ch | SPX Plays - Discord"])
    _wire_keyboard(win)

    assert id_mod.switch_server_keyboard(
        win, "SPX Plays", log=lambda *a, **k: None
    ) is True


def test_switch_cycles_until_title_matches():
    _install_win32()
    _focus[0] = 999
    win = _FakeWindow(
        4321,
        [
            "好友 - Discord",
            "#a | Cheddar Flow - Discord",
            "#b | Tradytics - Discord",
            "#c | SPX Plays - Discord",
        ],
    )
    _wire_keyboard(win)

    assert id_mod.switch_server_keyboard(
        win, "SPX Plays", log=lambda *a, **k: None
    ) is True
    assert win._i == 3


def test_switch_server_keyboard_first_short_circuit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dw, "switch_server_keyboard",
        lambda w, s, log=print: calls.append(("kbd", s)) or True,
    )
    monkeypatch.setattr(
        dw, "find_channel_control",
        lambda w, names: calls.append(("find", names)),
    )
    assert dr.switch_server(
        None, "SPX Plays", log=lambda *a, **k: None
    ) is True
    assert calls == [("kbd", "SPX Plays")]


def test_switch_server_falls_back_to_rail_click(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setattr(
        dw, "switch_server_keyboard",
        lambda w, s, log=print: calls.append(("kbd", s)) or False,
    )
    monkeypatch.setattr(
        dw, "find_channel_control",
        lambda w, names: calls.append(("find", names)) or sentinel,
    )
    monkeypatch.setattr(
        dw, "click_channel_control",
        lambda c, window=None, log=print:
            calls.append(("click",)) or True,
    )
    assert dr.switch_server(
        None, "SPX Plays", log=lambda *a, **k: None
    ) is True
    assert calls == [
        ("kbd", "SPX Plays"),
        ("find", ["SPX Plays"]),
        ("click",),
    ]


def test_switch_server_fails_when_nothing_reaches(monkeypatch):
    monkeypatch.setattr(
        dw, "switch_server_keyboard",
        lambda w, s, log=print: False,
    )
    monkeypatch.setattr(dw, "find_channel_control", lambda w, n: None)
    assert dr.switch_server(
        None, "SPX Plays", log=lambda *a, **k: None
    ) is False


def test_switch_server_fails_when_click_refused(monkeypatch):
    monkeypatch.setattr(
        dw, "switch_server_keyboard",
        lambda w, s, log=print: False,
    )
    monkeypatch.setattr(dw, "find_channel_control", lambda w, n: object())
    monkeypatch.setattr(
        dw, "click_channel_control",
        lambda c, window=None, log=print: False,
    )
    assert dr.switch_server(
        None, "SPX Plays", log=lambda *a, **k: None
    ) is False
