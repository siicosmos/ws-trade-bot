import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import psutil
import requests
import uiautomation as auto

try:
    from _ctypes import COMError as UIAError
except ImportError:
    UIAError = Exception

from inspect_discord import (
    close_extra_windows,
    tree_entry_names,
    find_channel_control,
    click_channel_control,
    find_discord_window,
    kill_discord,
    start_discord,
)

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
            r"\bchannel marker\b",
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
        s.settimeout(5)
        s.sendto(b"\x1b" + 47 * b"\0", ("pool.ntp.org", 123))
        data, _ = s.recvfrom(1024)
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
        log(
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
DIVIDER_RE = re.compile(
    r"\s*(?:新的|新訊息|NEW|New messages? below|新着)\s*"
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


READER_LOG = None        # resolved lazily - repo_root is later
READER_LOG_MAX = 2_000_000
_stayup_log_ts = 0.0


def _append_reader_log(line):
    """Crash tracebacks survive here even when the process dies."""
    path = READER_LOG or os.path.join(repo_root(), "reader.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > READER_LOG_MAX:
            if os.path.exists(path + ".1"):
                os.replace(path + ".1", path + ".2")
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def _supervised_thread(name, target, restart_delay=30):
    """Reader-local copy of the supervision policy: a loop that
    dies is logged and relaunched after a backoff."""
    def _runner():
        while True:
            try:
                target()
                log(f"{name} thread exited unexpectedly - "
                    f"restarting in {restart_delay}s")
            except Exception as e:
                log(f"{name} thread crashed: {e} - restarting "
                    f"in {restart_delay}s")
            time.sleep(restart_delay)

    t = threading.Thread(target=_runner, daemon=True, name=name)
    t.start()
    return t


class WebhookLog:
    def __init__(self, url):
        self.url = url
        self.lines = []
        self.lock = threading.Lock()
        if url:
            _supervised_thread("reader-webhook", self._run)
            # the final lines ("reader stopped", a crash
            # traceback) must survive the process exit
            import atexit

            atexit.register(self.flush_now)

    def add(self, line):
        if not self.url:
            return
        with self.lock:
            self.lines.append(str(line)[:500])
            if len(self.lines) > 200:
                self.lines = self.lines[-200:]

    def flush_now(self):
        with self.lock:
            batch, self.lines = self.lines, []
        if not batch or not self.url:
            return
        text = ""
        for line in batch:
            if len(text) + len(line) + 1 > 1900:
                self._post(text)
                text = ""
            text += line + "\n"
        if text.strip():
            self._post(text)

    def _post(self, text):
        try:
            requests.post(
                self.url, json={"content": text[:1900]}, timeout=10
            )
        except requests.RequestException:
            pass

    def _run(self):
        while True:
            time.sleep(3)
            self.flush_now()


_log_hook = None


def _startup_banner():
    """Running commit + whether this start came from an
    auto-update (the .last_update.json the updater writes)."""
    root = repo_root()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        commit = head.stdout.strip() if head.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        commit = ""
    line = (
        f"reader starting - commit {commit or '?'}"
        + (
            f' "{git_commit_line(root, commit)}"'
            if commit and git_commit_line(root, commit) else ""
        )
    )
    try:
        with open(os.path.join(root, ".last_update.json")) as f:
            rec = json.load(f)
        if (
            isinstance(rec, dict)
            and rec.get("commit") == (commit or "")
            and rec.get("ts")
        ):
            age = max(0, time.time() - float(rec["ts"]))
            if age < 3600:
                ago = f"{int(age // 60)}m ago"
            elif age < 86400:
                ago = f"{int(age // 3600)}h ago"
            else:
                ago = f"{int(age // 86400)}d ago"
            how = str(rec.get("how") or "pull")
            line += (
                " (auto-updated " + ago + ")"
                if how == "auto" else
                " (updated " + ago + " via " + how + ")"
            )
    except (OSError, ValueError, TypeError):
        pass
    log(line)


def log(msg):
    line = f"{time.strftime('%d/%b/%Y %H:%M:%S')} {msg}"
    print(line)
    if _log_hook is not None:
        _log_hook.add(line)
    _append_reader_log(line)


def find_config_path():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "..", "config.yaml"),
        os.path.join(here, "config.yaml"),
    )
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    return None


def load_config():
    import yaml

    path = find_config_path()
    if path is None:
        log("config.yaml not found - copy config.example.yaml to config.yaml")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def git_commit_line(root, sha):
    """One-line 'sha subject' for a commit, empty on failure."""
    if not sha:
        return ""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--oneline", sha], cwd=root,
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def notify_restart(webhook_url, reason, commits=""):
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            timeout=10,
            json={
                "embeds": [
                    {
                        "title": "Reader restarting",
                        "color": 3066993,
                        "fields": [
                            {
                                "name": "reason",
                                "value": str(reason)[:1000],
                                "inline": True,
                            }
                        ] + (
                            [
                                {
                                    "name": "commits",
                                    "value": str(commits)[:1000] or "-",
                                    "inline": True,
                                }
                            ]
                            if commits
                            else []
                        ),
                    }
                ]
            },
        )
    except requests.RequestException:
        pass


def status_base_url(pipeline_url):
    if pipeline_url.endswith("/alert"):
        return pipeline_url[: -len("/alert")]
    return pipeline_url


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


def find_message_container(window, marker, title_channel="", diag=None,
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
                if not marker or marker.lower() in name.lower():
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


def new_messages(current, tail):
    if not current:
        return []
    if not tail:
        return []
    last = tail[-1]
    for i in range(len(current) - 1, -1, -1):
        if current[i] == last:
            return current[i + 1 :]
    return []


def post_message(url, text, token="", ts=None, verify=True, channel=""):
    headers = {"X-Auth-Token": token} if token else {}
    sent = ts.strftime("%m-%d %H:%M") if ts else None
    try:
        resp = requests.post(
            url,
            json={
                "text": text,
                "author": "",
                "ts": ts.timestamp() if ts else None,
                "parsed_ts": time.time(),
                "channel": channel or "",
            },
            headers=headers, timeout=10, verify=verify,
        )
        prefix = f"[sent {sent}] " if sent else ""
        log(f"-> {resp.status_code} {prefix}{text[:80]}")
        return 200 <= resp.status_code < 300
    except requests.RequestException as e:
        log(f"post failed: {e}")
        return False


def _uia_prop(obj, name):
    value = getattr(obj, name)
    return value() if callable(value) else value


_snap_warn_ts = 0.0


def snap_to_bottom(container, log_fn=None):
    """Discord virtualizes the message list: only the scrolled-in
    viewport exists in the accessibility tree, so a pane left higher
    up hides the newest alerts. Snap it (or a scrollable ancestor)
    to the bottom."""
    global _snap_warn_ts
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
    if not pattern:
        # fallback: focus the pane and send End - Discord jumps the
        # chat to the newest messages, materializing them in the tree
        try:
            container.SetFocus()
            auto.SendKeys("{End}", waitTime=0.05)
            if log_fn and time.time() - _snap_warn_ts > 3600:
                log_fn(
                    "no scroll pattern - using End key to jump to "
                    "latest messages"
                )
                _snap_warn_ts = time.time()
        except Exception as e:
            if log_fn and time.time() - _snap_warn_ts > 600:
                log_fn(f"auto-scroll fallback failed: {e}")
                _snap_warn_ts = time.time()
        return
    try:
        visible = _uia_prop(pattern, "VerticalViewSize")
        if visible is not None and visible >= 100:
            return
        pct = _uia_prop(pattern, "VerticalScrollPercent")
        if pct is None or pct < 98:
            pattern.SetScrollPercent(-1, 100)
            if log_fn:
                log_fn(
                    f"pane at {pct if pct is not None else '?'}% - "
                    f"scrolling to latest"
                )
    except Exception:
        pass


def heartbeat_status(allowed, title_channel):
    """What to tell the server while no message pane is attached.

    On a quiet allowed channel the recency window empties the pane and
    the container is torn down, but the window title still proves which
    channel we are on - report it instead of "waiting".
    """
    if allowed and title_channel:
        return title_channel, True
    return None, False


def sync_with_server(base_url, auth_token, channel, ok, verify=True):
    headers = {"X-Auth-Token": auth_token} if auth_token else {}
    try:
        resp = requests.post(
            f"{base_url}/api/reader_status",
            json={"channel": channel, "ok": ok},
            headers=headers, timeout=5, verify=verify,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def channel_allowed(title_channel, channels, marker):
    if marker:
        return True
    if not channels or not title_channel:
        return True
    name = title_channel.lstrip("#").lower()
    return any(entry in name for entry in channels)


def merged_config(resp, marker, poll_interval, max_items, channels,
                  channel_servers=None, reopen_seconds=15,
                  restart_after=90, auto_switch=True):
    changed = False
    if resp is None:
        return (marker, poll_interval, max_items, channels,
                channel_servers, reopen_seconds, restart_after,
                auto_switch, False)
    if "auto_switch" in resp:
        want = bool(resp.get("auto_switch"))
        if want != auto_switch:
            auto_switch = want
            changed = True
    new_servers = resp.get("channel_servers")
    if isinstance(new_servers, dict):
        normalized = {
            str(k).strip().lower(): str(v).strip()
            for k, v in new_servers.items()
            if str(k).strip() and str(v).strip()
        }
        if normalized != (channel_servers or {}):
            channel_servers = normalized
            changed = True
    try:
        r = int(resp.get("discord_reopen_seconds"))
        if 5 <= r <= 600 and r != reopen_seconds:
            reopen_seconds = r
            changed = True
    except (TypeError, ValueError):
        pass
    try:
        r = int(resp.get("discord_restart_seconds"))
        if 30 <= r <= 3600 and r != restart_after:
            restart_after = r
            changed = True
    except (TypeError, ValueError):
        pass
    new_channels = resp.get("channels")
    new_marker = resp.get("channel_marker")
    if isinstance(new_marker, str) and new_marker != marker:
        marker = new_marker
        changed = True
    new_channels = resp.get("channels")
    if isinstance(new_channels, list):
        normalized = sorted(
            {
                str(c).strip().lower()[:100]
                for c in new_channels
                if str(c).strip()
            }
        )
        if normalized != channels:
            channels = normalized
            changed = True
    try:
        p = float(resp.get("poll_interval"))
        if 0.2 <= p <= 10:
            poll_interval = p
    except (TypeError, ValueError):
        pass
    try:
        m = int(resp.get("max_items"))
        if 5 <= m <= 200:
            max_items = m
    except (TypeError, ValueError):
        pass
    return (
        marker, poll_interval, max_items, channels, channel_servers,
        reopen_seconds, restart_after, auto_switch, changed,
    )


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, ".."))


SEEN_FILE = os.path.join(repo_root(), ".reader_seen.json")
SEEN_RETAIN_SECONDS = 48 * 3600


def load_seen():
    """Messages already delivered, surviving reader restarts."""
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set(), {}
    cutoff = time.time() - SEEN_RETAIN_SECONDS
    fresh = {t: when for t, when in data.items() if when >= cutoff}
    return set(fresh), fresh


def save_seen(seen_at):
    try:
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen_at, f)
        os.replace(tmp, SEEN_FILE)
    except OSError:
        pass


READER_RESTART_FILES = ("reader/*", "requirements.txt")


def reader_relevant_changes(root, old, new):
    """True when the commits between old and new touch the reader.

    Unknown diffs count as relevant - never miss a real change.
    """
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", f"{old}..{new}"],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return True
    except (OSError, subprocess.SubprocessError):
        return True
    from fnmatch import fnmatch

    for line in r.stdout.splitlines():
        name = line.strip()
        if name and any(
            fnmatch(name, p) for p in READER_RESTART_FILES
        ):
            return True
    return False


def git_head(root):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _protected_pids(me):
    """Me plus every ancestor. On Windows a venv python.exe is
    a launcher that spawns the real interpreter with an identical
    command line - the launcher is our parent, and killing it
    (exit code 15) restart-loops us while we run on as an
    orphan. The reader imports no trader code, so this is a
    local copy of the pipeline's helper."""
    pids = {me}
    try:
        import psutil

        proc = psutil.Process(me)
        for _ in range(10):
            ppid = proc.ppid()
            if ppid <= 0 or ppid in pids:
                break
            pids.add(ppid)
            proc = psutil.Process(ppid)
    except Exception:
        pass
    return pids


def _terminate_stale_reader():
    """Kill leftover reader instances from a previous run - a
    hard restart can leave the old one attached to the Discord
    window."""
    try:
        import psutil
    except ImportError:
        return
    me = os.getpid()
    protected = _protected_pids(me)
    script = os.path.normcase(os.path.abspath(__file__))
    script_dir = os.path.normcase(os.path.dirname(script))
    stale = []
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["pid"] in protected:
                    continue
                name = (p.info["name"] or "").lower()
                if "python" not in name:
                    continue
                cmd = p.info["cmdline"] or []
                if not any(
                    os.path.normcase(os.path.basename(str(c)))
                    == os.path.basename(script)
                    for c in cmd
                ):
                    continue
                try:
                    cwd = os.path.normcase(p.cwd())
                except (
                    psutil.AccessDenied, psutil.NoSuchProcess
                ):
                    continue
                if cwd != script_dir:
                    continue
                stale.append(p)
            except (
                psutil.NoSuchProcess, psutil.AccessDenied
            ):
                continue
    except Exception as e:
        log(f"stale-reader check failed: {e}")
        return
    for p in stale:
        try:
            p.terminate()
        except (
            psutil.NoSuchProcess, psutil.AccessDenied
        ):
            pass
    try:
        _, alive = psutil.wait_procs(stale, timeout=3)
        for p in alive:
            try:
                p.kill()
            except (
                psutil.NoSuchProcess, psutil.AccessDenied
            ):
                pass
    except Exception:
        pass
    if stale:
        log(
            "terminated stale reader process(es): "
            + ", ".join(str(p.pid) for p in stale)
        )


def _log_previous_exit():
    """The .bat restart loop writes the exit code; surface it
    (timestamped + webhooked) on the next start."""
    path = os.path.join(repo_root(), "reader_exit.txt")
    try:
        with open(path, encoding="utf-8") as f:
            log(f"previous run: " + f.read().strip())
        os.remove(path)
    except OSError:
        pass


def main():
    _log_previous_exit()
    _terminate_stale_reader()
    raw_cfg = load_config()
    cfg = raw_cfg.get("reader") or {}
    discord_cfg = raw_cfg.get("discord") or {}
    webhook_url = str(discord_cfg.get("webhook_url") or "")
    update_webhook_url = (
        str(discord_cfg.get("update_webhook_url") or "") or webhook_url
    )
    pipeline_url = cfg.get("pipeline_url", "http://localhost:8080/alert")
    marker = str(cfg.get("channel_marker", ""))
    auto_scroll = bool(cfg.get("auto_scroll", True))
    auto_start_discord = bool(cfg.get("auto_start_discord", True))
    discord_start_command = cfg.get("discord_start_command") or None
    auto_switch = bool(cfg.get("auto_switch_channel", True))
    discord_server = str(cfg.get("discord_server") or "").strip()
    channel_servers = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (cfg.get("channel_servers") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    reopen_seconds = max(5, int(cfg.get("discord_reopen_seconds", 15)))
    restart_after = max(
        30, int(cfg.get("discord_restart_seconds", 90))
    )
    poll_interval = float(cfg.get("poll_interval", 0.5))
    max_items = int(cfg.get("max_items", 40))
    channels = sorted(
        {
            str(c).strip().lower()[:100]
            for c in cfg.get("channels") or []
            if str(c).strip()
        }
    )
    auth_token = cfg.get("auth_token", "")
    base_url = status_base_url(pipeline_url)
    verify_tls = not pipeline_url.lower().startswith("https")
    if not verify_tls:
        try:
            import urllib3

            urllib3.disable_warnings(
                urllib3.exceptions.InsecureRequestWarning
            )
        except ImportError:
            pass
        log("pipeline URL is https - skipping certificate verification")

    global _log_hook
    _log_hook = WebhookLog(
        str(discord_cfg.get("reader_log_webhook_url") or "")
    )

    _startup_banner()

    sync_clock()
    last_clock_check = time.time()

    # anything timestamped today is deliverable, even hours late - the
    # seen-set (persisted across restarts) keeps delivery at-most-once
    day_floor = (
        true_now().replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(minutes=2)
    )

    start_head = git_head(repo_root())
    last_head_check = time.time()

    tail = []
    pending = []   # (text, ts, channel) awaiting successful delivery
    last_read_summary = None

    def mark_seen(text):
        seen.add(text)
        seen_at[text] = time.time()
        if len(seen) > 5000:
            seen.clear()
            seen_at.clear()
        save_seen(seen_at)
    log("looking for Discord window...")
    window = None
    while window is None:
        window = find_discord_window()
        if window is None:
            if auto_start_discord:
                start_discord(
                    discord_start_command, log=log,
                    cooldown=reopen_seconds,
                )
            time.sleep(2)

    log(f"watching window {window.Name!r} (poll every {poll_interval}s)")
    if marker:
        log(f"channel marker: {marker!r}")
    else:
        log("channel marker empty - following whatever channel is open")
    if channels:
        log(f"allowed channels: {channels}")
    if channel_servers:
        log(f"channel servers: {channel_servers}")
    else:
        log(
            "channel servers: none - set server->channel pairs in "
            "the settings so the reader can switch servers"
        )

    container = None
    current_channel = None
    announced_channel = None
    last_title_channel = None
    resync = True    # first attach catches up today's unseen messages
    seen, seen_at = load_seen()
    wait_attempts = 0
    empty_polls = 0
    last_stale_log = 0.0
    sync_counter = 99
    scroll_counter = 0
    # local for the stay-up log (an assignment inside main()
    # shadows the module-level default - keep it initialized
    # before the loop like its neighbors)
    _stayup_log_ts = 0.0

    while True:
        if time.time() - last_clock_check > 600:
            last_clock_check = time.time()
            sync_clock()

        if time.time() - last_head_check > 10:
            last_head_check = time.time()
            head = git_head(repo_root())
            if head and start_head and head != start_head:
                if reader_relevant_changes(repo_root(), start_head, head):
                    log("repo updated on disk - restarting reader for "
                        "new code")
                    notify_restart(
                        update_webhook_url,
                        f"code updated to {head[:8]}",
                        commits=git_commit_line(repo_root(), head),
                    )
                    if _log_hook is not None:
                        _log_hook.flush_now()
                    os._exit(77)
                # the pull only touched files the reader does not
                # execute - keep the process running (logged at
                # most hourly - dev pushes land in bursts)
                now = time.time()
                if now - _stayup_log_ts > 3600:
                    log(
                        "repo updated (no reader changes) - "
                        "staying up"
                    )
                    _stayup_log_ts = now
                start_head = head

        try:
            try:
                title_channel = channel_from_title(window.Name)
            except UIAError:
                title_channel = ""
            if not title_channel:
                title_channel = last_title_channel or ""
            allowed = channel_allowed(title_channel, channels, marker)
            if (
                not marker
                and allowed
                and title_channel
                and last_title_channel is not None
                and title_channel != last_title_channel
            ):
                tail = []
                resync = True
                current_channel = title_channel
                if container is not None:
                    try:
                        cname = normalize_channel_name(
                            (container.Name or "")[:80], title_channel
                        )
                    except UIAError:
                        cname = ""
                    if cname != title_channel:
                        container = None
            if not allowed:
                if announced_channel is not None:
                    log(
                        f"channel {title_channel!r} not in allowed "
                        f"channels {channels} - waiting"
                    )
                    announced_channel = None
                    current_channel = None
                container = None
                tail = []
                resync = False
            # actively reach a channel we are allowed to read:
            # discord opens wherever it last was (often the
            # friends page after an update), where the title has
            # no channel at all
            if (
                auto_switch
                and (channels or marker)
                and (not title_channel or not allowed)
            ):
                target = channels[0] if channels else (marker or "")
                server = channel_servers.get(
                    target.lower()
                ) or discord_server
                ctrl = find_channel_control(window, [target])
                if ctrl is not None:
                    click_channel_control(ctrl, log=log)
                elif server:
                    # the channel lives on another server - a
                    # server's channels only appear once selected
                    srv = find_channel_control(window, [server])
                    if srv is not None:
                        click_channel_control(srv, log=log)
                    elif wait_attempts % 120 == 0:
                        log(
                            f"no {target!r} entry and no server "
                            f"{server!r} visible in the discord "
                            f"tree; clickable entries: "
                            f"{tree_entry_names(window)}"
                        )
                elif wait_attempts % 120 == 0:
                    log(
                        f"channel {title_channel!r} not allowed and "
                        f"no {target!r} entry visible - set a "
                        f"server->channel pair in the settings "
                        f"(e.g. 'SPX Plays=player-alerts'); "
                        f"clickable entries: "
                        f"{tree_entry_names(window)}"
                    )
            if title_channel:
                last_title_channel = title_channel

            if container is None:
                diag = []
                if allowed:
                    container = find_message_container(
                        window, marker,
                        "" if marker else title_channel,
                        diag,
                        strict_title=bool(channels) and not marker,
                    )
                if container is None:
                    wait_attempts += 1
                    # a discord update can orphan the held window
                    # handle: the cached title still reads but the
                    # ui tree walk returns nothing - re-acquire a
                    # fresh window handle instead of polling a
                    # zombie forever
                    if wait_attempts and wait_attempts % 20 == 0:
                        log(
                            "no message pane for 10s - re-acquiring "
                            "the discord window (possible stale "
                            "handle after a discord update)"
                        )
                        try:
                            fresh = find_discord_window()
                            if fresh is not None:
                                window = fresh
                            elif auto_start_discord:
                                # no window at all: the process was
                                # killed or is hung - popups get
                                # closed, then a fresh start
                                close_extra_windows(window, log=log)
                                start_discord(
                                    discord_start_command, log=log,
                                    cooldown=reopen_seconds,
                                )
                        except UIAError:
                            pass
                    # first failure, then roughly every 10s, then a
                    # hint after 30s of not finding the pane
                    if allowed and (
                        wait_attempts == 1
                        or wait_attempts % 20 == 0
                    ):
                        log(
                            f"no message pane found "
                            f"(title channel: {title_channel!r}) - "
                            f"{len(diag)} candidates scanned:"
                        )
                        notable = [
                            (ctype, cname, score)
                            for ctype, cname, score in diag
                            if cname
                        ]
                        for ctype, cname, score in notable[-8:]:
                            log(
                                f"  [{ctype}] name={cname!r} score={score}"
                            )
                        if not notable:
                            log(
                                "  (no named candidates - the Discord "
                                "window exposes no message list; is it "
                                "minimized to tray or showing no "
                                "channel?)"
                            )
                        if wait_attempts >= 60:
                            log(
                                "still no message pane after 30s - "
                                "bring the Discord window to the "
                                "foreground and open a channel, or "
                                "restart the reader"
                            )
                    resp = sync_with_server(
                        base_url, auth_token,
                        *heartbeat_status(allowed, title_channel),
                        verify_tls,
                    )
                    (marker, poll_interval, max_items, channels,
                     channel_servers, reopen_seconds, restart_after,
                     auto_switch, changed) = merged_config(
                        resp, marker, poll_interval, max_items, channels,
                        channel_servers, reopen_seconds, restart_after,
                        auto_switch,
                    )
                    if changed:
                        log(
                            f"channel config -> marker={marker!r} "
                            f"channels={channels} "
                            f"servers={channel_servers}"
                        )
                    time.sleep(5)
                    continue
                wait_attempts = 0

            try:
                name = normalize_channel_name(
                    (container.Name or "")[:80], title_channel
                )
            except UIAError:
                name = ""
            if not name:
                name = title_channel
            if name:
                current_channel = name
                if name != announced_channel:
                    tail = []
                    if announced_channel is None:
                        log(f"watching channel: {current_channel!r}")
                    else:
                        log(f"channel switched: {current_channel!r}")
                    announced_channel = name

            # heartbeats fire from here so a quiet channel (no recent
            # messages -> current_messages returns []) still reports
            # the open channel instead of going silent
            sync_counter += 1
            if sync_counter >= 10:
                sync_counter = 0
                resp = sync_with_server(
                    base_url, auth_token, current_channel,
                    container is not None, verify_tls,
                )
                (new_marker, new_poll, new_max, new_channels,
                 new_servers, new_reopen, new_restart, new_switch,
                 changed) = merged_config(
                    resp, marker, poll_interval, max_items, channels,
                    channel_servers, reopen_seconds, restart_after,
                    auto_switch,
                )
                if changed:
                    marker = new_marker
                    channels = new_channels
                    channel_servers = new_servers
                    poll_interval = new_poll
                    max_items = new_max
                    reopen_seconds = new_reopen
                    restart_after = new_restart
                    auto_switch = new_switch
                    container = None
                    log(
                        f"channel config -> marker={marker!r} "
                        f"channels={channels} "
                        f"servers={channel_servers}"
                    )
                    time.sleep(poll_interval)
                    continue
                poll_interval = new_poll
                max_items = new_max

            scroll_counter += 1
            if auto_scroll and scroll_counter % 10 == 0:
                snap_to_bottom(
                    container,
                    log_fn=lambda msg: log(msg),
                )

            msgs = current_messages(container, max_items, day_floor)
            summary = (
                f"{len(msgs)} message(s) read"
                + (f", newest: {msgs[-1][0][:70]!r}" if msgs else "")
            )
            if summary != last_read_summary:
                log(f"pane: {summary}")
                last_read_summary = summary
            if not msgs:
                empty_polls += 1
                if empty_polls >= 10:
                    container = None
                    empty_polls = 0
                time.sleep(poll_interval)
                continue
            empty_polls = 0

            # deliver retries first: a message only counts as seen once
            # the pipeline accepted it, otherwise a pipeline restart
            # would silently swallow alerts
            if pending:
                retry = []
                for text, ts, chan in pending:
                    if post_message(
                        pipeline_url, text, auth_token, ts, verify_tls, chan
                    ):
                        mark_seen(text)
                    else:
                        retry.append((text, ts, chan))
                pending = retry

            if resync:
                fresh = [
                    (t, ts) for t, ts in msgs[-5:]
                    if t not in seen
                    and all(t != p[0] for p in pending)
                ]
                resync = False
            else:
                fresh = new_messages(msgs, tail)
            if fresh:
                tail = msgs
                for text, ts in fresh:
                    if text in seen:
                        continue
                    if post_message(
                        pipeline_url, text, auth_token, ts, verify_tls,
                        current_channel or "",
                    ):
                        mark_seen(text)
                    else:
                        pending.append((text, ts, current_channel or ""))
            elif msgs != tail:
                tail = msgs
        except UIAError as e:
            if time.time() - last_stale_log > 300:
                log(
                    f"UIA stale element ({e}) - re-attaching "
                    f"(further occurrences hidden for 5m)"
                )
                last_stale_log = time.time()
            container = None
            current_channel = None
            try:
                window = find_discord_window()
            except UIAError:
                window = None
            if window is None:
                log("Discord window lost; waiting...")
                lost_since = time.time()
                restarted = False
                while window is None:
                    if auto_start_discord:
                        start_discord(
                            discord_start_command,
                            cooldown=reopen_seconds,
                        )
                    # running but windowless for restart_after (a
                    # stuck update screen) - kill and restart
                    if (
                        not restarted
                        and time.time() - lost_since > restart_after
                    ):
                        from inspect_discord import kill_discord

                        if kill_discord(log=log):
                            restarted = True
                        start_discord(
                            discord_start_command, log=log,
                            cooldown=reopen_seconds,
                        )
                        lost_since = time.time()
                    time.sleep(2)
                    try:
                        window = find_discord_window()
                    except UIAError:
                        window = None

        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("reader stopped")
    except SystemExit:
        raise
    except Exception:
        import traceback

        log("reader crashed:\n" + traceback.format_exc())
        sys.exit(1)
