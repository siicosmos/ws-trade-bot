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


def test_normalize_channel_name():
    assert dr.normalize_channel_name(
        "test-message中的消息", "#test-message"
    ) == "#test-message"
    assert dr.normalize_channel_name(
        "Messages in general", "#general"
    ) == "#general"
    assert dr.normalize_channel_name("", "#test-message") == "#test-message"
    assert dr.normalize_channel_name(
        "🚨│player-alerts", "#other"
    ) == "🚨│player-alerts"
    assert dr.normalize_channel_name(
        "test-message", "#test-message"
    ) == "#test-message"


def test_strip_ui_noise():
    raw = (
        "12:45 AM Friday, September 18, 2026 12:45 AM "
        "BOUGHT 0DTE SPX 7645c @ .65 ROLL UP LOTTO tiny size "
        ":thumbsup: 点击反应 :saluting_face: 点击反应 "
        ":chart_with_upwards_trend: 点击反应 添加反应 编辑 转发 更多"
    )
    assert dr.strip_ui_noise(raw) == (
        "BOUGHT 0DTE SPX 7645c @ .65 ROLL UP LOTTO tiny size"
    )
    assert dr.strip_ui_noise("plain message") == "plain message"
    assert dr.strip_ui_noise("") == ""


def test_looks_like_message_rejects_log_pastes():
    pastes = [
        "-> 200 12:45 AM BOUGHT 0DTE SPX 7645c @ .65 ROLL UP",
        "UIA error: (-2147220991)",
        "channel switched: '服务器'",
        "watching channel: '#test-message'",
        "no message pane found (title channel: 'trade-alerts')",
    ]
    for text in pastes:
        assert not dr.looks_like_message(text), text


def test_name_matches_title():
    assert dr.name_matches_title("trade-alerts中的消息", "trade-alerts")
    assert dr.name_matches_title("trade-alerts", "trade-alerts")
    assert dr.name_matches_title("#general", "general")
    assert not dr.name_matches_title("服务器", "trade-alerts")
    assert not dr.name_matches_title("", "trade-alerts")
    assert not dr.name_matches_title("trade-alerts", "")


def test_find_message_container_title_match_beats_score(monkeypatch):
    rail = _fake_ctrl(
        name="服务器",
        children=[
            _item(text="Let's Play Some Doto community"),
            _item(text="my-trade-alert-server SPX Plays"),
        ],
    )
    pane = _fake_ctrl(name="trade-alerts中的消息", children=[])
    monkeypatch.setattr(
        dr.auto, "WalkControl", lambda *a, **k: [(rail, 1), (pane, 4)]
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(object(), "", "trade-alerts") is pane


def test_parse_message_time_with_date():
    ts = dr.parse_message_time(
        "12:45 AM Friday, September 18, 2026 12:45 AM "
        "BOUGHT 0DTE SPX 7645c @ .65"
    )
    assert ts is not None
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (
        2026, 9, 18, 0, 45,
    )


def test_parse_message_time_today_only():
    from datetime import datetime
    ts = dr.parse_message_time("DoubleL 3:05 PM hello")
    assert ts is not None
    now = datetime.now()
    assert (ts.year, ts.month, ts.day) == (now.year, now.month, now.day)
    assert (ts.hour, ts.minute) == (15, 5)


def test_parse_message_time_none():
    assert dr.parse_message_time("BOUGHT 0DTE SPX 7645c @ .65") is None
    assert dr.parse_message_time("") is None


def test_is_recent_message():
    assert dr.is_recent_message("BOUGHT 0DTE SPX 7645c @ .65")
    assert not dr.is_recent_message(
        "11:30 PM December 25, 2020 11:30 PM old message"
    )
    assert not dr.is_recent_message(
        "11:59 PM December 31, 2099 11:59 PM future message"
    )


def test_channel_allowed():
    channels = ["test-alerts", "player-alerts"]
    assert dr.channel_allowed("#test-alerts", channels, "")
    assert dr.channel_allowed("test-alerts", channels, "")
    assert not dr.channel_allowed("trade-alerts", channels, "")
    assert dr.channel_allowed("anything", channels, "player-alerts")
    assert dr.channel_allowed("anything", [], "")
    assert dr.channel_allowed("", channels, "")


def test_merged_config_channels():
    resp = {"channels": ["Player-Alerts", " test-alerts ", ""]}
    marker, poll, items, channels, changed = dr.merged_config(
        resp, "", 0.5, 40, ["test-alerts"]
    )
    assert changed
    assert channels == ["player-alerts", "test-alerts"]
    assert not dr.merged_config(
        {"channels": ["test-alerts"]}, "", 0.5, 40, ["test-alerts"]
    )[4]


def test_channel_allowed_substring_match():
    channels = ["test-alerts", "player-alerts"]
    assert dr.channel_allowed("#🚨│player-alerts", channels, "")
    assert dr.channel_allowed("🚨│player-alerts", channels, "")
    assert not dr.channel_allowed("#trade-alerts", channels, "")
    assert not dr.channel_allowed("general", channels, "")


def test_find_message_container_strict_title(monkeypatch):
    rail = _fake_ctrl(
        name="服务器",
        children=[
            _item(text="Let's Play Some Doto community"),
            _item(text="my-trade-alert-server SPX Plays"),
        ],
    )
    monkeypatch.setattr(
        dr.auto, "WalkControl", lambda *a, **k: [(rail, 1)]
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.find_message_container(
        object(), "", "trade-alerts", strict_title=True
    ) is None
    assert dr.find_message_container(
        object(), "", "trade-alerts", strict_title=False
    ) is rail


def test_strip_ui_noise_author_prefix():
    raw = (
        "DoubleL 1:11 AM 1:11 AM Friday, September 18, 2026 1:11 AM "
        "BOUGHT 08/26 AVGO 352"
    )
    assert dr.strip_ui_noise(raw) == "BOUGHT 08/26 AVGO 352"


def test_meta_time_leading_only():
    from datetime import datetime
    ts = dr.meta_time(
        "DoubleL 1:11 AM 1:11 AM Friday, September 18, 2026 1:11 AM "
        "BOUGHT 08/26 AVGO 352"
    )
    assert ts is not None and ts.year == 2026
    now = datetime.now()
    ts2 = dr.meta_time(
        f"DoubleL {now.strftime('%I:%M %p')} fresh message"
    )
    assert ts2 is not None and ts2.date() == now.date()
    assert dr.meta_time("BOUGHT 0DTE SPX 7645c @ .65") is None
    assert dr.meta_time(
        "remember to target 4:00 PM on the close"
    ) is None


def test_current_messages_drops_old_by_meta(monkeypatch):
    from datetime import datetime
    container = _fake_ctrl(
        name="test-alerts",
        children=[
            _item(text="DoubleL 11:30 PM December 25, 2020 11:30 PM old"),
            _item(
                text=f"DoubleL {datetime.now().strftime('%I:%M %p')} "
                "BOUGHT 0DTE SPX 7645c @ .65"
            ),
        ],
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    assert dr.current_messages(container) == [
        f"BOUGHT 0DTE SPX 7645c @ .65"
    ]
