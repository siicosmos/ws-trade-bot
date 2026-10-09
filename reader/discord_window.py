"""Reading discord's window: the text/time parsing, the UIA
tree walk that finds the message pane and extracts messages, the
scroll-to-bottom machinery, and the server switch.

Pure reader-service concerns (config, posting, lifecycle, the
main loop) live in discord_reader.py, which re-exports this
module's names.
"""

import re
import time
from datetime import datetime, timedelta

import requests
import uiautomation as auto

try:
    from _ctypes import COMError as UIAError
except ImportError:
    UIAError = Exception

from inspect_discord import (
    ensure_visible,
    _foreground_discord,
    find_channel_control,
    click_channel_control,
    switch_server_keyboard,
)


def _log(msg):
    """The reader's log shipper (console + webhook + file) lives
    in discord_reader - imported lazily (this module is imported
    by it); print until the reader app is up."""
    try:
        import discord_reader as _dr

        _dr.log(msg)
    except Exception:
        print(msg)


CHROME_RE = re.compile(
    "|".join(
        [
            r"社区服务器",
            r"任何人都可以加入",
            r"个助力",
            r"创建频道",
            r"加入该服务器",
            r"anyone can join",
            r"community server",
            r"server boost",
            r"boost this server",
            r"create channel",
            r"频道的起点",
            # the server sidebar leaks into the message walk:
            # channel / pinned / text-channel / invite labels
            # (simplified + traditional locales)
            r"频道",
            r"文字信息",
            r"邀请到频道",
            r"未读信息",
        ]
    ),
    re.I,
)



DATE_TIME_CORE = (
    r"(?:\d{1,2}:\d{2}\s*(?:AM|PM)?[\s,]*){0,2}"
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"[\s,]*)?"
    r"(?:(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},\s*\d{4}[\s,]*)"
    r"(?:at\s+)?(?:\d{1,2}:\d{2}\s*(?:AM|PM)?\s*)?"
)

TS_PREFIX_RE = re.compile(
    r"^\s*(?:[^\d:,]{1,24}?\s+)?" + DATE_TIME_CORE,
    re.I,
)

MID_META_RE = re.compile(DATE_TIME_CORE, re.I)

UI_NOISE_RE = re.compile(
    "|".join(
        [
            r":[\w+-]+:\s*点击反应",
            r"点击反应",
            r"添加反应",
            r"编辑",
            r"转发",
            r"更多",
        ]
    )
)

DATE_ONLY_RE = re.compile(
    r"^(?:今天|昨天|today|yesterday)?\s*"
    r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"[,]?\s*)?"
    r"(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\s+\d{1,2},?\s*\d{4}\s*$",
    re.I,
)

TODAY_ONLY_RE = re.compile(r"^(今天|昨天|today|yesterday)$", re.I)


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*(AM|PM))?\b", re.I)
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
    re.I,
)

LOG_PASTE_RE = re.compile(
    "|".join(
        [
            r"-> \d{3} ",
            r"\bUIA error\b",
            r"\bchannel switched\b",
            r"\bwatching channel\b",
            r"\bwatching window\b",
            r"\bpoll every\b",
            r"\bchecking dependencies\b",
            r"\blooking for Discord\b",
            r"\bno message pane found\b",
        ]
    )
)


META_LEAD_RE = re.compile(
    r"^(?:\S{1,24}\s+)?\d{1,2}:\d{2}\s*(?:AM|PM)\b", re.I
)

# Discord embed author line: "APP 今天 06:39", "APP — 06:39", "Today at 6:39 AM"
EMBED_META_RE = re.compile(
    r"(?:今天|yesterday|today(?:\s+at)?)\s*\d{1,2}:\d{2}"
    r"|\S{0,24}\s*[-–—]\s*\d{1,2}:\d{2}",
    re.I,
)


def strip_ui_noise(text):
    if not text:
        return text
    text = UI_NOISE_RE.sub(" ", text)
    if not TS_PREFIX_RE.match(text):
        mid = MID_META_RE.search(text)
        if mid and mid.start() > 0:
            text = text[mid.start():]
    text = TS_PREFIX_RE.sub("", text)
    text = META_LEAD_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


_clock_offset = 0.0


def internet_offset():
    try:
        import socket
        import struct

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(5)
            s.sendto(b"\x1b" + 47 * b"\0", ("pool.ntp.org", 123))
            data, _ = s.recvfrom(1024)
        finally:
            # a recv timeout used to leak the socket (a new one
            # every 600s clock check)
            s.close()
        true_unix = struct.unpack("!12I", data)[10] - 2208988800
        return true_unix - time.time()
    except OSError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        r = requests.head("https://www.cloudflare.com", timeout=5)
        date_hdr = r.headers.get("Date")
        if date_hdr:
            return parsedate_to_datetime(date_hdr).timestamp() - time.time()
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None


def true_now():
    return datetime.now() + timedelta(seconds=_clock_offset)


def sync_clock():
    global _clock_offset
    offset = internet_offset()
    if offset is None:
        return
    if abs(offset - _clock_offset) > 5:
        _log(
            f"local clock off by {offset:+.0f}s vs internet time "
            f"- correcting"
        )
    _clock_offset = offset


def parse_message_time(text):
    if not text:
        return None
    times = TIME_RE.findall(text)
    if not times:
        return None
    hour, minute, ampm = times[-1]
    hour, minute = int(hour), int(minute)
    if ampm is not None:
        if ampm.upper() == "PM" and hour != 12:
            hour += 12
        elif ampm.upper() == "AM" and hour == 12:
            hour = 0
    now = true_now()
    d = DATE_RE.search(text)
    if d:
        month = MONTHS[d.group(1).lower()]
        day = int(d.group(2))
        year = int(d.group(3))
    else:
        base = now - timedelta(days=1) if re.search(
            r"昨天|yesterday", text, re.I
        ) else now
        month, day, year = base.month, base.day, base.year
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def meta_time(text):
    m = TS_PREFIX_RE.match(text)
    if m:
        return parse_message_time(text[: m.end()])
    m = META_LEAD_RE.match(text)
    if m:
        return parse_message_time(m.group(0))
    m = MID_META_RE.search(text)
    if m:
        return parse_message_time(m.group(0))
    m = EMBED_META_RE.search(text)
    if m:
        return parse_message_time(m.group(0))
    return None


def is_recent_message(text, max_age_minutes=10, floor=None):
    ts = meta_time(text)
    if ts is None:
        return True
    if floor is not None:
        # floor = start of day: anything timestamped today is deliverable,
        # even hours late (found later, or delivered during a UIA blind
        # period); earlier history stays excluded. The persisted seen-set
        # keeps delivery at-most-once per day.
        return (
            ts >= floor - timedelta(minutes=2)
            and ts <= true_now() + timedelta(minutes=5)
        )
    age = (true_now() - ts).total_seconds() / 60
    return -5 <= age <= max_age_minutes


# Discord's unread divider ("New" / localized) can sit inside a
# message item's text walk - two adjacent messages then arrive as
# one delivered text. Split on it and treat each side as its own
# message.
# Discord's unread divider ("New" / localized) can sit inside a
# message item's text walk - two adjacent messages then arrive as
# one delivered text. Split on it and treat each side as its own
# message.
DIVIDER_RE = re.compile(
    r"\s*(?:新的|新訊息|NEW|New messages? below|新着|未读信息|未讀訊息)\s*"
)


def split_divider(text):
    parts = [p.strip() for p in DIVIDER_RE.split(text or "")]
    return [p for p in parts if p]


def looks_like_message(text):
    if not text:
        return False
    if CHROME_RE.search(text) or LOG_PASTE_RE.search(text):
        return False
    if DATE_ONLY_RE.match(text) or TODAY_ONLY_RE.match(text):
        return False
    return True


def message_likeness(text):
    if not looks_like_message(text):
        return -1
    words = text.split()
    if len(words) >= 3:
        return 1
    if re.search(r"[\d@]", text):
        return 1
    return 0


def clean_channel(name):
    if not name:
        return ""
    return "".join(ch for ch in name if ch.isprintable()).strip()


def channel_from_title(title):
    if not title or "|" not in title:
        return ""
    return clean_channel(title.split("|")[0])


def strip_channel_wrapper(name):
    s = clean_channel(name)
    if s.endswith("中的消息"):
        s = s[: -len("中的消息")].strip()
    elif s.lower().startswith("messages in "):
        s = s[len("messages in "):].strip()
    return s


def name_matches_title(name, title_channel):
    if not name or not title_channel:
        return False
    s = strip_channel_wrapper(name)
    if not s:
        return False
    return s.lstrip("#").lower() == title_channel.lstrip("#").lower()


def normalize_channel_name(name, title_channel=""):
    s = strip_channel_wrapper(name)
    if not s:
        return title_channel
    if title_channel and s.lstrip("#").lower() == title_channel.lstrip("#").lower():
        return title_channel
    return s


def container_score(ctrl):
    score = 0
    for text in child_texts(ctrl):
        if message_likeness(text) > 0:
            score += 1
    return score


def child_texts(ctrl, sample=10):
    texts = []
    for item in message_items(ctrl)[:sample]:
        try:
            name = (item.Name or "").strip()
        except UIAError:
            name = ""
        try:
            text = item_text(item)
        except UIAError:
            text = ""
        if name:
            texts.append(name)
        if text and text != name:
            texts.append(text)
    return texts


def find_message_container(window, title_channel="", diag=None,
                           strict_title=False):
    candidates = []
    after_title_match = 0
    try:
        walker = auto.WalkControl(
            window, includeTop=False, maxDepth=30
        )
        for ctrl, depth in walker:
            try:
                if ctrl.ControlType not in (
                    auto.ControlType.ListControl,
                    auto.ControlType.DocumentControl,
                    auto.ControlType.PaneControl,
                    auto.ControlType.GroupControl,
                    auto.ControlType.TableControl,
                    auto.ControlType.CustomControl,
                ):
                    continue
                name = ctrl.Name or ""
                candidates.append(ctrl)
                if title_channel and name_matches_title(
                    name, title_channel
                ):
                    after_title_match += 1
                    if after_title_match > 6:
                        break
            except UIAError:
                continue
    except UIAError:
        pass
    best = None
    best_score = 0
    fallback = None
    fallback_score = 0
    # the message list control is unnamed in some discord
    # builds - track the best unnamed candidate separately so
    # the strict path can still attach when the WINDOW title
    # already matches the wanted channel
    best_unnamed = None
    best_unnamed_score = 0
    for ctrl in candidates:
        try:
            ctrl_name = ctrl.Name or ""
        except UIAError:
            ctrl_name = ""
        score = container_score(ctrl)
        matches_title = name_matches_title(ctrl_name, title_channel)
        if matches_title:
            score += 3
        if diag is not None:
            try:
                diag.append(
                    (ctrl.ControlTypeName, ctrl_name[:40], score)
                )
            except UIAError:
                pass
        if matches_title and score >= best_score:
            best = ctrl
            best_score = score
        if not ctrl_name and score > best_unnamed_score:
            best_unnamed = ctrl
            best_unnamed_score = score
        if not strict_title and score > 0 and score >= fallback_score:
            fallback = ctrl
            fallback_score = score
    if best is not None:
        return best
    if strict_title:
        # the window title already matches the wanted channel:
        # a solid unnamed list is the target's message list
        return best_unnamed if best_unnamed_score >= 3 else None
    return fallback


def message_items(container):
    try:
        return container.GetChildren()
    except UIAError:
        return []


def item_text(item):
    """Message text from the item's text controls.

    Button subtrees are skipped: reaction chips (emoji + count)
    are buttons, and their text leaking into the message makes the
    same message look new again when someone reacts to it.
    """
    parts = []

    def walk(ctrl, depth):
        if depth > 12:
            return
        try:
            children = ctrl.GetChildren()
        except (UIAError, AttributeError):
            return
        for child in children:
            try:
                ctype = child.ControlType
            except (UIAError, AttributeError):
                continue
            if ctype == auto.ControlType.ButtonControl:
                continue
            try:
                if ctype == auto.ControlType.TextControl and child.Name:
                    parts.append(child.Name.strip())
            except (UIAError, AttributeError):
                continue
            walk(child, depth + 1)

    try:
        walk(item, 0)
    except (UIAError, AttributeError):
        pass
    return " ".join(parts).strip()


def current_messages(container, max_items=40, floor=None):
    texts = []
    for item in message_items(container)[-max_items:]:
        text = item_text(item)
        if not text:
            try:
                text = (item.Name or "").strip()
            except UIAError:
                text = ""
        if not text:
            continue
        for part in split_divider(text):
            if not looks_like_message(part):
                continue
            if not is_recent_message(part, floor=floor):
                continue
            texts.append(
                (strip_ui_noise(part), meta_time(part))
            )
    return texts


def new_messages(current, tail, seen=None):
    if not current:
        return []
    if not tail:
        return []
    last = tail[-1]
    for i in range(len(current) - 1, -1, -1):
        if current[i] == last:
            return current[i + 1 :]
    # the tail anchor scrolled out of the window (a chatter burst
    # pushed it off) - the old behaviour returned nothing and
    # every new message was skipped until a channel switch forced
    # a resync. fall back to what this window shows that was never
    # seen (the dedupe set gates the delivery downstream)
    if seen is not None:
        return [m for m in current[-10:] if m[0] not in seen]
    return []


def _uia_prop(obj, name):
    value = getattr(obj, name)
    return value() if callable(value) else value


def switch_server(window, server, log=print):
    """Reach another server: cycle the rail with discord's own
    hotkeys first (mouse clicks are unreliable across monitors,
    dpi scales and rendering glitches), then fall back to
    clicking the server rail entry - it stays in the ui tree
    even when the hotkey never lands (focus withheld or the
    keybind swallowed elsewhere)."""
    if switch_server_keyboard(window, server, log=log):
        return True
    srv = find_channel_control(window, [server])
    if srv is not None and click_channel_control(
        srv, window=window, log=log,
    ):
        log(f"switched to server {server!r} via the rail click")
        return True
    return False


_snap_warn_ts = 0.0


def snap_to_bottom(container, log_fn=None):
    """Discord virtualizes the message list: only the scrolled-in
    viewport exists in the accessibility tree, so a pane left higher
    up hides the newest alerts. Snap it (or a scrollable ancestor)
    to the bottom."""
    node = container
    pattern = None
    for _ in range(4):
        if node is None:
            break
        try:
            pattern = node.GetScrollPattern()
        except Exception:
            pattern = None
        if pattern:
            break
        try:
            node = node.GetParentControl()
        except Exception:
            node = None

    def _warn(msg, period=3600):
        global _snap_warn_ts
        if log_fn and time.time() - _snap_warn_ts > period:
            log_fn(msg)
            _snap_warn_ts = time.time()

    if pattern is not None:
        try:
            visible = _uia_prop(pattern, "VerticalViewSize")
            if visible is not None and visible >= 100:
                return
            pct = _uia_prop(pattern, "VerticalScrollPercent")
            # 97+: close enough to the bottom that the newest
            # messages are in the tree (some builds never report
            # a full 100)
            if pct is None or pct >= 97:
                return
            if log_fn:
                log_fn(
                    f"pane at {pct if pct is not None else '?'}% - "
                    f"scrolling to latest"
                )
            try:
                pattern.SetScrollPercent(-1, 100)
            except Exception:
                pass
            time.sleep(0.3)
            now_pct = _uia_prop(pattern, "VerticalScrollPercent")
            if now_pct is not None and (
                now_pct >= 97 or now_pct > pct + 1
            ):
                return
            # some discord builds accept SetScrollPercent but
            # ignore it (chromium) - wheel the pane down before
            # reaching for the keyboard
            try:
                container.WheelDown(
                    wheelTimes=25, interval=0.02, waitTime=0.05
                )
            except Exception:
                pass
            time.sleep(0.3)
            now_pct = _uia_prop(pattern, "VerticalScrollPercent")
            if now_pct is None or now_pct >= 97 or now_pct > pct:
                return
            _warn("pane still not at the bottom after scrolling")
        except Exception:
            pass
    # fallback: post the End key to the window WITHOUT focus -
    # a posted WM_KEYDOWN reaches chromium even when the window
    # cannot be foregrounded (the screen-off/locked case, where
    # SetForegroundWindow is refused); ignored by builds that
    # only read focused input
    try:
        hwnd = container.GetTopLevelControl().NativeWindowHandle
    except Exception:
        hwnd = 0
    if hwnd:
        try:
            import ctypes as _ctypes

            user32 = _ctypes.windll.user32   # windows only
            WM_KEYDOWN, WM_KEYUP, VK_END = 0x0100, 0x0101, 0x23
            for _ in range(3):
                user32.PostMessageW(
                    hwnd, WM_KEYDOWN, VK_END, 0x00520001
                )
                user32.PostMessageW(hwnd, WM_KEYUP, VK_END, 0xC0520001)
                time.sleep(0.3)
                try:
                    if pattern is not None:
                        now_pct = _uia_prop(
                            pattern, "VerticalScrollPercent"
                        )
                        if now_pct is not None and now_pct >= 97:
                            return
                except Exception:
                    pass
        except AttributeError:
            pass   # not windows (tests / non-windows dev)
    # last resort: focus the pane and send End - Discord jumps
    # the chat to the newest messages, materializing them in the
    # tree (a readable window does not need the restore attempt)
    ensure_visible(container, log=log_fn or print)
    try:
        window = container.GetTopLevelControl()
    except Exception:
        window = None
    if window is None or not _foreground_discord(
        window, log=log_fn or print,
    ):
        _warn(
            "End-key fallback skipped - discord would not take "
            "focus (keys would land in another window)"
        )
        return
    try:
        container.SetFocus()
        auto.SendKeys("{End}", waitTime=0.05)
        if pattern is not None:
            time.sleep(0.5)
            try:
                now_pct = _uia_prop(pattern, "VerticalScrollPercent")
                # the End key worked - the newest messages are in
                # the tree; stay quiet (this is the working path
                # on chromium builds that ignore SetScrollPercent)
                if now_pct is None or now_pct >= 97:
                    return
                _warn(
                    f"End key did not reach the bottom "
                    f"(pane at {now_pct}%)",
                    period=21600,
                )
            except Exception:
                pass
        else:
            _warn(
                "no scroll pattern - using End key to jump to "
                "latest messages", period=21600,
            )
    except Exception as e:
        _warn(f"auto-scroll fallback failed: {e}", period=600)
