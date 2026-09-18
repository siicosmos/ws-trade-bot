import os
import re
import subprocess
import sys
import time

import psutil
import requests
import uiautomation as auto

from inspect_discord import find_discord_window

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
        ]
    ),
    re.I,
)


def looks_like_message(text):
    if not text:
        return False
    if CHROME_RE.search(text):
        return False
    return True


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
        print("config.yaml not found - copy config.example.yaml to config.yaml")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("reader") or {}


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


def container_score(ctrl, sample=8):
    try:
        items = message_items(ctrl)[:sample]
    except auto.COMError:
        return 0
    score = 0
    for item in items:
        try:
            text = item_text(item)
        except auto.COMError:
            continue
        if message_likeness(text) > 0:
            score += 1
    return score


def find_message_container(window, marker):
    candidates = []
    for ctrl, depth in auto.WalkControl(window, includeTop=False, maxDepth=14):
        try:
            if ctrl.ControlType not in (
                auto.ControlType.ListControl,
                auto.ControlType.DocumentControl,
            ):
                continue
            name = ctrl.Name or ""
            if not marker or marker.lower() in name.lower():
                candidates.append(ctrl)
        except auto.COMError:
            continue
    best = None
    best_score = 0
    for ctrl in candidates:
        score = container_score(ctrl)
        if score > 0 and score >= best_score:
            best = ctrl
            best_score = score
    return best


def message_items(container):
    try:
        return container.GetChildren()
    except auto.COMError:
        return []


def item_text(item):
    parts = []
    for ctrl, depth in auto.WalkControl(item, includeTop=False, maxDepth=8):
        try:
            if ctrl.ControlType == auto.ControlType.TextControl and ctrl.Name:
                parts.append(ctrl.Name.strip())
        except auto.COMError:
            continue
    return " ".join(parts).strip()


def current_messages(container, max_items=40):
    texts = []
    for item in message_items(container)[-max_items:]:
        text = item_text(item)
        if text and looks_like_message(text):
            texts.append(text)
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


def post_message(url, text, token=""):
    headers = {"X-Auth-Token": token} if token else {}
    try:
        resp = requests.post(
            url, json={"text": text, "author": "", "ts": time.time()},
            headers=headers, timeout=10,
        )
        print(f"-> {resp.status_code} {text[:80]}")
    except requests.RequestException as e:
        print(f"post failed: {e}")


def sync_with_server(base_url, auth_token, channel, ok):
    headers = {"X-Auth-Token": auth_token} if auth_token else {}
    try:
        resp = requests.post(
            f"{base_url}/api/reader_status",
            json={"channel": channel, "ok": ok},
            headers=headers, timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def merged_config(resp, marker, poll_interval, max_items):
    marker_changed = False
    if resp is None:
        return marker, poll_interval, max_items, False
    new_marker = resp.get("channel_marker")
    if isinstance(new_marker, str) and new_marker != marker:
        marker = new_marker
        marker_changed = True
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
    return marker, poll_interval, max_items, marker_changed


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, ".."))


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


def main():
    cfg = load_config()
    pipeline_url = cfg.get("pipeline_url", "http://localhost:8080/alert")
    marker = str(cfg.get("channel_marker", ""))
    poll_interval = float(cfg.get("poll_interval", 0.5))
    max_items = int(cfg.get("max_items", 40))
    auth_token = cfg.get("auth_token", "")
    base_url = status_base_url(pipeline_url)

    start_head = git_head(repo_root())
    last_head_check = time.time()

    tail = []
    print("looking for Discord window...")
    window = None
    while window is None:
        window = find_discord_window()
        if window is None:
            time.sleep(2)

    print(f"watching window {window.Name!r} (poll every {poll_interval}s)")
    if marker:
        print(f"channel marker: {marker!r}")
    else:
        print("channel marker empty - following whatever channel is open")

    container = None
    current_channel = None
    announced_channel = None
    announced_wait = False
    sync_counter = 99

    while True:
        try:
            if container is None:
                container = find_message_container(window, marker)
                if container is None:
                    if not announced_wait:
                        print(
                            f"waiting for a channel matching {marker!r} - "
                            f"open it in Discord or clear the marker"
                        )
                        announced_wait = True
                    resp = sync_with_server(
                        base_url, auth_token, None, False
                    )
                    marker, poll_interval, max_items, changed = merged_config(
                        resp, marker, poll_interval, max_items
                    )
                    if changed:
                        print(f"channel marker -> {marker!r}")
                    time.sleep(5)
                    continue
                announced_wait = False

            try:
                name = (container.Name or "")[:80]
            except auto.COMError:
                name = ""
            if name:
                current_channel = name
                if name != announced_channel:
                    tail = []
                    if announced_channel is None:
                        print(f"watching channel: {current_channel!r}")
                    else:
                        print(f"channel switched: {current_channel!r}")
                    announced_channel = name

            msgs = current_messages(container, max_items)
            if not msgs:
                container = None
                time.sleep(poll_interval)
                continue

            fresh = new_messages(msgs, tail)
            if fresh:
                tail = msgs
                for text in fresh:
                    post_message(pipeline_url, text, auth_token)
            elif msgs != tail:
                tail = msgs
        except KeyboardInterrupt:
            break
        except auto.COMError as e:
            print(f"UIA error: {e}")
            container = None
            current_channel = None
            window = find_discord_window()
            if window is None:
                print("Discord window lost; waiting...")
                while window is None:
                    time.sleep(2)
                    window = find_discord_window()

        sync_counter += 1
        if sync_counter >= 10:
            sync_counter = 0
            resp = sync_with_server(
                base_url, auth_token, current_channel, container is not None
            )
            new_marker, new_poll, new_max, changed = merged_config(
                resp, marker, poll_interval, max_items
            )
            if changed:
                marker = new_marker
                container = None
                print(f"channel marker -> {marker!r}")
            poll_interval = new_poll
            max_items = new_max

        if time.time() - last_head_check > 30:
            last_head_check = time.time()
            head = git_head(repo_root())
            if head and start_head and head != start_head:
                print("repo updated on disk - restarting reader for new code")
                os._exit(77)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
