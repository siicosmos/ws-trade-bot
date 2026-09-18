import argparse

import psutil
import uiautomation as auto


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


def find_discord_window():
    pids = discord_pids()
    root = auto.GetRootControl()
    for win in root.GetChildren():
        try:
            if win.ControlType != auto.ControlType.WindowControl:
                continue
            if win.ProcessId in pids or "discord" in (win.Name or "").lower():
                return win
        except auto.COMError:
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
    except auto.COMError:
        return
    try:
        for child in control.GetChildren():
            dump(child, depth + 1, out, max_depth)
    except auto.COMError:
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
