import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reader"))

import uiautomation as auto
from inspect_discord import find_discord_window


def main():
    window = find_discord_window()
    if window is None:
        print("Discord window not found")
        return
    print(f"window: {window.Name!r}")
    for ctrl, depth in auto.WalkControl(window, includeTop=False, maxDepth=14):
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
            name = (ctrl.Name or "")[:60]
            try:
                children = ctrl.GetChildren()
            except auto.COMError:
                children = []
            print(
                f"[{ctrl.ControlTypeName}] depth={depth} name={name!r} "
                f"children={len(children)}"
            )
            for child in children[:6]:
                try:
                    texts = []
                    for c, d in auto.WalkControl(
                        child, includeTop=False, maxDepth=6
                    ):
                        if c.ControlType == auto.ControlType.TextControl and c.Name:
                            texts.append(c.Name.strip()[:60])
                        if len(texts) >= 3:
                            break
                    child_name = (child.Name or "")[:40]
                    print(
                        f"    child [{child.ControlTypeName}] "
                        f"name={child_name!r} -> {texts}"
                    )
                except auto.COMError:
                    continue
        except auto.COMError:
            continue


if __name__ == "__main__":
    main()
