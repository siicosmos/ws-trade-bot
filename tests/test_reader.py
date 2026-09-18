import os
import sys
import types
from unittest.mock import MagicMock

_READER_DIR = os.path.join(os.path.dirname(__file__), "..", "reader")
sys.path.insert(0, _READER_DIR)

sys.modules.setdefault("uiautomation", MagicMock())
sys.modules.setdefault("psutil", MagicMock())
sys.modules.setdefault("inspect_discord", MagicMock())

import discord_reader as dr  # noqa: E402


def _fake_ctrl(name="", children=None, control_type=None):
    return types.SimpleNamespace(
        ControlType=control_type or dr.auto.ControlType.ListControl,
        Name=name,
        GetChildren=lambda: list(children or []),
    )


def _item(text="", name=""):
    return types.SimpleNamespace(text=text, Name=name)


def test_looks_like_message_rejects_observed_chrome():
    junk = [
        "Let's Play Some Doto 社区服务器 任何人都可以加入该服务器。 等级 1 3 个助力 Cheddar Flow",
        "SPX Plays 社区服务器 任何人都可以加入该服务器。 等级 1 3 个助力 Cheddar Flow",
        "创建频道",
        "Community Server. Anyone can join this server.",
    ]
    for text in junk:
        assert not dr.looks_like_message(text), text


def test_looks_like_message_accepts_real_alerts():
    real = [
        "BOUGHT 0DTE SPX 7645c @ .65 @everyone ROLL UP LOTTO 🎲 tiny size",
        "SOLD 1/4 0DTE SPX 7645c @ 1.30 runners FREE",
        "ALL OUT 0DTE SPX 7645c @ 1.10",
    ]
    for text in real:
        assert dr.looks_like_message(text), text


def test_message_likeness():
    assert dr.message_likeness("BOUGHT 0DTE SPX 7645c @ .65 tiny size") == 1
    assert dr.message_likeness("123") == 1
    assert dr.message_likeness("DoubleL") == 0
    assert dr.message_likeness("Cheddar Flow") == 0
    assert dr.message_likeness("创建频道") == -1


def test_channel_from_title():
    title = "⁠test-message | my-trade-alert-server - Discord"
    assert dr.channel_from_title(title) == "test-message"
    assert dr.channel_from_title("") == ""
    assert dr.channel_from_title("no separator") == ""


def test_new_messages_seeds_silently():
    assert dr.new_messages(["a", "b", "c"], []) == []


def test_new_messages_detects_fresh():
    assert dr.new_messages(["a", "b", "c"], ["a", "b"]) == ["c"]
    assert dr.new_messages(["a", "b"], ["x", "y"]) == []


def test_find_message_container_prefers_message_pane(monkeypatch):
    chrome = _fake_ctrl(name="服务器", children=[_item(text="创建频道")])
    members = _fake_ctrl(
        name="成员列表", children=[_item(text="DoubleL"), _item(text="Cheddar Flow")]
    )
    msgs = _fake_ctrl(
        name="test-alerts",
        children=[
            _item(text="DoubleL 00:13 BOUGHT 0DTE SPX 7645c @ .65 tiny size"),
            _item(text="DoubleL 00:14 123"),
        ],
    )
    monkeypatch.setattr(
        dr.auto, "WalkControl",
        lambda *a, **k: [(chrome, 1), (msgs, 2), (members, 3)],
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "") is msgs


def test_find_message_container_accepts_pane_type(monkeypatch):
    msgs = _fake_ctrl(
        name="test-message",
        control_type=dr.auto.ControlType.PaneControl,
        children=[
            _item(text="DoubleL 00:13 BOUGHT 0DTE SPX 7645c @ .65 tiny size"),
        ],
    )
    monkeypatch.setattr(dr.auto, "WalkControl", lambda *a, **k: [(msgs, 2)])
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "") is msgs


def test_find_message_container_scores_item_names(monkeypatch):
    msgs = _fake_ctrl(
        name="test-message",
        children=[_item(name="DoubleL, 今天 00:13"), _item(name="Liam, 今天 00:14")],
    )
    monkeypatch.setattr(dr.auto, "WalkControl", lambda *a, **k: [(msgs, 2)])
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "") is msgs


def test_find_message_container_title_bonus(monkeypatch):
    other = _fake_ctrl(
        name="",
        children=[
            _item(text="word word word digit 1"),
            _item(text="word word word digit 2"),
        ],
    )
    msgs = _fake_ctrl(
        name="test-message",
        children=[_item(text="DoubleL 00:13 BOUGHT 0DTE SPX 7645c")],
    )
    monkeypatch.setattr(
        dr.auto, "WalkControl", lambda *a, **k: [(other, 1), (msgs, 2)]
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "", "test-message") is msgs


def test_find_message_container_rejects_all_chrome(monkeypatch):
    chrome = _fake_ctrl(
        name="服务器",
        children=[
            _item(text="创建频道"),
            _item(text="SPX Plays 社区服务器 任何人都可以加入该服务器。"),
        ],
    )
    monkeypatch.setattr(dr.auto, "WalkControl", lambda *a, **k: [(chrome, 1)])
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "") is None


def test_current_messages_filters_chrome(monkeypatch):
    container = _fake_ctrl(
        name="test-alerts",
        children=[
            _item(text="创建频道"),
            _item(text="BOUGHT 0DTE SPX 7645c @ .65 tiny size"),
            _item(text="123"),
        ],
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.current_messages(container) == [
        "BOUGHT 0DTE SPX 7645c @ .65 tiny size",
        "123",
    ]


def test_current_messages_falls_back_to_item_name(monkeypatch):
    container = _fake_ctrl(
        name="test-message",
        children=[
            _item(name="DoubleL, 今天 00:13"),
            _item(name="Liam, 今天 00:14"),
        ],
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.current_messages(container) == [
        "DoubleL, 今天 00:13",
        "Liam, 今天 00:14",
    ]


def test_git_head_in_repo():
    import re as _re
    root = os.path.abspath(os.path.join(_READER_DIR, ".."))
    head = dr.git_head(root)
    assert head and _re.fullmatch(r"[0-9a-f]{40}", head)


def test_git_head_none_outside_repo():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert dr.git_head(d) is None
