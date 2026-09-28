import argparse
import ctypes
import time as _time

import psutil
import uiautomation as auto

try:
    from _ctypes import COMError as UIAError
except ImportError:
    UIAError = Exception


def _make_dpi_aware():
    """Without this, windows virtualizes ui coordinates on
    scaled displays and every synthetic mouse click lands at
    the wrong physical position - the channel switch clicks
    into empty space."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_make_dpi_aware()


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
_last_channel_click = 0.0


def start_discord(command=None, log=print, cooldown=15):
    """Start (or foreground) discord when no window is available.

    The default command drives discord's updater, which starts
    the app under the current version - re-running it while the
    app sits in the tray signals the single instance to show its
    window. Rate limited to one attempt every 15s so a dead
    discord comes back quickly."""
    import os
    import subprocess
    import time as _time

    global _last_discord_start
    if _time.time() - _last_discord_start < cooldown:
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


def kill_discord(log=print):
    """Terminate every discord process - used when the app is
    running but never shows a window (hung update screen)."""
    import time as _time

    killed = []
    for p in psutil.process_iter(["name", "pid"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith("discord"):
                p.terminate()
                killed.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        log(
            "terminated unresponsive discord process(es): "
            + ", ".join(str(p) for p in killed)
        )
    _last_discord_start = _time.time()   # the restart starts fresh
    return killed


def close_extra_windows(window, log=print):
    """Send WM_CLOSE to discord's OTHER top-level windows -
    update banners and popups that block the message list."""
    closed = []
    pids = discord_pids()
    try:
        root = auto.GetRootControl()
        windows = root.GetChildren()
    except UIAError:
        return []
    main_pid = None
    try:
        main_pid = window.ProcessId if window is not None else None
    except UIAError:
        main_pid = None
    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            pid = win.ProcessId
            if pid not in pids or pid == main_pid:
                continue
            hwnd = win.NativeWindowHandle
            if not hwnd:
                continue
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            closed += 1
            log(f"closed discord popup window {win.Name!r}")
        except UIAError:
            continue
    return closed


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


def find_channel_control(window, channel_names):
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


def _ensure_on_screen(ctrl, log=print):
    """A minimized (or tray) discord reports (0,0) clickable
    points - every synthetic click lands on nothing. Restore
    the window so the points map to the screen."""
    try:
        pt = ctrl.GetClickablePoint()
    except Exception:
        pt = None
    if pt and (pt[0] > 0 or pt[1] > 0):
        return True
    try:
        top = ctrl.GetTopLevelControl()
        hwnd = top.NativeWindowHandle
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        # the restore + ui tree refresh takes a moment
        _time.sleep(1.5)
        pt = ctrl.GetClickablePoint()
        if pt and (pt[0] > 0 or pt[1] > 0):
            log("restored the discord window (was minimized)")
            return True
    except Exception as e:
        log(f"discord restore failed: {e}")
    return False


def click_channel_control(ctrl, log=print):
    """Click a channel entry; rate limited to one attempt a
    minute to avoid fighting the user."""
    import time as _time

    global _last_channel_click
    now = _time.time()
    if now - _last_channel_click < 60:
        return False
    _last_channel_click = now
    if not _ensure_on_screen(ctrl, log):
        log(
            "channel switch skipped: discord is minimized or "
            "offscreen"
        )
        return False
    # ui-automation activation first: no cursor movement, so a
    # user moving their physical mouse cannot break the switch
    try:
        ctrl.DoDefaultAction()
        log("switched discord to the target channel (uia action)")
        return True
    except Exception:
        pass
    try:
        ctrl.GetInvokePattern().Invoke()
        log("switched discord to the target channel (invoke)")
        return True
    except Exception:
        pass
    try:
        pt = ctrl.GetClickablePoint()
        ctrl.Click(simulateMove=False)
        log(
            "switched discord to the target channel (mouse at "
            f"{int(pt[0])},{int(pt[1])})"
        )
        return True
    except Exception as e:
        log(f"channel switch failed: {e}")
        return False


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


def tree_entry_names(window, sample=12):
    """Named clickable entries in the window's ui tree - the
    diagnostic for why a channel/server entry cannot be
    found."""
    import inspect_discord as _self

    names = []
    try:
        walker = auto.WalkControl(
            window, includeTop=False, maxDepth=30
        )
        for ctrl, depth in walker:
            if len(names) >= sample:
                break
            try:
                if ctrl.ControlType not in (
                    auto.ControlType.ListItemControl,
                    auto.ControlType.TreeItemControl,
                    auto.ControlType.HyperlinkControl,
                    auto.ControlType.ButtonControl,
                    auto.ControlType.TabItemControl,
                ):
                    continue
                name = (ctrl.Name or "").strip()
            except UIAError:
                continue
            if name and name not in names:
                names.append(name[:40])
    except UIAError:
        pass
    return names
