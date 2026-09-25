import argparse

import psutil
import uiautomation as auto

try:
    from _ctypes import COMError as UIAError
except ImportError:
    UIAError = Exception


def discord_pids():
    pids = set()
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith("discord"):
                pids.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


_last_discord_start = 0.0


def start_discord(command=None, log=print):
    """Start (or foreground) discord when no window is available.

    The default command drives discord's updater, which starts
    the app under the current version - re-running it while the
    app sits in the tray signals the single instance to show its
    window. Rate limited to one attempt a minute."""
    import os
    import subprocess
    import time as _time

    global _last_discord_start
    if _time.time() - _last_discord_start < 60:
        return False
    _last_discord_start = _time.time()
    cmd = command or [
        os.path.expandvars(
            "%LocalAppData%\\Discord\\Update.exe"
        ),
        "--processStart", "Discord.exe",
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"discord not showing a window - started {cmd[0]}")
        return True
    except Exception as e:
        log(f"discord start failed: {e}")
        return False


def find_discord_window():
    pids = discord_pids()
    root = auto.GetRootControl()
    try:
        windows = root.GetChildren()
    except UIAError:
        return None
    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            if win.ProcessId in pids:
                return win
        except UIAError:
            continue
    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            if (win.Name or "").lower().endswith(" - discord"):
                return win
        except UIAError:
            continue
    return None


def dump(control, depth, out, max_depth):
    if depth > max_depth:
        return
    try:
        name = (control.Name or "").replace("\n", " ")[:100]
        line = (
            f"{'  ' * depth}[{control.ControlTypeName}] "
            f"class={control.ClassName or ''} "
            f"name=\"{name}\" "
            f"children={control.ChildCount}"
        )
        print(line)
        out.append(line)
    except UIAError:
        return
    try:
        for child in control.GetChildren():
            dump(child, depth + 1, out, max_depth)
    except UIAError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--out", default="discord_tree.txt")
    args = ap.parse_args()

    win = find_discord_window()
    if win is None:
        print("Discord window not found. Is the app running?")
        return

    print(f"Found window: {win.Name!r} (pid {win.ProcessId})")
    lines = []
    dump(win, 0, lines, args.depth)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nTree written to {args.out}")


if __name__ == "__main__":
    main()


def find_channel_control(window, channel_names, max_depth=30):
    """A clickable element in the discord ui tree whose name
    matches one of the target channel names (sidebar entries are
    named like '#player-alerts' or with emoji prefixes) - or
    None when the channel is not in the current tree (wrong
    server selected)."""
    targets = [c.lower() for c in channel_names if c]
    if not targets:
        return None
    try:
        walker = auto.WalkControl(
            window, includeTop=False, maxDepth=30
        )
        for ctrl, depth in walker:
            try:
                if ctrl.ControlType not in (
                    auto.ControlType.ListItemControl,
                    auto.ControlType.TreeItemControl,
                    auto.ControlType.HyperlinkControl,
                    auto.ControlType.ButtonControl,
                    auto.ControlType.TabItemControl,
                ):
                    continue
                name = ctrl.Name or ""
            except UIAError:
                continue
            low = name.lower()
            if not any(c in name.lower() for c in targets):
                continue
            try:
                ctrl.GetClickablePoint()
                return ctrl
            except UIAError:
                continue
    except UIAError:
        return None
    return None


_last_channel_click = 0.0


def click_channel_control(ctrl, log=print):
    """Click a channel entry; rate limited to one attempt a
    minute to avoid fighting the user."""
    import time as _time

    global _last_discord_start
    now = _time.time()
    if now - _last_discord_start < 60:
        return False
    _last_discord_start = now
    try:
        ctrl.Click(simulateMove=False)
        log("switched discord to the target channel")
        return True
    except Exception as e:
        log(f"channel switch click failed: {e}")
        return False
