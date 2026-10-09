import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

import psutil
import requests
import uiautomation as auto

try:
    from _ctypes import COMError as UIAError
except ImportError:
    UIAError = Exception

from inspect_discord import (
    _window_readable,
    close_extra_windows,
    ensure_visible,
    tree_entry_names,
    find_channel_control,
    click_channel_control,
    find_discord_window,
    updater_window_visible,
    kill_discord,
    start_discord,
)

_last_hb_fail_log = 0.0

# the discord-window reading layer (text/time parsing, the UIA
# tree walk, scrolling, server switching) - re-exported so this
# module's public surface (and main's calls) stay here
from discord_window import (  # noqa: F401,E402
    CHROME_RE, DATE_ONLY_RE, DATE_RE, DATE_TIME_CORE, DIVIDER_RE,
    EMBED_META_RE, META_LEAD_RE, MID_META_RE, MONTHS, TIME_RE,
    TODAY_ONLY_RE, TS_PREFIX_RE, UI_NOISE_RE,
    channel_from_title, child_texts, clean_channel,
    container_score, current_messages, find_message_container,
    internet_offset, is_recent_message, item_text,
    looks_like_message, message_items,
    message_likeness, meta_time, name_matches_title, new_messages,
    normalize_channel_name, parse_message_time, snap_to_bottom,
    split_divider, strip_channel_wrapper, strip_ui_noise,
    switch_server, sync_clock, true_now,
)

READER_LOG = None        # resolved lazily - repo_root is later
READER_LOG_MAX = 2_000_000
_stayup_log_ts = 0.0


def _append_reader_log(line):
    """Crash tracebacks survive here even when the process dies.
    The log lives in the repo's logs/ folder, next to the info
    and consumer logs."""
    folder = os.path.join(repo_root(), "logs")
    path = READER_LOG or os.path.join(folder, "reader.log")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
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
                # same shape as core.ops.supervise: type + message
                # lead (the webhook truncation keeps the id), the
                # traceback follows for the log
                log(f"{name} thread crashed: "
                    f"{type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                    f"- restarting in {restart_delay}s")
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
            # traceback) must survive the process exit - best
            # effort: a ctrl+c landing inside the flush is
            # swallowed rather than dumping a traceback
            import atexit

            def _flush_quiet():
                try:
                    self.flush_now()
                except BaseException:
                    pass

            atexit.register(_flush_quiet)

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
    auto-update (the info server's .last_update_info.json the
    updater writes)."""
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
        with open(os.path.join(root, "info", ".last_update_info.json"),
                  encoding="utf-8") as f:
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
    try:
        print(line)
    except UnicodeEncodeError:
        # a redirected .bat console (cp1252/cp936) cannot encode
        # every discord string - a crash here escaped the main
        # loop's UIA handler and killed the reader
        print(line.encode("ascii", "replace").decode("ascii"))
    if _log_hook is not None:
        _log_hook.add(line)
    _append_reader_log(line)


def own_config_path():
    """The reader's yaml in config/ - the single source of
    truth for reader settings and the reader's webhooks (reader
    log, raw alerts)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "config", "reader.config.yaml")


def find_config_path():
    if os.path.exists(own_config_path()):
        return os.path.abspath(own_config_path())
    return None


def merge_new_config_keys(live_path, example_path):
    """Add the example's missing keys to the flat live config,
    with the example's comments - add-only (the user's values
    are never touched). A local copy of core.ops.config_merge's
    section merge: the reader imports no trader code, and its
    config is flat (top-level keys)."""
    import yaml as _yaml

    if not os.path.exists(live_path) or not os.path.exists(
        example_path
    ):
        return []
    with open(live_path, encoding="utf-8") as f:
        live_text = f.read()
    with open(example_path, encoding="utf-8") as f:
        example_text = f.read()
    live_keys = set(
        _yaml.safe_load(live_text) or {}
    )
    example_cfg = _yaml.safe_load(example_text) or {}
    # the example's key blocks: a key line + its comment lines
    blocks = {}
    pending = []
    for line in example_text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*\s*):", line)
        if m:
            blocks[m.group(1).strip()] = pending + [line]
            pending = []
        elif line.strip().startswith("#") or not line.strip():
            pending.append(line)
        else:
            pending = []
    missing = [
        (k, b) for k, b in blocks.items()
        if k not in live_keys and not k.startswith("#")
    ]
    if not missing:
        return []
    out = live_text.rstrip("\n")
    added = []
    for key, block in missing:
        added.append(key)
        out += "\n\n" + "\n".join(block).rstrip("\n")
    with open(live_path, "w", encoding="utf-8") as f:
        f.write(out.rstrip("\n") + "\n")
    return added


def load_config():
    import yaml

    path = find_config_path()
    if path is None:
        log("config/reader.config.yaml not found - copy "
            "config/reader.example.config.yaml to "
            "config/reader.config.yaml")
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


_reader_log_webhook = ""
_last_misconfig_notice = 0.0


def notify_reader(webhook_url, title, value, color=15844367):
    """A one-shot embed notice to the reader's log webhook -
    misconfigurations and stalls must reach the phone, not die
    in a log batch."""
    if not webhook_url:
        return
    try:
        requests.post(
            webhook_url,
            timeout=10,
            json={"embeds": [{
                "title": title,
                "color": color,
                "fields": [{
                    "name": "detail",
                    "value": str(value)[:1000],
                    "inline": True,
                }],
            }]},
        )
    except requests.RequestException:
        pass


def status_base_url(info_server_url):
    if info_server_url.endswith("/alert"):
        return info_server_url[: -len("/alert")]
    return info_server_url



def post_raw_alert(url, text):
    """Copy-paste feed: the raw alert text to its own channel -
    fire and forget, no retry (it is a convenience copy)."""
    if not url:
        return
    try:
        requests.post(
            url, json={"content": str(text)[:1900]}, timeout=10
        )
    except requests.RequestException:
        pass


_last_post_fail_log = 0.0


def post_message(url, text, token="", ts=None, verify=True, channel=""):
    global _last_post_fail_log
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
        ok = 200 <= resp.status_code < 300
        # the pending-retry loop re-posts every poll until the
        # pipeline accepts - a persistent rejection (token mismatch,
        # server down) spammed the log every ~2s per message. log
        # the successes fully; rate-limit the failures to 1 / 30s
        if ok:
            prefix = f"[sent {sent}] " if sent else ""
            log(f"-> {resp.status_code} {prefix}{text[:80]}")
        else:
            now = time.time()
            if now - _last_post_fail_log > 30:
                _last_post_fail_log = now
                hint = (
                    " - check reader.auth_token vs info.auth_token"
                    if resp.status_code == 401 else ""
                )
                log(
                    f"-> {resp.status_code} posting {text[:60]!r} "
                    f"(retrying){hint}"
                )
        return ok
    except requests.RequestException as e:
        log(f"post failed: {type(e).__name__}: {e}")
        return False



def heartbeat_status(allowed, title_channel):
    """What to tell the server while no message pane is attached.

    On a quiet allowed channel the recency window empties the pane and
    the container is torn down, but the window title still proves which
    channel we are on - report it instead of "waiting".
    """
    if allowed and title_channel:
        return title_channel, True, None
    return None, False, (
        "no discord channel attached - bring the discord window "
        "to the foreground"
    )


def sync_with_server(base_url, auth_token, channel, ok, verify=True,
                     error=None):
    # heartbeat only - the info server no longer pushes settings
    # back (they would override the yaml on every poll)
    headers = {"X-Auth-Token": auth_token} if auth_token else {}
    try:
        resp = requests.post(
            f"{base_url}/api/reader_status",
            json={"channel": channel, "ok": ok, "error": error},
            headers=headers, timeout=5, verify=verify,
        )
        # a rejected heartbeat (token mismatch) used to be fully
        # silent: the reader line showed offline with no reason
        # anywhere. log it, rate-limited
        global _last_hb_fail_log
        if resp.status_code >= 300:
            now = time.time()
            if now - _last_hb_fail_log > 60:
                _last_hb_fail_log = now
                log(
                    f"heartbeat rejected by the info server "
                    f"(HTTP {resp.status_code}) - check that "
                    f"reader.auth_token matches info.auth_token"
                )
    except requests.RequestException:
        pass


def channel_allowed(title_channel, channels):
    if not channels or not title_channel:
        return True
    name = title_channel.lstrip("#").lower()
    return any(entry in name for entry in channels)


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, ".."))


_READER_HEARTBEAT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".reader_alive"
)
_STANDBY_NOTICE = 3600


def _touch_reader_heartbeat():
    try:
        with open(_READER_HEARTBEAT, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _reader_heartbeat_alive():
    """True when ANOTHER reader process is alive and its
    heartbeat is fresh (< 90s old)."""
    try:
        with open(_READER_HEARTBEAT, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
        if not pid or pid == os.getpid():
            return False
        if not psutil.pid_exists(pid):
            return False
        age = time.time() - os.path.getmtime(_READER_HEARTBEAT)
        return age < 90
    except (OSError, ValueError):
        return False


def _remove_reader_heartbeat():
    try:
        os.remove(_READER_HEARTBEAT)
    except OSError:
        pass


# the seen-set lives in the reader's own folder - git pulls never
# touch untracked files, so the dedupe history survives updates
SEEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".reader_seen.json"
)
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
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
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
        log(f"stale-reader check failed: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return
    survivors = []
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
        survivors = alive
    except Exception:
        pass
    if stale:
        gone = [p for p in stale if p not in survivors]
        if gone:
            log(
                "terminated stale reader process(es): "
                + ", ".join(str(p.pid) for p in gone)
            )
    for p in survivors:
        # an elevated orphan (a boot-time reader started with
        # admin rights) survives terminate+kill from a
        # non-elevated process - it ran alongside for 13h once,
        # posting with the boot-time config while every new
        # reader claimed to have terminated it
        log(
            f"stale reader process(es) SURVIVED termination: "
            f"{p.pid} - likely elevated (admin); close it "
            f"manually or run this launcher as admin"
        )
        notify_reader(
            _reader_log_webhook,
            "Reader: a stale reader could not be terminated",
            f"pid {p.pid} survived terminate+kill - it is "
            f"likely elevated (admin). two readers fight over "
            f"the discord pane; close it manually or run this "
            f"launcher as admin",
            color=15158332,
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
    global _last_stall_notice
    global _reader_log_webhook
    _last_stall_notice = time.time()
    # the webhook handle first: the stale-reader termination
    # notice (and the misconfig notice) fire before the log hook
    # exists
    raw_cfg = load_config()
    _reader_log_webhook = str(
        (raw_cfg.get("discord") or raw_cfg).get(
            "reader_log_webhook_url"
        ) or ""
    )
    _log_previous_exit()
    if _reader_heartbeat_alive():
        # another reader is alive and healthy (a second launcher
        # window): stand by instead of killing it - two readers
        # fighting over the pane killed each other every ~40s
        # through the night
        log(
            "another reader is alive and healthy - standing by "
            "(close this launcher window, or stop the other "
            "reader to take over)"
        )
        notify_reader(
            _reader_log_webhook,
            "Reader: second launcher detected - standing by",
            "a healthy reader already runs; this instance waits "
            "instead of killing it",
        )
        while _reader_heartbeat_alive():
            time.sleep(15)
        log("the other reader stopped - taking over")
    _terminate_stale_reader()
    _touch_reader_heartbeat()
    # the reader's own yaml is flat (keys at top level); the
    # legacy root config nests them under reader:/discord:
    cfg = raw_cfg.get("reader") or raw_cfg
    discord_cfg = raw_cfg.get("discord") or raw_cfg
    raw_alert_webhook_url = str(
        discord_cfg.get("raw_alert_webhook_url") or ""
    )
    update_webhook_url = str(discord_cfg.get("update_webhook_url") or "")
    info_server_url = cfg.get(
        "info_server_url", "http://localhost:8080/alert"
    )
    auto_scroll = bool(cfg.get("auto_scroll", True))
    auto_start_discord = bool(cfg.get("auto_start_discord", True))
    discord_start_command = cfg.get("discord_start_command") or None
    auto_switch = bool(cfg.get("auto_switch_channel", True))
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
    base_url = status_base_url(info_server_url)
    verify_tls = not info_server_url.lower().startswith("https")
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
    _log_hook = WebhookLog(_reader_log_webhook)

    _startup_banner()

    sync_clock()
    last_clock_check = time.time()
    config_path = find_config_path()
    config_mtime = (
        os.path.getmtime(config_path) if config_path else None
    )

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
    last_pane_read = time.time()

    def mark_seen(text):
        seen.add(text)
        seen_at[text] = time.time()
        if len(seen) > 5000:
            # prune the oldest entries instead of clearing: a full
            # wipe forgot the whole dedupe history at once and the
            # next resync re-delivered recent messages
            oldest = sorted(seen_at.items(), key=lambda kv: kv[1])
            for text_old, _ in oldest[:1000]:
                seen.discard(text_old)
                seen_at.pop(text_old, None)
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
    if not auth_token:
        # a silent misconfig: every alert post and the heartbeat
        # would be rejected with 401 and nothing would say why
        log(
            "auth_token is EMPTY - the info server will reject "
            "every alert and heartbeat (401). set it to the info "
            "server's info.auth_token"
        )
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
        # every loop path refreshes the heartbeat (the mtime is
        # the freshness signal - a quiet/empty pane must not
        # stale it and invite a takeover kill)
        _touch_reader_heartbeat()
        if time.time() - last_clock_check > 600:
            last_clock_check = time.time()
            sync_clock()

        if time.time() - last_head_check > 10:
            last_head_check = time.time()
            # an edited config/reader.config.yaml applies on restart -
            # the .bat loop brings the reader back in 5s
            if config_path:
                try:
                    mtime = os.path.getmtime(config_path)
                except OSError:
                    mtime = None
                if config_mtime is not None and mtime != config_mtime:
                    # a config created AFTER startup (mtime was
                    # None) must be watched too - the old guard
                    # `config_mtime and ...` never watched it
                    log("config/reader.config.yaml changed - restarting "
                        "reader to apply it")
                    notify_restart(
                        update_webhook_url,
                        "reader config changed",
                        commits=git_commit_line(repo_root(), git_head(
                            repo_root())),
                    )
                    if _log_hook is not None:
                        _log_hook.flush_now()
                    os._exit(77)
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

        # a failed navigation drops the window handle - re-find
        # it before anything touches window.Name
        if window is None:
            try:
                window = find_discord_window()
            except UIAError:
                window = None
            if window is None:
                # the update stub showing means discord is still
                # starting - say so instead of the generic lost
                # line (rate limited, it can sit here for a while)
                if updater_window_visible():
                    now = time.time()
                    if now - _stayup_log_ts > 30:
                        _stayup_log_ts = now
                        log(
                            "discord is still starting (only the "
                            "updater window shows) - waiting for the "
                            "main window"
                        )
                if auto_start_discord:
                    start_discord(
                        discord_start_command, log=log,
                        cooldown=reopen_seconds,
                    )
                log("Discord window lost; waiting...")
                time.sleep(2)
                continue

        try:
            try:
                title_channel = channel_from_title(window.Name)
            except UIAError:
                title_channel = ""
            if not title_channel:
                title_channel = last_title_channel or ""
            allowed = channel_allowed(title_channel, channels)
            if (
                allowed
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
                and channels
                and (not title_channel or not allowed)
            ):
                target = channels[0]
                server = channel_servers.get(target.lower())
                ctrl = find_channel_control(window, [target])
                if ctrl is not None:
                    if not click_channel_control(
                        ctrl, window=window, log=log,
                    ):
                        # a skipped/failed switch after a hide
                        # cycle means the held window tree is
                        # stale - re-acquire everything fresh
                        window = None
                elif server:
                    if switch_server(window, server, log=log):
                        container = None
                    elif wait_attempts % 120 == 0:
                        log(
                            f"no {target!r} entry and no server "
                            f"{server!r} reachable; clickable entries: "
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
                        window, title_channel, diag,
                        strict_title=bool(channels),
                    )
                # attach-time verification: the window title goes
                # stale on full-page views (the discovery page
                # keeps the last channel title), so the channel's
                # sidebar entry must be present and selected
                # before attaching. while attached, reads proceed
                # ungated - a display-off screen must not block
                # the reader
                if (
                    container is not None
                    and title_channel
                ):
                    bare = next(
                        (c for c in channels
                         if c in title_channel.lower()),
                        title_channel.lstrip("#").lower(),
                    )
                    entry = find_channel_control(window, [bare])
                    if entry is None:
                        container = None
                    else:
                        try:
                            selected = (
                                entry.GetSelectionItemPattern().IsSelected
                            )
                        except Exception:
                            selected = True
                        if not selected:
                            click_channel_control(
                                entry, window=window, log=log
                            )
                            container = None
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
                    channel, ok, reason = heartbeat_status(
                        allowed, title_channel
                    )
                    sync_with_server(
                        base_url, auth_token, channel, ok,
                        verify=verify_tls, error=reason,
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
                sync_with_server(
                    base_url, auth_token, current_channel,
                    container is not None, verify_tls,
                )

            scroll_counter += 1
            if auto_scroll and scroll_counter % 5 == 0:
                snap_to_bottom(
                    container,
                    log_fn=lambda msg: log(msg),
                )

            # mid-navigation (friends page, no channel open): the
            # ui lists score like message containers and their
            # strings would be posted as alerts - read nothing
            # until a real channel is open
            if not title_channel:
                container = None
                time.sleep(poll_interval)
                continue

            msgs = current_messages(container, max_items, day_floor)
            summary = (
                f"{len(msgs)} message(s) read"
                + (f", newest: {msgs[-1][0][:70]!r}" if msgs else "")
            )
            if summary != last_read_summary:
                log(f"pane: {summary}")
                last_read_summary = summary
            # the stall watchdog: a dead pane attach (the discord
            # window lost focus / re-rendered) reads nothing while
            # alerts pile up unseen - the 10:22 incident ran 1.5h
            # with only a log line nobody watches
            if msgs:
                last_pane_read = time.time()
            elif (
                time.time() - last_pane_read > 600
                and time.time() - _last_stall_notice > 3600
            ):
                _last_stall_notice = time.time()
                stalled_min = int(
                    (time.time() - last_pane_read) // 60
                )
                log(
                    f"no pane reads for {stalled_min}min - alerts "
                    f"are NOT flowing (discord window detached?)"
                )
                notify_reader(
                    _reader_log_webhook,
                    f"Reader stalled: no pane reads for "
                    f"{stalled_min}min",
                    "alerts are not flowing - the discord window "
                    "may be detached or unfocused; alerts posted "
                    "when it re-attaches",
                    color=15158332,
                )
            if not msgs:
                # a minimized or hidden discord renders nothing -
                # restore it. the window's readability is the
                # verdict: a stale container (normal after a
                # switch re-render) only drops the container; the
                # window handle drops only when the window itself
                # is gone or hidden
                if ensure_visible(container, window=window, log=log):
                    container = None
                elif not _window_readable(window):
                    container = None
                    window = None
                else:
                    container = None
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
                late_count = 0
                late_oldest = 0.0
                for text, ts, chan in pending:
                    if post_message(
                        info_server_url, text, auth_token, ts,
                        verify_tls, chan
                    ):
                        mark_seen(text)
                        post_raw_alert(raw_alert_webhook_url, text)
                        if ts is not None:
                            delay = time.time() - ts.timestamp()
                            if delay > 300:
                                late_count += 1
                                late_oldest = max(late_oldest, delay)
                    else:
                        retry.append((text, ts, chan))
                pending = retry
                if late_count and time.time() - _last_stall_notice > 60:
                    _last_stall_notice = time.time()
                    log(
                        f"delivered {late_count} alert(s) late "
                        f"(oldest {int(late_oldest // 60)}min) - the "
                        f"discord pane attach was down"
                    )
                    notify_reader(
                        _reader_log_webhook,
                        f"Reader delivered {late_count} alert(s) late",
                        f"oldest {int(late_oldest // 60)}min - the "
                        f"discord pane attach was down; the consumer "
                        f"executed them on receipt",
                        color=15844367,
                    )

            if resync:
                fresh = [
                    (t, ts) for t, ts in msgs[-5:]
                    if t not in seen
                    and all(t != p[0] for p in pending)
                ]
                resync = False
            else:
                fresh = new_messages(msgs, tail, seen)
            if fresh:
                tail = msgs
                late_count = 0
                late_oldest = 0.0
                for text, ts in fresh:
                    if text in seen:
                        continue
                    if post_message(
                        info_server_url, text, auth_token, ts,
                        verify_tls, current_channel or "",
                    ):
                        mark_seen(text)
                        post_raw_alert(raw_alert_webhook_url, text)
                        # a fresh message with an old timestamp is
                        # the re-attach backlog drain (the pane
                        # was unreadable while the alerts piled up)
                        if ts is not None:
                            delay = time.time() - ts.timestamp()
                            if delay > 300:
                                late_count += 1
                                late_oldest = max(late_oldest, delay)
                    else:
                        pending.append((text, ts, current_channel or ""))
                if late_count and time.time() - _last_stall_notice > 60:
                    _last_stall_notice = time.time()
                    log(
                        f"delivered {late_count} alert(s) late "
                        f"(oldest {int(late_oldest // 60)}min) - the "
                        f"discord pane attach was down"
                    )
                    notify_reader(
                        _reader_log_webhook,
                        f"Reader delivered {late_count} alert(s) late",
                        f"oldest {int(late_oldest // 60)}min - the "
                        f"discord pane attach was down; the consumer "
                        f"executed them on receipt",
                        color=15844367,
                    )
            elif msgs != tail:
                tail = msgs
        except UIAError as e:
            if time.time() - last_stale_log > 300:
                log(
                    f"UIA stale element "
                    f"({type(e).__name__}: {e}) - re-attaching "
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
    finally:
        _remove_reader_heartbeat()
