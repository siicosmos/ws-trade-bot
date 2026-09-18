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
    msgs = dr.current_messages(container)
    assert [t for t, _ in msgs] == [
        "BOUGHT 0DTE SPX 7645c @ .65 tiny size",
        "123",
    ]
    assert all(ts is None for _, ts in msgs)


def test_current_messages_falls_back_to_item_name(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(
        dr, "true_now", lambda: datetime(2026, 9, 18, 14, 3)
    )
    container = _fake_ctrl(
        name="test-message",
        children=[
            _item(name="DoubleL, 今天 14:01"),
            _item(name="Liam, 今天 14:02"),
        ],
    )
    monkeypatch.setattr(dr, "item_text", lambda it: it.text)
    msgs = dr.current_messages(container)
    assert [t for t, _ in msgs] == [
        "DoubleL, 今天 14:01",
        "Liam, 今天 14:02",
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


def test_looks_like_message_rejects_system_messages():
    junk = [
        "这是 ⁠test-alerts 频道的起点。 编辑频道",
        "September 18, 2026",
        "Today",
        "今天",
    ]
    for text in junk:
        assert not dr.looks_like_message(text), text


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
    msgs = dr.current_messages(container)
    assert [t for t, _ in msgs] == [f"BOUGHT 0DTE SPX 7645c @ .65"]


def _load_inspect_discord():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "real_inspect_discord", os.path.join(_READER_DIR, "inspect_discord.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_find_discord_window_ignores_vscode(monkeypatch):
    insp = _load_inspect_discord()
    vscode = types.SimpleNamespace(
        ControlType=dr.auto.ControlType.WindowControl,
        ProcessId=1,
        Name="discord_reader.py - ws-trade-bot - Visual Studio Code",
    )
    discord_win = types.SimpleNamespace(
        ControlType=dr.auto.ControlType.WindowControl,
        ProcessId=999,
        Name="⁠test-message | my-trade-alert-server - Discord",
    )
    monkeypatch.setattr(
        insp, "discord_pids", lambda: set()
    )
    monkeypatch.setattr(
        dr.auto, "GetRootControl",
        lambda: types.SimpleNamespace(
            GetChildren=lambda: [vscode, discord_win]
        ),
    )
    assert insp.find_discord_window() is discord_win


def test_find_discord_window_prefers_pid(monkeypatch):
    insp = _load_inspect_discord()
    other = types.SimpleNamespace(
        ControlType=dr.auto.ControlType.WindowControl,
        ProcessId=1,
        Name="something - Discord",
    )
    discord_win = types.SimpleNamespace(
        ControlType=dr.auto.ControlType.WindowControl,
        ProcessId=999,
        Name="whatever",
    )
    monkeypatch.setattr(insp, "discord_pids", lambda: {999})
    monkeypatch.setattr(
        dr.auto, "GetRootControl",
        lambda: types.SimpleNamespace(
            GetChildren=lambda: [other, discord_win]
        ),
    )
    assert insp.find_discord_window() is discord_win


def test_notify_restart_posts_webhook(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))

    monkeypatch.setattr(dr.requests, "post", fake_post)
    dr.notify_restart("http://hook", "code updated to abc1234")
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://hook"
    embed = payload["embeds"][0]
    assert embed["title"] == "Reader restarting"
    assert embed["fields"][0]["value"] == "code updated to abc1234"

    calls.clear()
    dr.notify_restart("", "anything")
    assert calls == []


def test_true_now_applies_clock_offset():
    from datetime import datetime
    dr._clock_offset = 90.0
    try:
        assert (dr.true_now() - datetime.now()).total_seconds() > 89
    finally:
        dr._clock_offset = 0.0
    assert abs((dr.true_now() - datetime.now()).total_seconds()) < 1


def test_is_recent_uses_offset(monkeypatch):
    from datetime import datetime, timedelta
    future = datetime.now() + timedelta(hours=3)
    text = future.strftime("%A, %B ") + f"{future.day}, {future.year} " + future.strftime("%I:%M %p")
    assert not dr.is_recent_message(text)
    dr._clock_offset = 3 * 3600.0
    try:
        assert dr.is_recent_message(text)
    finally:
        dr._clock_offset = 0.0


def test_internet_offset_shape():
    offset = dr.internet_offset()
    assert offset is None or abs(offset) < 300


def test_embed_prefixed_message_is_old_and_cleaned():
    raw = (
        "📢 SPX Plays • Option Alert APP 9/1/2026 10:42 AM "
        "📢 SPX Plays • Option Alert 9/1/2026 10:42 AM "
        "Tuesday, September 1, 2026 10:42 AM "
        "BOUGHT 0DTE SPX 7620p @ 1.5 small size SL 15m > 7644"
    )
    ts = dr.meta_time(raw)
    assert ts is not None
    assert (ts.year, ts.month, ts.day) == (2026, 9, 1)
    assert not dr.is_recent_message(raw)
    assert dr.strip_ui_noise(raw) == (
        "BOUGHT 0DTE SPX 7620p @ 1.5 small size SL 15m > 7644"
    )


def test_item_text_survives_stale_element(monkeypatch):
    class Boom(Exception):
        pass

    monkeypatch.setattr(dr, "UIAError", Boom)

    def boom_walk(*a, **k):
        raise Boom("stale")

    monkeypatch.setattr(dr.auto, "WalkControl", boom_walk)
    assert dr.item_text(object()) == ""


def test_heartbeat_status_reports_quiet_allowed_channel():
    from discord_reader import heartbeat_status

    # quiet allowed channel: report the channel, reader is healthy
    assert heartbeat_status(True, "🚨│player-alerts") == (
        "🚨│player-alerts", True
    )
    # disallowed channel: keep waiting semantics
    assert heartbeat_status(False, "#pipeline-log") == (None, False)
    # allowed but no title: nothing to report
    assert heartbeat_status(True, "") == (None, False)


def test_heartbeat_fires_while_channel_quiet(monkeypatch, tmp_path):
    # regression: a quiet channel (no recent messages) used to skip the
    # heartbeat entirely, so the portal said "waiting for any open
    # channel" while the reader was attached and healthy
    import discord_reader as dr

    cfg = {
        "reader": {
            "pipeline_url": "http://localhost:8080/alert",
            "poll_interval": 0.01,
            "channels": ["player-alerts"],
            "auth_token": "",
        },
        "discord": {"webhook_url": ""},
    }

    class FakeWindow:
        Name = "🚨│player-alerts | #general - Discord"

    class FakeContainer:
        Name = "🚨│player-alerts 中的消息"

    heartbeats = []

    def fake_sync(base_url, auth_token, channel, ok, verify=True):
        heartbeats.append((channel, ok))
        return None

    monkeypatch.setattr(dr, "SEEN_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setattr(dr, "load_config", lambda: cfg)
    monkeypatch.setattr(dr, "sync_clock", lambda: None)
    monkeypatch.setattr(dr, "git_head", lambda root: "abc123")
    monkeypatch.setattr(dr, "repo_root", lambda: ".")
    monkeypatch.setattr(dr, "find_discord_window", lambda: FakeWindow())
    monkeypatch.setattr(
        dr, "find_message_container",
        lambda *a, **k: FakeContainer(),
    )
    monkeypatch.setattr(
        dr, "current_messages", lambda container, max_items=40: []
    )
    monkeypatch.setattr(dr, "WebhookLog", lambda url: None)
    monkeypatch.setattr(dr, "sync_with_server", fake_sync)

    sleeps = {"n": 0}

    def fake_sleep(secs):
        sleeps["n"] += 1
        if sleeps["n"] > 120:
            raise KeyboardInterrupt

    monkeypatch.setattr(dr.time, "sleep", fake_sleep)

    try:
        dr.main()
    except KeyboardInterrupt:
        pass

    good = [
        (ch, ok) for ch, ok in heartbeats
        if ch and "player-alerts" in ch and ok
    ]
    assert good, f"no heartbeats while quiet: {heartbeats!r}"


def test_boot_floor_delivers_late_but_not_history(monkeypatch):
    from datetime import datetime, timedelta

    import discord_reader as dr

    fake_now = datetime(2026, 9, 18, 9, 26)
    monkeypatch.setattr(dr, "true_now", lambda: fake_now)
    boot = fake_now - timedelta(hours=6)

    # the incident: alert arrived 3h ago during a UIA blind period
    late = "APP — 06:39\nBOUGHT 09/25 ARM 300c @ 1.65 small size"
    assert dr.is_recent_message(late, floor=boot)

    # a bare late-evening time parses as later-today: future, rejected
    evening = "APP — 23:10\nBOUGHT 09/18 SPY 753c @ 6.22 full size"
    assert not dr.is_recent_message(evening, floor=boot)

    # no floor given: legacy 10-minute window behaviour
    assert not dr.is_recent_message(late)
    fresh = "APP — 09:24\nBOUGHT SPX 6000c tiny size"
    assert dr.is_recent_message(fresh)


def test_current_messages_passes_floor(monkeypatch):
    from datetime import datetime, timedelta

    import discord_reader as dr

    fake_now = datetime(2026, 9, 18, 9, 26)
    monkeypatch.setattr(dr, "true_now", lambda: fake_now)
    boot = fake_now - timedelta(hours=6)

    class Item:
        Name = "APP — 06:39 BOUGHT 09/25 ARM 300c @ 1.65 small size"

    monkeypatch.setattr(dr, "message_items", lambda c: [Item()])
    monkeypatch.setattr(dr, "item_text", lambda i: i.Name)

    msgs = dr.current_messages(None, 40, boot)
    assert msgs and "ARM 300c" in msgs[0][0]

    # without a floor the same (now stale) message is filtered
    assert dr.current_messages(None, 40) == []


def test_embed_meta_times_parse(monkeypatch):
    from datetime import datetime

    import discord_reader as dr

    monkeypatch.setattr(
        dr, "true_now", lambda: datetime(2026, 9, 18, 9, 26)
    )
    for text in (
        "📢 SPX Plays • Option Alert\nAPP\n今天 06:39\nBOUGHT ARM 300c",
        "APP — 06:39\nBOUGHT ARM 300c",
        "APP Today at 6:39 AM\nBOUGHT ARM 300c",
    ):
        assert dr.meta_time(text) == datetime(2026, 9, 18, 6, 39), text


def test_post_message_reports_success(monkeypatch):
    import discord_reader as dr

    class FakeResp:
        def __init__(self, code):
            self.status_code = code

    monkeypatch.setattr(
        dr.requests, "post", lambda *a, **k: FakeResp(200)
    )
    assert dr.post_message("http://x", "text")
    monkeypatch.setattr(
        dr.requests, "post", lambda *a, **k: FakeResp(401)
    )
    assert not dr.post_message("http://x", "text")

    def boom(*a, **k):
        raise dr.requests.RequestException("pipeline down")

    monkeypatch.setattr(dr.requests, "post", boom)
    assert not dr.post_message("http://x", "text")


def test_unsent_messages_are_retried(monkeypatch, tmp_path):
    # regression: a post that failed while the pipeline was restarting
    # used to be marked seen anyway, silently swallowing the alert
    import discord_reader as dr

    cfg = {
        "reader": {
            "pipeline_url": "http://localhost:8080/alert",
            "poll_interval": 0.01,
            "channels": ["player-alerts"],
        },
        "discord": {"webhook_url": ""},
    }

    class FakeWindow:
        Name = "🚨│player-alerts | #general - Discord"

    class FakeContainer:
        Name = "🚨│player-alerts 中的消息"

    msg = "BOUGHT 09/25 ARM 300c @ 1.65 small size"
    attempts = {"n": 0}

    def fake_post(url, text, token="", ts=None, verify=True, channel=""):
        attempts["n"] += 1
        return attempts["n"] > 3   # pipeline down for the first 3 tries

    monkeypatch.setattr(dr, "SEEN_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setattr(dr, "load_config", lambda: cfg)
    monkeypatch.setattr(dr, "sync_clock", lambda: None)
    monkeypatch.setattr(dr, "git_head", lambda root: "abc123")
    monkeypatch.setattr(dr, "repo_root", lambda: ".")
    monkeypatch.setattr(dr, "find_discord_window", lambda: FakeWindow())
    monkeypatch.setattr(
        dr, "find_message_container", lambda *a, **k: FakeContainer()
    )
    polls = {"n": 0}

    def fake_current(container, max_items=40, floor=None):
        polls["n"] += 1
        anchor = [("earlier message in channel", None)]
        if polls["n"] <= 3:
            return anchor
        return anchor + [(msg, None)]   # the alert arrives on poll 4

    monkeypatch.setattr(dr, "current_messages", fake_current)
    monkeypatch.setattr(dr, "WebhookLog", lambda url: None)
    monkeypatch.setattr(dr, "sync_with_server",
                        lambda *a, **k: None)
    monkeypatch.setattr(dr, "post_message", fake_post)

    sleeps = {"n": 0}

    def fake_sleep(secs):
        sleeps["n"] += 1
        if sleeps["n"] > 60:
            raise KeyboardInterrupt

    monkeypatch.setattr(dr.time, "sleep", fake_sleep)

    try:
        dr.main()
    except KeyboardInterrupt:
        pass

    # failed 3 times, then succeeded (delivered on the 4th attempt)
    assert attempts["n"] >= 4, attempts


def test_day_floor_catches_up_whole_day(monkeypatch):
    from datetime import datetime

    import discord_reader as dr

    fake_now = datetime(2026, 9, 18, 14, 3)
    monkeypatch.setattr(dr, "true_now", lambda: fake_now)
    day_floor = fake_now.replace(hour=0, minute=0, second=0, microsecond=0)

    # this morning's alert, discovered 7+ hours later: still deliverable
    morning = "APP — 06:39\nBOUGHT 09/25 ARM 300c @ 1.65 small size"
    assert dr.is_recent_message(morning, floor=day_floor)

    # yesterday's full-dated message: excluded
    yesterday = (
        "September 17, 2026 at 10:04 AM\nBOUGHT SPY 750c full size"
    )
    assert not dr.is_recent_message(yesterday, floor=day_floor)


def test_seen_persistence_round_trip(tmp_path, monkeypatch):
    import time as time_mod

    import discord_reader as dr

    monkeypatch.setattr(dr, "SEEN_FILE", str(tmp_path / "seen.json"))

    seen, seen_at = dr.load_seen()
    assert seen == set() and seen_at == {}

    seen_at["delivered today"] = time_mod.time() - 10
    seen_at["delivered long ago"] = time_mod.time() - 49 * 3600
    dr.save_seen(seen_at)

    seen2, seen_at2 = dr.load_seen()
    assert "delivered today" in seen2
    assert "delivered long ago" not in seen2


def test_restart_notice_includes_commit(monkeypatch, tmp_path):
    import discord_reader as dr

    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(json)

    monkeypatch.setattr(dr.requests, "post", fake_post)
    monkeypatch.setattr(
        dr, "git_commit_line",
        lambda root, sha: f"{sha[:8]} make them include all including commit",
    )

    dr.notify_restart(
        "http://hook", "code updated to abc12345",
        commits=dr.git_commit_line(".", "abc1234567"),
    )
    fields = {
        f["name"]: f["value"] for f in sent["embeds"][0]["fields"]
    }
    assert fields["commits"].startswith("abc12345")
    assert "including commit" in fields["commits"]

    # without commits, no commits field is added
    sent.clear()
    dr.notify_restart("http://hook", "manual")
    fields = {
        f["name"]: f["value"] for f in sent["embeds"][0]["fields"]
    }
    assert "commits" not in fields


def test_snap_to_bottom_scrolls_only_when_needed():
    import discord_reader as dr

    class Pattern:
        def __init__(self, visible, pct):
            self._visible = visible
            self._pct = pct
            self.called = None

        @property
        def VerticalViewSize(self):
            return self._visible

        @property
        def VerticalScrollPercent(self):
            return self._pct

        def SetScrollPercent(self, h, v):
            self.called = v

    class Container:
        def __init__(self, pattern):
            self._pattern = pattern

        def GetScrollPattern(self):
            return self._pattern

    # mid-scroll pane: snaps to 100
    p = Pattern(30, 40)
    logs = []
    dr.snap_to_bottom(Container(p), log_fn=logs.append)
    assert p.called == 100
    assert logs and "scrolling to latest" in logs[0]

    # already at the bottom: no scroll
    p2 = Pattern(30, 100)
    dr.snap_to_bottom(Container(p2))
    assert p2.called is None

    # fully visible (no scrollbar): no scroll
    p3 = Pattern(100, 0)
    dr.snap_to_bottom(Container(p3))
    assert p3.called is None


def test_store_keeps_message_time():
    import time as time_mod

    from trader.store import Store

    store = Store(":memory:")
    epoch = time_mod.time() - 3600  # an hour ago
    from datetime import datetime, timezone

    parsed_epoch = time_mod.time()
    store.record_signal(
        "k", "a", "text", True, ts_epoch=epoch, parsed_epoch=parsed_epoch
    )
    row = store.recent_signals()[0]
    expect = datetime.fromtimestamp(
        epoch, tz=timezone.utc
    ).isoformat(timespec="seconds")
    assert row["ts"] == expect
    expect_r = datetime.fromtimestamp(
        parsed_epoch, tz=timezone.utc
    ).isoformat(timespec="seconds")
    assert row["received_ts"] == expect_r


def test_snap_falls_back_to_end_key(monkeypatch):
    import discord_reader as dr

    class NoScroll:
        def GetScrollPattern(self):
            return None

        def SetFocus(self):
            NoScroll.focused = True

    NoScroll.focused = False
    sent = []
    monkeypatch.setattr(dr.auto, "SendKeys",
                        lambda keys, waitTime=None: sent.append(keys))

    logs = []
    dr.snap_to_bottom(NoScroll(), log_fn=logs.append)
    assert NoScroll.focused
    assert sent == ["{End}"]
