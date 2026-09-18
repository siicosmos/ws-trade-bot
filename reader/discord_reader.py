import os
import sys
import time

import psutil
import requests
import uiautomation as auto

from inspect_discord import find_discord_window


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
    return candidates[-1] if candidates else None


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
        if text:
            texts.append(text)
    return texts


def new_messages(current, tail):
    if not current:
        return []
    if not tail:
        return current[-3:]
    last = tail[-1]
    for i in range(len(current) - 1, -1, -1):
        if current[i] == last:
            return current[i + 1 :]
    return []


def post_message(url, text):
    try:
        resp = requests.post(
            url, json={"text": text, "author": "", "ts": time.time()}, timeout=10
        )
        print(f"-> {resp.status_code} {text[:80]}")
    except requests.RequestException as e:
        print(f"post failed: {e}")


def main():
    cfg = load_config()
    pipeline_url = cfg.get("pipeline_url", "http://localhost:8080/alert")
    poll_interval = float(cfg.get("poll_interval", 0.5))
    channel_marker = cfg.get("channel_marker", "")
    max_items = int(cfg.get("max_items", 40))

    tail = []
    print("looking for Discord window...")
    window = None
    while window is None:
        window = find_discord_window()
        if window is None:
            time.sleep(2)

    print(
        f"watching window {window.Name!r} "
        f"(poll every {poll_interval}s)"
    )

    container = None
    while True:
        try:
            if container is None:
                container = find_message_container(window, channel_marker)
                if container is None:
                    print("message container not found; adjust channel_marker")
                    time.sleep(5)
                    continue

            msgs = current_messages(container, max_items)
            if not msgs:
                container = None
                time.sleep(poll_interval)
                continue

            fresh = new_messages(msgs, tail)
            if fresh:
                tail = msgs
                for text in fresh:
                    post_message(pipeline_url, text)
            elif msgs != tail:
                tail = msgs
        except KeyboardInterrupt:
            break
        except auto.COMError as e:
            print(f"UIA error: {e}")
            container = None
            window = find_discord_window()
            if window is None:
                print("Discord window lost; waiting...")
                while window is None:
                    time.sleep(2)
                    window = find_discord_window()

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
