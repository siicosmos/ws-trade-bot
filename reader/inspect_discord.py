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

    def _is_main_window(win):
        # the update stub ("Discord Updater") shares the discord
        # process prefix but has no message pane - attaching to
        # it only produces focus-failure noise while the app is
        # still starting
        try:
            name = (win.Name or "").lower()
        except UIAError:
            return False
        return "updater" not in name

    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            if win.ProcessId in pids and _is_main_window(win):
                return win
        except UIAError:
            continue
    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            name = (win.Name or "").lower()
            if name.endswith(" - discord") and _is_main_window(win):
                return win
        except UIAError:
            continue
    return None


def updater_window_visible():
    """True when only discord's update stub is showing - the
    reader reports 'still starting' instead of failing focus on
    a window that can never host messages."""
    pids = discord_pids()
    try:
        windows = auto.GetRootControl().GetChildren()
    except UIAError:
        return False
    for win in windows:
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            if win.ProcessId in pids and "updater" in (
                win.Name or ""
            ).lower():
                return True
        except UIAError:
            continue
    return False


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
                    # this discord build exposes the server rail
                    # entries and channel entries as groups
                    auto.ControlType.GroupControl,
                ):
                    continue
                name = ctrl.Name or ""
            except UIAError:
                continue
            if not any(c in name.lower() for c in targets):
                continue
            # no clickable-point check here: the point may only
            # resolve once the window is restored/foregrounded -
            # click_channel_control handles that
            return ctrl
    except UIAError:
        return None
    return None


_last_channel_click = 0.0


def _ensure_on_screen(ctrl, window=None, log=print):
    """A minimized (or tray) discord reports (0,0) clickable
    points - every synthetic click lands on nothing. Restore
    the window so the points map to the screen. The hwnd comes
    from the WINDOW control (child elements report
    NativeWindowHandle 0, which makes ShowWindow a silent
    no-op), and the verdict is the window's own iconic/visible
    state - an element's clickable point can lag the restore."""
    def _readable():
        try:
            target = window if window is not None else ctrl
            return _window_readable(target)
        except Exception:
            return False

    if _readable():
        return True
    try:
        target = window if window is not None else ctrl
        top = target.GetTopLevelControl() or target
        hwnd = top.NativeWindowHandle
        if not hwnd:
            log("discord restore failed: no window handle")
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        # electron keeps its renderer suspended after a hide:
        # the single-instance start signal makes discord show
        # and repaint its window properly (same as double
        # clicking the tray icon)
        start_discord(log=log)
        # the restore + ui tree refresh takes a moment
        _time.sleep(1.5)
        if _readable():
            log("restored the discord window (was minimized)")
            return True
        # one more nudge: some builds need a second restore
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        _time.sleep(1.0)
        if _readable():
            log("restored the discord window (second attempt)")
            return True
    except Exception as e:
        log(f"discord restore failed: {e}")
    return False


def click_channel_control(ctrl, window=None, log=print):
    """Click a channel entry; rate limited to one attempt a
    minute to avoid fighting the user."""
    import time as _time

    global _last_channel_click
    now = _time.time()
    if now - _last_channel_click < 15:
        return False
    _last_channel_click = now
    if not _ensure_on_screen(ctrl, window=window, log=log):
        log(
            "channel switch skipped: discord is minimized or "
            "offscreen"
        )
        return False
    # ui-automation activation first: no cursor movement, so a
    # user moving their physical mouse cannot break the switch.
    # sidebar entries are selection items - selecting is the
    # proper activation
    try:
        ctrl.GetSelectionItemPattern().Select()
        log("switched discord to the target channel (select)")
        return True
    except Exception:
        pass
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
    # mouse last: it clicks whatever is visually on top, so
    # discord must be foregrounded, and a (0,0) point (an
    # element with no resolvable position) is refused
    try:
        pt = ctrl.GetClickablePoint()
    except Exception:
        pt = None
    if not pt or (pt[0] <= 0 and pt[1] <= 0):
        try:
            rect = ctrl.BoundingRectangle
            # the rail groups span the icon plus a (usually
            # hidden) label - aim at the icon at the left edge,
            # not the group center which can sit over nothing
            pt = (
                rect.left + min(20, max(8, (rect.right - rect.left) / 4)),
                (rect.top + rect.bottom) / 2,
            )
        except Exception:
            pt = None
    if not pt or (pt[0] <= 0 and pt[1] <= 0):
        log(
            "channel switch failed: the entry has no usable "
            "position (mouse click refused)"
        )
        return False
    try:
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


def tree_entry_names(window, sample=30):
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
                    auto.ControlType.GroupControl,
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


def _window_readable(control):
    """True when discord's window is on screen: hidden (tray)
    and minimized windows render nothing."""
    try:
        top = control.GetTopLevelControl()
        if top is None:
            return False
        hwnd = top.NativeWindowHandle
        return (
            bool(ctypes.windll.user32.IsWindowVisible(hwnd))
            and not ctypes.windll.user32.IsIconic(hwnd)
        )
    except Exception:
        return False


def ensure_visible(control, window=None, log=print):
    """A minimized OR tray-hidden electron window renders
    nothing - its ui tree exposes an empty message list.
    Restore/unhide it so messages can be read (behind other
    windows is fine; hidden is not). The WINDOW's readability
    is the verdict: a stale container element (normal after a
    switch re-render) must not read as a lost window."""
    try:
        target = window if window is not None else control
        if _window_readable(target):
            return False
        top = target.GetTopLevelControl() or target
        hwnd = top.NativeWindowHandle
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        _time.sleep(2.5)
        if _window_readable(target):
            # a restored tray window may still carry a suspended
            # renderer (its ui tree goes stale or empty) - the
            # single-instance start signal makes discord show and
            # repaint properly (same as double clicking the tray)
            try:
                start_discord(log=log)
            except Exception:
                pass
            log(
                "discord was hidden or minimized - restored it "
                "(a hidden window cannot be read)"
            )
            return True
    except Exception as e:
        log(f"discord restore check failed: {e}")
    return False


def server_from_title(title):
    """The server name from '<channel> | <server> - Discord'."""
    if not title or "|" not in title:
        return ""
    tail = title.rsplit("|", 1)[1]
    return tail.replace("- Discord", "").strip()


def _foreground_discord(window, log=print):
    """Bring the discord window to the foreground and verify it
    actually took focus - keyboard input goes wherever the focus
    is, and windows' foreground lock silently rejects a plain
    SetForegroundWindow from a background process (SetActive
    attaches to the foreground thread's input queue, which is
    allowed to hand focus over)."""
    try:
        hwnd = window.NativeWindowHandle
    except UIAError:
        hwnd = 0
    if not hwnd:
        log("discord focus failed: no window handle")
        return False
    fg = ctypes.windll.user32.GetForegroundWindow
    for attempt in range(3):
        if fg() == hwnd:
            return True
        try:
            window.SetActive()
        except Exception as e:
            log(f"discord focus attempt {attempt + 1} failed: {e}")
        _time.sleep(0.4)
    if fg() != hwnd:
        log("discord did not take focus - keys would land elsewhere")
        return False
    return True


def switch_server_keyboard(window, target_server, log=print):
    """Cycle the server rail with discord's ctrl+alt+down
    hotkey until the window title shows the target server -
    immune to mouse position, dpi and rendering issues. Takes
    focus: keyboard navigation needs the discord window
    foregrounded - keys sent while another window is focused
    go to that window instead."""
    if not _foreground_discord(window, log=log):
        return False
    log(f"navigating to server {target_server!r} via the keyboard")
    pressed = 0
    for _ in range(15):
        try:
            server = server_from_title(window.Name or "")
        except UIAError:
            server = ""
        if server and target_server.lower() in server.lower():
            log(f"switched to server {server!r}")
            return True
        auto.SendKeys("^%{down}", waitTime=0.3)
        pressed += 1
        _time.sleep(0.4)
        if pressed == 1:
            # a hotkey that does nothing (focus still elsewhere,
            # keybind swallowed by e.g. a graphics-driver hotkey)
            # shows up as an unchanged title
            try:
                log(f"title after the first hotkey: {window.Name!r}")
            except UIAError:
                pass
    log(f"server {target_server!r} not reached via the keyboard")
    return False
