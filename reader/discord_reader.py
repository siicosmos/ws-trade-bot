import json
import time

import requests
import psutil
import uiautomation as auto

from inspect_discord import find_discord_window


def load_config(path="reader_config.json"):
    with open(path, "r") as f:
        return json.load(f)


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
    items = []
    try:
        children = container.GetChildren()
    except auto.COMError:
        return items
    for child in children:
        if child.ControlType == auto.ControlType.ListItemControl:
            items.append(child)
        else:
            items.append(child)
    return items


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
    poll_interval = cfg.get("poll_interval", 1.5)
    channel_marker = cfg.get("channel_marker", "")
    max_items = cfg.get("max_items", 40)

    tail = []
    print("looking for Discord window...")
    while True:
        window = find_discord_window()
        if window:
            break
        time.sleep(2)

    print(f"watching window {window.Name!r} (poll every {poll_interval}s)")

    while True:
        try:
            container = find_message_container(window, channel_marker)
            if container is None:
                print("message container not found; adjust channel_marker")
                time.sleep(5)
                continue

            msgs = current_messages(container, max_items)
            fresh = new_messages(msgs, tail)
            if fresh:
                tail = msgs
                for text in fresh:
                    post_message(pipeline_url, text)
            elif msgs and msgs != tail:
                tail = msgs
        except auto.COMError as e:
            print(f"UIA error: {e}")
        except KeyboardInterrupt:
            break

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
